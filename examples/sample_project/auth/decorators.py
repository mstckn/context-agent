"""Auth decorators: endpoint protection and rate limiting."""

import functools
import time

from auth.tokens import verify_token, TokenExpired, TokenInvalid

_RATE_BUCKETS = {}


def require_auth(fn):
    """Decorator: first argument must be a valid token."""

    @functools.wraps(fn)
    def wrapper(token, *args, **kwargs):
        try:
            user_id = verify_token(token)
        except (TokenExpired, TokenInvalid):
            raise PermissionError("authentication required")
        return fn(user_id, *args, **kwargs)

    return wrapper


def rate_limit(max_calls=10, window_seconds=60):
    """Decorator factory: sliding-window rate limit per caller key."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(key, *args, **kwargs):
            now = time.time()
            bucket = [t for t in _RATE_BUCKETS.get(key, [])
                      if now - t < window_seconds]
            if len(bucket) >= max_calls:
                raise RuntimeError("rate limit exceeded")
            bucket.append(now)
            _RATE_BUCKETS[key] = bucket
            return fn(key, *args, **kwargs)

        return wrapper

    return decorator
