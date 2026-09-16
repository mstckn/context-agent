#!/usr/bin/env python3
"""Print one Context Agent provider secret for Codex command-backed auth.

The value is written only to stdout because Codex expects its auth helper to
return a bearer token there.  Errors and diagnostics go to stderr so they can
never be mistaken for credentials.
"""

from __future__ import annotations

import argparse
import sys

from secret_store import get_secret


def main() -> int:
    parser = argparse.ArgumentParser(description="Context Agent credential helper for Codex")
    parser.add_argument("--provider", required=True)
    args = parser.parse_args()

    provider_id = str(args.provider).strip().lower()
    secret = get_secret(provider_id)
    if not secret:
        print(f"No stored credential for provider: {provider_id}", file=sys.stderr)
        return 1

    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
