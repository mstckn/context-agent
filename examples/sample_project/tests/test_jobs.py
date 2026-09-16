"""Tests for the job queue, including duplicate-execution protection."""

from jobs.queue import JobQueue


def make_queue():
    q = JobQueue()
    q.register("send_email", lambda payload: f"sent:{payload.get('to')}")
    q.register("flaky", _flaky)
    return q


_attempts = {"flaky": 0}


def _flaky(payload):
    _attempts["flaky"] += 1
    if _attempts["flaky"] < 2:
        raise RuntimeError("transient failure")
    return "recovered"


def run_all(queue, limit=10):
    for _ in range(limit):
        record = queue.claim_next()
        if record is None:
            break
        queue.execute(record)


def test_submit_and_complete():
    q = make_queue()
    job_id = q.submit("send_email", {"to": "x@y.z"})
    run_all(q)
    assert q.status(job_id)["state"] == "completed"


def test_duplicate_submission_not_rerun():
    q = make_queue()
    first = q.submit("send_email", {"to": "a@b.c"}, idempotency_key="k1")
    run_all(q)
    second = q.submit("send_email", {"to": "a@b.c"}, idempotency_key="k1")
    # second submission returns the cached result, not a new job id
    assert second == "sent:a@b.c"
    assert q.metrics.counters.get("jobs.deduplicated") == 1


def test_retry_recovers_transient_failure():
    _attempts["flaky"] = 0
    q = make_queue()
    job_id = q.submit("flaky", {})
    run_all(q, limit=20)
    assert q.status(job_id)["state"] == "completed"
