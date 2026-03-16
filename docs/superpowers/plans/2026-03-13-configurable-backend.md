# Configurable Backend Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all hardcoded customer-specific values and make the app configurable via git-controlled `databricks.yml` (infra) and `config.yaml` (app behavior), deployable as a Databricks Asset Bundle.

**Architecture:** Two-layer config — bundle variables flow as env vars for infra settings (warehouse, model endpoint, title), while a `config.yaml` file controls app behavior (AI rules, audit table name, exclusions). A new `warehouse.py` module centralizes warehouse resolution. All existing modules (`audit.py`, `ai_gen.py`, `catalog.py`) are parameterized to read from the config layer instead of hardcoded values.

**Tech Stack:** Python 3.10+, FastAPI, Databricks SDK, PyYAML, Databricks Asset Bundles

**Spec:** `docs/superpowers/specs/2026-03-13-configurable-backend-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `databricks.yml` | Create | DAB bundle definition, variables, app resource |
| `config.yaml` | Create | App behavior config (AI rules, audit table name, exclusions) |
| `server/config.py` | Modify | Central config loader — env vars + YAML + auth functions |
| `server/warehouse.py` | Create | Warehouse ID resolution with caching |
| `server/audit.py` | Modify | Parameterized audit (catalog/schema args, no hardcoded table) |
| `server/ai_gen.py` | Modify | Read model + rules from config, no in-memory globals |
| `server/catalog.py` | Modify | Configurable exclusions, use shared warehouse module |
| `server/routes.py` | Modify | Update callers, add settings/warehouses endpoints |
| `app.py` | Modify | Use configurable title |
| `requirements.txt` | Modify | Add pyyaml |
| `README.md` | Modify | DAB deployment instructions, configuration docs |
| `tests/__init__.py` | Create | Make tests a package (empty file) |
| `tests/conftest.py` | Create | Shared pytest fixtures (warehouse cache reset, env cleanup) |
| `tests/test_config.py` | Create | Unit tests for config loading |
| `tests/test_warehouse.py` | Create | Unit tests for warehouse resolution |
| `tests/test_sql_safety.py` | Create | Unit tests for SQL escaping/validation |

---

## Chunk 1: Infrastructure and Config Foundation

### Task 1: Add `databricks.yml`

**Files:**
- Create: `databricks.yml`

- [ ] **Step 1: Create the bundle definition**

```yaml
bundle:
  name: uc-ai-descriptions

variables:
  warehouse_id:
    description: "SQL warehouse ID for executing statements (leave empty for auto-detect)"
    default: ""
  serving_endpoint:
    description: "Foundation Model API serving endpoint name"
    default: "databricks-claude-sonnet-4-6"
  app_title:
    description: "Display title for the app"
    default: "Unity Catalog AI Descriptions"

resources:
  apps:
    uc_ai_descriptions:  # underscore required — DAB resource keys must be valid identifiers
      name: uc-ai-descriptions  # the actual app name uses hyphens
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

- [ ] **Step 2: Validate the bundle**

Run: `databricks bundle validate`
Expected: No errors (warnings about unset workspace host are OK since we're not deploying yet)

- [ ] **Step 3: Commit**

```bash
git add databricks.yml
git commit -m "feat: add databricks.yml for DAB deployment"
```

---

### Task 2: Add `config.yaml`

**Files:**
- Create: `config.yaml`

- [ ] **Step 1: Create the app config file**

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

- [ ] **Step 2: Verify YAML parses correctly**

Run: `python -c "import yaml; print(yaml.safe_load(open('config.yaml')))"`
Expected: Dict with keys `responsible_ai_rules`, `audit`, `exclusions` printed to stdout. If `yaml` is not installed yet, run `pip install pyyaml` first.

- [ ] **Step 3: Commit**

```bash
git add config.yaml
git commit -m "feat: add config.yaml for app behavior settings"
```

---

### Task 3: Expand `server/config.py` + add pyyaml dependency

**Files:**
- Modify: `server/config.py` (full rewrite, preserve lines 1-31 auth functions)
- Modify: `requirements.txt` (add pyyaml)
- Create: `tests/test_config.py`

- [ ] **Step 1: Add pyyaml to requirements.txt**

Add `pyyaml>=6.0` as a new line at the end of `requirements.txt`.

- [ ] **Step 2: Install updated dependencies**

Run: `pip install -r requirements.txt`
Expected: pyyaml installs successfully

- [ ] **Step 3: Create test infrastructure**

Create `tests/__init__.py` (empty file) and `tests/conftest.py`:

```python
"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def reset_warehouse_cache():
    """Reset the warehouse cache before each test to prevent cross-test pollution."""
    yield
    try:
        from server.warehouse import reset_cache
        reset_cache()
    except ImportError:
        pass  # warehouse module not yet created
```

- [ ] **Step 4: Write the config loading test**

Create `tests/test_config.py`:

```python
"""Tests for config loading logic."""

import os
import tempfile
import pytest
import yaml


def _write_config(tmp_dir: str, data: dict) -> str:
    path = os.path.join(tmp_dir, "config.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


class TestAppConfig:
    """Test the AppConfig dataclass and loading logic."""

    def test_defaults_when_no_file(self):
        """Config should have sensible defaults if config.yaml is missing."""
        from server.config import AppConfig, load_config
        cfg = load_config("/nonexistent/path/config.yaml")
        assert cfg.serving_endpoint == "databricks-claude-sonnet-4-6"
        assert cfg.warehouse_id is None
        assert cfg.app_title == "Unity Catalog AI Descriptions"
        assert cfg.responsible_ai_rules == ""
        assert cfg.audit_table == "governance.ai_descriptions.audit_log"
        assert "__databricks_internal" in cfg.excluded_catalogs
        assert "information_schema" in cfg.excluded_schemas

    def test_loads_from_yaml(self):
        """Config should load values from a YAML file."""
        from server.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            data = {
                "responsible_ai_rules": "No PII allowed.",
                "audit": {"table": "_custom_audit"},
                "exclusions": {
                    "catalogs": ["hidden_catalog"],
                    "schemas": ["hidden_schema"],
                },
            }
            path = _write_config(tmp, data)
            cfg = load_config(path)
            assert cfg.responsible_ai_rules == "No PII allowed."
            assert cfg.audit_table == "_custom_audit"
            assert cfg.excluded_catalogs == ["hidden_catalog"]
            assert cfg.excluded_schemas == ["hidden_schema"]

    def test_env_vars_override(self):
        """Env vars should set infra config fields."""
        from server.config import load_config
        os.environ["SERVING_ENDPOINT"] = "my-custom-endpoint"
        os.environ["WAREHOUSE_ID"] = "abc123"
        os.environ["APP_TITLE"] = "My Custom Title"
        try:
            cfg = load_config("/nonexistent/path/config.yaml")
            assert cfg.serving_endpoint == "my-custom-endpoint"
            assert cfg.warehouse_id == "abc123"
            assert cfg.app_title == "My Custom Title"
        finally:
            del os.environ["SERVING_ENDPOINT"]
            del os.environ["WAREHOUSE_ID"]
            del os.environ["APP_TITLE"]

    def test_empty_warehouse_id_becomes_none(self):
        """Empty string WAREHOUSE_ID should resolve to None (auto-detect)."""
        from server.config import load_config
        os.environ["WAREHOUSE_ID"] = ""
        try:
            cfg = load_config("/nonexistent/path/config.yaml")
            assert cfg.warehouse_id is None
        finally:
            del os.environ["WAREHOUSE_ID"]
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `AppConfig` and `load_config` don't exist yet

- [ ] **Step 6: Implement the config module**

Rewrite `server/config.py` — keep all existing auth functions, add config loading above them:

```python
"""Configuration and authentication for Databricks Apps."""

import logging
import os
from dataclasses import dataclass, field

import yaml
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))


# ── App Configuration ────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """Central configuration loaded from env vars + config.yaml."""

    # Infra settings (from env vars / databricks.yml)
    serving_endpoint: str = "databricks-claude-sonnet-4-6"
    warehouse_id: str | None = None
    app_title: str = "Unity Catalog AI Descriptions"

    # App behavior settings (from config.yaml)
    responsible_ai_rules: str = ""
    audit_table: str = "governance.ai_descriptions.audit_log"
    excluded_catalogs: list[str] = field(
        default_factory=lambda: ["__databricks_internal", "system"]
    )
    excluded_schemas: list[str] = field(
        default_factory=lambda: ["information_schema"]
    )


def load_config(config_path: str = "") -> AppConfig:
    """Load config from YAML file + environment variables.

    Args:
        config_path: Path to config.yaml. If empty, looks for config.yaml
                     next to the app root (parent of server/).
    """
    if not config_path:
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(app_root, "config.yaml")

    cfg = AppConfig()

    # Load YAML if it exists
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            if "responsible_ai_rules" in data:
                cfg.responsible_ai_rules = str(data["responsible_ai_rules"]).strip()
            if "audit" in data and "table" in data["audit"]:
                cfg.audit_table = data["audit"]["table"]
            if "exclusions" in data:
                exc = data["exclusions"]
                if "catalogs" in exc:
                    cfg.excluded_catalogs = exc["catalogs"]
                if "schemas" in exc:
                    cfg.excluded_schemas = exc["schemas"]
        except Exception as e:
            logger.warning("Failed to load config.yaml: %s — using defaults", e)

    # Env vars override infra settings
    if os.environ.get("SERVING_ENDPOINT"):
        cfg.serving_endpoint = os.environ["SERVING_ENDPOINT"]
    wh = os.environ.get("WAREHOUSE_ID", "")
    cfg.warehouse_id = wh if wh else None
    if os.environ.get("APP_TITLE"):
        cfg.app_title = os.environ["APP_TITLE"]

    return cfg


# Module-level config instance, loaded once at import time
app_config = load_config()


# ── Authentication (unchanged) ───────────────────────────────────────────

def get_workspace_client() -> WorkspaceClient:
    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE", "DEFAULT")
    return WorkspaceClient(profile=profile)


def get_workspace_host() -> str:
    if IS_DATABRICKS_APP:
        host = os.environ.get("DATABRICKS_HOST", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    client = get_workspace_client()
    return client.config.host


def get_oauth_token() -> str:
    client = get_workspace_client()
    header = client.config.authenticate()
    if header and "Authorization" in header:
        return header["Authorization"].replace("Bearer ", "")
    raise RuntimeError("Could not obtain OAuth token")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: All 4 tests PASS

- [ ] **Step 8: Commit**

```bash
git add requirements.txt server/config.py tests/__init__.py tests/conftest.py tests/test_config.py
git commit -m "feat: central config loader with env vars + config.yaml support"
```

---

### Task 4: Extract `server/warehouse.py`

**Files:**
- Create: `server/warehouse.py`
- Create: `tests/test_warehouse.py`
- Modify: `server/catalog.py:71-111` (remove inline warehouse lookup)
- Modify: `server/audit.py:15-20` (remove `_get_warehouse_id`)

- [ ] **Step 1: Write the warehouse resolution test**

Create `tests/test_warehouse.py`:

```python
"""Tests for warehouse resolution logic.

Note: The autouse `reset_warehouse_cache` fixture in conftest.py clears
the warehouse cache between tests. Tests that call resolve_warehouse_id()
must still patch app_config to control the config-vs-autodetect path.
"""

from unittest.mock import MagicMock, patch
import pytest


class TestWarehouseResolution:

    def test_configured_id_used_directly(self):
        """When warehouse_id is set in config, use it without API calls."""
        from server.warehouse import resolve_warehouse_id, reset_cache
        reset_cache()  # ensure no stale cache
        with patch("server.warehouse.app_config") as mock_cfg:
            mock_cfg.warehouse_id = "configured-123"
            result = resolve_warehouse_id()
            assert result == "configured-123"

    def test_auto_detect_prefers_running(self):
        """Auto-detect should prefer a running warehouse."""
        from server.warehouse import _auto_detect_warehouse_id

        running_wh = MagicMock()
        running_wh.id = "running-456"
        running_wh.state = MagicMock()
        running_wh.state.value = "RUNNING"
        running_wh.warehouse_type = MagicMock()
        running_wh.warehouse_type.value = "PRO"

        stopped_wh = MagicMock()
        stopped_wh.id = "stopped-789"
        stopped_wh.state = MagicMock()
        stopped_wh.state.value = "STOPPED"
        stopped_wh.warehouse_type = MagicMock()
        stopped_wh.warehouse_type.value = "PRO"

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = [stopped_wh, running_wh]

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            result = _auto_detect_warehouse_id()
            assert result == "running-456"

    def test_auto_detect_falls_back_to_first(self):
        """When no warehouse is running, fall back to first available."""
        from server.warehouse import _auto_detect_warehouse_id

        stopped_wh = MagicMock()
        stopped_wh.id = "stopped-111"
        stopped_wh.state = MagicMock()
        stopped_wh.state.value = "STOPPED"
        stopped_wh.warehouse_type = MagicMock()
        stopped_wh.warehouse_type.value = "PRO"

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = [stopped_wh]

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            result = _auto_detect_warehouse_id()
            assert result == "stopped-111"

    def test_auto_detect_raises_when_none(self):
        """Should raise when no warehouses exist."""
        from server.warehouse import _auto_detect_warehouse_id

        mock_client = MagicMock()
        mock_client.warehouses.list.return_value = []

        with patch("server.warehouse.get_workspace_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="No SQL warehouse available"):
                _auto_detect_warehouse_id()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_warehouse.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement `server/warehouse.py`**

Create `server/warehouse.py`:

```python
"""SQL warehouse resolution with caching."""

import logging

from .config import app_config, get_workspace_client

logger = logging.getLogger(__name__)

_cached_warehouse_id: str | None = None


def _auto_detect_warehouse_id() -> str:
    """Find a warehouse, preferring running ones."""
    w = get_workspace_client()
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")

    # Prefer running warehouses
    for wh in warehouses:
        if wh.state and wh.state.value == "RUNNING":
            logger.info("Auto-detected running warehouse: %s", wh.id)
            return wh.id

    # Fall back to first available
    logger.info("No running warehouse found, using first available: %s", warehouses[0].id)
    return warehouses[0].id


def resolve_warehouse_id() -> str:
    """Get the warehouse ID — from config or auto-detect. Cached after first call."""
    global _cached_warehouse_id

    if _cached_warehouse_id is not None:
        return _cached_warehouse_id

    if app_config.warehouse_id:
        _cached_warehouse_id = app_config.warehouse_id
        logger.info("Using configured warehouse: %s", _cached_warehouse_id)
    else:
        _cached_warehouse_id = _auto_detect_warehouse_id()

    return _cached_warehouse_id


def reset_cache() -> None:
    """Clear the cached warehouse ID. Useful for testing or settings changes."""
    global _cached_warehouse_id
    _cached_warehouse_id = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_warehouse.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Update `server/audit.py` — replace `_get_warehouse_id` with import**

In `server/audit.py`, make all edits in a single pass:
- Add import: `from .warehouse import resolve_warehouse_id`
- Remove the `_get_warehouse_id` function (lines 15-20)
- Replace all 3 calls to `_get_warehouse_id()` with `resolve_warehouse_id()`

Use find-and-replace: `_get_warehouse_id()` → `resolve_warehouse_id()` across the file, then delete the function definition. The three call sites are in `ensure_audit_table()`, `log_action()`, and `get_audit_log()`.

- [ ] **Step 6: Update `server/catalog.py` — replace inline warehouse lookup**

In `server/catalog.py`:
- Add import: `from .warehouse import resolve_warehouse_id`
- In `apply_table_comment()` (lines 76-80): replace the 4-line warehouse lookup block with `warehouse_id = resolve_warehouse_id()`
- In `apply_column_comment()` (lines 98-101): same replacement

Before (lines 76-80):
```python
    # Find a SQL warehouse
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")
    warehouse_id = warehouses[0].id
```

After:
```python
    warehouse_id = resolve_warehouse_id()
```

Same pattern for lines 98-101.

- [ ] **Step 7: Verify app still imports cleanly**

Run: `python -c "from server import routes; print('OK')"`
Expected: Prints `OK` with no import errors

- [ ] **Step 8: Commit**

```bash
git add server/warehouse.py server/audit.py server/catalog.py tests/test_warehouse.py
git commit -m "refactor: extract warehouse.py, centralize warehouse resolution"
```

---

## Chunk 2: Parameterize Core Modules

### Task 5: Parameterize `audit.py` (centralized audit table)

**Files:**
- Modify: `server/audit.py` (remove hardcoded `AUDIT_TABLE`, read from config)

The audit table stays centralized (one table, configurable location) rather than per-schema. This enforces governance — the app's SP gets INSERT-only access on a dedicated schema, and data owners can't modify their own audit trail. It also keeps the `GET /api/audit` endpoint and frontend unchanged.

- [ ] **Step 1: Update `audit.py` — remove hardcoded table, use config**

In `server/audit.py`:

Remove the hardcoded constant (line 12):
```python
AUDIT_TABLE = "cat_nsp_z5zw62.retail_demo._ai_description_audit"
```

Update the imports — keep the warehouse import from Task 4 and add `app_config`:
```python
from .config import get_workspace_client, app_config
from .warehouse import resolve_warehouse_id
```

Replace all references to `AUDIT_TABLE` with `app_config.audit_table`. The function signatures stay the same — no new parameters needed since we're reading from config, not routing per-schema.

In `ensure_audit_table()`:
```python
    sql = f"""
    CREATE TABLE IF NOT EXISTS {app_config.audit_table} (
    ...
    """
```

In `log_action()`:
```python
    sql = f"""
    INSERT INTO {app_config.audit_table}
    VALUES (...)
    """
```

In `get_audit_log()`:
```python
    sql = f"""
    SELECT * FROM {app_config.audit_table}
    ...
    """
```

- [ ] **Step 2: Verify the `GET /api/audit` endpoint still works unchanged**

The `routes.py` audit endpoint (`GET /api/audit`) calls `audit.ensure_audit_table()` and `audit.get_audit_log()` — neither signature has changed, so `routes.py` needs no modifications. The frontend calls `GET /api/audit` with no params and continues to work.

Run: `python -c "from server import routes; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add server/audit.py
git commit -m "feat: centralized configurable audit table, replace hardcoded path"
```

---

### Task 6: Parameterize `ai_gen.py` + update routes + update frontend rules tab

**Files:**
- Modify: `server/ai_gen.py:25-43, 67, 115-376` (remove globals, use config)
- Modify: `server/routes.py:230-244, 254-272` (update rules + notebook endpoints)
- Modify: `static/index.html` (make rules tab read-only so it doesn't break)

- [ ] **Step 1: Update `ai_gen.py` — remove globals, use config**

In `server/ai_gen.py`:

Add config import:
```python
from .config import get_oauth_token, get_workspace_host, app_config
```

Remove the in-memory rules global and its accessors (lines 25-35):
```python
# DELETE these lines:
# In-memory custom rules (persisted per app session)
_custom_rules: str = ""

def get_custom_rules() -> str:
    return _custom_rules

def set_custom_rules(rules: str) -> None:
    global _custom_rules
    _custom_rules = rules
```

Update `_build_system_prompt()` (lines 38-43) to read from config:
```python
def _build_system_prompt() -> str:
    """Build system prompt including any custom Responsible AI rules."""
    prompt = DEFAULT_SYSTEM_PROMPT
    rules = app_config.responsible_ai_rules
    if rules:
        prompt += f"\n\nAdditional organizational rules:\n{rules}"
    return prompt
```

Update `generate_descriptions()` (line 67) — read model from config:
```python
    model = model or app_config.serving_endpoint
```

Update `generate_notebook_code()` — use config values for model and rules:

Remove the `custom_rules` parameter entirely:
```python
def generate_notebook_code(
    catalog_name: str,
    schema_name: str,
) -> str:
```

Inside the function body, update the `rules_block` logic (lines 121-126) to read from config instead of the removed parameter:
```python
    rules_block = ""
    if app_config.responsible_ai_rules:
        rules_block = f'''
CUSTOM_RULES = """{app_config.responsible_ai_rules}"""
system_prompt += f"\\n\\nAdditional organizational rules:\\n{{CUSTOM_RULES}}"
'''
```

Replace the hardcoded `MODEL = "databricks-claude-sonnet-4-6"` line (line 152). This line is inside the outer f-string (`notebook = f'''...'''`), so the `{app_config.serving_endpoint}` will be evaluated by the f-string — no doubled braces needed here:
```python
MODEL = "{app_config.serving_endpoint}"
```

**Note:** This is correct because this is the outer f-string doing the interpolation. Contrast with the `{{CATALOG}}` patterns nearby which use doubled braces to produce literal `{CATALOG}` in the output notebook.

- [ ] **Step 2: Update `routes.py` — rules endpoints**

Remove `POST /api/rules` and its `CustomRulesRequest` model (lines 232-244). **Note:** this will break the frontend's "Save Rules" button in the Responsible AI tab — the frontend fix is deferred to the frontend follow-up work.

Update `GET /api/rules` to read from config:
```python
@router.get("/rules")
async def get_rules():
    return {"rules": app_config.responsible_ai_rules}
```

Add import at top:
```python
from .config import app_config
```

- [ ] **Step 3: Update `routes.py` — notebook export endpoint**

Update `export_notebook` (lines 254-272) — remove the `custom_rules` argument:

```python
@router.post("/export-notebook")
async def export_notebook(req: NotebookExportRequest):
    """Generate a downloadable Databricks notebook for automated description generation."""
    try:
        code = ai_gen.generate_notebook_code(
            req.catalog_name,
            req.schema_name,
        )
        return PlainTextResponse(
            content=code,
            media_type="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename=ai_descriptions_{req.catalog_name}_{req.schema_name}.py"
            },
        )
    except Exception as e:
        logger.error("Notebook export failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 4: Update frontend rules tab to read-only**

In `static/index.html`, make the Responsible AI Rules tab reflect that rules are now git-controlled. The `GET /api/rules` endpoint still works (returns config-driven rules), so `loadRules()` still populates the textarea — we just need to prevent editing.

Update the textarea (line ~347 area) — add `readonly` and adjust placeholder:
```html
      <textarea class="rules-textarea" id="custom-rules" readonly placeholder="Rules are managed via config.yaml — edit the file and redeploy to change."
```

Remove the Save and Clear buttons (lines ~347-348), replace with an info message:
```html
        <span style="color:var(--text-dim); font-size:13px;">Rules are managed via config.yaml and applied on deploy.</span>
```

Update the help text (lines ~357-358) — replace the session persistence bullets:
```html
        <li>Rules are defined in <code>config.yaml</code> and version-controlled via git</li>
        <li>To change rules, edit <code>config.yaml</code> and redeploy the app</li>
```

The `loadRules()` and `saveRules()` JS functions: `loadRules()` still works (reads from `GET /api/rules`). `saveRules()` and `clearRules()` are now dead code — leave them for now (no buttons call them). They'll be cleaned up in the frontend refactor phase.

- [ ] **Step 5: Verify app imports cleanly**

Run: `python -c "from server import routes; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add server/ai_gen.py server/routes.py static/index.html
git commit -m "feat: ai_gen reads model + rules from config, remove in-memory globals, make rules tab read-only"
```

---

### Task 7: Parameterize `catalog.py`

**Files:**
- Modify: `server/catalog.py:13-28, 71-111` (configurable exclusions, shared warehouse)

- [ ] **Step 1: Update imports**

In `server/catalog.py`, add:
```python
from .config import get_workspace_client, app_config
```

Replace the existing import:
```python
from .config import get_workspace_client
```

(The `warehouse` import was already added in Task 4.)

- [ ] **Step 2: Replace hardcoded exclusion lists**

In `list_catalogs()` (line 18):
```python
# Before:
if c.name not in ("__databricks_internal", "system")
# After:
if c.name not in app_config.excluded_catalogs
```

In `list_schemas()` (line 27):
```python
# Before:
if s.name not in ("information_schema",)
# After:
if s.name not in app_config.excluded_schemas
```

- [ ] **Step 3: Verify app imports cleanly**

Run: `python -c "from server import routes; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add server/catalog.py
git commit -m "feat: catalog exclusions driven by config.yaml"
```

---

## Chunk 3: New Endpoints, App Title, and SQL Safety

### Task 8: Add settings/warehouse endpoints + configurable app title

**Files:**
- Modify: `server/routes.py` (add 2 new endpoints)
- Modify: `app.py:8` (use config title)

- [ ] **Step 1: Add `GET /api/settings` endpoint**

Add to `server/routes.py`:

```python
@router.get("/settings")
async def get_settings():
    """Return current effective configuration for the settings UI."""
    from .warehouse import resolve_warehouse_id
    try:
        wh_id = resolve_warehouse_id()
    except Exception:
        wh_id = None

    return {
        "app_title": app_config.app_title,
        "serving_endpoint": app_config.serving_endpoint,
        "warehouse_id": wh_id,
        "warehouse_configured": bool(app_config.warehouse_id),
        "responsible_ai_rules": app_config.responsible_ai_rules,
        "audit_table": app_config.audit_table,
        "exclusions": {
            "catalogs": app_config.excluded_catalogs,
            "schemas": app_config.excluded_schemas,
        },
    }
```

- [ ] **Step 2: Add `GET /api/warehouses` endpoint**

Add to `server/routes.py`:

```python
@router.get("/warehouses")
async def list_warehouses():
    """List available SQL warehouses for the settings UI dropdown."""
    try:
        w = get_workspace_client()
        warehouses = list(w.warehouses.list())
        return {
            "warehouses": [
                {
                    "id": wh.id,
                    "name": wh.name,
                    "state": wh.state.value if wh.state else "UNKNOWN",
                    "warehouse_type": wh.warehouse_type.value if wh.warehouse_type else "UNKNOWN",
                }
                for wh in warehouses
            ]
        }
    except Exception as e:
        logger.error("List warehouses failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
```

Update the imports at the top of `routes.py`. By this point (after Tasks 5-6), the file should already have `from .config import app_config`. Merge all config imports into one line:
```python
from .config import app_config, get_workspace_client
```
This replaces any existing partial imports from `.config` that were added in earlier tasks.

- [ ] **Step 3: Update `app.py` — configurable title**

In `app.py` (line 8), change:
```python
app = FastAPI(title="Unity Catalog AI Descriptions")
```
to:
```python
from server.config import app_config
app = FastAPI(title=app_config.app_title)
```

- [ ] **Step 4: Verify app imports and new endpoints are registered**

Run: `python -c "from server import routes; print([r.path for r in routes.router.routes])"`
Expected: List includes `/api/settings` and `/api/warehouses`

- [ ] **Step 5: Commit**

```bash
git add server/routes.py app.py
git commit -m "feat: add settings + warehouse list endpoints, configurable app title"
```

---

### Task 9: Improve SQL escaping

**Scope note:** The spec mentions parameterized queries for audit INSERT/SELECT. The Databricks Statement Execution API does support named parameters, but DDL statements (`COMMENT ON TABLE`, `ALTER COLUMN COMMENT`) do not. This task focuses on robust escaping and identifier validation for all SQL, plus parameterized queries for audit DML where the API supports it.

**Identifier limitation:** `validate_identifier` intentionally restricts names to alphanumeric, underscore, and hyphen. UC does allow spaces and other characters in backtick-quoted names, but accepting those increases attack surface. Names with spaces will be rejected with a clear error.

**Files:**
- Create: `server/sql_utils.py`
- Create: `tests/test_sql_safety.py`
- Modify: `server/catalog.py:71-111`
- Modify: `server/audit.py`

- [ ] **Step 1: Write SQL safety tests**

Create `tests/test_sql_safety.py`:

```python
"""Tests for SQL escaping and identifier validation."""

import pytest


class TestIdentifierValidation:

    def test_valid_three_part_name(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("my_catalog.my_schema.my_table") == True

    def test_rejects_semicolon(self):
        from server.sql_utils import validate_identifier
        with pytest.raises(ValueError):
            validate_identifier("catalog; DROP TABLE--")

    def test_rejects_comment_injection(self):
        from server.sql_utils import validate_identifier
        with pytest.raises(ValueError):
            validate_identifier("catalog--comment")

    def test_allows_backtick_names(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("my-catalog.my-schema.my-table") == True

    def test_allows_underscores_and_dots(self):
        from server.sql_utils import validate_identifier
        assert validate_identifier("cat_1.sch_2.tbl_3") == True


class TestCommentEscaping:

    def test_escapes_single_quotes(self):
        from server.sql_utils import escape_comment
        assert "\\'" in escape_comment("it's a test")

    def test_escapes_backslashes(self):
        from server.sql_utils import escape_comment
        assert "\\\\" in escape_comment("path\\to\\thing")

    def test_replaces_newlines(self):
        from server.sql_utils import escape_comment
        result = escape_comment("line1\nline2")
        assert "\n" not in result


class TestQuoteIdentifier:

    def test_backtick_wraps_parts(self):
        from server.sql_utils import quote_identifier
        assert quote_identifier("cat.sch.tbl") == "`cat`.`sch`.`tbl`"

    def test_escapes_backticks_in_names(self):
        from server.sql_utils import quote_identifier
        result = quote_identifier("cat.sch.my`table")
        assert "my``table" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sql_safety.py -v`
Expected: FAIL — `server.sql_utils` doesn't exist

- [ ] **Step 3: Implement `server/sql_utils.py`**

Create `server/sql_utils.py`:

```python
"""SQL safety utilities — escaping and identifier validation."""

import re


# Pattern for valid UC identifier parts: alphanumeric, underscore, hyphen
_VALID_IDENT_PART = re.compile(r"^[\w\-]+$")

# Dangerous patterns that should never appear in identifiers
_DANGEROUS_PATTERNS = re.compile(r"(--|;|/\*|\*/)")


def validate_identifier(name: str) -> bool:
    """Validate a dotted identifier (e.g., catalog.schema.table).

    Raises ValueError if the identifier contains dangerous patterns.
    Returns True if valid.
    """
    if _DANGEROUS_PATTERNS.search(name):
        raise ValueError(f"Invalid identifier — contains dangerous pattern: {name}")

    parts = name.split(".")
    for part in parts:
        # Strip backticks if already quoted
        clean = part.strip("`")
        if not clean:
            raise ValueError(f"Invalid identifier — empty part in: {name}")
        if not _VALID_IDENT_PART.match(clean):
            raise ValueError(
                f"Invalid identifier part '{clean}' in: {name}. "
                "Only alphanumeric, underscore, and hyphen are allowed."
            )
    return True


def quote_identifier(name: str) -> str:
    """Quote a dotted identifier with backticks (e.g., cat.sch.tbl -> `cat`.`sch`.`tbl`).

    Escapes any backticks within individual parts by doubling them.
    """
    parts = name.split(".")
    quoted = []
    for part in parts:
        clean = part.strip("`")
        escaped = clean.replace("`", "``")
        quoted.append(f"`{escaped}`")
    return ".".join(quoted)


def escape_comment(text: str) -> str:
    """Escape a string for use as a SQL string literal value.

    Handles single quotes, backslashes, and newlines.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sql_safety.py -v`
Expected: All tests PASS

- [ ] **Step 5: Update `catalog.py` — use sql_utils**

In `server/catalog.py`:

Add import:
```python
from .sql_utils import validate_identifier, quote_identifier, escape_comment
```

Update `apply_table_comment()` (lines 71-90):
```python
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
```

Update `apply_column_comment()` (lines 93-111):
```python
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
```

- [ ] **Step 6: Update `audit.py` — use sql_utils for escaping**

In `server/audit.py`:

Add import:
```python
from .sql_utils import escape_comment, quote_identifier
```

Update `log_action()` to use parameterized queries for the INSERT values. Replace the `esc()` helper and string-interpolated VALUES with the Statement Execution API's `parameters` argument:

```python
    from databricks.sdk.service.sql import StatementParameterListItem

    audit_table = quote_identifier(app_config.audit_table)
    sql = f"""
    INSERT INTO {audit_table}
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

    resp = w.statement_execution.execute_statement(
        warehouse_id=wh_id, statement=sql, parameters=params, wait_timeout="50s"
    )
```

Similarly, update `get_audit_log()` WHERE clause to use a parameter when filtering by table name:
```python
    if full_table_name:
        sql += " WHERE full_table_name = :table_filter"
        params = [StatementParameterListItem(name="table_filter", value=full_table_name)]
    else:
        params = None
```

Remove the inline `esc()` function (no longer needed for audit).

Update audit SQL to quote the configured table path:
```python
    audit_table = quote_identifier(app_config.audit_table)
```

Use `audit_table` in place of the raw `app_config.audit_table` in all SQL statements (`ensure_audit_table`, `log_action`, `get_audit_log`).

- [ ] **Step 7: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 8: Verify app imports cleanly**

Run: `python -c "from server import routes; print('OK')"`
Expected: `OK`

- [ ] **Step 9: Commit**

```bash
git add server/sql_utils.py server/catalog.py server/audit.py tests/test_sql_safety.py
git commit -m "feat: add SQL safety utilities, improve escaping across catalog + audit"
```

---

## Chunk 4: Update README

### Task 10: Rewrite README for DAB deployment + configuration docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update deployment instructions**

Replace Steps 2-5 (manual workspace import + deploy) with DAB-based deployment:

```markdown
### Step 2: Configure Variables

Edit `databricks.yml` to set your workspace host in the targets section. Optionally override variables:

\`\`\`bash
# Deploy with defaults
databricks bundle deploy -t dev

# Deploy with custom warehouse
databricks bundle deploy -t dev --var warehouse_id=<your-warehouse-id>
\`\`\`
```

- [ ] **Step 2: Add Configuration section**

Add a new "Configuration" section after "Deployment Steps" explaining the two config layers:

```markdown
## Configuration

### Infrastructure Settings (`databricks.yml`)

| Variable | Default | Description |
|----------|---------|-------------|
| `warehouse_id` | (auto-detect) | SQL warehouse ID. Leave empty to auto-detect a running warehouse. |
| `serving_endpoint` | `databricks-claude-sonnet-4-6` | Foundation Model API endpoint name |
| `app_title` | `Unity Catalog AI Descriptions` | Display title in the app header |

Set per-environment in the `targets` section or override at deploy time with `--var`.

### App Behavior Settings (`config.yaml`)

| Setting | Default | Description |
|---------|---------|-------------|
| `responsible_ai_rules` | (see file) | Rules injected into AI system prompt |
| `audit.table` | `governance.ai_descriptions.audit_log` | Centralized audit table (full three-part name) |
| `exclusions.catalogs` | `__databricks_internal`, `system` | Catalogs hidden from browse tree |
| `exclusions.schemas` | `information_schema` | Schemas hidden from browse tree |
```

- [ ] **Step 3: Remove manual audit table creation section**

Remove or replace Step 7 ("Create the Audit Table") with a note that the audit table is now auto-created in the same schema as the described tables.

- [ ] **Step 4: Update API Endpoints table**

Add the new endpoints and mark the removed one:
- Add `GET /api/settings`
- Add `GET /api/warehouses`
- Remove `POST /api/rules` (now read-only, git-controlled)
- Update `GET /api/audit` — note it now requires `catalog_name` and `schema_name` query params

- [ ] **Step 5: Update Project Structure section**

Update the project structure tree in README to:
- Keep `app.yaml` (updated in Task 11 with new env vars)
- Add `databricks.yml`
- Add `config.yaml`
- Add `server/warehouse.py`
- Add `server/sql_utils.py`
- Add `tests/` directory

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: update README for DAB deployment and configuration"
```

---

### Task 11: Update `app.yaml` with new environment variables

**Files:**
- Modify: `app.yaml`

`app.yaml` is required by the Databricks Apps runtime for direct `databricks apps deploy` deployments. It coexists with `databricks.yml`: the bundle config overrides these settings for DAB deployments, while `app.yaml` serves as the base config for non-DAB deployments.

- [ ] **Step 1: Update `app.yaml` to include new env vars**

The current `app.yaml` only defines `SERVING_ENDPOINT`. Add `WAREHOUSE_ID` and `APP_TITLE` with sensible defaults:

```yaml
command:
  - "python"
  - "-m"
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

env:
  - name: SERVING_ENDPOINT
    value: databricks-claude-sonnet-4-6
  - name: WAREHOUSE_ID
    value: ""
  - name: APP_TITLE
    value: "Unity Catalog AI Descriptions"
```

- [ ] **Step 2: Verify both config files are consistent**

Confirm that the env var names and defaults in `app.yaml` match the corresponding variables in `databricks.yml`.

- [ ] **Step 3: Commit**

```bash
git add app.yaml
git commit -m "chore: update app.yaml with WAREHOUSE_ID and APP_TITLE env vars"
```

---

### Task 12: Final verification

- [ ] **Step 1: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Verify clean import**

Run: `python -c "from server import routes; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Verify bundle validates**

Run: `databricks bundle validate`
Expected: No errors

- [ ] **Step 4: Review git log for clean history**

Run: `git log --oneline`
Expected: 11 new commits, each representing one logical change
