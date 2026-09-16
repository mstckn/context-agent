# Model Gateway (optional)

`scripts/model_gateway.py` — optional local HTTP gateway for MODE 3
(AUTO ROUTER). It exposes an OpenAI-compatible endpoint bound to
`127.0.0.1` only.

The gateway is OPTIONAL. Without it the router reports
"AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION" and the system
falls back to IDE MODEL; nothing breaks.

## Server

- Binds `127.0.0.1` only (`ThreadingHTTPServer`); default port `8791`
  (`--port` overrides).
- Server token: `ContextAgentGateway/1.0`; per-request logging is
  suppressed.
- On successful bind it prints
  `{"started": true, "port": N, "pid": P, "registry": "<path>"}`.
  If the port cannot be bound it prints
  `{"error": "port_unavailable", "port": N, "detail": ...}` and exits 1.

## Registry

- Path: `~/.context-agent/gateway.json`
- Content: `{"port": N, "pid": P, "started_at": "<iso timestamp>"}`
- Written atomically on start (temp file + `os.replace`). Removed on
  exit via `atexit` and on SIGTERM, only if the stored pid matches.
- Other components feature-detect the gateway through this file:
  `router.gateway_available()` reads it and probes `/healthz`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness probe (used by `router.gateway_available`). Returns 200 `{"status": "ok", "service": "context-agent-gateway", "routing_enabled": <bool>, "time": "<iso>"}`; `routing_enabled` reflects effective config `routing.enabled`. |
| `GET /v1/models` | OpenAI-format list of usable catalog models: only models with `enabled` true whose provider is also `enabled`. Returns `{"object": "list", "data": [{"id", "object": "model", "created": 0, "owned_by": <provider_id>}]}`. |
| `GET /` | Service info with the endpoint list. |
| `POST /v1/chat/completions` | Forwards to a configured provider model and records usage (below). |
| anything else | 404 `{"error": "not_found", "path": ...}` |

### POST /v1/chat/completions

Request body (OpenAI-style):

- `model` (required) — resolved against the catalog by `model_id`
  first, then by `remote_model_name`; the model must be enabled,
  otherwise 404 `unknown_or_disabled_model`.
- `messages` (required) — the first `system` message becomes the system
  prompt; remaining messages are flattened into the prompt (non-user
  roles prefixed as `[role] ...`). Empty list → 400 `messages_required`.
- `max_tokens` — default 2048.
- `stream` — boolean.
- `X-Context-Conversation-Id` or `X-Context-Session-Id` header — recommended
  stable conversation identity for concurrent IDEs. The gateway converts it
  to an opaque, project-isolated `x-opencode-session` value for OpenCode.
  An incoming `x-opencode-session` is accepted too. Without an explicit id,
  the project and first user message provide a compatibility fallback.

Execution goes through `router.execute_with_model`, so it requires an
enabled provider plus a stored secret for `needs_key` providers.

Responses:

- Non-stream: `chat.completion` JSON with one assistant choice and
  `usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`).
- Stream (`"stream": true`): SSE replay (`text/event-stream`). The
  underlying provider call is non-streaming, so the completed text is
  replayed as `chat.completion.chunk` events (48-character deltas),
  followed by a final chunk with `finish_reason: "stop"` and
  `data: [DONE]`.
- Errors: 400 `invalid_json`; 401 when the router reports `no_api_key`;
  other provider failures → 502, all as
  `{"error": {"message", "type", "provider_id"}}`.

Usage recording: on success a row is written to
`<project>/.context/router.db` with `executed_by: "gateway"`,
`decision: "EXECUTED"`, and the reported tokens/cost. Recording is
best-effort and never breaks execution. The project root comes from the
`X-Context-Project-Root` header, then the `CONTEXT_PROJECT_ROOT`
environment variable, then `find_project_root()`; if none resolves,
recording is skipped.

## CLI

```powershell
python scripts\model_gateway.py start [--port N]   # serve in foreground, default port 8791
python scripts\model_gateway.py status              # prints router.gateway_available() JSON
python scripts\model_gateway.py stop                # SIGTERM the registered pid, wait up to ~5 s
```

`stop` prints `{"stopped": true, "pid": N}`; if the process is already
gone it prints `{"stopped": true, "stale_registry": true, "pid": N}`;
with no registry it prints `{"running": false}`.

## Relation to the router

`router.gateway_available()` reads `~/.context-agent/gateway.json` and
probes `http://127.0.0.1:<port>/healthz` with a 1.5 s timeout.
`router.router_status()` marks `AUTO_ROUTER` available only when the
gateway responds; otherwise the effective mode falls back to `IDE_MODEL`
with an explicit `mode_note`.
