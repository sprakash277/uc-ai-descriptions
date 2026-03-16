"""SQL warehouse resolution with caching."""

import logging

from .config import app_config, get_workspace_client

logger = logging.getLogger(__name__)

_cached_warehouse_id: str | None = None


def _auto_detect_warehouse_id() -> str:
    """Find a warehouse, preferring running ones."""
    w = get_workspace_client()
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")

    # Prefer running warehouses
    for wh in warehouses:
        if wh.state and wh.state.value == "RUNNING":
            logger.info("Auto-detected running warehouse: %s", wh.id)
            return wh.id

    # Fall back to first available
    logger.info("No running warehouse found, using first available: %s", warehouses[0].id)
    return warehouses[0].id


def resolve_warehouse_id() -> str:
    """Get the warehouse ID — from config or auto-detect. Cached after first call."""
    global _cached_warehouse_id

    if _cached_warehouse_id is not None:
        return _cached_warehouse_id

    if app_config.warehouse_id:
        _cached_warehouse_id = app_config.warehouse_id
        logger.info("Using configured warehouse: %s", _cached_warehouse_id)
    else:
        _cached_warehouse_id = _auto_detect_warehouse_id()

    return _cached_warehouse_id


def reset_cache() -> None:
    """Clear the cached warehouse ID. Useful for testing or settings changes."""
    global _cached_warehouse_id
    _cached_warehouse_id = None
