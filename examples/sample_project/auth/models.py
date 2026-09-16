"""User model and repository."""

from dataclasses import dataclass, field


@dataclass
class User:
    """Application user. ``user_id`` is the stable identifier used
    across the API response contract."""

    user_id: str
    email: str
    display_name: str = ""
    is_active: bool = True
    scopes: list = field(default_factory=list)


class UserRepository:
    """In-memory user store standing in for a real database table."""

    def __init__(self):
        self._users = {}

    def create(self, email, display_name="", scopes=None):
        user_id = f"u_{len(self._users) + 1:04d}"
        user = User(user_id=user_id, email=email,
                    display_name=display_name, scopes=scopes or [])
        self._users[user_id] = user
        return user

    def get(self, user_id):
        return self._users.get(user_id)

    def find_by_email(self, email):
        for user in self._users.values():
            if user.email == email:
                return user
        return None

    def deactivate(self, user_id):
        user = self._users.get(user_id)
        if user:
            user.is_active = False
        return user
