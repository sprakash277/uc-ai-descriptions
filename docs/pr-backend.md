## Summary

Security hardening, test suite, operational fixes, and startup audit validation on top of the configurable backend merged in main. The existing functionality is fully preserved — this hardens the SQL layer, adds regression coverage, and makes the app easier to operate and debug.

### Security (critical)

- **Parameterized audit queries**: `audit.py` INSERT and WHERE clauses use `StatementParameterListItem` instead of string interpolation — eliminates SQL injection risk in audit logging
- **Identifier validation**: `sql_utils.py` detects dangerous SQL patterns (`--`, `;`, `/*`, `*/`) in identifiers before they reach any SQL statement
- **Identifier quoting**: `quote_identifier()` handles dotted `catalog.schema.table` names and escapes embedded backticks
- **Pre-execution validation**: `catalog.py` calls `validate_identifier()` before executing `COMMENT ON TABLE` and `ALTER COLUMN COMMENT` statements
- **Comment escaping**: `escape_comment()` strips newlines and carriage returns in addition to escaping quotes and backslashes

### Quality

- **`config.py`**: `warehouse_id` typed as `str | None` (None = auto-detect) instead of empty-string sentinel — cleaner type semantics
- **`warehouse.py`**: Extracted `_auto_detect_warehouse_id()` helper + `reset_cache()` for testability
- **`routes.py`**: `/api/settings` resolves the actual warehouse ID and reports `warehouse_configured` flag; `/api/warehouses` uses `.value` on enum fields
- **`ai_gen.py`**: Removed unused `os` import

### Operational fixes

- **Python logging**: Added `logging.basicConfig` to `app.py` so `logger.error()` calls in `server/` modules are no longer silently dropped — errors now appear in app logs at `<app-url>/logz`
- **Startup audit validation**: On every boot, the app validates the configured audit catalog and schema exist, auto-creates the schema and table if absent, and emits actionable ERROR logs with the exact GRANT commands needed if any step fails. The app degrades gracefully (audit disabled) rather than crashing.

### Documentation

- **Shared SP identity model**: README now prominently documents that the app runs all UC operations under a single shared service principal — what users can browse, what identity descriptions are applied under, and what the audit log captures (`"app_user"` for all entries in the current version)
- **Startup audit validation**: README "Startup Audit Validation" section covers the check sequence, how to read the logs, and a quick-reference error-to-fix table

### Tests (18 new, all passing)

| Test file | Coverage |
|-----------|----------|
| `test_config.py` | Config defaults, YAML loading, env var overrides, `warehouse_id` None handling |
| `test_warehouse.py` | Configured ID, auto-detect prefers running, fallback to first, empty list error |
| `test_sql_safety.py` | Identifier validation (valid names, semicolons, comment injection, hyphens), comment escaping, backtick quoting |

## Test plan

- [x] All 18 unit tests pass (`pytest tests/ -v`)
- [x] Clean import verified (`from server import routes`)
- [x] App deployed and verified on a live workspace (fevm-shauver-snap-demo)
- [x] Tested single-table description workflow end-to-end (Browse & Generate tab)
- [x] Tested bulk schema generation workflow end-to-end (Batch Schema tab)
- [x] Tested "Apply All" workflow in both tabs
- [x] Validated that audit log entries are written to the Delta table after applying descriptions
- [x] Validated that audit log entries are displayed in the Audit Log tab

This pull request was AI-assisted by Isaac.
