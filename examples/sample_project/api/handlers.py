"""HTTP handlers producing the public JSON response contract.

The login response shape ``{"user_id": ..., "token": ...}`` is consumed
by ``frontend/api_client.ts`` — changing field names is a breaking
API contract change.
"""

from auth.service import AuthService, AuthError
from auth.decorators import rate_limit

_auth = AuthService()


@rate_limit(max_calls=5, window_seconds=60)
def login_handler(email, password):
    """POST /login — authenticate and return the session payload."""
    try:
        result = _auth.login(email, password)
    except AuthError as exc:
        return {"status": 401, "body": {"error": str(exc)}}
    return {
        "status": 200,
        "body": {
            "user_id": result["user_id"],
            "token": result["token"],
        },
    }


def me_handler(token):
    """GET /me — return the profile of the authenticated user."""
    user_id = _auth.validate_session(token)
    if user_id is None:
        return {"status": 401, "body": {"error": "invalid session"}}
    user = _auth.users.get(user_id)
    return {
        "status": 200,
        "body": {
            "user_id": user.user_id,
            "email": user.email,
            "display_name": user.display_name,
        },
    }


def logout_handler(token):
    """POST /logout — invalidate the session."""
    _auth.logout(token)
    return {"status": 204, "body": {}}
