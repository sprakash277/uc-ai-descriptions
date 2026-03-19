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
