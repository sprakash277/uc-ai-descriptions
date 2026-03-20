# Plan: Per-User UC Permissions for Browse and Apply

**Goal:** Replace the current shared service-principal model with per-request user identity so that:
- Users can only browse catalogs/schemas/tables they have UC access to
- Users can only apply descriptions to tables/columns they have MODIFY permission on
- AI generation and audit logging continue to use the app SP

**Prerequisite:** PR #6 (identity) merged.

---

## Architecture overview

```
                         ┌─────────────────────────────────────────┐
Request → Databricks     │  browse / apply operations              │
          Apps proxy  →  │  WorkspaceClient(host, user_token)      │ → Unity Catalog
          injects        │                                         │   (enforces user perms)
          headers        └─────────────────────────────────────────┘

                         ┌─────────────────────────────────────────┐
                         │  AI generation / audit log writes       │
                         │  WorkspaceClient(host, SP credentials)  │ → Serving endpoint
                         └─────────────────────────────────────────┘                  Audit table
```

| Operation | Client | Why |
|-----------|--------|-----|
| List catalogs / schemas / tables | User | UC auto-filters to user's access |
| Get table details | User | UC enforces read permissions |
| Apply table/column comment | User | UC enforces MODIFY permission |
| AI generate (serving endpoint call) | SP | Serving endpoint auth, no UC row-level concern |
| Audit log writes | SP | User may not have write access to audit table |
| Warehouse resolution | SP | App-level infrastructure concern |

---

## Phase A — Enable OBO in Databricks Apps UI (one-time manual step)

This is not a code change. In the Databricks Apps UI:

1. Navigate to the app settings for `uc-ai-descriptions`
2. Enable **"On-behalf-of-user authorization"**
3. Select scopes: at minimum `sql` (for warehouse/SQL execution) and `unity-catalog` (for catalog API calls)
4. Save and redeploy

After this step, `x-forwarded-access-token` will be present in all proxied requests.
Verify with `/api/whoami` (which already shows all forwarded headers).

**Note:** Until Phase A is complete, Phases B–D should degrade gracefully to the SP client.

---

## Phase B — Token extraction and per-user WorkspaceClient

### `server/identity.py` additions

Add `get_user_token()` alongside the existing `get_current_user()`:

```python
def get_user_token(request: Request) -> str | None:
    """Return the user's OAuth token from the Databricks Apps proxy header.

    Only present when OBO authorization is enabled in the App settings.
    Returns None in local dev or when OBO is not configured.
    """
    return request.headers.get("x-forwarded-access-token") or None
```

### `server/config.py` additions

Add a factory for per-user workspace clients:

```python
def get_user_workspace_client(token: str) -> WorkspaceClient:
    """Create a WorkspaceClient authenticated as the calling user.

    Used for browse and apply operations so UC enforces the user's own
    permissions. Never cache this — create per request.
    """
    return WorkspaceClient(host=get_workspace_host(), token=token)
```

### `server/routes.py` — new FastAPI dependency

```python
from fastapi import Depends, Request
from .identity import get_user_token
from .config import get_user_workspace_client, get_workspace_client

def get_request_client(request: Request):
    """FastAPI dependency: user WorkspaceClient if OBO token is present,
    otherwise the app service principal client (graceful degradation)."""
    token = get_user_token(request)
    if token:
        return get_user_workspace_client(token)
    return get_workspace_client()
```

---

## Phase C — Thread user client through catalog.py

All catalog functions currently call `get_workspace_client()` internally. Update each to accept an optional `w` parameter:

```python
# Before
def list_catalogs() -> list[dict]:
    w = get_workspace_client()
    ...

# After
def list_catalogs(w=None) -> list[dict]:
    w = w or get_workspace_client()
    ...
```

Functions to update:
- `list_catalogs(w=None)`
- `list_schemas(catalog_name, w=None)`
- `list_tables(catalog_name, schema_name, w=None)`
- `get_table_details(full_name, w=None)`
- `apply_table_comment(full_name, comment, w=None)`
- `apply_column_comment(full_name, column_name, comment, w=None)`

The fallback to `get_workspace_client()` means existing callers (tests, audit bootstrapping) work unchanged.

---

## Phase D — Thread user client through routes.py

Inject `get_request_client` as a dependency on all browse and apply endpoints:

```python
@router.get("/catalogs")
async def get_catalogs(w=Depends(get_request_client)):
    return {"catalogs": catalog.list_catalogs(w=w)}

@router.get("/schemas/{catalog_name}")
async def get_schemas(catalog_name: str, w=Depends(get_request_client)):
    return {"schemas": catalog.list_schemas(catalog_name, w=w)}

@router.get("/tables/{catalog_name}/{schema_name}")
async def get_tables(catalog_name: str, schema_name: str, w=Depends(get_request_client)):
    return {"tables": catalog.list_tables(catalog_name, schema_name, w=w)}

@router.get("/table/{full_name:path}")
async def get_table_details(full_name: str, w=Depends(get_request_client)):
    return {"table": catalog.get_table_details(full_name, w=w)}

@router.post("/apply/table")
async def apply_table_comment(req: ApplyTableCommentRequest, w=Depends(get_request_client)):
    success = catalog.apply_table_comment(req.full_name, req.comment, w=w)
    return {"status": "success" if success else "failed", "full_name": req.full_name}

@router.post("/apply/column")
async def apply_column_comment(req: ApplyColumnCommentRequest, w=Depends(get_request_client)):
    success = catalog.apply_column_comment(req.full_name, req.column_name, req.comment, w=w)
    return {"status": "success" if success else "failed"}

@router.post("/apply/batch")
async def apply_batch(req: ApplyBatchRequest,
                      w=Depends(get_request_client),
                      current_user: str = Depends(get_current_user)):
    # Uses w for catalog operations, SP client for audit writes (unchanged)
    ...
```

**`/api/generate` and `/api/generate/batch`** — the generate endpoints call `catalog.get_table_details()` to build the prompt. These should also use the user client so the user can only generate descriptions for tables they can read.

**Audit log writes** in `apply_batch` continue using the SP client via `audit.ensure_audit_table()` and `audit.log_batch()` — no change needed there.

---

## Phase E — Error handling: permission denied

When a user lacks permission, UC will raise an exception. Currently all UC exceptions become HTTP 500. We should surface 403 distinctly so the frontend can show a meaningful message rather than a generic error.

```python
# In routes.py exception handlers:
from databricks.sdk.errors import PermissionDenied, NotFound

except PermissionDenied as e:
    raise HTTPException(status_code=403, detail=f"Permission denied: {e}")
```

Frontend: a 403 on apply should show "You don't have permission to modify this table" rather than a generic failure toast.

---

## Phase F — Tests

New tests in `tests/test_user_permissions.py`:

- `get_user_token` returns None when header absent, returns token when present
- `get_request_client` returns SP client when no token, user client when token present
- Mock catalog functions verify the correct client is passed through
- 403 handling: PermissionDenied from SDK maps to HTTP 403

---

## Summary table

| Phase | Change | Files |
|-------|--------|-------|
| A | Enable OBO in Apps UI | (manual, no code) |
| B | Token extraction + user client factory | `server/identity.py`, `server/config.py`, `server/routes.py` |
| C | Optional `w` param on all catalog functions | `server/catalog.py` |
| D | Inject user client into browse + apply routes | `server/routes.py` |
| E | 403 error handling | `server/routes.py`, `static/index.html` |
| F | Tests | `tests/test_user_permissions.py` |

## Risks and open questions

1. **Warehouse access**: The user must have `CAN USE` on the configured warehouse for SQL-based audit operations that run under their identity. Since audit writes still use the SP client, this isn't an issue — but if we ever move audit to use the user client, we'd need to think about this.

2. **Token expiry**: The token forwarded by Databricks Apps is valid for ~1 hour. Since we create a new client per request (never cached), expiry isn't an issue in practice — Databricks refreshes before forwarding.

3. **OBO scope selection**: The scopes declared when enabling OBO in the UI must cover what the app needs (catalog API calls + SQL warehouse). If scopes are too narrow the token will be valid but operations will fail with permission denied.

4. **Generate endpoints with restricted tables**: If a user can read a table but their token doesn't have sufficient scope for the serving endpoint call, AI generation will fail. This is the SP's job — only the `get_table_details()` call within generate should use the user client; the actual AI call continues using the SP's serving endpoint credentials.
