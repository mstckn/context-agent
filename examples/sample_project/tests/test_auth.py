"""Tests for authentication tokens and service."""

import time

from auth.tokens import issue_token, verify_token, TokenInvalid
from auth.service import AuthService, AuthError
from auth.models import UserRepository


def test_issue_and_verify_token():
    token = issue_token("u_0001", scopes=["read"])
    assert verify_token(token) == "u_0001"


def test_tampered_token_rejected():
    token = issue_token("u_0001")
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    try:
        verify_token(tampered)
        assert False, "should have raised"
    except TokenInvalid:
        pass


def test_login_success_returns_contract():
    users = UserRepository()
    users.create(email="a@example.com")
    service = AuthService(users=users)
    result = service.login("a@example.com", "pw")
    assert set(result.keys()) == {"user_id", "token"}


def test_login_unknown_email_fails():
    service = AuthService()
    try:
        service.login("missing@example.com", "pw")
        assert False
    except AuthError:
        pass


def test_logout_invalidates_session():
    users = UserRepository()
    users.create(email="b@example.com")
    service = AuthService(users=users)
    result = service.login("b@example.com", "pw")
    assert service.validate_session(result["token"])
    service.logout(result["token"])
    assert service.validate_session(result["token"]) is None
