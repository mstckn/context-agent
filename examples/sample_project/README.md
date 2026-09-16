# Sample Project

A small but realistic multi-module application used as a benchmark testbed
for the Context Agent. Modules:

- `auth/` — authentication service, tokens, decorators, user model
- `payments/` — invoices and refunds
- `analytics/` — event tracking
- `db/` — connection pool and transactions
- `jobs/` — background job queue with idempotency protection
- `api/` — HTTP handlers producing JSON responses
- `frontend/` — TypeScript client components
- `migrations/` — database migrations
- `tests/` — pytest suite
