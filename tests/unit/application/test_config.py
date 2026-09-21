"""
Tests for configuration loading and settings.
"""

import pytest
import yaml

from warp.application.config import (
    CatalogConfig,
    DatabaseConfig,
    PaginationConfig,
    Settings,
    SettingsConfig,
)
from warp.infrastructure.config_loader import interpolate_env_vars, load_config


class TestInterpolateEnvVars:
    """Tests for environment variable interpolation."""

    def test_simple_interpolation(self, monkeypatch):
        """Test simple ${VAR} interpolation."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        result = interpolate_env_vars("prefix_${TEST_VAR}_suffix")
        assert result == "prefix_test_value_suffix"

    def test_default_value(self, monkeypatch):
        """Test ${VAR:default} syntax."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        result = interpolate_env_vars("${NONEXISTENT_VAR:default_value}")
        assert result == "default_value"

    def test_empty_default(self, monkeypatch):
        """Test ${VAR:} with empty default."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        result = interpolate_env_vars("${NONEXISTENT_VAR:}")
        assert result == ""

    def test_env_overrides_default(self, monkeypatch):
        """Test that env var overrides default."""
        monkeypatch.setenv("MY_VAR", "from_env")
        result = interpolate_env_vars("${MY_VAR:default}")
        assert result == "from_env"

    def test_dict_interpolation(self, monkeypatch):
        """Test interpolation in nested dict."""
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_PORT", "5432")

        data = {"host": "${DB_HOST}", "port": "${DB_PORT}", "nested": {"value": "${DB_HOST}"}}
        result = interpolate_env_vars(data)

        assert result["host"] == "localhost"
        assert result["port"] == "5432"
        assert result["nested"]["value"] == "localhost"

    def test_list_interpolation(self, monkeypatch):
        """Test interpolation in list."""
        monkeypatch.setenv("ITEM", "test")

        data = ["${ITEM}", "static", "${ITEM}"]
        result = interpolate_env_vars(data)

        assert result == ["test", "static", "test"]

    def test_no_interpolation_needed(self):
        """Test that values without ${} are unchanged."""
        result = interpolate_env_vars("plain_string")
        assert result == "plain_string"

    def test_multiple_vars_in_string(self, monkeypatch):
        """Test multiple variables in single string."""
        monkeypatch.setenv("USER", "admin")
        monkeypatch.setenv("HOST", "localhost")

        result = interpolate_env_vars("${USER}@${HOST}")
        assert result == "admin@localhost"


class TestDatabaseConfig:
    """Tests for DatabaseConfig model."""

    def test_minimal_config(self):
        """Test with minimal required fields."""
        config = DatabaseConfig(name="test", type="postgresql", database="mydb", username="user")
        assert config.name == "test"
        assert config.type == "postgresql"
        assert config.host == "localhost"  # default
        assert config.port == 5432  # default
        assert config.password == ""  # default

    def test_full_config(self):
        """Test with all fields."""
        config = DatabaseConfig(
            name="prod",
            type="mysql",
            host="db.example.com",
            port=3306,
            database="production",
            username="app",
            password="secret123",
            options={"pool_size": 20},
        )
        assert config.host == "db.example.com"
        assert config.port == 3306
        assert config.options["pool_size"] == 20


class TestPaginationConfig:
    """Tests for PaginationConfig model."""

    def test_defaults(self):
        """Test default values."""
        config = PaginationConfig()
        assert config.default_limit == 50
        assert config.max_limit == 1000

    def test_custom_values(self):
        """Test custom values."""
        config = PaginationConfig(default_limit=25, max_limit=500)
        assert config.default_limit == 25
        assert config.max_limit == 500


class TestExportConfig:
    """Streaming export settings."""

    def test_defaults(self):
        from warp.application.config import ExportConfig

        cfg = ExportConfig()
        assert cfg.enabled is True
        assert cfg.max_rows == 0
        assert cfg.batch_size == 5000
        assert cfg.statement_timeout_ms == 0

    def test_nested_in_settings(self):
        from warp.application.config import SettingsConfig

        cfg = SettingsConfig(export={"enabled": False, "max_rows": 100})
        assert cfg.export.enabled is False
        assert cfg.export.max_rows == 100
        assert cfg.export.batch_size == 5000


class TestSettingsConfig:
    """Tests for SettingsConfig model."""

    def test_defaults(self):
        """Test default values."""
        config = SettingsConfig()
        assert config.auto_discover_tables is True
        assert config.excluded_tables == []
        assert config.enable_raw_query is False
        assert config.api_prefix == "/api/v1"

    def test_custom_values(self):
        """Test custom values."""
        config = SettingsConfig(
            auto_discover_tables=False,
            excluded_tables=["migrations", "sessions"],
            enable_raw_query=False,
            api_prefix="/api/v2",
        )
        assert config.auto_discover_tables is False
        assert "migrations" in config.excluded_tables
        assert config.enable_raw_query is False


class TestSettings:
    """Tests for main Settings model."""

    def test_empty_settings(self):
        """Test with no databases."""
        settings = Settings()
        assert settings.databases == []
        assert settings.settings is not None

    def test_with_database(self):
        """Test with database config."""
        settings = Settings(
            databases=[
                DatabaseConfig(name="test", type="postgresql", database="testdb", username="user")
            ]
        )
        assert len(settings.databases) == 1
        assert settings.databases[0].name == "test"


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_valid_config(self, temp_config_file):
        """Test loading a valid config file."""
        settings = load_config(temp_config_file)

        assert len(settings.databases) == 1
        assert settings.databases[0].name == "test_db"
        assert settings.databases[0].type == "postgresql"
        assert settings.settings.pagination.default_limit == 50

    def test_load_nonexistent_file(self):
        """Test loading nonexistent file raises error."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_env_interpolation_in_config(self, tmp_path, monkeypatch):
        """Test environment variable interpolation in config file."""
        monkeypatch.setenv("TEST_DB_HOST", "prod.db.com")
        monkeypatch.setenv("TEST_DB_PASS", "secret123")

        config_content = {
            "databases": [
                {
                    "name": "test",
                    "type": "postgresql",
                    "host": "${TEST_DB_HOST}",
                    "port": 5432,
                    "database": "mydb",
                    "username": "user",
                    "password": "${TEST_DB_PASS}",
                }
            ],
            "settings": {"auto_discover_tables": True},
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_content, f)

        settings = load_config(str(config_file))

        assert settings.databases[0].host == "prod.db.com"
        assert settings.databases[0].password == "secret123"

    def test_port_string_conversion(self, tmp_path, monkeypatch):
        """Test that string port from env is converted to int."""
        monkeypatch.setenv("DB_PORT", "5433")

        config_content = {
            "databases": [
                {
                    "name": "test",
                    "type": "postgresql",
                    "host": "localhost",
                    "port": "${DB_PORT}",
                    "database": "mydb",
                    "username": "user",
                    "password": "",
                }
            ]
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_content, f)

        settings = load_config(str(config_file))

        assert settings.databases[0].port == 5433
        assert isinstance(settings.databases[0].port, int)


class TestCatalogConfig:
    """Tests for CatalogConfig model."""

    def test_defaults(self):
        """Test default values."""
        config = CatalogConfig()
        assert config.storage_path == "./catalogs"
        assert config.default_format == "json"
        assert config.auto_cross_reference is True
        assert config.auto_enrich_openapi is True
        assert config.openapi_enrichment_lang == "en"

    def test_custom_values(self):
        """Test custom values."""
        config = CatalogConfig(
            storage_path="/tmp/catalogs",
            auto_enrich_openapi=False,
            openapi_enrichment_lang="tr",
        )
        assert config.storage_path == "/tmp/catalogs"
        assert config.auto_enrich_openapi is False
        assert config.openapi_enrichment_lang == "tr"

    def test_catalog_config_in_settings(self):
        """Test CatalogConfig is accessible through SettingsConfig."""
        settings = SettingsConfig(
            catalog={
                "auto_enrich_openapi": True,
                "openapi_enrichment_lang": "de",
            }
        )
        assert settings.catalog.auto_enrich_openapi is True
        assert settings.catalog.openapi_enrichment_lang == "de"

    def test_catalog_config_from_yaml(self, tmp_path):
        """Test CatalogConfig loads correctly from YAML config."""
        config_content = {
            "databases": [
                {
                    "name": "test",
                    "type": "postgresql",
                    "host": "localhost",
                    "port": 5432,
                    "database": "mydb",
                    "username": "user",
                }
            ],
            "settings": {
                "catalog": {
                    "storage_path": "./my_catalogs",
                    "auto_enrich_openapi": False,
                    "openapi_enrichment_lang": "tr",
                }
            },
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_content, f)

        settings = load_config(str(config_file))
        assert settings.settings.catalog.storage_path == "./my_catalogs"
        assert settings.settings.catalog.auto_enrich_openapi is False
        assert settings.settings.catalog.openapi_enrichment_lang == "tr"


class TestPIIPatternsSingleSource:
    def test_config_default_matches_domain_default(self):
        from warp.application.config import DEFAULT_PII_PATTERNS, AnalysisConfig
        from warp.domain import samples

        assert AnalysisConfig().pii_column_patterns == list(DEFAULT_PII_PATTERNS)
        assert samples.DEFAULT_PII_PATTERNS is DEFAULT_PII_PATTERNS
        assert "social_security" in DEFAULT_PII_PATTERNS


class TestProductionValidation:
    def _safe(self):
        from warp.application.config import ApiKeyConfig, Settings

        s = Settings()
        s.settings.auth.enabled = True
        s.settings.auth.api_keys = [ApiKeyConfig(key="k", permissions=["all"])]
        s.settings.auth.public_paths = ["/health"]
        return s

    def test_non_production_never_reports(self):
        from warp.application.config import Settings, validate_production_config

        assert validate_production_config(Settings(), "development", ["*"]) == []

    def test_safe_production_config_has_no_violations(self):
        from warp.application.config import validate_production_config

        assert validate_production_config(self._safe(), "production", ["https://a"]) == []

    def test_public_openapi_is_a_violation_by_default(self):
        from warp.application.config import validate_production_config

        s = self._safe()
        s.settings.auth.public_paths = ["/health", "/openapi.json"]
        violations = validate_production_config(s, "production", ["https://a"])
        assert len(violations) == 1
        assert "/openapi.json" in violations[0]
        assert "allow_public_openapi" in violations[0]

    def test_public_openapi_can_be_accepted_explicitly(self):
        from warp.application.config import validate_production_config

        s = self._safe()
        s.settings.auth.public_paths = ["/health", "/openapi.json"]
        s.settings.auth.allow_public_openapi = True
        assert validate_production_config(s, "production", ["https://a"]) == []

    def test_row_filters_without_auth_are_a_violation(self):
        # Nobody would be identified, so the rules would restrict nothing —
        # the most dangerous kind of misconfiguration: silently permissive.
        from warp.application.config import ApiKeyConfig, validate_production_config

        s = self._safe()
        s.settings.auth.enabled = False
        s.settings.auth.api_keys = [
            ApiKeyConfig(
                key="k",
                name="acme",
                tenant="acme",
                row_filters={"orders": [{"column": "tenant_id", "value": "${tenant}"}]},
            )
        ]
        joined = " ".join(validate_production_config(s, "production", ["https://a"]))
        assert "row_filters" in joined

    def test_row_filters_with_auth_enabled_are_fine(self):
        from warp.application.config import ApiKeyConfig, validate_production_config

        s = self._safe()
        s.settings.auth.api_keys = [
            ApiKeyConfig(
                key="k",
                name="acme",
                tenant="acme",
                permissions=["all"],
                row_filters={"orders": [{"column": "tenant_id", "value": "${tenant}"}]},
            )
        ]
        assert validate_production_config(s, "production", ["https://a"]) == []

    def test_default_config_in_production_lists_every_problem(self):
        from warp.application.config import Settings, validate_production_config

        violations = validate_production_config(Settings(), "production", ["*"])
        joined = " ".join(violations)
        assert "auth.enabled" in joined
        assert "CORS_ORIGINS" in joined
        assert "/openapi.json" in joined


class TestSettingsHelpers:
    def test_database_lookup(self):
        from warp.application.config import DatabaseConfig, Settings
        from warp.domain.errors import DatabaseNotConfiguredError

        s = Settings(
            databases=[DatabaseConfig(name="a", type="postgresql", database="x", username="u")]
        )
        assert s.database("a").name == "a"
        with pytest.raises(DatabaseNotConfiguredError) as exc:
            s.database("nope")
        assert exc.value.status_code == 404
        assert exc.value.details["available"] == ["a"]

    @pytest.mark.parametrize(
        "provider,cloud",
        [("openai", True), ("Anthropic", True), ("gemini", True), ("ollama", False)],
    )
    def test_is_cloud_provider(self, provider, cloud):
        from warp.application.config import LLMConfig

        assert LLMConfig(provider=provider).is_cloud_provider is cloud


class TestHashMaskingNeedsAKey:
    """A `hash` rule with no key would not actually mask the column.

    An unkeyed digest of an email or an 11-digit national id is reversible by
    enumeration, so the two tempting fallbacks — skip the rule, or hash
    unkeyed — both leave PII readable while reporting that it is masked.
    """

    def _with_masking(self, **masking):
        from warp.application.config import ApiKeyConfig, Settings

        s = Settings()
        s.settings.auth.enabled = True
        s.settings.auth.api_keys = [ApiKeyConfig(key="k", permissions=["all"])]
        s.settings.auth.public_paths = ["/health"]
        s.settings.masking.enabled = True
        for name, value in masking.items():
            setattr(s.settings.masking, name, value)
        return s

    def test_hash_without_a_secret_will_not_parse(self):
        """Refused where the configuration is read, not where it is applied.

        Checking it inside `_masking_for` made the refusal depend on
        `auto_discover_tables` being on and on the environment being
        production; a misconfiguration is neither.
        """
        from pydantic import ValidationError

        from warp.application.config import MaskingConfig

        with pytest.raises(ValidationError, match="hash_secret"):
            MaskingConfig(enabled=True, rules={"email": "hash"})

    def test_hash_hidden_in_by_role_will_not_parse_either(self):
        """`by_role` is the source that gets forgotten."""
        from pydantic import ValidationError

        from warp.application.config import MaskingConfig

        with pytest.raises(ValidationError, match="hash_secret"):
            MaskingConfig(
                enabled=True, rules={"email": "redact"}, by_role={"support": {"phone": "hash"}}
            )

    def test_a_hash_rule_is_fine_while_masking_is_off(self):
        """Nothing is masked, so nothing needs a key."""
        from warp.application.config import MaskingConfig

        assert MaskingConfig(enabled=False, rules={"email": "hash"}).hash_secret is None

    def test_hash_with_a_secret_is_fine(self):
        from warp.application.config import validate_production_config

        settings = self._with_masking(rules={"email": "hash"}, hash_secret="x" * 40)
        assert validate_production_config(settings, "production", ["https://a"]) == []

    def test_a_short_secret_is_a_violation(self):
        """A passphrase somebody chose is itself dictionary-attackable."""
        from warp.application.config import validate_production_config

        settings = self._with_masking(rules={"email": "hash"}, hash_secret="hunter2")
        violations = validate_production_config(settings, "production", ["https://a"])
        assert any("shorter than" in v for v in violations)

    def test_other_strategies_need_no_secret(self):
        from warp.application.config import validate_production_config

        settings = self._with_masking(rules={"email": "partial", "phone": "last4"})
        assert validate_production_config(settings, "production", ["https://a"]) == []

    def test_masking_switched_off_is_not_checked(self):
        from warp.application.config import validate_production_config

        settings = self._with_masking(rules={"email": "hash"})
        settings.settings.masking.enabled = False
        assert validate_production_config(settings, "production", ["https://a"]) == []
