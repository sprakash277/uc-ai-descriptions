"""Centralized warehouse resolution with caching."""

import logging
from typing import Optional

from .config import app_config, get_workspace_client

logger = logging.getLogger(__name__)

_cached_warehouse_id: Optional[str] = None


def resolve_warehouse_id() -> str:
    """Resolve the warehouse ID to use for SQL execution.

    Priority:
    1. Configured warehouse_id (from env var / config)
    2. First running serverless warehouse
    3. First running warehouse of any type
    4. First available warehouse
    """
    global _cached_warehouse_id

    if app_config.warehouse_id:
        return app_config.warehouse_id

    if _cached_warehouse_id:
        return _cached_warehouse_id

    w = get_workspace_client()
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")

    # Prefer running serverless
    for wh in warehouses:
        if wh.state and str(wh.state) == "RUNNING" and wh.warehouse_type and "SERVERLESS" in str(wh.warehouse_type):
            _cached_warehouse_id = wh.id
            logger.info("Auto-selected running serverless warehouse: %s (%s)", wh.name, wh.id)
            return _cached_warehouse_id

    # Prefer any running warehouse
    for wh in warehouses:
        if wh.state and str(wh.state) == "RUNNING":
            _cached_warehouse_id = wh.id
            logger.info("Auto-selected running warehouse: %s (%s)", wh.name, wh.id)
            return _cached_warehouse_id

    # Fall back to first available
    _cached_warehouse_id = warehouses[0].id
    logger.info("Auto-selected first available warehouse: %s (%s)", warehouses[0].name, warehouses[0].id)
    return _cached_warehouse_id
