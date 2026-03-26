"""Unity Catalog operations — browse and update table/column descriptions.

## Why SQL instead of the UC REST API?

The natural way to browse Unity Catalog would be to use the Databricks SDK's
UC REST API methods (WorkspaceClient.catalogs.list(), .schemas.list(), etc.).
We intentionally do NOT do that here.  The reason is a fundamental limitation
of how Databricks Apps OBO (On-Behalf-Of) authorization works.

### The OBO scope problem

Databricks Apps OBO forwards the calling user's OAuth token so the app can act
on their behalf.  The token's capabilities are bounded by which API scopes were
declared in user_api_scopes in databricks.yml.  As of 2026, the only valid
values are:

    sql              — SQL Statement Execution API (warehouses)
    dashboards.genie — Genie AI/BI spaces
    files.files      — Files API

There is NO scope that covers the Unity Catalog REST API
(/api/2.1/unity-catalog/*).  If you call w.catalogs.list() with an OBO token,
the Databricks platform returns:

    403 Forbidden: Invalid scope, required scopes: unity-catalog

And "unity-catalog" cannot be declared in user_api_scopes — the platform
rejects it as an invalid scope value.

### The SQL workaround

The SQL Statement Execution API (covered by the sql scope) can browse UC
metadata just as well as the REST API, via:

  - SHOW CATALOGS
  - information_schema.schemata   — schemas in a catalog
  - information_schema.tables     — tables in a schema
  - information_schema.columns    — columns in a table

Critically, Unity Catalog's information_schema views are *automatically
permission-filtered* — a query returns only the objects the calling user has
been granted access to.  This means running these queries through an OBO SQL
token gives us exactly the per-user access enforcement we want, without needing
a UC REST API scope.

### Operation-to-client mapping

  Browse (list_catalogs, list_schemas, list_tables, get_table_details)
      → OBO user token via sql scope → SQL Statement Execution → UC filters by user perms

  Apply (apply_table_comment, apply_column_comment)
      → OBO user token via sql scope → SQL Statement Execution → UC enforces user's MODIFY rights

  AI generation (catalog.get_table_details called from ai_gen)
      → OBO user token if present → user can only generate for tables they can read

  Audit log writes (audit.py)
      → App service principal always → user may not have write access to the audit catalog
"""

import logging
from typing import Any

from databricks.sdk.service.sql import StatementState, StatementParameterListItem

from .config import get_workspace_client, app_config
from .warehouse import resolve_warehouse_id
from .sql_utils import validate_identifier, quote_identifier, escape_comment

logger = logging.getLogger(__name__)


# ── SQL helper ────────────────────────────────────────────────────────────

def _run_sql(w, sql: str, params: list | None = None) -> list[dict]:
    """Execute SQL via the warehouse and return rows as a list of dicts.

    All catalog operations route through here — both browse (SHOW / information_schema
    queries) and apply (COMMENT ON TABLE / ALTER TABLE COLUMN COMMENT).  When the
    caller passes an OBO-scoped WorkspaceClient, UC enforces the calling user's
    permissions on every statement.

    Raises RuntimeError on SQL failure.  Returns [] for DDL or empty results.
    """
    wh_id = resolve_warehouse_id()
    kwargs: dict = dict(warehouse_id=wh_id, statement=sql, wait_timeout="50s")
    if params:
        kwargs["parameters"] = params

    resp = w.statement_execution.execute_statement(**kwargs)

    if not resp.status or resp.status.state != StatementState.SUCCEEDED:
        msg = (
            resp.status.error.message
            if resp.status and resp.status.error
            else "unknown error"
        )
        state = resp.status.state if resp.status else "no status"
        raise RuntimeError(f"SQL failed ({state}): {msg}")

    if not resp.result or not resp.result.data_array:
        return []

    cols = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(cols, row)) for row in resp.result.data_array]


# ── Browse ────────────────────────────────────────────────────────────────
#
# All three browse functions use SQL instead of the UC REST API.
# See module docstring for the full explanation.  Short version:
#
#   - The UC REST API requires a "unity-catalog" OBO scope that does not exist.
#   - SQL Statement Execution (covered by the "sql" OBO scope) works instead.
#   - information_schema views are permission-filtered by UC automatically.

def list_catalogs(w=None) -> list[dict]:
    # SHOW CATALOGS respects the calling user's UC permissions when executed
    # via an OBO-scoped client — only catalogs the user has USE CATALOG on appear.
    w = w or get_workspace_client()
    rows = _run_sql(w, "SHOW CATALOGS")
    return [
        {"name": r["catalog"], "comment": r.get("comment") or ""}
        for r in rows
        if r["catalog"] not in app_config.excluded_catalogs
    ]


def list_schemas(catalog: str, w=None) -> list[dict]:
    # information_schema.schemata is UC permission-filtered: only schemas the
    # user has USE SCHEMA on (or has inherited via USE CATALOG) are returned.
    w = w or get_workspace_client()
    validate_identifier(catalog)
    cat_q = quote_identifier(catalog)
    sql = (
        f"SELECT schema_name, comment "
        f"FROM {cat_q}.information_schema.schemata"
    )
    rows = _run_sql(w, sql)
    return [
        {"name": r["schema_name"], "comment": r.get("comment") or ""}
        for r in rows
        if r["schema_name"] not in app_config.excluded_schemas
    ]


def list_tables(catalog: str, schema: str, w=None) -> list[dict]:
    # information_schema.tables is UC permission-filtered: only tables the
    # user has SELECT (or higher) on are returned.
    w = w or get_workspace_client()
    validate_identifier(f"{catalog}.{schema}")
    cat_q = quote_identifier(catalog)
    sql = (
        f"SELECT table_name, table_type, comment "
        f"FROM {cat_q}.information_schema.tables "
        f"WHERE table_schema = :schema_name"
    )
    params = [StatementParameterListItem(name="schema_name", value=schema)]
    rows = _run_sql(w, sql, params=params)
    return [
        {
            "name": r["table_name"],
            "full_name": f"{catalog}.{schema}.{r['table_name']}",
            "table_type": r.get("table_type") or "",
            "comment": r.get("comment") or "",
        }
        for r in rows
    ]


def get_table_details(full_name: str, w=None) -> dict[str, Any]:
    """Get table metadata including columns via information_schema."""
    w = w or get_workspace_client()
    validate_identifier(full_name)

    parts = full_name.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected 3-part name (catalog.schema.table), got: {full_name}")
    catalog_name, schema_name, table_name = parts
    cat_q = quote_identifier(catalog_name)

    id_params = [
        StatementParameterListItem(name="schema_name", value=schema_name),
        StatementParameterListItem(name="table_name", value=table_name),
    ]

    # Table-level info
    table_sql = (
        f"SELECT table_name, table_catalog, table_schema, table_type, comment, "
        f"data_source_format, storage_path, created "
        f"FROM {cat_q}.information_schema.tables "
        f"WHERE table_schema = :schema_name AND table_name = :table_name"
    )
    table_rows = _run_sql(w, table_sql, params=id_params)
    if not table_rows:
        raise RuntimeError(f"Table not found or not accessible: {full_name}")
    t = table_rows[0]

    # Column info
    col_sql = (
        f"SELECT column_name, data_type, is_nullable, comment "
        f"FROM {cat_q}.information_schema.columns "
        f"WHERE table_schema = :schema_name AND table_name = :table_name "
        f"ORDER BY ordinal_position"
    )
    col_rows = _run_sql(w, col_sql, params=id_params)

    columns = [
        {
            "name": r["column_name"],
            "type_text": r.get("data_type") or "",
            "comment": r.get("comment") or "",
            "nullable": (r.get("is_nullable") or "YES").upper() == "YES",
        }
        for r in col_rows
    ]

    return {
        "full_name": full_name,
        "name": t["table_name"],
        "catalog_name": t["table_catalog"],
        "schema_name": t["table_schema"],
        "table_type": t.get("table_type") or "",
        "comment": t.get("comment") or "",
        "columns": columns,
        "data_source_format": t.get("data_source_format") or "",
        "storage_location": t.get("storage_path") or "",
        "created_at": str(t.get("created") or ""),
    }


# ── Apply ─────────────────────────────────────────────────────────────────

def apply_table_comment(full_name: str, comment: str, w=None) -> bool:
    """Apply a comment to a table using SQL."""
    validate_identifier(full_name)
    w = w or get_workspace_client()
    escaped = escape_comment(comment)
    _run_sql(w, f"COMMENT ON TABLE {quote_identifier(full_name)} IS '{escaped}'")
    return True


def apply_column_comment(full_name: str, column_name: str, comment: str, w=None) -> bool:
    """Apply a comment to a column using SQL."""
    validate_identifier(full_name)
    w = w or get_workspace_client()
    escaped = escape_comment(comment)
    col_quoted = column_name.replace("`", "``")
    _run_sql(w, f"ALTER TABLE {quote_identifier(full_name)} ALTER COLUMN `{col_quoted}` COMMENT '{escaped}'")
    return True
