"""API routes for the UC AI Descriptions App."""

import logging
import traceback
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from databricks.sdk.errors import PermissionDenied

from . import catalog, ai_gen, audit, reference
from .config import app_config, get_workspace_client, get_user_workspace_client
from .identity import get_current_user, get_user_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def get_request_client(request: Request):
    """FastAPI dependency: user WorkspaceClient when OBO token is present,
    otherwise the app service principal client (graceful degradation).

    NOTE: OBO must be enabled in the Databricks Apps UI before the user
    token will be forwarded. Until then all requests use the SP client.
    """
    token = get_user_token(request)
    if token:
        return get_user_workspace_client(token)
    return get_workspace_client()


# ── Browse endpoints ─────────────────────────────────────────────────────

@router.get("/catalogs")
async def get_catalogs(w=Depends(get_request_client)):
    try:
        return {"catalogs": catalog.list_catalogs(w=w)}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("List catalogs failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/{catalog_name}")
async def get_schemas(catalog_name: str, w=Depends(get_request_client)):
    try:
        return {"schemas": catalog.list_schemas(catalog_name, w=w)}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("List schemas failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tables/{catalog_name}/{schema_name}")
async def get_tables(catalog_name: str, schema_name: str, w=Depends(get_request_client)):
    try:
        return {"tables": catalog.list_tables(catalog_name, schema_name, w=w)}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("List tables failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/table/{full_name:path}")
async def get_table_details(full_name: str, w=Depends(get_request_client)):
    try:
        return {"table": catalog.get_table_details(full_name, w=w)}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("Get table details failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── AI Generation (single table) ────────────────────────────────────────

class GenerateRequest(BaseModel):
    full_name: str
    model: Optional[str] = None
    rules_override: Optional[str] = None  # Per-session rules; None = use org rules from config.yaml


def _retrieve_reference_chunks(table_info: dict) -> list[dict]:
    """Retrieve top-k reference chunks if the reference service is enabled; else []."""
    svc = reference.get_reference_service()
    if svc is None:
        return []
    try:
        return svc.retrieve(table_info, top_k=app_config.reference_top_k)
    except Exception as e:
        logger.warning("Reference retrieval failed: %s", e)
        return []


@router.post("/generate")
async def generate_descriptions(req: GenerateRequest, w=Depends(get_request_client)):
    """Generate AI descriptions for a table and its columns."""
    try:
        table_info = catalog.get_table_details(req.full_name, w=w)
        ref_chunks = _retrieve_reference_chunks(table_info)
        suggestions = ai_gen.generate_descriptions(
            table_info,
            model=req.model,
            rules_override=req.rules_override,
            reference_context=ref_chunks,
        )
        return {
            "status": "success",
            "table_full_name": req.full_name,
            "current_table_comment": table_info["comment"],
            "suggestions": suggestions,
            "columns": table_info["columns"],
            "sources": suggestions.get("sources", []),
        }
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("Generate failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── AI Generation (single item — table or one column) ───────────────────

class GenerateItemRequest(BaseModel):
    full_name: str
    item_name: Optional[str] = None  # None = table description; column name = column description
    model: Optional[str] = None
    rules_override: Optional[str] = None


@router.post("/generate/item")
async def generate_item_description(req: GenerateItemRequest, w=Depends(get_request_client)):
    """Re-generate AI description for a single table or column (using full table context)."""
    try:
        table_info = catalog.get_table_details(req.full_name, w=w)
        ref_chunks = _retrieve_reference_chunks(table_info)
        suggestions = ai_gen.generate_descriptions(
            table_info,
            model=req.model,
            rules_override=req.rules_override,
            reference_context=ref_chunks,
        )
        if req.item_name is None:
            description = suggestions["table_description"]
        else:
            description = suggestions["column_descriptions"].get(req.item_name, "")
        return {"status": "success", "description": description, "sources": suggestions.get("sources", [])}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("Generate item failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Batch Generation (entire schema) ────────────────────────────────────

class BatchGenerateRequest(BaseModel):
    catalog_name: str
    schema_name: str
    model: Optional[str] = None
    rules_override: Optional[str] = None  # Per-session rules; None = use org rules from config.yaml


@router.post("/generate/batch")
async def batch_generate_descriptions(req: BatchGenerateRequest, w=Depends(get_request_client)):
    """Generate AI descriptions for ALL tables in a schema."""
    try:
        tables = catalog.list_tables(req.catalog_name, req.schema_name, w=w)
        results = []
        errors = []

        for t in tables:
            # Skip audit tables
            if t["name"].startswith("_ai_"):
                continue
            try:
                table_info = catalog.get_table_details(t["full_name"], w=w)
                ref_chunks = _retrieve_reference_chunks(table_info)
                suggestions = ai_gen.generate_descriptions(
                    table_info,
                    model=req.model,
                    rules_override=req.rules_override,
                    reference_context=ref_chunks,
                )
                results.append({
                    "full_name": t["full_name"],
                    "table_name": t["name"],
                    "current_comment": table_info["comment"],
                    "suggestions": suggestions,
                    "columns": table_info["columns"],
                    "sources": suggestions.get("sources", []),
                })
            except Exception as e:
                errors.append({"table": t["full_name"], "error": str(e)})
                logger.error("Batch generate failed for %s: %s", t["full_name"], e)

        return {
            "status": "success",
            "catalog": req.catalog_name,
            "schema": req.schema_name,
            "tables_processed": len(results),
            "tables_failed": len(errors),
            "results": results,
            "errors": errors,
        }
    except Exception as e:
        logger.error("Batch generate failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Apply descriptions ───────────────────────────────────────────────────

class ApplyTableCommentRequest(BaseModel):
    full_name: str
    comment: str


class ApplyColumnCommentRequest(BaseModel):
    full_name: str
    column_name: str
    comment: str


class ApplyBatchRequest(BaseModel):
    full_name: str
    table_comment: Optional[str] = None
    column_comments: dict[str, str] = {}
    # Audit info
    ai_table_suggestion: Optional[str] = None
    ai_column_suggestions: dict[str, str] = {}
    current_table_comment: Optional[str] = None
    current_column_comments: dict[str, str] = {}


@router.post("/apply/table")
async def apply_table_comment(req: ApplyTableCommentRequest, w=Depends(get_request_client)):
    try:
        success = catalog.apply_table_comment(req.full_name, req.comment, w=w)
        return {"status": "success" if success else "failed", "full_name": req.full_name}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("Apply table comment failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply/column")
async def apply_column_comment(req: ApplyColumnCommentRequest, w=Depends(get_request_client)):
    try:
        success = catalog.apply_column_comment(req.full_name, req.column_name, req.comment, w=w)
        return {"status": "success" if success else "failed"}
    except PermissionDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error("Apply column comment failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply/batch")
async def apply_batch(req: ApplyBatchRequest,
                      w=Depends(get_request_client),
                      current_user: str = Depends(get_current_user)):
    """Apply approved table and column descriptions in one call, with audit logging."""
    results = {"table": None, "columns": {}, "errors": []}
    audit_actions = []

    try:
        if req.table_comment:
            try:
                success = catalog.apply_table_comment(req.full_name, req.table_comment, w=w)
                results["table"] = "success" if success else "failed"
                if success:
                    audit_actions.append({
                        "item_type": "TABLE",
                        "item_name": req.full_name.split(".")[-1],
                        "previous": req.current_table_comment or "",
                        "ai_suggested": req.ai_table_suggestion or req.table_comment,
                        "final": req.table_comment,
                        "action": "approved" if req.table_comment == (req.ai_table_suggestion or "") else "edited",
                    })
            except Exception as e:
                results["table"] = "failed"
                results["errors"].append(f"Table: {e}")

        for col_name, comment in req.column_comments.items():
            try:
                success = catalog.apply_column_comment(req.full_name, col_name, comment, w=w)
                results["columns"][col_name] = "success" if success else "failed"
                if success:
                    ai_orig = req.ai_column_suggestions.get(col_name, comment)
                    audit_actions.append({
                        "item_type": "COLUMN",
                        "item_name": col_name,
                        "previous": req.current_column_comments.get(col_name, ""),
                        "ai_suggested": ai_orig,
                        "final": comment,
                        "action": "approved" if comment == ai_orig else "edited",
                    })
            except Exception as e:
                results["columns"][col_name] = "failed"
                results["errors"].append(f"Column {col_name}: {e}")

        # Write audit log
        if audit_actions:
            try:
                audit.ensure_audit_table()
                logged = audit.log_batch(req.full_name, audit_actions, applied_by=current_user)
                results["audit_logged"] = logged
            except Exception as e:
                logger.error("Audit logging failed: %s", e)
                results["audit_logged"] = 0

        return {"status": "success", "results": results}
    except Exception as e:
        logger.error("Batch apply failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Custom Rules (Responsible AI) ────────────────────────────────────────

@router.get("/rules")
async def get_rules():
    return {"rules": app_config.responsible_ai_rules}


# ── Notebook Export ──────────────────────────────────────────────────────

class NotebookExportRequest(BaseModel):
    catalog_name: str
    schema_name: str


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


# ── Audit Log ────────────────────────────────────────────────────────────

@router.get("/audit")
async def get_audit_log(table: Optional[str] = None, limit: int = 50):
    try:
        audit.ensure_audit_table()
        entries = audit.get_audit_log(full_table_name=table, limit=limit)
        return {"entries": entries, "count": len(entries)}
    except Exception as e:
        logger.error("Audit log query failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Settings + Warehouse Info ─────────────────────────────────────────

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


@router.get("/health")
async def health():
    return {"status": "ok", "service": "uc-ai-descriptions"}
