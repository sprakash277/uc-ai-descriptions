"""API routes for the UC AI Descriptions App."""

import logging
import traceback
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import app_config, get_workspace_client
from . import catalog, ai_gen, audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# ── Browse endpoints ─────────────────────────────────────────────────────

@router.get("/catalogs")
async def get_catalogs():
    try:
        return {"catalogs": catalog.list_catalogs()}
    except Exception as e:
        logger.error("List catalogs failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/{catalog_name}")
async def get_schemas(catalog_name: str):
    try:
        return {"schemas": catalog.list_schemas(catalog_name)}
    except Exception as e:
        logger.error("List schemas failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tables/{catalog_name}/{schema_name}")
async def get_tables(catalog_name: str, schema_name: str):
    try:
        return {"tables": catalog.list_tables(catalog_name, schema_name)}
    except Exception as e:
        logger.error("List tables failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/table/{full_name:path}")
async def get_table_details(full_name: str):
    try:
        return {"table": catalog.get_table_details(full_name)}
    except Exception as e:
        logger.error("Get table details failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── AI Generation (single table) ────────────────────────────────────────

class GenerateRequest(BaseModel):
    full_name: str
    model: Optional[str] = None


@router.post("/generate")
async def generate_descriptions(req: GenerateRequest):
    """Generate AI descriptions for a table and its columns."""
    try:
        table_info = catalog.get_table_details(req.full_name)
        suggestions = ai_gen.generate_descriptions(table_info, model=req.model)
        return {
            "status": "success",
            "table_full_name": req.full_name,
            "current_table_comment": table_info["comment"],
            "suggestions": suggestions,
            "columns": table_info["columns"],
        }
    except Exception as e:
        logger.error("Generate failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Batch Generation (entire schema) ────────────────────────────────────

class BatchGenerateRequest(BaseModel):
    catalog_name: str
    schema_name: str
    model: Optional[str] = None


@router.post("/generate/batch")
async def batch_generate_descriptions(req: BatchGenerateRequest):
    """Generate AI descriptions for ALL tables in a schema."""
    try:
        tables = catalog.list_tables(req.catalog_name, req.schema_name)
        results = []
        errors = []

        for t in tables:
            if t["name"].startswith("_ai_"):
                continue
            try:
                table_info = catalog.get_table_details(t["full_name"])
                suggestions = ai_gen.generate_descriptions(table_info, model=req.model)
                results.append({
                    "full_name": t["full_name"],
                    "table_name": t["name"],
                    "current_comment": table_info["comment"],
                    "suggestions": suggestions,
                    "columns": table_info["columns"],
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
    ai_table_suggestion: Optional[str] = None
    ai_column_suggestions: dict[str, str] = {}
    current_table_comment: Optional[str] = None
    current_column_comments: dict[str, str] = {}


@router.post("/apply/table")
async def apply_table_comment(req: ApplyTableCommentRequest):
    try:
        success = catalog.apply_table_comment(req.full_name, req.comment)
        return {"status": "success" if success else "failed", "full_name": req.full_name}
    except Exception as e:
        logger.error("Apply table comment failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply/column")
async def apply_column_comment(req: ApplyColumnCommentRequest):
    try:
        success = catalog.apply_column_comment(req.full_name, req.column_name, req.comment)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        logger.error("Apply column comment failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply/batch")
async def apply_batch(req: ApplyBatchRequest):
    """Apply approved table and column descriptions in one call, with audit logging."""
    results = {"table": None, "columns": {}, "errors": []}
    audit_actions = []

    # Parse catalog/schema from full_name for co-located audit
    try:
        cat, sch, _ = audit.parse_full_name(req.full_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if req.table_comment:
            try:
                success = catalog.apply_table_comment(req.full_name, req.table_comment)
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
                success = catalog.apply_column_comment(req.full_name, col_name, comment)
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

        # Write audit log (co-located with described tables)
        if audit_actions:
            try:
                audit.ensure_audit_table(cat, sch)
                logged = audit.log_batch(cat, sch, req.full_name, audit_actions)
                results["audit_logged"] = logged
            except Exception as e:
                logger.error("Audit logging failed: %s", e)
                results["audit_logged"] = 0

        return {"status": "success", "results": results}
    except Exception as e:
        logger.error("Batch apply failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Rules (read-only from config.yaml) ────────────────────────────────

@router.get("/rules")
async def get_rules():
    """Return Responsible AI rules from config.yaml (read-only)."""
    return {"rules": app_config.responsible_ai_rules}


# ── Settings & Warehouses ────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    """Return current effective app configuration."""
    return {
        "app_title": app_config.app_title,
        "serving_endpoint": app_config.serving_endpoint,
        "warehouse_id": app_config.warehouse_id or "(auto-detect)",
        "audit_table_name": app_config.audit_table_name,
        "excluded_catalogs": app_config.excluded_catalogs,
        "excluded_schemas": app_config.excluded_schemas,
        "responsible_ai_rules": app_config.responsible_ai_rules,
    }


@router.get("/warehouses")
async def list_warehouses():
    """List available SQL warehouses with their state."""
    try:
        w = get_workspace_client()
        warehouses = list(w.warehouses.list())
        return {
            "warehouses": [
                {
                    "id": wh.id,
                    "name": wh.name,
                    "state": str(wh.state) if wh.state else "UNKNOWN",
                    "warehouse_type": str(wh.warehouse_type) if wh.warehouse_type else "",
                }
                for wh in warehouses
            ]
        }
    except Exception as e:
        logger.error("List warehouses failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ── Notebook Export ──────────────────────────────────────────────────────

class NotebookExportRequest(BaseModel):
    catalog_name: str
    schema_name: str


@router.post("/export-notebook")
async def export_notebook(req: NotebookExportRequest):
    """Generate a downloadable Databricks notebook for automated description generation."""
    try:
        code = ai_gen.generate_notebook_code(req.catalog_name, req.schema_name)
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
async def get_audit_log(
    catalog_name: str = Query(..., description="Catalog containing the audit table"),
    schema_name: str = Query(..., description="Schema containing the audit table"),
    table: Optional[str] = None,
    limit: int = 50,
):
    """Retrieve audit log from the co-located audit table in the specified catalog.schema."""
    try:
        audit.ensure_audit_table(catalog_name, schema_name)
        entries = audit.get_audit_log(catalog_name, schema_name, full_table_name=table, limit=limit)
        return {"entries": entries, "count": len(entries)}
    except Exception as e:
        logger.error("Audit log query failed: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health():
    return {"status": "ok", "service": "uc-ai-descriptions"}
