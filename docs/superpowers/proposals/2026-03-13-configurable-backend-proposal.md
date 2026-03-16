# Proposal: Make UC AI Descriptions Configurable and DAB-Deployable

## Problem

The app has hardcoded values tied to a specific customer deployment — an audit table path (`cat_nsp_z5zw62.retail_demo._ai_description_audit`), a fixed model endpoint, and warehouse selection that always grabs the first available. Deploying for a new customer requires editing source code. There's no configuration mechanism, and the app isn't set up as a Databricks Asset Bundle.

## Proposed Changes

### 1. Two-layer git-controlled configuration

All configuration lives in the repo — no runtime persistence or admin UI needed.

**`databricks.yml`** handles infrastructure settings that vary per deployment environment:

```yaml
variables:
  warehouse_id:
    description: "SQL warehouse ID (leave empty for auto-detect)"
    default: ""
  serving_endpoint:
    description: "Foundation Model API endpoint"
    default: "databricks-claude-sonnet-4-6"
  app_title:
    description: "Display title for the app"
    default: "Unity Catalog AI Descriptions"
```

These flow into the app as environment variables. Override per-environment in `targets` or at deploy time with `--var`.

**`config.yaml`** handles app behavior settings:

```yaml
responsible_ai_rules: |
  - Never include PII field names or example values in descriptions.
  - Use business-friendly language suitable for a data catalog audience.

audit:
  table: "governance.ai_descriptions.audit_log"

exclusions:
  catalogs:
    - "__databricks_internal"
    - "system"
  schemas:
    - "information_schema"
```

### 2. DAB deployment replaces manual workspace imports

The current deployment requires 6+ manual CLI commands to upload files and deploy. With `databricks.yml`, it becomes:

```bash
databricks bundle deploy -t dev
```

The existing `app.yaml` is updated to include the new environment variables (`WAREHOUSE_ID`, `APP_TITLE`). Both files coexist: `app.yaml` serves direct `databricks apps deploy` deployments, while `databricks.yml` overrides its settings for DAB deployments.

### 3. Centralized warehouse resolution

Currently, both `catalog.py` and `audit.py` independently call `w.warehouses.list()` on every SQL operation (picking `warehouses[0]`). A new `server/warehouse.py` module:
- Uses the configured warehouse ID if set
- Otherwise auto-detects, preferring running warehouses
- Caches the result for the app's lifetime

### 4. Centralized audit table (configurable location)

Instead of a hardcoded audit table path, the audit table location is configured in `config.yaml` as a full three-part name. The app's service principal gets INSERT-only access; table/schema owners in described catalogs have no access to the audit trail. This enforces append-only semantics and prevents anyone with data permissions from tampering with the change log.

### 5. Responsible AI rules become git-controlled

Currently stored in an in-memory global variable — lost on every restart, set via a POST endpoint. The proposal moves rules into `config.yaml`, making them:
- Version-controlled and reviewable via PR
- Consistent across restarts
- Read-only from the app's perspective

### 6. SQL safety improvements

- Identifier validation and backtick quoting for table/column names in DDL
- Parameterized queries for audit INSERT/SELECT operations
- Improved string escaping for COMMENT statements (handles backslashes, newlines)

## New Files

| File | Purpose |
|------|---------|
| `databricks.yml` | DAB bundle definition |
| `config.yaml` | App behavior configuration |
| `server/warehouse.py` | Warehouse resolution with caching |
| `server/sql_utils.py` | SQL escaping and identifier validation |
| `tests/` | Unit tests for config, warehouse, and SQL safety |

## Breaking Changes

| Change | Impact | Migration |
|--------|--------|-----------|
| `POST /api/rules` removed | Frontend "Save Rules" button becomes read-only | Frontend updated in same task: textarea disabled, buttons removed, help text updated |
| `app.yaml` updated | Anyone parsing `app.yaml` for env vars | New env vars added (`WAREHOUSE_ID`, `APP_TITLE`); still compatible with `databricks apps deploy` |

## Trade-offs and Decisions

### Centralized audit table vs. per-schema co-location
**Chosen:** Single centralized audit table in a dedicated catalog/schema.
**Alternative:** Per-schema co-location (audit table next to described tables).
**Rationale:** A centralized table enforces proper governance — the app's SP gets INSERT-only access, while table/schema owners in described catalogs have no ability to modify the audit trail. Co-location would inherit permissions from the parent schema, allowing data owners to tamper with their own audit records. The centralized approach also simplifies the code (no per-schema routing) and keeps the `GET /api/audit` endpoint backward-compatible with the existing frontend.

### Config in YAML file vs. Delta table
**Chosen:** YAML file in git.
**Alternative:** Delta table for runtime persistence.
**Rationale:** Since this is DAB-deployed and git-managed, config changes should go through the same PR review process as code changes. No runtime admin UI is needed.

### Identifier validation rejects names with spaces
**Chosen:** Only allow alphanumeric, underscore, and hyphen in identifiers.
**Alternative:** Accept any UC-valid name (including spaces in backtick-quoted names).
**Rationale:** Reduces SQL injection surface. Names with spaces will get a clear error message. This covers the vast majority of real-world UC naming conventions.

## Open Questions

1. **Audit table permissions:** The app's service principal needs CREATE TABLE + INSERT on the configured audit schema (e.g., `governance.ai_descriptions`). This is a one-time setup. The audit table should be append-only from the app's perspective — grant only INSERT, not UPDATE/DELETE.

2. **Notebook export:** The exported notebook currently hardcodes the model name. The proposal updates it to use the configured value. Should the notebook also embed the Responsible AI rules, or should it reference them from a separate source?

3. **Excluded catalogs/schemas:** The defaults exclude `system` and `__databricks_internal`. Are there other catalogs/schemas that should be excluded by default?

## Implementation Plan

The work is broken into 12 small tasks across 4 chunks, each producing an independently testable commit. The full implementation plan with exact code and test cases is at:

- **Spec:** [`docs/superpowers/specs/2026-03-13-configurable-backend-design.md`](specs/2026-03-13-configurable-backend-design.md)
- **Plan:** [`docs/superpowers/plans/2026-03-13-configurable-backend.md`](../plans/2026-03-13-configurable-backend.md)

### Chunk summary

| Chunk | Tasks | What it does |
|-------|-------|-------------|
| 1: Foundation | 1-4 | Add `databricks.yml`, `config.yaml`, expand config loader, extract warehouse module |
| 2: Parameterize | 5-7 | Remove hardcoded values from audit, ai_gen, and catalog modules |
| 3: Endpoints + Safety | 8-9 | Add settings/warehouse API endpoints, improve SQL escaping |
| 4: Docs + Cleanup | 10-12 | Update README, update `app.yaml` with new env vars, final verification |

Minimal frontend changes are included only where a backend change would break a visible feature (e.g., the rules tab is made read-only when `POST /api/rules` is removed). The broader frontend refactor is a separate follow-up.
