## Summary

Full refresh of the UC AI Descriptions app: security hardening, a test suite, operational improvements, and a significant set of UX upgrades to the single-table and batch schema workflows. Tested end-to-end on a live AWS workspace (fevm-shauver-snap-demo).

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

## Test plan

Testing was performed on a live AWS workspace (fevm-shauver-snap-demo).

- [x] All 18 unit tests pass (`pytest tests/ -v`)
- [x] Single-table workflow: generate → per-item regen → approve / reject / edit → apply to metastore
- [x] Batch schema workflow: generate for all tables → expand all → per-item regen → apply individual / apply all for table / apply all tables
- [x] Cascading applied state: all column buttons individually → per-table button auto-updates → global button auto-updates
- [x] Session rules override: enter override → generate → confirm orange badge + custom text in Rules tab; clear → confirm revert to org rules
- [x] Audit log: entries written to Delta table after applying descriptions in both tabs; displayed correctly in Audit Log tab
- [x] Startup audit validation: confirmed actionable error logs appear when catalog/schema permissions are missing

This pull request was AI-assisted by Isaac.
