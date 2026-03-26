"""Tests for server/identity.py — user identity extraction from Databricks headers."""

from server.identity import get_current_user


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_prefers_email_header():
    req = _FakeRequest({
        "x-forwarded-email": "alice@example.com",
        "x-forwarded-preferred-username": "alice@example.com",
        "x-forwarded-user": "123456@789",
    })
    assert get_current_user(req) == "alice@example.com"


def test_falls_back_to_preferred_username():
    req = _FakeRequest({
        "x-forwarded-preferred-username": "bob@example.com",
        "x-forwarded-user": "123456@789",
    })
    assert get_current_user(req) == "bob@example.com"


def test_falls_back_to_user_id():
    req = _FakeRequest({"x-forwarded-user": "123456@789"})
    assert get_current_user(req) == "123456@789"


def test_falls_back_to_local_user_when_no_headers():
    req = _FakeRequest({})
    user = get_current_user(req)
    assert user and user != ""


def test_empty_header_value_skipped():
    req = _FakeRequest({
        "x-forwarded-email": "",
        "x-forwarded-preferred-username": "carol@example.com",
    })
    assert get_current_user(req) == "carol@example.com"
