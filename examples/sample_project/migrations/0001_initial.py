"""0001 — initial schema.

Creates the core tables used by auth sessions, invoices, refunds and jobs.
Consumers: db.connection (transaction writes), payments.service,
auth.service, jobs.queue.
"""

SCHEMA_VERSION = "0001"

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS sessions (
        user_id TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD'
    )""",
    """CREATE TABLE IF NOT EXISTS refunds (
        id TEXT PRIMARY KEY,
        invoice_id TEXT NOT NULL REFERENCES invoices(id),
        amount_cents INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'pending'
    )""",
]


def upgrade(conn):
    """Apply the migration."""
    for stmt in STATEMENTS:
        conn.execute(stmt)
    return SCHEMA_VERSION


def downgrade(conn):
    """Reverse the migration."""
    for table in ("jobs", "refunds", "invoices", "sessions"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
