# Plan: Persistent, Access-Controlled Responsible AI Rules Editing

**Target branch:** `feature/rules-editing` (branch from `feature/security-hardening-and-tests` after PR #4 merges)
**Depends on:** PR #4 merged (security hardening + tests)

---

## Summary Table

| Phase | Description | Files Created | Files Modified | Complexity |
|-------|-------------|---------------|----------------|------------|
| 1 | Per-user identity foundation | `server/identity.py` | `server/routes.py` | Low |
| 2 | Rules Delta table | `server/rules.py`, `tests/test_rules.py` | `server/config.py`, `server/ai_gen.py`, `app.py`, `config.yaml` | Medium |
| 3 | Access control + API endpoints | — | `server/rules.py`, `server/routes.py`, `server/config.py`, `config.yaml` | Medium |
| 4 | Frontend editing UI | — | `static/index.html` | Low-Medium |

---

## Phase 1 — Per-User Identity Foundation

### Goal

Capture the calling user's email/username from the Databricks-injected OAuth token on every request. Do NOT yet use this token for UC operations — just extract identity for audit logging and future access-control checks.

### How Databricks Apps inject identity

Databricks Apps proxy every browser request through their runtime and inject the logged-in user's OAuth token as the `X-Forwarded-Access-Token` HTTP header. This is a standard JWT (RS256). The `sub` claim contains the user's email or service principal ID. Decoding the payload (no signature verification needed — the token was issued by Databricks and is already trusted at the proxy boundary) is sufficient to extract identity.

As a fallback for local development (where the header is absent), identity falls back to `"dev_user"` or an environment variable.

### Files to create

**`server/identity.py`** — new module

```python
"""Per-request user identity extraction for Databricks Apps."""

import base64
import json
import logging
import os

from fastapi import Request

logger = logging.getLogger(__name__)

_LOCAL_USER = os.environ.get("DEV_USER_EMAIL", "dev_user")


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature.

    The token is injected by the Databricks Apps runtime and is already
    trusted — we only need the payload to read the 'sub' / 'email' claim.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logger.warning("JWT decode failed: %s", e)
        return {}


def get_current_user(request: Request) -> str:
    """FastAPI dependency — returns the calling user's email/username.

    Reads X-Forwarded-Access-Token (injected by Databricks Apps runtime).
    Falls back to DEV_USER_EMAIL env var or 'dev_user' when running locally.
    """
    token = request.headers.get("X-Forwarded-Access-Token", "")
    if not token:
        return _LOCAL_USER

    payload = _decode_jwt_payload(token)
    user = payload.get("email") or payload.get("sub") or _LOCAL_USER
    return str(user)


def get_forwarded_token(request: Request) -> str | None:
    """Return the raw forwarded OAuth token, or None if absent."""
    return request.headers.get("X-Forwarded-Access-Token") or None
```

### Files to modify

**`server/routes.py`** — inject `get_current_user` into `apply/batch` so `applied_by` reflects the real user:

```python
from fastapi import APIRouter, Depends, HTTPException, Request
from .identity import get_current_user

@router.post("/apply/batch")
async def apply_batch(req: ApplyBatchRequest, current_user: str = Depends(get_current_user)):
    # Replace hardcoded "app_user" with current_user in audit.log_batch() call
    logged = audit.log_batch(req.full_name, audit_actions, applied_by=current_user)
```

### What can be tested independently

- Unit tests for `_decode_jwt_payload` with a known JWT fixture (no network calls).
- Test that `get_current_user` returns `"dev_user"` when no header is present.
- Test that `get_current_user` extracts `email` from a mock JWT payload.
- Integration: hit `POST /api/apply/batch` locally and verify `applied_by` in audit log is no longer `"app_user"`.

---

## Phase 2 — Rules Delta Table

### Goal

Move the active Responsible AI rules from a config.yaml string to a versioned Delta table. At startup: create the table if absent, seed from `app_config.responsible_ai_rules` if empty. Wire `ai_gen._build_system_prompt()` to read from Delta at call time.

### New config.yaml keys

```yaml
rules:
  table: "governance.ai_descriptions.rules_history"   # 3-part UC name
  # admin_group added in Phase 3
```

If `rules.table` is absent, default to the same catalog.schema as `audit.table` with table name `rules_history`.

### AppConfig changes (`server/config.py`)

```python
# Add to AppConfig dataclass
rules_table: str = ""   # resolved at load time; empty = derive from audit_table

# Add to load_config()
if "rules" in data and "table" in data["rules"]:
    cfg.rules_table = data["rules"]["table"]
if not cfg.rules_table:
    parts = cfg.audit_table.split(".")
    if len(parts) == 3:
        cfg.rules_table = f"{parts[0]}.{parts[1]}.rules_history"
    else:
        cfg.rules_table = "governance.ai_descriptions.rules_history"
```

### New file: `server/rules.py`

```python
"""Persistent Responsible AI rules stored in a Delta table."""

import logging
from typing import Optional

from .config import get_workspace_client, app_config
from .warehouse import resolve_warehouse_id
from .sql_utils import quote_identifier

logger = logging.getLogger(__name__)


def _rules_table_quoted() -> str:
    return quote_identifier(app_config.rules_table)


def ensure_rules_table() -> bool:
    """CREATE IF NOT EXISTS the rules history table."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()
    sql = f"""
    CREATE TABLE IF NOT EXISTS {_rules_table_quoted()} (
        rules_id    BIGINT GENERATED ALWAYS AS IDENTITY,
        rules_text  STRING  NOT NULL COMMENT 'Full text of the rules at this version',
        changed_by  STRING  NOT NULL COMMENT 'Email of user who saved this version',
        changed_at  TIMESTAMP NOT NULL COMMENT 'When this version was saved',
        note        STRING  COMMENT 'Optional change note'
    ) USING DELTA
    COMMENT 'Version history of Responsible AI rules for UC AI Descriptions'
    """
    resp = w.statement_execution.execute_statement(
        warehouse_id=wh_id, statement=sql, wait_timeout="50s"
    )
    return resp.status and resp.status.state == StatementState.SUCCEEDED


def get_active_rules() -> str:
    """Return the most recently saved rules text, or empty string if table is empty."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()
    sql = f"""
    SELECT rules_text FROM {_rules_table_quoted()}
    ORDER BY changed_at DESC, rules_id DESC LIMIT 1
    """
    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, wait_timeout="30s"
        )
        if (resp.status and resp.status.state == StatementState.SUCCEEDED
                and resp.result and resp.result.data_array):
            return resp.result.data_array[0][0] or ""
        return ""
    except Exception as e:
        logger.error("get_active_rules failed: %s", e)
        return ""


def save_rules(text: str, changed_by: str, note: Optional[str] = None) -> bool:
    """Insert a new rules version row. Returns True on success."""
    from databricks.sdk.service.sql import StatementState, StatementParameterListItem
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()
    sql = f"""
    INSERT INTO {_rules_table_quoted()} (rules_text, changed_by, changed_at, note)
    VALUES (:rules_text, :changed_by, current_timestamp(), :note)
    """
    params = [
        StatementParameterListItem(name="rules_text", value=text),
        StatementParameterListItem(name="changed_by", value=changed_by),
        StatementParameterListItem(name="note", value=note or ""),
    ]
    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, parameters=params, wait_timeout="50s"
        )
        return resp.status and resp.status.state == StatementState.SUCCEEDED
    except Exception as e:
        logger.error("save_rules failed: %s", e)
        return False


def get_rules_history(limit: int = 20) -> list[dict]:
    """Return recent rules change history, most recent first."""
    from databricks.sdk.service.sql import StatementState
    w = get_workspace_client()
    wh_id = resolve_warehouse_id()
    sql = f"""
    SELECT rules_id, rules_text, changed_by, changed_at, note
    FROM {_rules_table_quoted()}
    ORDER BY changed_at DESC, rules_id DESC LIMIT {int(limit)}
    """
    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id, statement=sql, wait_timeout="30s"
        )
        if not resp.result or not resp.result.data_array:
            return []
        columns = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(columns, row)) for row in resp.result.data_array]
    except Exception as e:
        logger.error("get_rules_history failed: %s", e)
        return []


def validate_rules_setup() -> bool:
    """Bootstrap the rules table at app startup. Mirrors validate_audit_setup() pattern.

    1. Verify catalog access.
    2. Verify or create schema.
    3. CREATE TABLE IF NOT EXISTS.
    4. Seed from app_config.responsible_ai_rules if table is empty.
    """
    table = app_config.rules_table
    parts = table.split(".")
    if len(parts) != 3:
        logger.error("Rules table '%s' is not a valid 3-part name.", table)
        return False

    catalog_name, schema_name, _ = parts
    full_schema = f"{catalog_name}.{schema_name}"
    w = get_workspace_client()

    try:
        w.catalogs.get(catalog_name)
    except Exception as e:
        logger.error("Rules setup: cannot access catalog '%s' (%s).", catalog_name, e)
        return False

    try:
        w.schemas.get(full_schema)
    except Exception:
        try:
            w.schemas.create(name=schema_name, catalog_name=catalog_name)
        except Exception as err:
            logger.error("Rules setup: cannot create schema '%s' (%s).", full_schema, err)
            return False

    try:
        if not ensure_rules_table():
            logger.error("Rules setup: table creation failed for '%s'.", table)
            return False
    except Exception as e:
        logger.error("Rules setup: could not create table '%s' (%s).", table, e)
        return False

    seed_text = app_config.responsible_ai_rules
    if seed_text and not get_active_rules():
        ok = save_rules(seed_text, changed_by="system:seed", note="Seeded from config.yaml")
        if ok:
            logger.info("Rules setup: seeded from config.yaml into '%s'.", table)

    logger.info("Rules: table '%s' is ready.", table)
    return True
```

### Modify `server/ai_gen.py`

Change `_build_system_prompt()` to call `rules.get_active_rules()`. Priority: `rules_override` → Delta → `app_config.responsible_ai_rules` (fallback if Delta unreachable):

```python
from . import rules as rules_module

def _build_system_prompt(rules_override: str | None = None) -> str:
    prompt = DEFAULT_SYSTEM_PROMPT
    if rules_override is not None:
        active_rules = rules_override
    else:
        try:
            active_rules = rules_module.get_active_rules()
        except Exception:
            logger.warning("Delta rules unreachable — falling back to config.yaml rules")
            active_rules = app_config.responsible_ai_rules
    if active_rules:
        prompt += f"\n\nAdditional organizational rules:\n{active_rules}"
    return prompt
```

Also update `generate_notebook_code()` to call `rules_module.get_active_rules()` so exported notebooks reflect the current live rules.

### Modify `app.py`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from server.audit import validate_audit_setup
    from server.rules import validate_rules_setup
    validate_audit_setup()
    validate_rules_setup()
    yield
```

### What can be tested independently

- Unit tests for `ensure_rules_table()`, `save_rules()`, `get_active_rules()` with mocked `statement_execution`.
- Unit test for `validate_rules_setup()`: verify it seeds when table is empty, skips when populated.
- Unit test for `_build_system_prompt()`: verify fallback chain (override → Delta → config).

---

## Phase 3 — Access Control + API Endpoints

### Goal

Two-layer access control: group membership check via SCIM using the user's forwarded token. Expose `GET /api/rules/can-edit`, `POST /api/rules`, and `GET /api/rules/history`.

### New config.yaml keys

```yaml
rules:
  table: "governance.ai_descriptions.rules_history"
  admin_group: "data-governance-admins"   # Databricks workspace group name
```

If `admin_group` is absent or empty, `can_edit_rules()` returns `False` for all users — safe default.

### AppConfig changes (`server/config.py`)

```python
rules_admin_group: str = ""   # empty = UI editing disabled

# In load_config():
if "rules" in data and "admin_group" in data["rules"]:
    cfg.rules_admin_group = data["rules"]["admin_group"]
```

### Access control in `server/rules.py`

Uses the **user's forwarded token** (not the SP token) to call SCIM. Fails safely (returns `False`) if token is invalid — no additional SP grants required. Results cached per-user for 5 minutes.

```python
import time

_can_edit_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 300


def can_edit_rules(user_token: str, user_email: str) -> bool:
    if not app_config.rules_admin_group:
        return False
    now = time.time()
    cached = _can_edit_cache.get(user_email)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]
    result = _check_group_membership(user_token, user_email, app_config.rules_admin_group)
    _can_edit_cache[user_email] = (result, now)
    return result


def _check_group_membership(user_token: str, user_email: str, group_name: str) -> bool:
    import requests
    from .config import get_workspace_host
    host = get_workspace_host()
    try:
        resp = requests.get(
            f"{host}/api/2.0/preview/scim/v2/Groups",
            headers={"Authorization": f"Bearer {user_token}"},
            params={"filter": f'displayName eq "{group_name}"', "attributes": "members"},
            timeout=10,
        )
        resp.raise_for_status()
        resources = resp.json().get("Resources", [])
        if not resources:
            logger.warning("Rules admin group '%s' not found.", group_name)
            return False
        members = resources[0].get("members", [])
        return any(m.get("display", "").lower() == user_email.lower() for m in members)
    except Exception as e:
        logger.error("SCIM group check failed for '%s': %s", user_email, e)
        return False
```

### New/updated API endpoints in `server/routes.py`

```python
# GET /api/rules — updated to read from Delta
@router.get("/rules")
async def get_rules():
    try:
        text = rules_module.get_active_rules()
        return {"rules": text, "source": "delta"}
    except Exception:
        return {"rules": app_config.responsible_ai_rules, "source": "config"}

# GET /api/rules/can-edit — new
@router.get("/rules/can-edit")
async def get_rules_can_edit(request: Request, current_user: str = Depends(get_current_user)):
    token = get_forwarded_token(request)
    if not token:
        return {"can_edit": False, "reason": "no_token"}
    return {"can_edit": rules_module.can_edit_rules(user_token=token, user_email=current_user)}

# POST /api/rules — new
class SaveRulesRequest(BaseModel):
    rules_text: str
    note: Optional[str] = None

@router.post("/rules")
async def save_rules_endpoint(
    req: SaveRulesRequest, request: Request, current_user: str = Depends(get_current_user)
):
    token = get_forwarded_token(request)
    if not token:
        raise HTTPException(status_code=403, detail="Authentication required")
    if not rules_module.can_edit_rules(user_token=token, user_email=current_user):
        raise HTTPException(status_code=403, detail="Not a member of the rules admin group.")
    text = req.rules_text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Rules text cannot be empty")
    if len(text) > 10_000:
        raise HTTPException(status_code=422, detail="Rules text exceeds 10,000 character limit")
    ok = rules_module.save_rules(text=text, changed_by=current_user, note=req.note)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save rules — check server logs")
    logger.info("Rules updated by %s", current_user)
    return {"status": "saved", "changed_by": current_user}

# GET /api/rules/history — new (visible to all users)
@router.get("/rules/history")
async def get_rules_history_endpoint(limit: int = 20):
    try:
        history = rules_module.get_rules_history(limit=min(limit, 100))
        return {"history": history, "count": len(history)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### What can be tested independently

- Unit test: `can_edit_rules()` returns `False` when `rules_admin_group` is empty.
- Unit test: `can_edit_rules()` with mocked `requests.get` returning a SCIM group containing the user.
- Unit test: cache TTL (mock `time.time()`).
- Unit test: `POST /api/rules` returns 403 when group check fails; 422 for empty text.
- Integration: `GET /api/rules/can-edit` with no header returns `{"can_edit": false}`.

---

## Phase 4 — Frontend Editing UI

### Goal

Rules tab shows read-only view for regular users (unchanged). Admins see an Edit button. In edit mode, textarea is enabled with Save/Cancel buttons and an optional change note field. All users see a collapsible change history table.

### JS additions to `static/index.html`

**Note on XSS:** All history data from the API must be treated as untrusted. Use `textContent` for all plain-text fields (timestamps, emails, notes). The rules preview truncation can be done server-side or via `textContent` on a DOM element — never interpolate database values directly into an HTML string.

```javascript
let rulesCanEdit = false;
let rulesOriginalText = '';

async function loadRulesTab() {
  const [rulesData, permData] = await Promise.all([
    apiFetch(`${API}/rules`),
    apiFetch(`${API}/rules/can-edit`),
  ]);
  rulesOriginalText = rulesData.rules || '';
  rulesCanEdit = permData.can_edit || false;

  document.getElementById('custom-rules').value = rulesOriginalText;
  document.getElementById('custom-rules').readOnly = true;
  document.getElementById('rules-edit-btn').style.display = rulesCanEdit ? 'inline-flex' : 'none';

  const badge = document.getElementById('rules-source-badge');
  badge.textContent = rulesData.source === 'delta' ? 'Live (Delta)' : 'Config fallback';

  await loadRulesHistory();
}

function enterRulesEditMode() {
  if (!rulesCanEdit) return;
  const ta = document.getElementById('custom-rules');
  ta.readOnly = false;
  ta.focus();
  document.getElementById('rules-edit-btn').style.display = 'none';
  document.getElementById('rules-save-row').style.display = 'flex';
}

function cancelRulesEdit() {
  const ta = document.getElementById('custom-rules');
  ta.value = rulesOriginalText;
  ta.readOnly = true;
  document.getElementById('rules-edit-btn').style.display = rulesCanEdit ? 'inline-flex' : 'none';
  document.getElementById('rules-save-row').style.display = 'none';
  document.getElementById('rules-note').value = '';
}

async function saveRules() {
  const text = document.getElementById('custom-rules').value.trim();
  const note = document.getElementById('rules-note').value.trim();
  if (!text) { showToast('Rules text cannot be empty', true); return; }
  const btn = document.getElementById('rules-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    await apiFetch(`${API}/rules`, {
      method: 'POST',
      body: JSON.stringify({ rules_text: text, note: note || null }),
    });
    showToast('Rules saved successfully');
    rulesOriginalText = text;
    cancelRulesEdit();
    await loadRulesHistory();
  } catch (e) {
    showToast(`Save failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Rules';
  }
}

async function loadRulesHistory() {
  const tbody = document.getElementById('rules-history-body');
  // Clear and rebuild using DOM methods to avoid XSS (textContent for all user-supplied values)
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  try {
    const data = await apiFetch(`${API}/rules/history`);
    if (!data.history.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.textContent = 'No history yet.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const row of data.history) {
      const tr = document.createElement('tr');
      [
        row.changed_at || '',
        row.changed_by || '',
        (row.rules_text || '').slice(0, 80) + ((row.rules_text || '').length > 80 ? '…' : ''),
        row.note || '',
      ].forEach(val => {
        const td = document.createElement('td');
        td.textContent = val;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
  } catch (e) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = `Failed: ${e.message}`;
    td.style.color = 'var(--red)';
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}
```

### HTML changes to the Rules tab

Key structural additions to `#tab-rules`:
- `id="rules-source-badge"` span in the section title — shows "Live (Delta)" or "Config fallback"
- `id="rules-edit-btn"` button (hidden by default, shown for admins via `loadRulesTab()`)
- `id="rules-save-row"` flex div (hidden until edit mode): change note input, Save button, Cancel button
- Change history table: `<tbody id="rules-history-body">` populated via DOM methods
- Wire `switchTab('rules')` to call `loadRulesTab()` instead of `loadRules()`

### What can be tested independently

- Manual: Rules tab load as non-admin — Edit button absent, textarea read-only.
- Manual: Rules tab load as admin — Edit button present; clicking enables textarea + shows Save/Cancel.
- Manual: Save a change — history row appears, badge shows "Live (Delta)".
- Manual: Cancel edit — textarea reverts.
- Manual: Save with blank text — error toast before API call.

---

## Rollout Strategy

### No startup race condition

`validate_rules_setup()` runs synchronously in the FastAPI `lifespan` context, which must complete before the app serves any requests. Seeding is part of that sequence. No generation request can reach `_build_system_prompt()` until after seeding completes.

### Fallback chain in `_build_system_prompt()`

`rules_override` → `get_active_rules()` → `app_config.responsible_ai_rules`

If the Delta table is unreachable (e.g., warehouse cold start), config.yaml rules are used and a WARNING is logged. This prevents a generation outage at the cost of possibly stale rules.

### Migration checklist for operators

1. Add `rules.table` to `config.yaml` (or accept the default derived from `audit.table`).
2. Optionally add `rules.admin_group`. If absent, UI editing stays disabled — safe default.
3. Ensure the SP has `CREATE TABLE` and `MODIFY` on the target schema (same grants as the audit table).
4. Deploy. On first boot, config.yaml rules are written as the initial Delta row.
5. Verify rules appear correctly in the Responsible AI Rules tab.
6. `responsible_ai_rules` in config.yaml remains as a seed/fallback only after first boot.

### Escape hatch: git-controlled rules always win

Add `rules.seed_always: true` to config.yaml if config.yaml should override the Delta table on every deploy (not recommended — defeats UI editing, but useful for orgs not ready to delegate). Implement by calling `save_rules()` unconditionally in `validate_rules_setup()` rather than only when empty.

---

## Dependencies Between Phases

```
Phase 1 (identity)     Phase 2 (Delta table)
       \                      /
        \                    /
         Phase 3 (access control + API)
                  |
         Phase 4 (frontend UI)
```

Phase 1 and Phase 2 are **independent** and can be developed in parallel on separate sub-branches.
Phase 3 requires both Phase 1 and Phase 2.
Phase 4 requires Phase 3.

---

## Files Created or Modified by Phase

### Phase 1
- **Create:** `server/identity.py`
- **Modify:** `server/routes.py` (inject `get_current_user` into `apply/batch`)

### Phase 2 (backend branch)
- **Create:** `server/rules.py`
- **Create:** `tests/test_rules.py`
- **Modify:** `server/config.py` (add `rules_table` to `AppConfig`)
- **Modify:** `server/ai_gen.py` (`_build_system_prompt` reads from Delta; `generate_notebook_code` uses live rules)
- **Modify:** `app.py` (add `validate_rules_setup()` to lifespan)
- **Modify:** `config.yaml` (add `rules.table` key)

### Phase 3 (backend branch)
- **Modify:** `server/rules.py` (add `can_edit_rules`, `_check_group_membership`, cache)
- **Modify:** `server/routes.py` (update `GET /api/rules`; add `GET /api/rules/can-edit`, `POST /api/rules`, `GET /api/rules/history`)
- **Modify:** `server/config.py` (add `rules_admin_group` to `AppConfig`)
- **Modify:** `config.yaml` (add `rules.admin_group` key)

### Phase 4 (frontend branch)
- **Modify:** `static/index.html` (Rules tab HTML + JS: `loadRulesTab`, `enterRulesEditMode`, `cancelRulesEdit`, `saveRules`, `loadRulesHistory`)
