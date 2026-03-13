"""Audit logging — write description approvals to a Delta table."""

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import get_workspace_client

logger = logging.getLogger(__name__)

# Default audit table location
AUDIT_TABLE = "cat_nsp_z5zw62.retail_demo._ai_description_audit"


def _get_warehouse_id() -> str:
    w = get_workspace_client()
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")
    return warehouses[0].id


def ensure_audit_table() -> bool:
    """Create the audit table if it doesn't exist."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = _get_warehouse_id()

    sql = f"""
    CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
        full_table_name STRING,
        item_type STRING COMMENT 'TABLE or COLUMN',
        item_name STRING,
        previous_description STRING,
        ai_suggested_description STRING,
        final_description STRING COMMENT 'What was actually applied (may be edited)',
        action STRING COMMENT 'approved, rejected, edited',
        applied_by STRING,
        applied_at TIMESTAMP
    ) USING DELTA
    COMMENT 'Audit log for AI-generated description changes'
    """

    resp = w.statement_execution.execute_statement(
        warehouse_id=wh_id, statement=sql, wait_timeout="50s"
    )
    return resp.status and resp.status.state == StatementState.SUCCEEDED


def log_action(
    full_table_name: str,
    item_type: str,
    item_name: str,
    previous_description: str,
    ai_suggested_description: str,
    final_description: str,
    action: str,
    applied_by: str = "app_user",
) -> bool:
    """Log a single description action to the audit table."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = _get_warehouse_id()

    def esc(s: str) -> str:
        return s.replace("'", "\\'").replace("\n", " ")

    sql = f"""
    INSERT INTO {AUDIT_TABLE}
    VALUES (
        '{esc(full_table_name)}',
        '{esc(item_type)}',
        '{esc(item_name)}',
        '{esc(previous_description)}',
        '{esc(ai_suggested_description)}',
        '{esc(final_description)}',
        '{esc(action)}',
        '{esc(applied_by)}',
        current_timestamp()
    )
    """

    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, wait_timeout="50s"
        )
        return resp.status and resp.status.state == StatementState.SUCCEEDED
    except Exception as e:
        logger.error("Audit log failed: %s", e)
        return False


def log_batch(
    full_table_name: str,
    actions: list[dict],
    applied_by: str = "app_user",
) -> int:
    """Log multiple actions. Each dict has: item_type, item_name, previous, ai_suggested, final, action."""
    success_count = 0
    for a in actions:
        ok = log_action(
            full_table_name=full_table_name,
            item_type=a["item_type"],
            item_name=a["item_name"],
            previous_description=a.get("previous", ""),
            ai_suggested_description=a.get("ai_suggested", ""),
            final_description=a.get("final", ""),
            action=a["action"],
            applied_by=applied_by,
        )
        if ok:
            success_count += 1
    return success_count


def get_audit_log(full_table_name: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Retrieve recent audit log entries."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = _get_warehouse_id()

    where = ""
    if full_table_name:
        where = f"WHERE full_table_name = '{full_table_name}'"

    sql = f"""
    SELECT * FROM {AUDIT_TABLE}
    {where}
    ORDER BY applied_at DESC
    LIMIT {limit}
    """

    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, wait_timeout="50s"
        )
        if not resp.result or not resp.result.data_array:
            return []

        columns = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(columns, row)) for row in resp.result.data_array]
    except Exception as e:
        logger.error("Audit log query failed: %s", e)
        return []
