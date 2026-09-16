"""Token issuing and verification.

This module owns the token contract: every other module that touches
authentication tokens must go through these functions.
"""

import hashlib
import hmac
import time

SECRET_KEY = "dev-secret-change-me"
TOKEN_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 86400 * 7


class TokenExpired(Exception):
    """Raised when a token signature is valid but the token is too old."""


class TokenInvalid(Exception):
    """Raised when a token fails signature verification."""


def _sign(payload):
    """Produce an HMAC signature for a token payload string."""
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(user_id, scopes=None):
    """Issue a signed session token for ``user_id``.

    Returns a string of the form ``user_id.expires_at.signature``.
    """
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{user_id}.{expires_at}"
    signature = _sign(payload)
    scope_part = ",".join(sorted(scopes or []))
    return f"{payload}.{signature}:{scope_part}"


def verify_token(token):
    """Verify a token and return the decoded user_id.

    Raises TokenExpired or TokenInvalid on failure. This is the single
    verification contract used by decorators, middleware and handlers.
    """
    try:
        payload, _scope_part = token.rsplit(":", 1)
        user_id, expires_at_raw, signature = payload.rsplit(".", 2)
    except ValueError:
        raise TokenInvalid("malformed token")

    expected = _sign(f"{user_id}.{expires_at_raw}")
    if not hmac.compare_digest(expected, signature):
        raise TokenInvalid("bad signature")

    expires_at = int(expires_at_raw)
    if time.time() > expires_at:
        raise TokenExpired("token expired")
    return user_id


def refresh_token(token):
    """Exchange a still-valid token for a fresh one with a new expiry."""
    user_id = verify_token(token)
    return issue_token(user_id)

