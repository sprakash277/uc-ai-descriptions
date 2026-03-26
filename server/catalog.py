"""Unity Catalog operations — browse and update table/column descriptions."""

import logging
from typing import Any

from databricks.sdk.service.catalog import ColumnInfo

from .config import get_workspace_client, app_config
from .warehouse import resolve_warehouse_id
from .sql_utils import validate_identifier, quote_identifier, escape_comment

logger = logging.getLogger(__name__)


def list_catalogs() -> list[dict]:
    w = get_workspace_client()
    return [
        {"name": c.name, "comment": c.comment or ""}
        for c in w.catalogs.list()
        if c.name not in app_config.excluded_catalogs
    ]


def list_schemas(catalog: str) -> list[dict]:
    w = get_workspace_client()
    return [
        {"name": s.name, "comment": s.comment or ""}
        for s in w.schemas.list(catalog_name=catalog)
        if s.name not in app_config.excluded_schemas
    ]


def list_tables(catalog: str, schema: str) -> list[dict]:
    w = get_workspace_client()
    return [
        {
            "name": t.name,
            "full_name": t.full_name,
            "table_type": str(t.table_type) if t.table_type else "",
            "comment": t.comment or "",
        }
        for t in w.tables.list(catalog_name=catalog, schema_name=schema)
    ]


def get_table_details(full_name: str) -> dict[str, Any]:
    """Get table metadata including columns."""
    w = get_workspace_client()
    t = w.tables.get(full_name)
    columns = []
    if t.columns:
        for col in t.columns:
            columns.append({
                "name": col.name,
                "type_text": col.type_text or "",
                "comment": col.comment or "",
                "nullable": col.nullable if col.nullable is not None else True,
            })
    return {
        "full_name": t.full_name,
        "name": t.name,
        "catalog_name": t.catalog_name,
        "schema_name": t.schema_name,
        "table_type": str(t.table_type) if t.table_type else "",
        "comment": t.comment or "",
        "columns": columns,
        "data_source_format": str(t.data_source_format) if t.data_source_format else "",
        "storage_location": t.storage_location or "",
        "created_at": str(t.created_at) if t.created_at else "",
    }


def apply_table_comment(full_name: str, comment: str) -> bool:
    """Apply a comment to a table using SQL."""
    from databricks.sdk.service.sql import StatementState

    validate_identifier(full_name)
    w = get_workspace_client()
    warehouse_id = resolve_warehouse_id()

    escaped = escape_comment(comment)
    sql = f"COMMENT ON TABLE {quote_identifier(full_name)} IS '{escaped}'"

    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
    )
    return resp.status and resp.status.state == StatementState.SUCCEEDED


def apply_column_comment(full_name: str, column_name: str, comment: str) -> bool:
    """Apply a comment to a column using SQL."""
    from databricks.sdk.service.sql import StatementState

    validate_identifier(full_name)
    w = get_workspace_client()
    warehouse_id = resolve_warehouse_id()

    escaped = escape_comment(comment)
    col_quoted = column_name.replace("`", "``")
    sql = f"ALTER TABLE {quote_identifier(full_name)} ALTER COLUMN `{col_quoted}` COMMENT '{escaped}'"

    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
    )
    return resp.status and resp.status.state == StatementState.SUCCEEDED
