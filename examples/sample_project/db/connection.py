"""Connection pool and transaction contract.

Every module that writes to the database must use ``transaction()``
so writes are atomic. This is the database contract boundary.
"""

import threading
from contextlib import contextmanager

_local = threading.local()


class ConnectionPool:
    """Very small pooling layer; real drivers would sit behind this."""

    def __init__(self, size=4):
        self.size = size
        self._lock = threading.Lock()
        self._connections = []

    def acquire(self):
        with self._lock:
            if self._connections:
                return self._connections.pop()
        return Connection()

    def release(self, conn):
        with self._lock:
            self._connections.append(conn)


class Connection:
    """A single logical connection."""

    def __init__(self):
        self.statements = []
        self.in_txn = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return self

    def begin(self):
        self.in_txn = True

    def commit(self):
        self.in_txn = False

    def rollback(self):
        self.in_txn = False


_pool = ConnectionPool()


def get_pool():
    """Return the process-wide connection pool."""
    return _pool


@contextmanager
def transaction():
    """Context manager: acquire a pooled connection, begin a transaction,
    commit on success and roll back on error.

    Usage:
        with transaction() as conn:
            conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    """
    conn = _pool.acquire()
    conn.begin()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.release(conn)
