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

    # Reference docs (per-schema UC Volume). Opt-in: empty volume_name disables the feature.
    reference_volume_name: str = ""
    reference_per_doc_max_chars: int = 8000
    reference_total_max_chars: int = 40000


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
            if "reference" in data and isinstance(data["reference"], dict):
                ref = data["reference"]
                if "volume_name" in ref:
                    cfg.reference_volume_name = str(ref["volume_name"] or "").strip()
                if "per_doc_max_chars" in ref:
                    cfg.reference_per_doc_max_chars = int(ref["per_doc_max_chars"])
                if "total_max_chars" in ref:
                    cfg.reference_total_max_chars = int(ref["total_max_chars"])
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

def get_user_workspace_client(token: str) -> WorkspaceClient:
    """Create a WorkspaceClient authenticated as the calling user.

    Used for browse and apply operations so Unity Catalog enforces the
    user's own permissions. Create per request — never cache.

    Uses a custom CredentialsStrategy to bypass env var credential detection,
    which would otherwise conflict with the app SP's DATABRICKS_CLIENT_ID/SECRET.
    """
    from databricks.sdk.credentials_provider import CredentialsStrategy

    bearer = f"Bearer {token}"

    class _OBOToken(CredentialsStrategy):
        def auth_type(self) -> str:
            return "obo-token"

        def __call__(self, cfg):
            return lambda: {"Authorization": bearer}

    return WorkspaceClient(host=get_workspace_host(), credentials_strategy=_OBOToken())


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
