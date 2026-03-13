# Unity Catalog AI Descriptions - Databricks App

A Databricks App that generates AI-powered descriptions for Unity Catalog tables and columns using Claude on Foundation Model API (FMAPI), with human-in-the-loop review and Responsible AI guardrails.

**Live App:** `https://uc-ai-descriptions-<workspace-id>.azure.databricksapps.com`

## Problem Statement

Databricks Unity Catalog supports AI-generated descriptions for tables and columns via the UI, but customers need:
1. **Programmatic/automated access** - Apply AI descriptions at scale via API, not one table at a time in the UI
2. **Human-in-the-loop review** - Review AI suggestions before applying, with ability to edit
3. **Responsible AI guardrails** - Enforce organizational rules (PII handling, terminology, compliance)
4. **Audit trail** - Track who approved what, when, and what the AI originally suggested vs what was applied

## Architecture

```
Browser  -->  Databricks App (FastAPI)  -->  Foundation Model API (Claude Sonnet)
                    |                              |
                    v                              v
             Unity Catalog API            AI Description Generation
             (browse / apply)             (single + batch)
                    |
                    v
             Delta Table (Audit Log)
```

## Features

### 1. Browse & Generate (Single Table)
Browse the Unity Catalog tree (catalogs > schemas > tables), view table metadata and columns, then generate AI descriptions with one click. Review each suggestion and approve, edit, or reject before applying to the metastore.

![Browse & Generate](screenshots/01-browse-generate.png)

### 2. Batch Schema Processing
Generate AI descriptions for ALL tables in a schema at once. Results are expandable per-table with individual approve/apply controls for both table-level and column-level descriptions.

![Batch Schema Results](screenshots/02-batch-schema-results.png)

![Batch Expanded](screenshots/02b-batch-expanded.png)

### 3. Responsible AI Rules
Define custom rules that are injected into the AI system prompt for every generation request. Use this to enforce organizational standards like:
- Never include PII field names or examples
- Use company-specific terminology
- Include data sensitivity classification hints
- Ensure descriptions are suitable for external data sharing catalogs

![Responsible AI Rules](screenshots/03-responsible-ai-rules.png)

### 4. Audit Log
Every approved or edited description is logged to a Delta table (`_ai_description_audit`) with full provenance:
- Who approved it
- When it was applied
- What the AI originally suggested
- What was actually applied (may differ if edited)
- Whether it was approved as-is or edited

![Audit Log](screenshots/04-audit-log.png)

### 5. Export as Databricks Notebook
Download a self-contained Python notebook that can be scheduled as a Databricks Workflow job. The notebook:
1. Creates a `_ai_description_reviews` Delta table
2. Uses `ai_query()` to generate descriptions for all tables in a schema
3. Inserts suggestions as "pending" for human review
4. Applies approved descriptions via `COMMENT ON TABLE` / `ALTER COLUMN COMMENT`
5. Tracks full audit trail

![Export Notebook](screenshots/05-export-notebook.png)

## Deployment Steps

### Prerequisites
- Databricks workspace with Unity Catalog enabled
- Databricks CLI v0.229.0+ authenticated (`databricks auth login --host <workspace-url> --profile <profile>`)
- A SQL warehouse (serverless recommended)
- Access to a Foundation Model serving endpoint (e.g., `databricks-claude-sonnet-4-6`)

### Step 1: Clone the Repo and Navigate to the App
```bash
git clone https://github.com/sprakash277/uc-ai-descriptions.git
cd uc-ai-descriptions
```

### Step 2: Authenticate with Databricks CLI
```bash
# Login to your workspace (opens browser for SSO)
databricks auth login --host https://<your-workspace>.azuredatabricks.net --profile <your-profile>

# Verify authentication
databricks auth profiles | grep <your-profile>
```

### Step 3: Create the Databricks App
```bash
databricks apps create uc-ai-descriptions \
  --description "Unity Catalog AI Descriptions with human-in-the-loop review" \
  -p <your-profile>
```

This creates the app and provisions a service principal (SP) for it. Note the SP's application ID from the output — you'll need it for permissions.

### Step 4: Upload Source Code to Workspace
```bash
# Set variables for convenience
PROFILE=<your-profile>
EMAIL=<your-email>
WS_PATH="/Workspace/Users/${EMAIL}/uc-ai-descriptions"

# Create workspace directories
databricks workspace mkdirs ${WS_PATH} -p ${PROFILE}
databricks workspace mkdirs ${WS_PATH}/server -p ${PROFILE}
databricks workspace mkdirs ${WS_PATH}/static -p ${PROFILE}

# Upload app entry point and config files
databricks workspace import ${WS_PATH}/app.py \
  --file app.py --language PYTHON --overwrite -p ${PROFILE}

databricks workspace import ${WS_PATH}/app.yaml \
  --file app.yaml --format AUTO --overwrite -p ${PROFILE}

databricks workspace import ${WS_PATH}/requirements.txt \
  --file requirements.txt --format AUTO --overwrite -p ${PROFILE}

# Upload all server module files
for f in server/*.py; do
  databricks workspace import "${WS_PATH}/${f}" \
    --file "${f}" --language PYTHON --overwrite -p ${PROFILE}
done

# Upload the frontend
databricks workspace import ${WS_PATH}/static/index.html \
  --file static/index.html --format AUTO --overwrite -p ${PROFILE}
```

**Verify the upload:**
```bash
databricks workspace list ${WS_PATH} -p ${PROFILE}
databricks workspace list ${WS_PATH}/server -p ${PROFILE}
```

You should see: `app.py`, `app.yaml`, `requirements.txt`, `server/` (with `__init__.py`, `config.py`, `catalog.py`, `ai_gen.py`, `audit.py`, `routes.py`), and `static/` (with `index.html`).

### Step 5: Deploy the App
```bash
databricks apps deploy uc-ai-descriptions \
  --source-code-path ${WS_PATH} \
  -p ${PROFILE}
```

Wait for the deploy to complete. You should see `"state": "SUCCEEDED"` in the output.

### Step 6: Grant Service Principal Permissions

The app runs as a service principal. Find its application ID:
```bash
databricks apps get uc-ai-descriptions -p ${PROFILE} | grep -A5 service_principal
```

**a) Unity Catalog permissions** (run in a SQL notebook or via Databricks SQL):
```sql
-- Replace <sp-application-id> with the SP's UUID and <catalog>.<schema> with your target
GRANT USE CATALOG ON CATALOG <catalog_name> TO `<sp-application-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog_name>.<schema_name> TO `<sp-application-id>`;
GRANT SELECT ON SCHEMA <catalog_name>.<schema_name> TO `<sp-application-id>`;

-- Grant ability to set comments (COMMENT ON TABLE / ALTER COLUMN COMMENT)
GRANT MODIFY ON SCHEMA <catalog_name>.<schema_name> TO `<sp-application-id>`;

-- Grant audit table creation and write access
GRANT CREATE TABLE ON SCHEMA <catalog_name>.<schema_name> TO `<sp-application-id>`;
```

**b) SQL Warehouse access:**
```bash
# Get your warehouse ID
databricks warehouses list -p ${PROFILE}

# Grant CAN_USE permission to the SP
curl -X PATCH "https://<workspace>/api/2.0/permissions/sql/warehouses/<warehouse-id>" \
  -H "Authorization: Bearer $(databricks auth token -p ${PROFILE} | jq -r .access_token)" \
  -H "Content-Type: application/json" \
  -d '{"access_control_list": [{"service_principal_name": "<sp-application-id>", "permission_level": "CAN_USE"}]}'
```

**c) Foundation Model serving endpoint** — add as an app resource so the SP gets auto-granted access:
```bash
curl -X PATCH "https://<workspace>/api/2.0/apps/uc-ai-descriptions" \
  -H "Authorization: Bearer $(databricks auth token -p ${PROFILE} | jq -r .access_token)" \
  -H "Content-Type: application/json" \
  -d '{"resources": [{"name": "serving-endpoint", "serving_endpoint": {"name": "databricks-claude-sonnet-4-6", "permission": "CAN_QUERY"}}]}'
```

After adding resources, redeploy to pick up the changes:
```bash
databricks apps deploy uc-ai-descriptions \
  --source-code-path ${WS_PATH} \
  -p ${PROFILE}
```

### Step 7: Create the Audit Table (Optional)

The app will try to auto-create the audit table, but if the SP lacks CREATE TABLE permission, create it manually:
```sql
CREATE TABLE IF NOT EXISTS <catalog>.<schema>._ai_description_audit (
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
COMMENT 'Audit log for AI-generated description changes';

-- Grant the SP access
GRANT ALL PRIVILEGES ON TABLE <catalog>.<schema>._ai_description_audit TO `<sp-application-id>`;
```

> **Note:** Update the `AUDIT_TABLE` variable in `server/audit.py` to match your catalog and schema.

### Step 8: Verify Deployment
```bash
# Get app URL
databricks apps get uc-ai-descriptions -p ${PROFILE}
```

Navigate to the app URL in your browser. You should see the 5-tab interface. Test the workflow:
1. **Browse & Generate** — Select a catalog > schema > table, click "Generate AI Descriptions"
2. **Approve/Edit** — Review suggestions, approve or edit, click "Apply to Metastore"
3. **Audit Log** — Click "Refresh Audit Log" to see the entries
4. **Batch Schema** — Select a catalog and schema, click "Generate for All Tables"
5. **Export Notebook** — Download a notebook for scheduled automation

### Updating the App

After making code changes, re-upload and redeploy:
```bash
# Re-upload changed files (example: updating the frontend)
databricks workspace import ${WS_PATH}/static/index.html \
  --file static/index.html --format AUTO --overwrite -p ${PROFILE}

# Redeploy
databricks apps deploy uc-ai-descriptions \
  --source-code-path ${WS_PATH} \
  -p ${PROFILE}
```

### Viewing App Logs

Access real-time logs by appending `/logz` to your app URL:
```
https://uc-ai-descriptions-<workspace-id>.azure.databricksapps.com/logz
```

## Project Structure

```
uc-ai-descriptions/
  app.py              # FastAPI entry point, serves static frontend + API
  app.yaml            # Databricks App configuration
  requirements.txt    # Python dependencies
  server/
    __init__.py       # Package init
    config.py         # Dual-mode auth (Databricks App vs local dev)
    catalog.py        # Unity Catalog operations (browse, apply comments)
    ai_gen.py         # AI description generation via FMAPI + notebook export
    audit.py          # Delta table audit logging
    routes.py         # All API endpoints
  static/
    index.html        # Single-page frontend (HTML/CSS/JS)
  screenshots/        # App screenshots for documentation
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/catalogs` | List all catalogs |
| GET | `/api/schemas/{catalog}` | List schemas in a catalog |
| GET | `/api/tables/{catalog}/{schema}` | List tables in a schema |
| GET | `/api/table/{full_name}` | Get table details + columns |
| POST | `/api/generate` | Generate AI descriptions for a single table |
| POST | `/api/generate/batch` | Generate AI descriptions for all tables in a schema |
| POST | `/api/apply/table` | Apply a table comment |
| POST | `/api/apply/column` | Apply a column comment |
| POST | `/api/apply/batch` | Apply multiple comments with audit logging |
| GET | `/api/rules` | Get current Responsible AI rules |
| POST | `/api/rules` | Set Responsible AI rules |
| POST | `/api/export-notebook` | Download automation notebook |
| GET | `/api/audit` | Query audit log entries |
| GET | `/api/health` | Health check |

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `SERVING_ENDPOINT` | `databricks-claude-sonnet-4-6` | Foundation Model endpoint |
| `DATABRICKS_HOST` | auto-detected | Workspace URL |
| `DATABRICKS_PROFILE` | `DEFAULT` | CLI profile (local dev only) |

## Local Development

```bash
# Set your Databricks CLI profile
export DATABRICKS_PROFILE=<your-profile>

# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn app:app --reload --port 8000

# Open http://localhost:8000
```
