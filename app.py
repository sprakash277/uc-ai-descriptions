"""UC AI Descriptions — Databricks App entry point."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from server.config import app_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup validation on app boot."""
    from server.audit import validate_audit_setup
    validate_audit_setup()
    yield


app = FastAPI(title=app_config.app_title, lifespan=lifespan)

from server.routes import router
app.include_router(router)

# Serve static frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(os.path.join(static_dir, "index.html"))
