"""Configuration and authentication for UC AI Descriptions."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

# ── App Configuration ────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """App-level configuration loaded from config.yaml + env vars."""
    # Infrastructure (from env vars / databricks.yml)
    warehouse_id: str = ""
    serving_endpoint: str = "databricks-claude-sonnet-4-6"
    app_title: str = "Unity Catalog AI Descriptions"

    # App behavior (from config.yaml)
    responsible_ai_rules: str = ""
    audit_table_name: str = "_ai_description_audit"
    excluded_catalogs: list[str] = field(default_factory=lambda: ["__databricks_internal", "system"])
    excluded_schemas: list[str] = field(default_factory=lambda: ["information_schema"])


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load config from config.yaml (app behavior) and env vars (infrastructure)."""
    config = AppConfig()

    # Layer 1: Load config.yaml (resolve relative to project root, not CWD)
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "")
    if config_path:
        path = Path(config_path)
    else:
        path = Path(__file__).resolve().parent.parent / "config.yaml"

    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        config.responsible_ai_rules = data.get("responsible_ai_rules", "").strip()
        audit_cfg = data.get("audit", {})
        if audit_cfg.get("table_name"):
            config.audit_table_name = audit_cfg["table_name"]
        exclusions = data.get("exclusions", {})
        if "catalogs" in exclusions:
            config.excluded_catalogs = exclusions["catalogs"]
        if "schemas" in exclusions:
            config.excluded_schemas = exclusions["schemas"]

    # Layer 2: Env vars override (from databricks.yml variables)
    config.warehouse_id = os.environ.get("WAREHOUSE_ID", config.warehouse_id)
    config.serving_endpoint = os.environ.get("SERVING_ENDPOINT", config.serving_endpoint)
    config.app_title = os.environ.get("APP_TITLE", config.app_title)

    return config


# Singleton config — loaded once at import time
app_config = load_config()


# ── Authentication ───────────────────────────────────────────────────────

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
