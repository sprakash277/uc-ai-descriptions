"""Audit logging — write description approvals to a Delta table."""

import logging
from typing import Optional

from .config import get_workspace_client, app_config
from .warehouse import resolve_warehouse_id
from .sql_utils import quote_identifier

logger = logging.getLogger(__name__)


def _audit_table_quoted() -> str:
    """Return the backtick-quoted audit table path from config."""
    return quote_identifier(app_config.audit_table)


def ensure_audit_table() -> bool:
    """Create the audit table if it doesn't exist."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()

    sql = f"""
    CREATE TABLE IF NOT EXISTS {_audit_table_quoted()} (
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
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()

    sql = f"""
    INSERT INTO {_audit_table_quoted()}
    VALUES (:full_table_name, :item_type, :item_name, :previous,
            :ai_suggested, :final, :action, :applied_by, current_timestamp())
    """

    params = [
        StatementParameterListItem(name="full_table_name", value=full_table_name),
        StatementParameterListItem(name="item_type", value=item_type),
        StatementParameterListItem(name="item_name", value=item_name),
        StatementParameterListItem(name="previous", value=previous_description),
        StatementParameterListItem(name="ai_suggested", value=ai_suggested_description),
        StatementParameterListItem(name="final", value=final_description),
        StatementParameterListItem(name="action", value=action),
        StatementParameterListItem(name="applied_by", value=applied_by),
    ]

    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, parameters=params, wait_timeout="50s"
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
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()

    sql = f"""
    SELECT * FROM {_audit_table_quoted()}
    """

    if full_table_name:
        sql += " WHERE full_table_name = :table_filter"
        params = [StatementParameterListItem(name="table_filter", value=full_table_name)]
    else:
        params = None

    sql += f"""
    ORDER BY applied_at DESC
    LIMIT {limit}
    """

    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, parameters=params, wait_timeout="50s"
        )
        if not resp.result or not resp.result.data_array:
            return []

        columns = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(columns, row)) for row in resp.result.data_array]
    except Exception as e:
        logger.error("Audit log query failed: %s", e)
        return []
