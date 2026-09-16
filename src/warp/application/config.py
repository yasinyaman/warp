"""Application configuration models and production safety validation."""

from typing import Any

from pydantic import BaseModel, Field

from warp.domain.errors import DatabaseNotConfiguredError
from warp.domain.samples import DEFAULT_PII_PATTERNS


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    name: str
    type: str  # postgresql, mysql
    host: str = "localhost"
    port: int = 5432
    database: str
    username: str
    password: str = ""
    options: dict[str, Any] = Field(default_factory=dict)
    # Optional read-only credentials used ONLY by the raw SQL endpoint, so that
    # even a bypass of the command whitelist cannot mutate data. Leave empty to
    # reuse the main (read-write) connection.
    readonly_username: str = ""
    readonly_password: str = ""

    def readonly_config(self) -> dict[str, Any] | None:
        """Build an adapter config for the read-only connection, or None.

        Returns a copy of this config with the username/password swapped for the
        read-only credentials, or None when no read-only user is configured.
        """
        if not self.readonly_username:
            return None
        cfg = self.model_dump()
        cfg["name"] = f"{self.name}__readonly"
        cfg["username"] = self.readonly_username
        cfg["password"] = self.readonly_password
        return cfg


class PaginationConfig(BaseModel):
    """Pagination settings."""

    default_limit: int = 50
    max_limit: int = 1000


class ApiKeyConfig(BaseModel):
    """API Key configuration with permissions."""

    key: str
    name: str = "default"
    permissions: list[str] = Field(default_factory=lambda: ["read"])
    # Permissions: read, create, update, delete, query (for raw queries)
    # Or use "all" for full access


class AuthConfig(BaseModel):
    """Authentication configuration."""

    enabled: bool = False
    header_name: str = "X-API-Key"
    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
    # Public endpoints that don't require auth (e.g., health check)
    public_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/docs", "/redoc", "/openapi.json"]
    )
    # The enriched OpenAPI spec exposes column descriptions and x-llm-context.
    # In production, startup is refused while "/openapi.json" is public unless
    # this is explicitly set to true (see validate_production_config).
    allow_public_openapi: bool = False


class CatalogConfig(BaseModel):
    """Catalog storage configuration."""

    storage_path: str = "./catalogs"
    default_format: str = "json"
    auto_cross_reference: bool = True
    auto_enrich_openapi: bool = True
    openapi_enrichment_lang: str = "en"
    # Inject real sample values from the database into the OpenAPI spec as
    # `example`/`examples`/`x-llm-context.examples`. Off by default: the spec
    # is often reachable without a key and samples may contain sensitive data.
    openapi_include_examples: bool = False


class AnalysisConfig(BaseModel):
    """Schema analysis configuration."""

    sample_limit: int = 5
    include_row_count: bool = True
    excluded_tables: list[str] = Field(default_factory=list)
    excluded_schemas: list[str] = Field(
        default_factory=lambda: ["information_schema", "pg_catalog"]
    )
    # Privacy: sending raw sample rows to a *cloud* LLM is opt-in. When False,
    # samples are still read for stats/catalog but not sent to cloud providers.
    share_samples_with_cloud_llm: bool = False
    # Mask PII-looking column values before sending samples to any LLM.
    mask_pii_samples: bool = True
    pii_column_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_PII_PATTERNS))


# Providers that send prompt data off the local machine to a third-party API.
# (Ollama runs locally and is intentionally excluded.)
CLOUD_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini"})


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    language_prompts: dict[str, str] = Field(default_factory=dict)

    @property
    def is_cloud_provider(self) -> bool:
        """Whether prompts (and any sample data) leave the local machine."""
        return self.provider.lower() in CLOUD_PROVIDERS


class I18nConfig(BaseModel):
    """Internationalization configuration."""

    default_language: str = "en"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    fallback_language: str = "en"
    auto_translate: bool = True
    translation_strategy: str = "single"  # single | multi


class SettingsConfig(BaseModel):
    """Application settings."""

    auto_discover_tables: bool = True
    excluded_tables: list[str] = Field(default_factory=list)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    enable_raw_query: bool = False  # default off; opt-in only, refused in production
    raw_query_whitelist: list[str] = Field(default_factory=lambda: ["SELECT"])
    # Columns clients may never write (mass-assignment protection). The primary
    # key and auto-generated columns are always protected in addition to these.
    readonly_columns: list[str] = Field(default_factory=lambda: ["created_at", "updated_at"])
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

    databases: list[DatabaseConfig] = Field(default_factory=list)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)

    def database(self, name: str) -> DatabaseConfig:
        """Return the configuration of the database called `name`.

        Raises:
            DatabaseNotConfiguredError: If no database with that name is configured.
        """
        for db in self.databases:
            if db.name == name:
                return db
        raise DatabaseNotConfiguredError(name, [db.name for db in self.databases])


def validate_production_config(
    settings: Settings,
    app_env: str,
    cors_origins: list[str] | None = None,
) -> list[str]:
    """Check security-sensitive configuration for production deployments.

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
    violations: list[str] = []

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

    openapi_public = any(
        p.rstrip("/") in ("/openapi.json", "/openapi", "/") for p in cfg.auth.public_paths
    )
    if openapi_public and not cfg.auth.allow_public_openapi:
        violations.append(
            "auth.public_paths exposes /openapi.json: the enriched OpenAPI spec "
            "(column descriptions, x-llm-context) would be readable without an API "
            "key. Remove it from auth.public_paths, or set "
            "auth.allow_public_openapi: true to accept this explicitly."
        )

    return violations
