"""Tests for server/identity.py — JWT decode and user extraction."""

import base64
import json

from server.identity import _decode_jwt_payload, get_current_user


def _make_jwt(payload: dict) -> str:
    """Build a minimal (unsigned) JWT with the given payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesig"


# ── _decode_jwt_payload ────────────────────────────────────────────────

def test_decode_extracts_email():
    token = _make_jwt({"email": "alice@example.com", "sub": "12345"})
    payload = _decode_jwt_payload(token)
    assert payload["email"] == "alice@example.com"


def test_decode_handles_padding():
    # payloads of various lengths all decode cleanly
    for extra in range(4):
        token = _make_jwt({"x": "a" * extra})
        payload = _decode_jwt_payload(token)
        assert "x" in payload


def test_decode_returns_empty_on_bad_token():
    assert _decode_jwt_payload("notajwt") == {}
    assert _decode_jwt_payload("") == {}
    assert _decode_jwt_payload("a.b.c.d.e") == {}  # wrong part count is fine, uses parts[1]


def test_decode_returns_empty_on_invalid_base64():
    assert _decode_jwt_payload("header.!!!.sig") == {}


# ── get_current_user ───────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def test_get_current_user_returns_email_from_token():
    token = _make_jwt({"email": "bob@example.com"})
    req = _FakeRequest({"X-Forwarded-Access-Token": token})
    assert get_current_user(req) == "bob@example.com"


def test_get_current_user_falls_back_to_sub():
    token = _make_jwt({"sub": "service-principal-id"})
    req = _FakeRequest({"X-Forwarded-Access-Token": token})
    assert get_current_user(req) == "service-principal-id"


def test_get_current_user_falls_back_when_no_header():
    req = _FakeRequest({})
    user = get_current_user(req)
    # Should be dev_user or whatever DEV_USER_EMAIL is set to — not empty
    assert user and user != ""


def test_get_current_user_falls_back_on_bad_token():
    req = _FakeRequest({"X-Forwarded-Access-Token": "not.a.jwt"})
    user = get_current_user(req)
    assert user and user != ""
