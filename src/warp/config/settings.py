"""
Configuration loader with YAML support and environment variable interpolation.
"""
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """Database connection configuration."""
    name: str
    type: str  # postgresql, mysql
    host: str = "localhost"
    port: int = 5432
    database: str
    username: str
    password: str = ""
    options: Dict[str, Any] = Field(default_factory=dict)


class PaginationConfig(BaseModel):
    """Pagination settings."""
    default_limit: int = 50
    max_limit: int = 1000


class ApiKeyConfig(BaseModel):
    """API Key configuration with permissions."""
    key: str
    name: str = "default"
    permissions: List[str] = Field(default_factory=lambda: ["read"])
    # Permissions: read, create, update, delete, query (for raw queries)
    # Or use "all" for full access


class AuthConfig(BaseModel):
    """Authentication configuration."""
    enabled: bool = False
    header_name: str = "X-API-Key"
    api_keys: List[ApiKeyConfig] = Field(default_factory=list)
    # Public endpoints that don't require auth (e.g., health check)
    public_paths: List[str] = Field(default_factory=lambda: ["/health", "/docs", "/redoc", "/openapi.json"])


class CatalogConfig(BaseModel):
    """Catalog storage configuration."""
    storage_path: str = "./catalogs"
    default_format: str = "json"
    auto_cross_reference: bool = True
    auto_enrich_openapi: bool = True
    openapi_enrichment_lang: str = "en"


class AnalysisConfig(BaseModel):
    """Schema analysis configuration."""
    sample_limit: int = 5
    include_row_count: bool = True
    excluded_tables: List[str] = Field(default_factory=list)
    excluded_schemas: List[str] = Field(
        default_factory=lambda: ["information_schema", "pg_catalog"]
    )


class LLMConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    language_prompts: Dict[str, str] = Field(default_factory=dict)


class I18nConfig(BaseModel):
    """Internationalization configuration."""
    default_language: str = "en"
    languages: List[str] = Field(default_factory=lambda: ["en"])
    fallback_language: str = "en"
    auto_translate: bool = True
    translation_strategy: str = "single"  # single | multi


class SettingsConfig(BaseModel):
    """Application settings."""
    auto_discover_tables: bool = True
    excluded_tables: List[str] = Field(default_factory=list)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    enable_raw_query: bool = False  # default off; opt-in only, refused in production
    raw_query_whitelist: List[str] = Field(default_factory=lambda: ["SELECT"])
    api_prefix: str = "/api/v1"
    docs_url: str = "/docs"
    redoc_url: str = "/redoc"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)


class Settings(BaseModel):
    """Main configuration container."""
    databases: List[DatabaseConfig] = Field(default_factory=list)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)


def interpolate_env_vars(value: Any) -> Any:
    """
    Recursively interpolate environment variables in configuration values.
    Supports ${VAR_NAME} and ${VAR_NAME:default} syntax.
    """
    if isinstance(value, str):
        pattern = r'\$\{([^}:]+)(?::([^}]*))?\}'

        def replacer(match):
            var_name = match.group(1)
            default_value = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default_value)

        return re.sub(pattern, replacer, value)

    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]

    return value


def load_config(config_path: Optional[str] = None) -> Settings:
    """
    Load configuration from YAML file with environment variable interpolation.

    Args:
        config_path: Path to the YAML configuration file.
                    Defaults to config/database.yaml relative to project root.

    Returns:
        Settings object with loaded configuration.
    """
    if config_path is None:
        # Default to config/database.yaml relative to project root
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "database.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    # Interpolate environment variables
    interpolated_config = interpolate_env_vars(raw_config)

    # Convert port values to integers if they came from env vars as strings
    if "databases" in interpolated_config:
        for db in interpolated_config["databases"]:
            if "port" in db and isinstance(db["port"], str):
                db["port"] = int(db["port"])

    return Settings(**interpolated_config)


# Global settings instance (initialized on first access)
_settings: Optional[Settings] = None


def get_settings(config_path: Optional[str] = None) -> Settings:
    """
    Get or initialize the global settings instance.

    Args:
        config_path: Optional path to configuration file.

    Returns:
        Settings instance.
    """
    global _settings
    if _settings is None:
        _settings = load_config(config_path)
    return _settings


def reload_settings(config_path: Optional[str] = None) -> Settings:
    """
    Reload settings from configuration file.

    Args:
        config_path: Optional path to configuration file.

    Returns:
        New Settings instance.
    """
    global _settings
    _settings = load_config(config_path)
    return _settings


def validate_production_config(
    settings: Settings,
    app_env: str,
    cors_origins: Optional[List[str]] = None,
) -> List[str]:
    """
    Check security-sensitive configuration for production deployments.

    Returns a list of human-readable violation messages. The list is only
    populated when ``app_env == "production"``; in any other environment the
    same options are legitimate defaults and no violations are reported.

    The caller is expected to refuse startup (fail-safe) when the returned
    list is non-empty.

    Args:
        settings: Loaded application settings.
        app_env: The current ``APP_ENV`` value.
        cors_origins: The effective CORS allowlist (e.g. from ``CORS_ORIGINS``).

    Returns:
        A list of violation messages (empty when the configuration is safe).
    """
    violations: List[str] = []

    if app_env != "production":
        return violations

    cfg = settings.settings

    if not cfg.auth.enabled:
        violations.append(
            "auth.enabled is false: every endpoint would be public. "
            "Enable authentication for production."
        )

    if cfg.enable_raw_query:
        violations.append(
            "settings.enable_raw_query is true: the raw SQL endpoint would be "
            "exposed. Set enable_raw_query: false for production."
        )

    if cors_origins and any(origin.strip() == "*" for origin in cors_origins):
        violations.append(
            "CORS_ORIGINS allows '*': any origin could call the API. "
            "Set CORS_ORIGINS to an explicit, comma-separated allowlist."
        )

    return violations
