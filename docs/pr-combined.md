## Summary

Full refresh of the UC AI Descriptions app: security hardening, a test suite, operational improvements, and a significant set of UX upgrades to the single-table and batch schema workflows. Tested end-to-end on a live AWS workspace.

---

## Backend changes

### Security (critical)

- **Parameterized audit queries**: `audit.py` INSERT and WHERE clauses use `StatementParameterListItem` instead of string interpolation — eliminates SQL injection risk in audit logging
- **Identifier validation**: `sql_utils.py` detects dangerous SQL patterns (`--`, `;`, `/*`, `*/`) in identifiers before they reach any SQL statement
- **Identifier quoting**: `quote_identifier()` handles dotted `catalog.schema.table` names and escapes embedded backticks
- **Pre-execution validation**: `catalog.py` calls `validate_identifier()` before executing `COMMENT ON TABLE` and `ALTER COLUMN COMMENT` statements
- **Comment escaping**: `escape_comment()` strips newlines and carriage returns in addition to escaping quotes and backslashes

### New endpoints

- **`POST /api/generate/item`**: Re-generates the AI description for a single table or column. Uses the full table context for quality, returns only the requested item. Supports `model` and `rules_override` parameters. Powers the per-description ↺ Regen buttons in the UI.
- **`POST /api/generate`** and **`POST /api/generate/batch`**: Extended with `rules_override: Optional[str]` — null uses org rules from `config.yaml`, a non-null string overrides just for that request (powers per-session rule overrides in the UI).

### Operational

- **Python logging**: Added `logging.basicConfig` to `app.py` so `logger.error()` calls in `server/` are no longer silently dropped — errors now appear in app logs at `<app-url>/logz`
- **Startup audit validation**: On every boot, the app validates the configured audit catalog and schema exist, auto-creates the schema and table if absent, and emits actionable ERROR logs with the exact GRANT commands needed if any step fails. Degrades gracefully (audit disabled) rather than crashing.

### Quality

- **`config.py`**: `warehouse_id` typed as `str | None` (None = auto-detect) instead of empty-string sentinel
- **`warehouse.py`**: Extracted `_auto_detect_warehouse_id()` helper + `reset_cache()` for testability
- **`routes.py`**: `/api/settings` resolves the actual warehouse ID and reports `warehouse_configured` flag

### Tests (18 new, all passing)

| Test file | Coverage |
|-----------|----------|
| `test_config.py` | Config defaults, YAML loading, env var overrides, `warehouse_id` None handling |
| `test_warehouse.py` | Configured ID, auto-detect prefers running, fallback to first, empty list error |
| `test_sql_safety.py` | Identifier validation (valid names, semicolons, comment injection, hyphens), comment escaping, backtick quoting |

### Documentation

- README: "Shared SP identity model" section + "Startup Audit Validation" section with check sequence, log URLs, and error→fix table
- `docs/plan-rules-editing.md`: 4-phase implementation plan for persistent, access-controlled rules editing (future work)

---

## Frontend changes

### Bug fixes

- **Null crash in batch generate**: `desc.replace()` was called on null column descriptions for large tables, causing a JS runtime error. Fixed with a `(desc || '').replace(...)` null guard.
- **approveAll / rejectAll skipped table description**: Both functions skipped the `__table__` key. Removed the skip.
- **Batch apply silent failures**: Column "Apply" buttons had no loading state. Fixed with disabled/textContent state changes, null filtering before the API request, and accurate success/failure counts in the toast.

### Browse & Generate tab

- **Unified description display**: Removed the redundant "Table Description" box above the AI suggestions — table and column descriptions now appear in a single unified list.
- **Clear All button**: Resets all suggestions to pending, restoring the original AI text.
- **Per-description Regen button**: Each suggestion row (table + every column) has a **↺ Regen** button. Calls `/api/generate/item`, updates only that row, resets its status to pending — all other rows and their approve/reject state are preserved.

### Batch Schema tab

- **Expand All**: Opens all table cards at once.
- **Apply All Tables**: Applies every table sequentially with loading state and "✓ All Applied" on completion.
- **Column button state during apply**: All column "Apply" buttons show "..." immediately when table-level apply fires, then "Applied" on success or revert on failure.
- **Cascading applied state**: When all column buttons are applied individually, the per-table button auto-updates; when all per-table buttons are applied, the global button auto-updates.
- **Per-description Regen button**: Each column row and the table description have a **↺** button. Regenerates in place, syncs `batchResults` state, and resets the Apply button so the new description is what gets applied.

### Responsible AI Rules tab

- **Session rules override**: A collapsible panel on Browse, Batch, and Rules tabs lets users enter custom rules for their current session. Session rules are passed as `rules_override` on every generate call; null means "use org defaults."
- **Currently effective rules display**: The Rules tab shows whichever rules are active — session override or org defaults — with a source badge ("Session override active" in orange, or "System default (config.yaml)"). When an override is active, the org rules appear below in a dimmed section for reference.

---

---

## Per-user UC permissions (Phase 3)

Browse, generate, and apply operations now run under the **calling user's own identity** rather than the shared app service principal. Users can only see and modify objects their UC permissions allow.

### How it works

When a user opens the app, Databricks Apps prompts them to authorize it (once per app version). Their OAuth token is then forwarded on every request as `X-Forwarded-Access-Token`. A FastAPI dependency (`get_request_client`) detects the token and constructs a per-request `WorkspaceClient` authenticated as that user. If no token is present (OBO not enabled, or local dev), the app falls back gracefully to the SP.

```
Browse / Apply:  User OBO token → SQL Execution → UC enforces user's permissions
AI Generation:   User OBO token → catalog read only (serving endpoint call stays SP)
Audit log:       SP always      → user may not have write access to audit catalog
```

### Why browse uses SQL instead of the UC REST API

The original design called for passing the user's `WorkspaceClient` to `w.catalogs.list()`, `w.schemas.list()`, etc. This doesn't work.

Databricks Apps OBO only supports three `user_api_scopes` values: `sql`, `dashboards.genie`, and `files.files`. The Unity Catalog REST API requires a `unity-catalog` scope — which is not a valid `user_api_scopes` value. Attempting it returns `403: required scopes: unity-catalog`.

**The fix:** Unity Catalog's `information_schema` views (`schemata`, `tables`, `columns`) and `SHOW CATALOGS` are accessible via the SQL Statement Execution API, which the `sql` scope covers. These views are automatically permission-filtered by UC — queries return only what the calling user has access to. Routing all browse operations through the warehouse via an OBO token gives us per-user enforcement with no additional scope required.

See `server/catalog.py` module docstring and `docs/plan-user-permissions.md` for full detail.

### OBO setup required (workspace admin, one-time)

1. Workspace settings → Previews → enable **"On-Behalf-Of User Authorization"**
2. Restart any existing apps

`databricks.yml` declares `user_api_scopes: [sql]` — no further UI steps needed after the workspace toggle.

### Technical notes

- `get_user_workspace_client()` in `config.py` uses a custom `CredentialsStrategy` rather than passing `token=` directly. This is necessary because the app SP's `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` env vars are always present and cause the SDK to throw "more than one authorization method configured" if a token is also passed.
- `catalog.py` was rewritten to use `_run_sql()` for all operations — browse via `information_schema`, apply via `COMMENT ON TABLE` / `ALTER TABLE COLUMN COMMENT`. The UC REST API is no longer used anywhere in the browse path.
- 6 tests added/updated in `test_user_permissions.py`

---

## Test plan

Testing was performed on a live AWS workspace.

- [x] All 18 unit tests pass (`pytest tests/ -v`)
- [x] Single-table workflow: generate → per-item regen → approve / reject / edit → apply to metastore
- [x] Batch schema workflow: generate for all tables → expand all → per-item regen → apply individual / apply all for table / apply all tables
- [x] Cascading applied state: all column buttons individually → per-table button auto-updates → global button auto-updates
- [x] Session rules override: enter override → generate → confirm orange badge + custom text in Rules tab; clear → confirm revert to org rules
- [x] Audit log: entries written to Delta table after applying descriptions in both tabs; displayed correctly in Audit Log tab
- [x] Startup audit validation: confirmed actionable error logs appear when catalog/schema permissions are missing

This pull request was AI-assisted by Isaac.
