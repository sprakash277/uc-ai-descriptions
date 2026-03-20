"""Tests for per-user permission enforcement plumbing.

These tests cover the token extraction and client-selection logic.
They do not test UC permission enforcement itself (that requires a live
workspace) — they verify that the correct client is passed through so
that UC *can* enforce permissions when OBO is enabled.
"""

from unittest.mock import MagicMock, patch

from server.identity import get_user_token
from server.config import get_user_workspace_client


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = {k.lower(): v for k, v in headers.items()}


# ── get_user_token ─────────────────────────────────────────────────────

def test_get_user_token_returns_token_when_present():
    req = _FakeRequest({"x-forwarded-access-token": "my-oauth-token"})
    assert get_user_token(req) == "my-oauth-token"


def test_get_user_token_returns_none_when_absent():
    req = _FakeRequest({})
    assert get_user_token(req) is None


def test_get_user_token_returns_none_for_empty_value():
    req = _FakeRequest({"x-forwarded-access-token": ""})
    assert get_user_token(req) is None


# ── get_request_client dependency ─────────────────────────────────────

def test_get_request_client_uses_user_client_when_token_present():
    """When OBO token is present, a user-scoped WorkspaceClient is returned."""
    from server.routes import get_request_client
    req = _FakeRequest({"x-forwarded-access-token": "user-token-abc"})
    with patch("server.routes.get_user_workspace_client") as mock_user_wc, \
         patch("server.routes.get_workspace_client") as mock_sp_wc:
        mock_user_wc.return_value = MagicMock(name="user_client")
        get_request_client(req)
        mock_user_wc.assert_called_once_with("user-token-abc")
        mock_sp_wc.assert_not_called()


def test_get_request_client_falls_back_to_sp_when_no_token():
    """When OBO token is absent (OBO not enabled), SP client is used."""
    from server.routes import get_request_client
    req = _FakeRequest({})
    with patch("server.routes.get_user_workspace_client") as mock_user_wc, \
         patch("server.routes.get_workspace_client") as mock_sp_wc:
        mock_sp_wc.return_value = MagicMock(name="sp_client")
        get_request_client(req)
        mock_sp_wc.assert_called_once()
        mock_user_wc.assert_not_called()


# ── catalog functions accept w= parameter ─────────────────────────────

def test_catalog_functions_use_provided_client():
    """catalog.py functions use the passed-in client, not get_workspace_client()."""
    from server import catalog

    mock_w = MagicMock()
    mock_w.catalogs.list.return_value = []

    with patch("server.catalog.get_workspace_client") as mock_default:
        catalog.list_catalogs(w=mock_w)
        mock_w.catalogs.list.assert_called_once()
        mock_default.assert_not_called()


def test_catalog_functions_fall_back_to_default_client():
    """catalog.py functions fall back to SP client when w= not provided."""
    from server import catalog

    mock_default_w = MagicMock()
    mock_default_w.catalogs.list.return_value = []

    with patch("server.catalog.get_workspace_client", return_value=mock_default_w):
        catalog.list_catalogs()
        mock_default_w.catalogs.list.assert_called_once()
