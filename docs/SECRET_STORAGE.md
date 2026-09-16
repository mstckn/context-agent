# Secret Storage

`scripts/secret_store.py` stores provider API keys (autoroute spec §10,
§50, §51). Hard rule: `ProviderConfig` != `ProviderSecret`. Provider
settings live in `~/.context-agent/providers.json`; secrets never do.
Context Intelligence never needs provider secrets.

## Storage

- File: `~/.context-agent/secrets.json` — outside every project tree
  (enforced by tests; nothing secret-shaped may appear under
  `<root>/.context`).
- Per-provider entry: `encrypted` (base64), `method`, `mask`,
  `updated_at`. The encrypted payload is never exposed by any listing.
- Windows: encrypted with DPAPI (`CryptProtectData`), `method: "dpapi"`.
  Other platforms: obfuscated with a machine-local key,
  `method: "obfuscated"`. The storage method is reported honestly.
- On non-Windows systems the file is `chmod 0600`.

## Masks only

Raw keys are never returned by any listing or JSON output. Only the mask
is shown: first 3 characters + `...` + last 4 characters (e.g.
`sk-...7654`). Keys of length ≤ 7 mask to `***`. Stored keys are never
displayed back to the user.

## CLI

```powershell
python scripts\secret_store.py --list
python scripts\secret_store.py --has openai
python scripts\secret_store.py --delete openai

# set / replace: the key is passed via the SECRET_VALUE environment
# variable — NEVER as a command-line argument
$env:SECRET_VALUE = "sk-..."
python scripts\secret_store.py --set openai
Remove-Item Env:SECRET_VALUE
```

`--set` reads `SECRET_VALUE`; if the variable is empty and stdin is a
TTY, it falls back to a hidden `getpass` prompt. The result echoes only
`provider_id`, `mask`, `method`, and `action` (`created` / `replaced`).
Setting an existing provider again replaces the key; `--delete` removes
it.

## Where secrets never appear

Secrets must not enter: repository files, Context MCP output / context
packages, Context Ledger, Project Memory, prompts, logs, routing traces
or explanations, usage events, diagnostics exports, or frontend GET
responses. `get_secret()` is internal only; its return value is used
solely to build request headers (`Authorization: Bearer ...`, or
`x-api-key` for Anthropic) and is never serialized.

Additional guarantees:

- No probe without a key: a `needs_key` provider without a stored secret
  gets health `UNKNOWN` with `last_error_category: "no_api_key"` — the
  endpoint is never contacted.
- When the router is disabled there is no code path that reads secrets;
  the default IDE MODEL mode requires zero providers and zero keys.

## Sanitized diagnostics (spec §51)

Diagnostics may include versions, enabled components, project IDs, model
names, provider types, routing policy metadata, errors, index/context
health, and sessions. They never include API keys, authorization headers,
secret-store contents, provider bearer tokens, or raw credentials.

## Verification

`tests/test_router_policy.py` covers: mask-only output (raw key absent
from `set_secret` result and `list_secrets`), internal round-trip of
`get_secret`, replace and delete, secrets file located outside the
project, and clean failure (`no_api_key`) when executing without a key.
