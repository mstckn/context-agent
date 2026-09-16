#!/usr/bin/env python3
"""
ledger.py — Persistent cross-turn context ledger ("what the model already knows").

The old dedup table was cleared at the start of every build, so unchanged
files were re-sent on every turn. The ledger instead persists, per session:

  * which files/symbols/ranges were already sent to the model
  * with which representation (summary < symbols < outline < range < full)
  * the content hash that was sent
  * when it was sent and how much conversation has happened since

From this the engine decides, deterministically, whether a context item must
be resent, can be skipped, or only needs a lighter refresh.

CONTEXT DECAY
-------------
Dedup must not assume the LLM remembers forever. The ledger estimates how
much of the active context window the previously sent item still occupies:

    retention = clamp(1 - (tokens_since * decay_weight) / window, 0..1)

``tokens_since`` is the number of conversation tokens generated since the
item was last sent (tracked from the session's cumulative token counter).
``decay_weight`` depends on importance: PINNED architectural facts decay
slower than EPHEMERAL detail.

States: FRESH / LIKELY_PRESENT / DECAYING / EXPIRED.
EXPIRED or hash-changed items are reintroduced into context.

CLI
---
  python ledger.py --record --session S --path P --repr full --hash H --tokens 123
  python ledger.py --status --session S --path P [--new-hash H2]
  python ledger.py --diff   --session S --files-json '[{"path":"a.py","hash":"..."}]'
  python ledger.py --turn   --session S --tokens 1500     # tokens generated this turn
  python ledger.py --list   --session S
  python ledger.py --forget --session S [--path P]

Importable API: ContextLedger (used by engine.py / agent.py).
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:  # run directly: `python ledger.py`
    from paths import find_context_dir, resolve_scope
except ImportError:  # imported as a package
    from scripts.paths import find_context_dir, resolve_scope

# Representation strength ordering: a stronger view covers weaker ones.
REPRESENTATIONS = {
    "summary": 0,
    "symbols": 1,
    "outline": 2,
    "skeleton": 2,
    "range": 3,
    "full": 4,
}

# Decay weights by importance class (higher = forgets faster).
DECAY_WEIGHTS = {
    "ephemeral": 2.0,
    "normal": 1.0,
    "important": 0.6,
    "pinned": 0.35,
}

# Retention thresholds → knowledge state.
STATE_THRESHOLDS = (
    (0.85, "FRESH"),
    (0.50, "LIKELY_PRESENT"),
    (0.20, "DECAYING"),
    (0.0, "EXPIRED"),
)

DEFAULT_WINDOW_TOKENS = 120_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    representation TEXT NOT NULL,
    content_hash TEXT,
    structural_hash TEXT,
    ranges_seen TEXT,
    symbols_seen TEXT,
    token_cost INTEGER DEFAULT 0,
    importance TEXT DEFAULT 'normal',
    task TEXT DEFAULT '',
    client TEXT DEFAULT '',
    model TEXT DEFAULT '',
    git_commit TEXT DEFAULT '',
    first_sent_at TEXT,
    last_sent_at TEXT,
    turn INTEGER DEFAULT 0,
    sent_session_tokens INTEGER DEFAULT 0,
    superseded INTEGER DEFAULT 0,
    UNIQUE(scope, session_id, path, representation)
);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON context_ledger(scope, session_id);
CREATE INDEX IF NOT EXISTS idx_ledger_path ON context_ledger(scope, path);

CREATE TABLE IF NOT EXISTS ledger_sessions (
    scope TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    window_tokens INTEGER DEFAULT 120000,
    updated_at TEXT,
    PRIMARY KEY (scope, session_id)
);
"""


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    try:  # migration for older ledger databases
        conn.execute(
            "ALTER TABLE ledger_sessions ADD COLUMN last_package_tokens INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # column already present
    return conn


def retention_state(retention):
    for threshold, state in STATE_THRESHOLDS:
        if retention >= threshold:
            return state
    return "EXPIRED"


class ContextLedger:
    """Persistent record of what a model session has already seen."""

    def __init__(self, db_path):
        self.conn = _connect(db_path)

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    # ── session accounting ──────────────────────────────────────────

    def _session_row(self, scope, session_id):
        return self.conn.execute(
            "SELECT turn, total_tokens, window_tokens, last_package_tokens "
            "FROM ledger_sessions WHERE scope=? AND session_id=?",
            (scope, session_id),
        ).fetchone()

    def session_state(self, scope, session_id, window_tokens=None):
        initial_window = window_tokens or DEFAULT_WINDOW_TOKENS
        self.conn.execute(
            "INSERT INTO ledger_sessions (scope, session_id, turn, total_tokens, "
            "window_tokens, last_package_tokens, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(scope, session_id) DO NOTHING",
            (scope, session_id, 0, 0, initial_window, 0,
             datetime.now().isoformat()),
        )
        if window_tokens:
            self.conn.execute(
                "UPDATE ledger_sessions SET window_tokens=? WHERE scope=? AND session_id=?",
                (window_tokens, scope, session_id),
            )
        self.conn.commit()
        row = self._session_row(scope, session_id)
        turn, total, window, last_pkg = row
        return {"turn": turn, "total_tokens": total, "window_tokens": window,
                "last_package_tokens": last_pkg}

    def set_last_package_tokens(self, scope, session_id, tokens):
        """Remember how big the last sent package was (decay fuel for next turn)."""
        self.session_state(scope, session_id)
        self.conn.execute(
            "UPDATE ledger_sessions SET last_package_tokens=?, updated_at=? "
            "WHERE scope=? AND session_id=?",
            (max(0, int(tokens or 0)), datetime.now().isoformat(), scope, session_id),
        )
        self.conn.commit()

    def advance_turn(self, scope, session_id, tokens_generated=0, window_tokens=None):
        """Register that the conversation moved forward by ``tokens_generated``."""
        self.session_state(scope, session_id, window_tokens)
        self.conn.execute(
            "UPDATE ledger_sessions SET turn=turn+1, total_tokens=total_tokens+?, updated_at=? "
            "WHERE scope=? AND session_id=?",
            (max(0, int(tokens_generated or 0)), datetime.now().isoformat(),
             scope, session_id),
        )
        self.conn.commit()
        state = self.session_state(scope, session_id)
        return {"turn": state["turn"], "total_tokens": state["total_tokens"],
                "window_tokens": state["window_tokens"]}

    # ── recording what was sent ─────────────────────────────────────

    def record(self, scope, session_id, path, representation, content_hash="",
               token_cost=0, ranges=None, symbols=None, importance="normal",
               task="", client="", model="", git_commit="", structural_hash=""):
        """Record (or refresh) that a representation of ``path`` was sent."""
        representation = representation if representation in REPRESENTATIONS else "full"
        state = self.session_state(scope, session_id)
        now = datetime.now().isoformat()
        rep_level = REPRESENTATIONS[representation]

        # Serialize the read/merge/write sequence so concurrent MCP clients do
        # not race on the UNIQUE key or lose partial-range metadata.
        self.conn.execute("BEGIN IMMEDIATE")

        try:
            row = self.conn.execute(
                "SELECT id, representation, first_sent_at FROM context_ledger "
                "WHERE scope=? AND session_id=? AND path=? AND representation=?",
                (scope, session_id, path, representation),
            ).fetchone()

            # Merge ranges/symbols with what was seen before.
            existing = self._existing_meta(scope, session_id, path, representation)
            if existing:
                merged_ranges = sorted(set(existing["ranges"]) | set(tuple(r) for r in (ranges or [])))
                merged_symbols = sorted(set(existing["symbols"]) | set(symbols or []))
            else:
                merged_ranges = sorted(set(tuple(r) for r in (ranges or [])))
                merged_symbols = sorted(set(symbols or []))

            core = (
                content_hash or "", structural_hash or "",
                json.dumps(merged_ranges), json.dumps(merged_symbols),
                int(token_cost or 0), importance, task, client, model,
                git_commit or "",
            )
            if row:
                self.conn.execute(
                    "UPDATE context_ledger SET content_hash=?, structural_hash=?, "
                    "ranges_seen=?, symbols_seen=?, token_cost=?, importance=?, task=?, "
                    "client=?, model=?, git_commit=?, last_sent_at=?, turn=?, "
                    "sent_session_tokens=?, superseded=0 WHERE id=?",
                    core + (now, state["turn"], state["total_tokens"], row[0]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO context_ledger (scope, session_id, path, representation, "
                    "content_hash, structural_hash, ranges_seen, symbols_seen, token_cost, "
                    "importance, task, client, model, git_commit, first_sent_at, "
                    "last_sent_at, turn, sent_session_tokens) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scope, session_id, path, representation) + core +
                    (now, now, state["turn"], state["total_tokens"]),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"recorded": True, "path": path, "representation": representation,
                "rep_level": rep_level}

    def _existing_meta(self, scope, session_id, path, representation):
        row = self.conn.execute(
            "SELECT ranges_seen, symbols_seen FROM context_ledger "
            "WHERE scope=? AND session_id=? AND path=? AND representation=?",
            (scope, session_id, path, representation),
        ).fetchone()
        if not row:
            return None
        try:
            ranges = [tuple(r) for r in json.loads(row[0] or "[]")]
        except Exception:
            ranges = []
        try:
            symbols = json.loads(row[1] or "[]")
        except Exception:
            symbols = []
        return {"ranges": ranges, "symbols": symbols}

    # ── querying ────────────────────────────────────────────────────

    def best_known(self, scope, session_id, path):
        """Return the strongest recorded representation for ``path``."""
        rows = self.conn.execute(
            "SELECT representation, content_hash, ranges_seen, symbols_seen, "
            "token_cost, importance, last_sent_at, turn, sent_session_tokens "
            "FROM context_ledger WHERE scope=? AND session_id=? AND path=? "
            "AND superseded=0",
            (scope, session_id, path),
        ).fetchall()
        if not rows:
            return None
        best = max(rows, key=lambda r: REPRESENTATIONS.get(r[0], 0))
        try:
            ranges = json.loads(best[2] or "[]")
        except Exception:
            ranges = []
        try:
            symbols = json.loads(best[3] or "[]")
        except Exception:
            symbols = []
        return {
            "path": path,
            "representation": best[0],
            "rep_level": REPRESENTATIONS.get(best[0], 0),
            "content_hash": best[1],
            "ranges_seen": ranges,
            "symbols_seen": symbols,
            "token_cost": best[4],
            "importance": best[5],
            "last_sent_at": best[6],
            "turn": best[7],
            "sent_session_tokens": best[8],
        }

    def status(self, scope, session_id, path, requested="full", new_hash=None,
               window_tokens=None):
        """Decide whether ``path`` needs to be (re)sent.

        Returns a dict with ``decision`` in
        {send, refresh, skip} and the decay ``state``.
        """
        session = self.session_state(scope, session_id, window_tokens)
        known = self.best_known(scope, session_id, path)
        requested_level = REPRESENTATIONS.get(requested, 4)

        if known is None:
            return {"decision": "send", "reason": "never_sent",
                    "state": "EXPIRED", "known": None}

        # File changed on disk → always resend.
        if new_hash and known["content_hash"] and new_hash != known["content_hash"]:
            return {"decision": "send", "reason": "content_changed",
                    "state": "EXPIRED", "known": known}

        # Model only saw a weaker view than now required → upgrade.
        if known["rep_level"] < requested_level:
            return {"decision": "send", "reason": "representation_upgrade",
                    "state": self._decay_state(known, session), "known": known}

        state = self._decay_state(known, session)
        if state in ("FRESH", "LIKELY_PRESENT"):
            return {"decision": "skip", "reason": f"known_to_model:{state.lower()}",
                    "state": state, "known": known}
        if state == "DECAYING":
            # Important material gets a cheap skeleton refresh; ephemeral
            # detail may simply be re-sent by the caller.
            return {"decision": "refresh", "reason": "decaying",
                    "state": state, "known": known}
        return {"decision": "send", "reason": "expired_from_window",
                "state": state, "known": known}

    def _decay_state(self, known, session):
        window = max(1, int(session.get("window_tokens") or DEFAULT_WINDOW_TOKENS))
        tokens_since = max(0, session["total_tokens"] - known["sent_session_tokens"])
        weight = DECAY_WEIGHTS.get(known.get("importance") or "normal", 1.0)
        retention = max(0.0, min(1.0, 1.0 - (tokens_since * weight) / window))
        return retention_state(retention)

    def diff(self, scope, session_id, candidates, window_tokens=None):
        """Classify candidate files against the ledger.

        ``candidates``: [{"path": ..., "hash": ...}, ...]
        Returns {"new": [...], "changed": [...], "unchanged": [...],
                 "decaying": [...]} with ledger decisions attached.
        """
        out = {"new": [], "changed": [], "unchanged": [], "decaying": []}
        for cand in candidates:
            status = self.status(scope, session_id, cand["path"],
                                 requested=cand.get("representation", "full"),
                                 new_hash=cand.get("hash"),
                                 window_tokens=window_tokens)
            entry = {"path": cand["path"], **status}
            if status["reason"] == "never_sent":
                out["new"].append(entry)
            elif status["reason"] == "content_changed":
                out["changed"].append(entry)
            elif status["decision"] == "skip":
                out["unchanged"].append(entry)
            else:
                out["decaying"].append(entry)
        return out

    def known_files(self, scope, session_id, limit=200):
        rows = self.conn.execute(
            "SELECT path, representation, content_hash, last_sent_at, token_cost "
            "FROM context_ledger WHERE scope=? AND session_id=? AND superseded=0 "
            "ORDER BY last_sent_at DESC LIMIT ?",
            (scope, session_id, limit),
        ).fetchall()
        return [{"path": r[0], "representation": r[1], "content_hash": r[2],
                 "last_sent_at": r[3], "token_cost": r[4]} for r in rows]

    def forget(self, scope, session_id, path=None):
        if path:
            self.conn.execute(
                "DELETE FROM context_ledger WHERE scope=? AND session_id=? AND path=?",
                (scope, session_id, path),
            )
        else:
            # Tam unutma: dosya kayıtları + oturum sayaçları sıfırlanır.
            self.conn.execute(
                "DELETE FROM context_ledger WHERE scope=? AND session_id=?",
                (scope, session_id),
            )
            self.conn.execute(
                "UPDATE ledger_sessions SET turn=0, total_tokens=0, "
                "last_package_tokens=0, updated_at=? WHERE scope=? AND session_id=?",
                (datetime.now().isoformat(), scope, session_id),
            )
        self.conn.commit()

    def purge_old(self, days=7):
        """Housekeeping: drop very old ledger rows (memory-bounded growth)."""
        self.conn.execute(
            "DELETE FROM context_ledger WHERE last_sent_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        self.conn.commit()


# ── CLI plumbing ────────────────────────────────────────────────────────

def _ledger():
    context_dir, _ = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı. Önce: python .context/scripts/index.py"}))
        sys.exit(1)
    return ContextLedger(context_dir / "symbols.db")


def main():
    parser = argparse.ArgumentParser(description="Context knowledge ledger")
    parser.add_argument("--session", default=None,
                        help="Session/conversation id (default: $CONTEXT_AGENT_SESSION or scope)")
    parser.add_argument("--scope", default=None)
    parser.add_argument("--window", type=int, default=None,
                        help="Model context window tokens for decay math")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", action="store_true")
    group.add_argument("--status", metavar="PATH")
    group.add_argument("--diff", action="store_true")
    group.add_argument("--turn", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--forget", action="store_true")
    parser.add_argument("--path")
    parser.add_argument("--repr", default="full", choices=sorted(REPRESENTATIONS))
    parser.add_argument("--hash", dest="content_hash", default="")
    parser.add_argument("--new-hash", dest="new_hash", default=None)
    parser.add_argument("--tokens", type=int, default=0)
    parser.add_argument("--importance", default="normal",
                        choices=sorted(DECAY_WEIGHTS))
    parser.add_argument("--task", default="")
    parser.add_argument("--files-json", default=None,
                        help='JSON list of {"path":..,"hash":..} for --diff')
    args = parser.parse_args()

    scope = args.scope or resolve_scope()
    import os
    session = args.session or os.environ.get("CONTEXT_AGENT_SESSION") or scope
    ledger = _ledger()

    try:
        if args.record:
            if not args.path:
                print(json.dumps({"error": "--path gerekli"}))
                return
            result = ledger.record(scope, session, args.path, args.repr,
                                   content_hash=args.content_hash,
                                   token_cost=args.tokens,
                                   importance=args.importance, task=args.task)
            print(json.dumps(result, indent=2))
        elif args.status:
            print(json.dumps(ledger.status(scope, session, args.status,
                                           requested=args.repr,
                                           new_hash=args.new_hash,
                                           window_tokens=args.window), indent=2))
        elif args.diff:
            candidates = json.loads(args.files_json or "[]")
            print(json.dumps(ledger.diff(scope, session, candidates,
                                         window_tokens=args.window), indent=2))
        elif args.turn:
            print(json.dumps(ledger.advance_turn(scope, session, args.tokens,
                                                 window_tokens=args.window), indent=2))
        elif args.list:
            print(json.dumps({
                "session": ledger.session_state(scope, session),
                "known": ledger.known_files(scope, session),
            }, indent=2))
        elif args.forget:
            ledger.forget(scope, session, args.path)
            print(json.dumps({"forgotten": args.path or session}))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
