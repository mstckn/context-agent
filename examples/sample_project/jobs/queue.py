"""Background job queue.

NOTE: This file is intentionally long. The execution-protection logic
(idempotency keys and worker locks) lives near the END of the file.
"""

import threading
import time
import traceback

from db.connection import transaction
from analytics.events import track_event
from utils.logging import get_logger

logger = get_logger("jobs.queue")

QUEUE_POLL_INTERVAL = 0.5
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0


class JobError(Exception):
    """Raised when a job fails permanently."""


class JobRecord:
    """Internal representation of a queued job."""

    def __init__(self, job_id, name, payload, idempotency_key=None):
        self.job_id = job_id
        self.name = name
        self.payload = payload
        self.idempotency_key = idempotency_key
        self.attempts = 0
        self.state = "pending"
        self.result = None
        self.error = None

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "name": self.name,
            "state": self.state,
            "attempts": self.attempts,
            "idempotency_key": self.idempotency_key,
        }


class Scheduler:
    """Schedules periodic jobs. Kept simple on purpose."""

    def __init__(self):
        self._schedule = []

    def every(self, interval_seconds, job_name, payload=None):
        self._schedule.append({
            "interval": interval_seconds,
            "name": job_name,
            "payload": payload or {},
            "next_run": time.time() + interval_seconds,
        })

    def due_jobs(self):
        now = time.time()
        return [s for s in self._schedule if s["next_run"] <= now]

    def mark_ran(self, entry):
        entry["next_run"] = time.time() + entry["interval"]


class MetricsCollector:
    """Collects counters about queue health."""

    def __init__(self):
        self.counters = {}

    def incr(self, name, value=1):
        self.counters[name] = self.counters.get(name, 0) + value

    def snapshot(self):
        return dict(self.counters)


class DeadLetterStore:
    """Stores jobs that failed permanently for later inspection."""

    def __init__(self):
        self._dead = []

    def push(self, record):
        self._dead.append(record.to_dict())
        track_event("job.dead_letter", {"job_id": record.job_id})

    def list_failed(self):
        return list(self._dead)


class RetryPolicy:
    """Decides whether a failed job should be retried."""

    def __init__(self, max_attempts=MAX_RETRY_ATTEMPTS,
                 backoff=RETRY_BACKOFF_SECONDS):
        self.max_attempts = max_attempts
        self.backoff = backoff

    def should_retry(self, record):
        return record.attempts < self.max_attempts

    def delay_for(self, record):
        return self.backoff * record.attempts


class WorkerPool:
    """Runs jobs on worker threads."""

    def __init__(self, queue, workers=2):
        self.queue = queue
        self.workers = workers
        self._threads = []
        self._stop = threading.Event()

    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._run, name=f"worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            record = self.queue.claim_next()
            if record is None:
                time.sleep(QUEUE_POLL_INTERVAL)
                continue
            self.queue.execute(record)


class JobQueue:
    """Central job queue with submission, claiming and execution.

    The queue supports concurrent workers. Correctness under concurrency
    depends on the protection helpers defined below (idempotency and
    locking) — read those before changing claim/execute behavior.
    """

    def __init__(self):
        self._pending = []
        self._running = {}
        self._finished = {}
        self._lock = threading.Lock()
        self._handlers = {}
        self._completed_keys = {}
        self._active_locks = set()
        self.metrics = MetricsCollector()
        self.dead_letters = DeadLetterStore()
        self.retry_policy = RetryPolicy()
        self._sequence = 0

    # ── registration ────────────────────────────────────────────────

    def register(self, name, handler):
        """Register a callable handler for a job name."""
        self._handlers[name] = handler

    # ── submission ──────────────────────────────────────────────────

    def submit(self, name, payload=None, idempotency_key=None):
        """Submit a job for execution.

        If ``idempotency_key`` was already completed successfully the
        queue returns the previous result instead of running again.
        See ``_check_idempotency`` below.
        """
        cached = self._check_idempotency(idempotency_key)
        if cached is not None:
            self.metrics.incr("jobs.deduplicated")
            return cached

        with self._lock:
            self._sequence += 1
            job_id = f"job_{self._sequence:06d}"
            record = JobRecord(job_id, name, payload or {},
                               idempotency_key=idempotency_key)
            self._pending.append(record)
        track_event("job.submitted", record.to_dict())
        return job_id

    def claim_next(self):
        """Claim the oldest pending job for a worker, or None."""
        with self._lock:
            if not self._pending:
                return None
            record = self._pending.pop(0)
            if not self._acquire_worker_lock(record):
                # Someone else owns this logical unit; requeue.
                self._pending.append(record)
                return None
            record.state = "running"
            self._running[record.job_id] = record
        return record

    def execute(self, record):
        """Run a claimed job with retry and protection logic."""
        handler = self._handlers.get(record.name)
        if handler is None:
            record.state = "failed"
            record.error = f"no handler for {record.name}"
            self._finish(record)
            return

        record.attempts += 1
        try:
            with transaction() as conn:
                conn.execute("UPDATE jobs SET state='running' WHERE id=?",
                             (record.job_id,))
            result = handler(record.payload)
            record.result = result
            record.state = "completed"
            self.metrics.incr("jobs.completed")
        except Exception as exc:
            record.error = f"{exc}\n{traceback.format_exc(limit=2)}"
            if self.retry_policy.should_retry(record):
                record.state = "pending"
                self.metrics.incr("jobs.retried")
                with self._lock:
                    self._running.pop(record.job_id, None)
                    self._pending.append(record)
                self._release_worker_lock(record)
                time.sleep(self.retry_policy.delay_for(record))
                return
            record.state = "failed"
            self.metrics.incr("jobs.failed")
            self.dead_letters.push(record)
        self._finish(record)

    def _finish(self, record):
        with self._lock:
            self._running.pop(record.job_id, None)
            self._finished[record.job_id] = record
            self._remember_completion(record)
        self._release_worker_lock(record)
        track_event("job.finished", record.to_dict())

    def status(self, job_id):
        """Return the state dict for a job."""
        record = self._finished.get(job_id)
        if record is None:
            with self._lock:
                running = self._running.get(job_id)
                if running:
                    return running.to_dict()
                for pending in self._pending:
                    if pending.job_id == job_id:
                        return pending.to_dict()
            return {"job_id": job_id, "state": "unknown"}
        return record.to_dict()

    # ── protection helpers ──────────────────────────────────────────

    def _check_idempotency(self, idempotency_key):
        """Prevent running the same logical job twice.

        A completed job whose submission carried an ``idempotency_key``
        registers that key here; later submissions with the same key
        short-circuit and return the stored result. This is how the
        queue avoids running a duplicated job request.
        """
        if idempotency_key is None:
            return None
        return self._completed_keys.get(idempotency_key)

    def _remember_completion(self, record):
        if record.idempotency_key and record.state == "completed":
            self._completed_keys[record.idempotency_key] = record.result

    def _acquire_worker_lock(self, record):
        """Take an exclusive lock for the job's logical unit.

        Two jobs sharing an idempotency key (or job name+payload when no
        key is given) are treated as the same logical unit; only one may
        run at a time.
        """
        unit = record.idempotency_key or f"{record.name}:{sorted(record.payload.items())}"
        if unit in self._active_locks:
            return False
        self._active_locks.add(unit)
        return True

    def _release_worker_lock(self, record):
        unit = record.idempotency_key or f"{record.name}:{sorted(record.payload.items())}"
        self._active_locks.discard(unit)
