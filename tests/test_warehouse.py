"""Tests for warehouse resolution logic.

Note: The autouse `reset_warehouse_cache` fixture in conftest.py clears
the warehouse cache between tests. Tests that call resolve_warehouse_id()
must still patch app_config to control the config-vs-autodetect path.
"""

from unittest.mock import MagicMock, patch
import pytest


class TestWarehouseResolution:

    def test_configured_id_used_directly(self):
        """When warehouse_id is set in config, use it without API calls."""
        from server.warehouse import resolve_warehouse_id, reset_cache
        reset_cache()  # ensure no stale cache
        with patch("server.warehouse.app_config") as mock_cfg:
            mock_cfg.warehouse_id = "configured-123"
            result = resolve_warehouse_id()
            assert result == "configured-123"

    def test_auto_detect_prefers_running(self):
        """Auto-detect should prefer a running warehouse."""
        from server.warehouse import _auto_detect_warehouse_id

        running_wh = MagicMock()
        running_wh.id = "running-456"
        running_wh.state = MagicMock()
        running_wh.state.value = "RUNNING"
        running_wh.warehouse_type = MagicMock()
        running_wh.warehouse_type.value = "PRO"

        stopped_wh = MagicMock()
        stopped_wh.id = "stopped-789"
        stopped_wh.state = MagicMock()
        stopped_wh.state.value = "STOPPED"
        stopped_wh.warehouse_type = MagicMock()
        stopped_wh.warehouse_type.value = "PRO"

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = [stopped_wh, running_wh]

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            result = _auto_detect_warehouse_id()
            assert result == "running-456"

    def test_auto_detect_falls_back_to_first(self):
        """When no warehouse is running, fall back to first available."""
        from server.warehouse import _auto_detect_warehouse_id

        stopped_wh = MagicMock()
        stopped_wh.id = "stopped-111"
        stopped_wh.state = MagicMock()
        stopped_wh.state.value = "STOPPED"
        stopped_wh.warehouse_type = MagicMock()
        stopped_wh.warehouse_type.value = "PRO"

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = [stopped_wh]

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            result = _auto_detect_warehouse_id()
            assert result == "stopped-111"

    def test_auto_detect_raises_when_none(self):
        """Should raise when no warehouses exist."""
        from server.warehouse import _auto_detect_warehouse_id

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = []

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="No SQL warehouse available"):
                _auto_detect_warehouse_id()
