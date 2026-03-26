"""Shared pytest fixtures."""

import sys
from unittest.mock import MagicMock

import pytest


# ── Databricks SDK stub ────────────────────────────────────────────────
# The Databricks SDK is not installed in the local dev environment.
# Stub it out so unit tests can import server modules without the full SDK.
# Tests that need real SDK behavior run in CI where it's installed.

def _stub_databricks():
    if "databricks" not in sys.modules:
        sdk_mock = MagicMock()
        # PermissionDenied needs to be a real exception class for `except` to work
        class PermissionDenied(Exception): pass
        sdk_mock.errors.PermissionDenied = PermissionDenied
        sys.modules["databricks"] = sdk_mock
        sys.modules["databricks.sdk"] = sdk_mock.sdk
        sys.modules["databricks.sdk.service"] = sdk_mock.sdk.service
        sys.modules["databricks.sdk.service.catalog"] = sdk_mock.sdk.service.catalog
        sys.modules["databricks.sdk.service.sql"] = sdk_mock.sdk.service.sql
        sys.modules["databricks.sdk.errors"] = sdk_mock.errors
        sys.modules.setdefault("openai", MagicMock())

_stub_databricks()


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_warehouse_cache():
    """Reset the warehouse cache before each test to prevent cross-test pollution."""
    yield
    try:
        from server.warehouse import reset_cache
        reset_cache()
    except ImportError:
        pass
