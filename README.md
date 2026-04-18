# Unity Catalog AI Descriptions - Databricks App

A Databricks App that generates AI-powered descriptions for Unity Catalog tables and columns using Claude on Foundation Model API (FMAPI), with human-in-the-loop review, per-user UC permissions, and Responsible AI guardrails.

Deployable as a **Databricks Asset Bundle** with git-controlled configuration.

## Problem Statement

Databricks Unity Catalog supports AI-generated descriptions for tables and columns via the UI, but customers need:
1. **Programmatic/automated access** - Apply AI descriptions at scale via API, not one table at a time in the UI
2. **Human-in-the-loop review** - Review AI suggestions before applying, with ability to edit
3. **Responsible AI guardrails** - Enforce organizational rules (PII handling, terminology, compliance)
4. **Audit trail** - Track who approved what, when, and what the AI originally suggested vs what was applied
5. **Per-user permissions** - Browse and apply operations respect each user's own UC grants

## Architecture

```
Browser (SPA)
    |
    |  REST API calls (/api/*)
    v
Databricks Apps Proxy
    |  Injects: x-forwarded-email, x-forwarded-access-token (OBO)
    v
FastAPI (app.py + routes.py)
    |
    |-- get_request_client() -----> OBO token present?
    |                                 YES: WorkspaceClient(user token) --> UC enforces user's perms
    |                                 NO:  WorkspaceClient(SP creds)   --> SP perms (graceful fallback)
    |
    +-- catalog.py (SQL-based) ---> SQL Statement Execution API
    |     SHOW CATALOGS               --> per-user filtered
    |     information_schema.*        --> per-user filtered
    |     COMMENT ON TABLE / ALTER    --> UC enforces user's MODIFY rights
    |
    +-- ai_gen.py ----------------> Foundation Model API (Claude Sonnet)
    |     System prompt + org rules     via OpenAI-compatible client
    |     + session rules override
    |     + reference markdown (spliced into user prompt when enabled)
    |
    +-- reference.py -------------> UC Volume + ai_parse_document
    |     /Volumes/<cat>/<sch>/reference_docs/  (per-schema, opt-in)
    |     PDFs -> ai_parse_document (SQL AI fn, per-page billed)
    |     .md/.txt -> native read via Files API
    |     (path, mtime) in-memory cache; concatenated markdown is
    |     prepended to ai_gen's user prompt as "Reference documentation"
    |
    +-- audit.py -----------------> Delta Table (always via SP)
          Audit log writes              user may not have write access
```

### Why SQL Instead of the UC REST API?

Databricks Apps OBO only supports three `user_api_scopes`: `sql`, `dashboards.genie`, and `files.files`. There is **no scope for the Unity Catalog REST API** (`unity-catalog` is not a valid scope). Calling `w.catalogs.list()` with an OBO token returns `403: required scopes: unity-catalog`.

The workaround: UC's `information_schema` views and `SHOW CATALOGS` are accessible via the SQL Statement Execution API (covered by the `sql` scope), and these views are **automatically permission-filtered** by UC. This gives us per-user access enforcement without needing a UC API scope.

### Identity & Authorization Model

| Component | Identity Used | Why |
|-----------|--------------|-----|
| Browse (catalogs, schemas, tables) | User OBO token | UC filters results by user's grants |
| Apply (COMMENT ON TABLE, ALTER COLUMN) | User OBO token | UC enforces user's MODIFY rights |
| AI Generation (catalog read) | User OBO token | User can only generate for tables they can read |
| Audit log writes | App Service Principal | User may not have write access to the audit catalog |
| `applied_by` in audit entries | User email (from headers) | Tracks which user approved each change |
| Reference-doc parse (`ai_parse_document` SQL) | App Service Principal | SP runs the batched SQL over the Volume on the shared warehouse |
| Reference-doc Volume read | App Service Principal | SP needs `READ VOLUME` on each schema's `reference_docs` Volume |

When OBO is not enabled, all operations gracefully fall back to the SP client.

### Configuration Architecture

Two-layer git-controlled configuration:

| Layer | File | Controls |
|-------|------|----------|
| Infrastructure | `databricks.yml` | Warehouse ID, serving endpoint, app title, OBO scopes (env vars per target) |
| App Behavior | `config.yaml` | Responsible AI rules, centralized audit table path, catalog/schema exclusions |

---

## End-to-End Code Flow

### Step 1: Clone and Configure

```bash
git clone https://github.com/sprakash277/uc-ai-descriptions.git
cd uc-ai-descriptions
```

**Edit `config.yaml`** to customize Responsible AI rules, audit table, and exclusions:
```yaml
responsible_ai_rules: |
  - Never include PII field names or example values in descriptions.
  - Use business-friendly language suitable for a data catalog audience.
  - Do not reference internal system names or implementation details.

audit:
  table: "governance.ai_descriptions.audit_log"

exclusions:
  catalogs:
    - "__databricks_internal"
    - "system"
  schemas:
    - "information_schema"
```

### Step 2: Authenticate with Databricks CLI

```bash
databricks auth login --host https://<your-workspace-url> --profile <your-profile>
databricks auth profiles | grep <your-profile>
```

### Step 3: Enable OBO (Workspace Admin, One-Time)

Per-user UC permissions require On-Behalf-Of (OBO) authorization:

1. Go to your workspace **Settings > Previews**
2. Enable **"On-Behalf-Of User Authorization"** (or "Databricks Apps user token passthrough")

> **Note:** If OBO is not enabled, the app still works — it falls back to the shared SP identity for all operations.

### Step 4: Deploy the Bundle

```bash
# Validate the bundle configuration
databricks bundle validate --profile <your-profile>

# Deploy (uploads files + creates/updates app resource)
databricks bundle deploy --profile <your-profile>

# Deploy the app runtime
databricks apps deploy uc-ai-descriptions \
  --source-code-path /Workspace/Users/<your-email>/.bundle/uc-ai-descriptions/dev/files \
  -p <your-profile>
```

### Step 5: Set OBO Scopes

After the app is created, set the `user_api_scopes` via API:

```bash
databricks api patch /api/2.0/apps/uc-ai-descriptions -p <your-profile> --json '{
  "user_api_scopes": ["sql"]
}'
```

> **Note:** The Terraform provider currently has a bug with `user_api_scopes` in `databricks.yml`, so this must be set via the REST API after app creation.

### Step 6: Grant Service Principal Permissions

The SP still needs base permissions for audit logging and as a fallback when OBO is not available.

Find the SP application ID:
```bash
databricks apps get uc-ai-descriptions -p <your-profile> | grep service_principal_client_id
```

Grant Unity Catalog permissions (run in SQL or notebook):
```sql
-- Replace <sp-id> with the SP's UUID from the command above

-- Permissions on data catalogs/schemas the app will describe
GRANT USE CATALOG ON CATALOG <catalog> TO `<sp-id>`;
GRANT USE SCHEMA ON CATALOG <catalog> TO `<sp-id>`;
GRANT SELECT ON CATALOG <catalog> TO `<sp-id>`;
GRANT MODIFY ON CATALOG <catalog> TO `<sp-id>`;

-- Permissions on the audit table catalog/schema
GRANT USE CATALOG ON CATALOG <audit-catalog> TO `<sp-id>`;
GRANT USE SCHEMA ON SCHEMA <audit-catalog>.<audit-schema> TO `<sp-id>`;
GRANT CREATE SCHEMA ON CATALOG <audit-catalog> TO `<sp-id>`;
GRANT CREATE TABLE ON SCHEMA <audit-catalog>.<audit-schema> TO `<sp-id>`;
GRANT MODIFY ON SCHEMA <audit-catalog>.<audit-schema> TO `<sp-id>`;
```

### Step 6a: Configure Reference Documentation (Optional)

The app can use customer-provided reference docs (data dictionaries, glossaries, runbooks) as context for the AI when generating descriptions. Docs live in a UC Volume per schema so **governance is UC-native** — schema owners control their own Volume, no central corpus.

**Convention:** for `<catalog>.<schema>.<table>`, the app looks at `/Volumes/<catalog>/<schema>/<volume_name>/` where `volume_name` is configured in `config.yaml` (default `reference_docs`). If the Volume doesn't exist or the SP lacks `READ VOLUME`, the app silently skips reference context for that schema.

**Per schema you want to enable:**

```sql
-- Create the Volume (managed volume is simplest)
CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.reference_docs;

-- Grant the app SP read access
GRANT READ VOLUME ON VOLUME <catalog>.<schema>.reference_docs TO `<sp-id>`;
```

Then drop reference docs into the Volume via UI (Catalog → Volumes → Upload) or:

```bash
databricks fs cp ./data_dictionary.pdf dbfs:/Volumes/<cat>/<sch>/reference_docs/ -p <your-profile>
databricks fs cp ./glossary.md         dbfs:/Volumes/<cat>/<sch>/reference_docs/ -p <your-profile>
```

**Supported formats:** `.pdf` (parsed via `ai_parse_document` — includes OCR + table extraction), `.md`, `.txt`. Other formats are skipped with a warning log.

**Cost note:** `ai_parse_document` is per-page billed. PDFs are parsed once per `(path, mtime)` and cached in memory, so subsequent generates on the same schema reuse cached markdown until a file changes. Re-parse happens automatically when the Volume's mtimes change or when a user clicks **Refresh** in the reference docs panel.

**Config:** set `reference.volume_name: "reference_docs"` in `config.yaml` (already set to this default). Leave empty to disable the feature globally.

### Step 7: Verify Deployment

**Important — start the app before the first deploy.** `databricks apps create` usually starts compute automatically, but if it was ever stopped you must start it (via UI → Compute → Apps → `uc-ai-descriptions` → Start, or CLI `databricks apps start uc-ai-descriptions -p <your-profile>`) **before** running `databricks apps deploy` — otherwise deploy fails with a compute-unavailable error. Compute startup takes 2–4 minutes.

```bash
# Check status — should show RUNNING with user_api_scopes: ['sql']
databricks apps get uc-ai-descriptions -p <your-profile>
```

**Also grant `CAN_USE` on the SQL warehouse to the app SP AND every OBO user** (or a group that covers them). Without this, the browse tree and all `information_schema` queries fail with a warehouse-permission error. Example CLI (replace IDs and usernames):

```bash
databricks api patch /api/2.0/permissions/warehouses/<warehouse-id> -p <your-profile> --json '{
  "access_control_list": [
    {"service_principal_name": "<sp-application-id>", "permission_level": "CAN_USE"},
    {"user_name": "user@company.com", "permission_level": "CAN_USE"}
  ]
}'
```

When a user opens the app for the first time, Databricks will prompt them to **authorize** the app (one-time per app version). After authorization, all browse and apply operations run under their own UC identity.

> **Troubleshooting — blank screens or stuck auth state on first load:** on the initial load after deployment, or after toggling OBO ↔ SP authentication modes, the browser may hold stale auth state. Clear the browser cache or open the app in an incognito window to force a fresh handshake.

### Startup Audit Validation

Every time the app starts, it automatically validates and bootstraps the audit table configured in `config.yaml`. The startup check:

1. **Validates** the `audit.table` setting is a valid `catalog.schema.table` name
2. **Checks** the catalog is accessible to the service principal
3. **Creates the schema** if it doesn't exist (requires `CREATE SCHEMA` privilege on the catalog)
4. **Creates the audit table** if it doesn't exist (requires `CREATE TABLE` + `MODIFY` on the schema)

**If audit setup fails**, the app continues running but audit logging will be disabled. Look for `ERROR server.audit:` lines in the app logs:

```
https://uc-ai-descriptions-<workspace-id>.databricksapps.com/logz
# or
databricks apps logs uc-ai-descriptions --tail-lines 50 -p <your-profile>
```

Common error patterns and fixes:

| Error | Fix |
|-------|-----|
| `cannot access catalog '<catalog>'` | `GRANT USE CATALOG ON CATALOG <catalog> TO '<sp-id>';` |
| `schema '...' does not exist and could not be created` | `GRANT CREATE SCHEMA ON CATALOG <catalog> TO '<sp-id>';` |
| `could not create table '...'` | `GRANT CREATE TABLE ON SCHEMA <schema> TO '<sp-id>'; GRANT MODIFY ON SCHEMA <schema> TO '<sp-id>';` |
| `not a valid 3-part name` | Fix `audit.table` in `config.yaml` — must be `catalog.schema.table` |

---

## App UI Walkthrough

Once deployed, navigate to the app URL. The app has five tabs that cover the full workflow.

### Tab 1: Browse & Generate (Single Table)

The landing page shows the **Catalog Browser** on the left. Catalogs shown are filtered by the user's own UC permissions (when OBO is enabled) and by the exclusion list in `config.yaml`.

![Browse & Generate - Landing](screenshots/01-browse-generate.png)

Click a catalog to expand it and see schemas. Click a schema to see tables.

![Browse & Generate - Expanded Tree](screenshots/01-browse-generate-fully-expanded.png)

**Flow:**
1. Select a catalog from the tree (e.g., `samples`)
2. Expand to see schemas (e.g., `nyctaxi`)
3. Click a table (e.g., `trips`) to load its metadata and columns
4. Click **"Generate AI Descriptions"** to invoke Claude via FMAPI
5. Review the AI-generated descriptions for the table and each column
6. **Approve** (apply as-is), **Edit** (modify then apply), or **Reject** each suggestion
7. Use **Regen** to regenerate a single description without affecting others
8. Click **"Apply to Metastore"** to write approved descriptions to Unity Catalog
9. If your UC permissions don't include MODIFY, you'll see a **403 Permission denied** error

**Reference docs bar (when enabled):** a bar above the table editor shows `📚 N reference docs` for the selected schema with an **on/off toggle**. Flip the toggle to compare AI output with vs. without the reference context — the A/B lever. Expand each generated description's **"Informed by N reference source(s)"** disclosure to see which file(s) and snippet(s) contributed. A collapsible **Reference Docs panel** lists each file with its parse status, char count, and a **Refresh** button to force re-parse after you update a doc in the Volume.

### Tab 2: Batch Schema Processing

Generate AI descriptions for **ALL tables in a schema** at once. Select a catalog and schema from the dropdowns, then click **"Generate for All Tables"**.

![Batch Schema](screenshots/02-batch-schema-results.png)

**Flow:**
1. Select a catalog from the dropdown
2. Select a schema from the dropdown
3. Click **"Generate for All Tables"**
4. Results appear in expandable per-table cards
5. **Expand All** to open all cards at once
6. **Regen** individual table or column descriptions in place
7. **Apply** per-column, per-table, or use **Apply All Tables** for bulk application
8. Button states cascade: when all columns are applied, the table button auto-updates; when all tables are applied, the global button auto-updates

The reference-docs bar and toggle work identically here — every table in the batch is generated with the same reference context (or none, when toggled off).

### Tab 3: Responsible AI Rules

Rules are defined in `config.yaml` and injected into the AI system prompt for every generation request. The Rules tab shows which rules are currently active.

![Responsible AI Rules](screenshots/03-responsible-ai-rules.png)

**Session Rules Override:** A collapsible panel on Browse, Batch, and Rules tabs lets you enter custom rules for your current browser session:
- Override rules apply only to your session — not saved, not shared
- An orange **"CUSTOM"** badge indicates when a session override is active
- The Rules tab shows both the active rules and the org defaults for reference
- Reset to org defaults at any time

**How Rules Are Applied:**
- Rules are injected into the AI system prompt before every generation request
- They apply to both single-table and batch schema generation
- Session overrides are passed as `rules_override` in every generate API call
- System rules are version-controlled in `config.yaml` — changes require a redeploy
- For automated notebooks, export a notebook — rules are embedded at export time

### Tab 4: Audit Log (Centralized)

Every approved or edited description is logged to a centralized Delta table (configured in `config.yaml` as `audit.table`). The audit table lives in a dedicated catalog/schema, separate from the described data, to enforce append-only governance.

![Audit Log](screenshots/04-audit-log.png)

**Audit entries track:**
- **Who** approved the description (`applied_by` — the user's email from Databricks Apps headers)
- **When** the description was applied
- **What** the AI originally suggested
- **What** was actually applied (may differ if edited)
- **Whether** it was approved as-is or edited

> **Note:** Audit log writes always use the app service principal (the user may not have write access to the audit catalog). The `applied_by` field records the real user's email extracted from Databricks Apps identity headers.

Click **"Refresh Audit Log"** to load entries from the centralized audit table.

### Tab 5: Export as Databricks Notebook

Download a self-contained Python notebook that can be scheduled as a Databricks Workflow job.

![Export Notebook](screenshots/05-export-notebook.png)

**The exported notebook:**
1. Creates a `_ai_description_reviews` Delta table in the target schema
2. Iterates all tables in the schema, calls `ai_query()` to generate descriptions
3. Inserts AI suggestions into the review table with status `pending`
4. Displays pending suggestions for human review (edit `final_description`, set `status='approved'`)
5. Applies all approved descriptions via `COMMENT ON TABLE` / `ALTER COLUMN COMMENT`
6. Tracks full audit trail: who approved, when, AI vs final description

### Reference Documentation (used across Tabs 1 & 2)

Reference Documentation is an optional feature that lets the AI use your own data dictionaries, glossaries, and runbooks as context when generating descriptions. It's most useful for schemas with cryptic column names, encoded value sets (status codes, channel codes), or domain-specific vocabulary the LLM can't guess from names alone.

**How it works:**

1. For every generate call, the app looks for `/Volumes/<catalog>/<schema>/reference_docs/` (Volume name configurable via `reference.volume_name` in `config.yaml`).
2. Any new or changed files are parsed: PDFs via `ai_parse_document()` (OCR + tables + layout), `.md`/`.txt` via native read. Parsed markdown is cached in memory keyed on `(path, mtime)` — unchanged files are never re-parsed.
3. All cached markdown is concatenated (with `[Source: <filename>]` separators), capped at `reference.per_doc_max_chars` per file and `reference.total_max_chars` total, and prepended to the AI's user prompt.
4. The model's response includes a `sources` list (filename + ~200-char snippet of what was retrieved) — shown in the UI as an **"Informed by N reference source(s)"** disclosure under each generated description.

**Per-session toggle:** the bar above Browse/Batch has an **on/off switch**. When off, the app sends `use_reference: false` with each generate request and skips retrieval entirely — same table, same prompt, no reference context. Useful for showing the before/after lift of reference docs on the same schema.

**Reference Docs panel:** a collapsible panel in the app lists every file the app sees in the current schema's Volume, its parse status (✅ parsed · ⚠️ parsed with warnings · ❌ failed), the extracted char count, and the Volume path. The **Refresh** button calls `POST /api/reference/refresh` to force re-parse — use this when you've just updated a doc and want the next generate to reflect the change immediately rather than waiting for the next natural cache check.

**Setup:** see [Step 6a](#step-6a-configure-reference-documentation-optional) above.

---

## Configuration

### `databricks.yml` — Infrastructure (per-environment)

| Variable | Default | Description |
|----------|---------|-------------|
| `warehouse_id` | `""` (auto-detect) | SQL warehouse ID; empty = auto-select running serverless warehouse |
| `serving_endpoint` | `databricks-claude-sonnet-4-6` | Foundation Model API endpoint name |
| `app_title` | `Unity Catalog AI Descriptions` | Display title in the app header |
| `service_principal_id` | `""` (auto-generate) | SP application ID (UUID) to run the app as; empty = Databricks creates a dedicated SP automatically |

Override per target:
```yaml
targets:
  prod:
    variables:
      serving_endpoint: "databricks-claude-sonnet-4-6"
      warehouse_id: "abc123def456"
```

### `config.yaml` — App Behavior (git-controlled)

| Setting | Default | Description |
|---------|---------|-------------|
| `responsible_ai_rules` | (see file) | Rules injected into every AI generation prompt |
| `audit.table` | `governance.ai_descriptions.audit_log` | Centralized audit table (full three-part name) |
| `exclusions.catalogs` | `["__databricks_internal", "system"]` | Catalogs hidden from the browse tree |
| `exclusions.schemas` | `["information_schema"]` | Schemas hidden from the browse tree |
| `reference.volume_name` | `"reference_docs"` | Per-schema Volume name the app reads reference docs from. Empty string disables the feature. |
| `reference.per_doc_max_chars` | `8000` | Max chars from each parsed reference doc included in the prompt (head is kept; tail is clipped) |
| `reference.total_max_chars` | `40000` | Max total chars of reference markdown per generate request; exceeded size is logged as a warning |

---

## Project Structure

```
uc-ai-descriptions/
  databricks.yml        # DAB bundle definition (infrastructure config)
  config.yaml           # App behavior config (rules, exclusions, audit)
  app.py                # FastAPI entry point with lifespan startup validation
  app.yaml              # Databricks App runtime command config
  requirements.txt      # Python dependencies
  server/
    __init__.py
    config.py           # Config loader (config.yaml + env vars) + auth (SP + OBO clients)
    identity.py         # Per-request user identity extraction from Databricks Apps headers
    warehouse.py        # Centralized warehouse resolution with caching
    sql_utils.py        # SQL safety (identifier validation, quoting, comment escaping)
    catalog.py          # Unity Catalog operations via SQL (browse + apply comments)
    ai_gen.py           # AI description generation via FMAPI + notebook export
    reference.py        # Per-schema reference docs: UC Volume + ai_parse_document + (path,mtime) cache
    audit.py            # Centralized Delta table audit logging with startup validation
    routes.py           # All API endpoints with per-user client injection
  static/
    index.html          # Single-page frontend (HTML/CSS/JS)
  tests/
    conftest.py         # Shared fixtures + Databricks SDK stubs
    test_config.py      # Config loading tests
    test_identity.py    # Identity extraction tests
    test_sql_safety.py  # SQL escaping + validation tests
    test_warehouse.py   # Warehouse resolution tests
    test_user_permissions.py  # OBO client selection tests
  docs/
    plan-rules-editing.md      # Implementation plan for persistent rules editing
    plan-user-permissions.md   # Implementation plan for per-user UC permissions
  screenshots/          # App screenshots for documentation
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | Health check |
| GET | `/api/settings` | — | Current effective configuration |
| GET | `/api/warehouses` | SP | List available SQL warehouses with state |
| GET | `/api/catalogs` | OBO/SP | List catalogs (per-user filtered when OBO enabled) |
| GET | `/api/schemas/{catalog}` | OBO/SP | List schemas in a catalog |
| GET | `/api/tables/{catalog}/{schema}` | OBO/SP | List tables in a schema |
| GET | `/api/table/{full_name}` | OBO/SP | Get table details + columns |
| POST | `/api/generate` | OBO/SP | Generate AI descriptions for a single table. Request body accepts `use_reference: bool` (default `true`) to skip reference-doc retrieval for this call. |
| POST | `/api/generate/item` | OBO/SP | Regenerate a single table or column description. Also honors `use_reference`. |
| POST | `/api/generate/batch` | OBO/SP | Generate AI descriptions for all tables in a schema. Reference docs are fetched once per batch and reused across every table's LLM call. |
| GET | `/api/reference/status` | SP | Status of the reference-docs Volume for a given schema: file list, parse status per file (`parsed` / `parsed_with_warnings` / `failed` / `pending`), char counts, Volume path. Query params: `catalog`, `schema`. |
| POST | `/api/reference/refresh` | SP | Force re-parse of the schema's reference Volume, bypassing the `(path, mtime)` cache. Body: `{catalog, schema_name, force?}`. Use after updating a doc in the Volume to make the next generate reflect the change immediately. |
| POST | `/api/apply/table` | OBO/SP | Apply a table comment (UC enforces MODIFY) |
| POST | `/api/apply/column` | OBO/SP | Apply a column comment (UC enforces MODIFY) |
| POST | `/api/apply/batch` | OBO/SP + SP(audit) | Apply multiple comments with audit logging |
| GET | `/api/rules` | — | Get Responsible AI rules from config.yaml |
| POST | `/api/export-notebook` | — | Download automation notebook |
| GET | `/api/audit` | SP | Query centralized audit log entries |

**Auth column:** OBO/SP = uses user's OBO token when available, falls back to SP. SP = always uses the app service principal.

## Local Development

```bash
export DATABRICKS_PROFILE=<your-profile>
# Optional: set a dev user identity for audit logging
export DEV_USER_EMAIL=your-email@company.com
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
# Open http://localhost:8000
```

When running locally, the app uses the Databricks CLI profile for authentication. OBO headers are not present, so identity falls back to `DEV_USER_EMAIL` or `"dev_user"`.

## Updating the App

After code changes:
```bash
databricks bundle deploy --profile <your-profile>
databricks apps deploy uc-ai-descriptions \
  --source-code-path /Workspace/Users/<your-email>/.bundle/uc-ai-descriptions/dev/files \
  -p <your-profile>
```

## Viewing App Logs

```
https://uc-ai-descriptions-<workspace-id>.databricksapps.com/logz
```

Or via CLI:
```bash
databricks apps logs uc-ai-descriptions --tail-lines 50 -p <your-profile>
```
