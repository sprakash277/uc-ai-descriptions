"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def reset_warehouse_cache():
    """Reset the warehouse cache before each test to prevent cross-test pollution."""
    yield
    try:
        from server.warehouse import reset_cache
        reset_cache()
    except ImportError:
        pass  # warehouse module not yet created
