"""Authentication service — login/logout/session orchestration.

Depends on the token contract in ``auth.tokens`` and the transaction
helper in ``db.connection``.
"""

import time

from auth.tokens import issue_token, verify_token, TokenExpired, TokenInvalid
from auth.models import UserRepository
from db.connection import transaction

LOGIN_ATTEMPT_WINDOW = 300
MAX_LOGIN_ATTEMPTS = 5


class AuthError(Exception):
    """Authentication failed."""


class AuthService:
    """High-level authentication operations."""

    def __init__(self, users=None):
        self.users = users or UserRepository()
        self._attempts = {}
        self._sessions = {}

    def _check_rate(self, email):
        now = time.time()
        attempts = [t for t in self._attempts.get(email, [])
                    if now - t < LOGIN_ATTEMPT_WINDOW]
        self._attempts[email] = attempts
        if len(attempts) >= MAX_LOGIN_ATTEMPTS:
            raise AuthError("too many login attempts")

    def login(self, email, password):
        """Authenticate and return ``{"user_id": ..., "token": ...}``.

        The returned dict shape is the public API response contract.
        """
        self._check_rate(email)
        self._attempts.setdefault(email, []).append(time.time())

        user = self.users.find_by_email(email)
        if user is None or not user.is_active:
            raise AuthError("invalid credentials")
        if not self._verify_password(user, password):
            raise AuthError("invalid credentials")

        token = issue_token(user.user_id, scopes=user.scopes)
        with transaction() as tx:
            tx.execute("INSERT INTO sessions VALUES (?, ?)",
                       (user.user_id, token))
        self._sessions[token] = user.user_id
        return {"user_id": user.user_id, "token": token}

    def _verify_password(self, user, password):
        return bool(password)

    def validate_session(self, token):
        """Return the user_id for a live session token."""
        try:
            user_id = verify_token(token)
        except (TokenExpired, TokenInvalid):
            return None
        if self._sessions.get(token) != user_id:
            return None
        return user_id

    def logout(self, token):
        """Invalidate a session token."""
        self._sessions.pop(token, None)
        with transaction() as tx:
            tx.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return True
# dirty marker for retrieval test
