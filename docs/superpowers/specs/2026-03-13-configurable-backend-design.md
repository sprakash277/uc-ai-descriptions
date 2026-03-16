# Configurable Backend for UC AI Descriptions

## Problem

The app has hardcoded values tied to a specific customer deployment (audit table path, model endpoint, warehouse selection) and lacks a configuration mechanism suitable for a DAB-managed, git-controlled deployment. This makes it difficult for SAs to deploy for new customers or for customers to operate long-term.

## Goals

1. Remove all customer-specific hardcoded values from source code
2. Establish a two-layer git-controlled configuration system (bundle variables + app config file)
3. Make the app deployable as a Databricks Asset Bundle
4. Improve SQL safety
5. Keep changes incremental and independently testable

## Non-Goals

- Frontend redesign (deferred to a follow-up; minimal fixes included where backend changes would break visible features)
- Runtime config persistence (all config is git-controlled)
- Multi-tenant support

## Configuration Architecture

### Layer 1: `databricks.yml` (infrastructure config)

Bundle variables that flow into the app as environment variables. These control infra-level settings that differ per deployment environment.

```yaml
bundle:
  name: uc-ai-descriptions

variables:
  warehouse_id:
    description: "SQL warehouse ID for executing statements"
    default: ""
  serving_endpoint:
    description: "Foundation Model API serving endpoint name"
    default: "databricks-claude-sonnet-4-6"
  app_title:
    description: "Display title for the app"
    default: "Unity Catalog AI Descriptions"

resources:
  apps:
    uc-ai-descriptions:
      name: uc-ai-descriptions
      description: "AI-powered UC table/column descriptions with human-in-the-loop review"
      source_code_path: ./
      config:
        command:
          - python
          - -m
          - uvicorn
          - "app:app"
          - "--host"
          - "0.0.0.0"
          - "--port"
          - "8000"
        env:
          - name: SERVING_ENDPOINT
            value: ${var.serving_endpoint}
          - name: WAREHOUSE_ID
            value: ${var.warehouse_id}
          - name: APP_TITLE
            value: ${var.app_title}

targets:
  dev:
    default: true
    workspace:
      host: ${workspace.host}
  prod:
    workspace:
      host: ${workspace.host}
    variables:
      serving_endpoint: "databricks-claude-sonnet-4-6"
```

### Layer 2: `config.yaml` (app-level config)

Content-oriented settings that are checked into git and loaded at app startup. These control app behavior rather than infrastructure.

```yaml
# Responsible AI rules injected into every AI generation prompt.
# These enforce organizational standards for generated descriptions.
responsible_ai_rules: |
  - Never include PII field names or example values in descriptions.
  - Use business-friendly language suitable for a data catalog audience.

# Centralized audit table — full three-part name.
# The app's SP needs CREATE TABLE + INSERT on this schema.
# Keep in a dedicated catalog/schema separate from described data
# to enforce append-only governance (data owners can't modify their own audit trail).
audit:
  table: "governance.ai_descriptions.audit_log"

# Catalogs and schemas to exclude from the browse tree.
exclusions:
  catalogs:
    - "__databricks_internal"
    - "system"
  schemas:
    - "information_schema"
```

### Relationship with `app.yaml`

The existing `app.yaml` is kept and updated with the new environment variables (`WAREHOUSE_ID`, `APP_TITLE`). Both files coexist:
- **`app.yaml`** — read by the Databricks Apps runtime for direct `databricks apps deploy` deployments
- **`databricks.yml`** — used by DAB (`databricks bundle deploy`), overrides `app.yaml` settings with bundle variable values

This ensures the app works with both deployment methods.

### Config separation

The two layers control distinct, non-overlapping settings:
- **`databricks.yml` env vars** control infrastructure: `serving_endpoint`, `warehouse_id`, `app_title`
- **`config.yaml`** controls app behavior: responsible AI rules, centralized audit table path, catalog/schema exclusions

There is no overlap by design. If a future setting needs per-environment variation, it should be added as a bundle variable → env var.

## Implementation Plan

### Step 1: Add `databricks.yml` + update README

**Files:** new `databricks.yml`, `README.md`

Create the DAB bundle definition with variables for warehouse ID, serving endpoint, and app title. Rewrite README deployment instructions to use `databricks bundle deploy` instead of manual workspace imports. Keep the permissions/grants section (still needed post-deploy).

### Step 2: Add `config.yaml` + document

**Files:** new `config.yaml`, `README.md`

Create `config.yaml` with the schema above and sensible defaults. Add a "Configuration" section to README explaining both config layers and how to customize.

### Step 3: Expand `server/config.py`

**Files:** `server/config.py`, `requirements.txt`

Replace the minimal auth-only config module with a central config loader that:
- Loads `config.yaml` from the app root at startup (add `pyyaml` to `requirements.txt`)
- Reads environment variables for infra settings
- Exposes a typed config object (dataclass or similar) importable by other modules
- Keeps the existing `get_workspace_client()`, `get_workspace_host()`, and `get_oauth_token()` functions
- Falls back to sensible defaults if `config.yaml` is missing

Key config fields:
- `serving_endpoint: str` (from env var `SERVING_ENDPOINT`)
- `warehouse_id: str | None` (from env var `WAREHOUSE_ID`, empty string = auto-detect)
- `app_title: str` (from env var `APP_TITLE`)
- `responsible_ai_rules: str` (from `config.yaml`)
- `audit_table: str` (from `config.yaml`, full three-part name e.g. `governance.ai_descriptions.audit_log`)
- `excluded_catalogs: list[str]` (from `config.yaml`)
- `excluded_schemas: list[str]` (from `config.yaml`)

### Step 4: Extract `server/warehouse.py`

**Files:** new `server/warehouse.py`, `server/catalog.py`, `server/audit.py`

Create a single module responsible for warehouse ID resolution:
- If `warehouse_id` is configured (non-empty), use it directly
- Otherwise, list warehouses and pick the first running serverless one, falling back to the first available
- Cache the resolved ID for the lifetime of the app process (avoid repeated API calls)
- Expose a `get_warehouse_id() -> str` function

Update `catalog.py` and `audit.py` to import from `warehouse.py` instead of duplicating the warehouse lookup logic.

### Step 5: Parameterize `audit.py` (centralized audit table)

**Files:** `server/audit.py`, `README.md`

- Remove the hardcoded `AUDIT_TABLE = "cat_nsp_z5zw62.retail_demo._ai_description_audit"` constant
- Read the audit table path from `app_config.audit_table` (full three-part name from `config.yaml`)
- Use `resolve_warehouse_id()` from the new warehouse module
- Function signatures stay the same — no catalog/schema routing needed since the audit table is centralized
- The `GET /api/audit` endpoint is unchanged (still reads from one table), so the frontend keeps working
- Update README: document the audit table setup (dedicated catalog/schema, INSERT-only grant for the app SP)

### Step 6: Parameterize `ai_gen.py` + update calling routes

**Files:** `server/ai_gen.py`, `server/routes.py`

- Remove the in-memory `_custom_rules` global and `get_custom_rules()` / `set_custom_rules()` functions
- Read Responsible AI rules from the config object instead
- Read the model endpoint from config instead of hardcoding
- Update `generate_notebook_code()` to inject the configured model name and rules rather than hardcoded values
- **Update `routes.py`**: the `POST /export-notebook` endpoint currently calls `ai_gen.get_custom_rules()` which is being removed — update it to pass rules from config. The `GET /api/rules` endpoint becomes a read-only view of config; the `POST /api/rules` endpoint is removed (rules are git-controlled now).
- **Update `static/index.html`**: make the Responsible AI Rules tab read-only (textarea readonly, remove Save/Clear buttons, update help text) so the frontend doesn't break when `POST /api/rules` is removed.

### Step 7: Parameterize `catalog.py`

**Files:** `server/catalog.py`

- Replace hardcoded exclusion lists in `list_catalogs()` and `list_schemas()` with values from config
- Update `apply_table_comment()` and `apply_column_comment()` to use `get_warehouse_id()` from the warehouse module (these are the most frequent callers of the duplicate warehouse-listing logic)

### Step 8: Add settings/warehouse endpoints to `routes.py`

**Files:** `server/routes.py`

- Add `GET /api/settings` — returns the current effective configuration (title, model, rules, exclusions, warehouse info). This prepares for the future settings UI panel.
- Update `app.py` to use `APP_TITLE` from config for the FastAPI title (currently hardcoded as `"Unity Catalog AI Descriptions"`).
- Add `GET /api/warehouses` — lists available warehouses with their status (running, stopped, etc.) for the future dropdown
- Remove `POST /api/rules` (rules are now git-controlled)
- Update `GET /api/rules` to read from config

### Step 9: Improve SQL escaping

**Files:** `server/catalog.py`, `server/audit.py`

- **Identifier injection**: `full_name` and `column_name` are interpolated directly into DDL. Validate that they match expected patterns (e.g., `catalog.schema.table`) and quote with backticks to prevent SQL injection via crafted table/column names. Same applies to the configured audit table path.
- For `COMMENT ON TABLE` and `ALTER COLUMN COMMENT` DDL statements (which cannot use parameterized queries), replace the minimal `replace("'", "\\'")` with proper escaping that also handles backslashes and other special characters
- For audit log INSERT statements, use parameterized queries via the statement execution API where supported
- For audit log SELECT statements, use parameterized queries for the WHERE clause filter

## Testing Strategy

Each step can be verified independently:
- Steps 1-2: `databricks bundle validate` succeeds; config file parses without error
- Step 3: App starts locally and loads config correctly
- Step 4: Warehouse selection works with and without a configured ID
- Steps 5-7: Existing functionality works with config-driven values instead of hardcoded ones
- Step 8: New endpoints return expected data
- Step 9: Descriptions containing quotes, backslashes, and special characters apply without error

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Breaking existing deployments that rely on env vars | Step 3 preserves backward compatibility — env vars still work, `config.yaml` is additive |
| `config.yaml` not present in workspace after deploy | App falls back to sensible defaults for all config values |
| Warehouse auto-detect picks a stopped warehouse | Step 4 prefers running serverless warehouses before falling back |
