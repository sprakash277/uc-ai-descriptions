"""Tests for config loading logic."""

import os
import tempfile

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
                "audit": {"table": "my_catalog.my_schema.my_audit"},
                "exclusions": {
                    "catalogs": ["hidden_catalog"],
                    "schemas": ["hidden_schema"],
                },
            }
            path = _write_config(tmp, data)
            cfg = load_config(path)
            assert cfg.responsible_ai_rules == "No PII allowed."
            assert cfg.audit_table == "my_catalog.my_schema.my_audit"
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
