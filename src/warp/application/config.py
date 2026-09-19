"""Application configuration models and production safety validation."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from warp.domain.errors import DatabaseNotConfiguredError
from warp.domain.samples import DEFAULT_PII_PATTERNS


class DatabaseConfig(BaseModel):
    """Database connection configuration.

    ``options`` reaches the adapter untouched. Common keys: ``pool_size``,
    ``pool_min_size``, ``ssl`` (PostgreSQL) and ``schema`` (the schema to
    introspect; defaults to ``public`` / the database name / ``dbo``). The ODBC
    adapter (``mssql``, ``sqlserver``, ``odbc``) also reads ``driver`` (ODBC
    driver name), ``connection_string`` (used verbatim instead of
    host/port/database), ``encrypt`` and ``trust_server_certificate`` (SQL
    Server TLS), ``extra`` (raw ``Key=Value;`` pairs appended to the
    connection string) and ``pool_recycle`` (seconds).
    """

    name: str
    type: str  # postgresql | mysql | mssql | odbc (see DatabaseFactory.get_supported_types)
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


class ExportConfig(BaseModel):
    """Streaming export (``GET|POST /{table}/export``) settings.

    Export is read-only (it needs the ``read`` permission) and streams rows in
    batches straight from a server-side cursor, so it is safe to leave enabled
    in production. ``max_rows`` caps a single export (0 = unlimited) and
    ``statement_timeout_ms`` bounds the database statement (0 = driver default).
    """

    enabled: bool = True
    max_rows: int = 0
    batch_size: int = 5000
    statement_timeout_ms: int = 0


class RowRuleConfig(BaseModel):
    """One mandatory row condition attached to an API key."""

    column: str
    operator: str = "eq"
    # ``${tenant}`` / ``${username}`` are filled in from the calling key.
    value: Any = None


class ApiKeyConfig(BaseModel):
    """API Key configuration with permissions."""

    key: str
    name: str = "default"
    permissions: list[str] = Field(default_factory=lambda: ["read"])
    # Permissions: read, create, update, delete, query (for raw queries)
    # Or use "all" for full access

    # Row-level security. `tenant` and the key's `name` are what `${tenant}`
    # and `${username}` resolve to inside a rule's value.
    tenant: str | None = None
    roles: list[str] = Field(default_factory=list)
    # table name -> conditions ANDed onto every read of that table, and
    # enforced on writes. A caller cannot widen or drop them.
    row_filters: dict[str, list[RowRuleConfig]] = Field(default_factory=dict)

    @property
    def has_row_rules(self) -> bool:
        """Whether this key carries any row-level restriction."""
        return any(self.row_filters.values())


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


class MaskingConfig(BaseModel):
    """Column masking, keyed by the catalog's semantic types.

    Rules attach to a semantic type rather than a column name, so a newly
    discovered PII column is masked as soon as the catalog labels it.
    """

    enabled: bool = False
    # semantic type -> redact | partial | last4 | hash | null
    rules: dict[str, str] = Field(default_factory=dict)
    # role -> its own semantic-type rules, overriding `rules` entirely
    by_role: dict[str, dict[str, str]] = Field(default_factory=dict)
    # roles that see raw values
    exempt_roles: list[str] = Field(default_factory=list)
    # Keys the `hash` strategy, which refuses to run without one. Store it
    # apart from anything it masks: whoever holds it can re-derive the values.
    hash_secret: str | None = None

    @property
    def strategies(self) -> set[str]:
        """Every strategy this configuration asks for, from both rule sources.

        ``by_role`` is the one that gets forgotten — a support role's own rules
        live there and nowhere else.
        """
        used = set(self.rules.values())
        for rules in self.by_role.values():
            used |= set(rules.values())
        return used


class AuditConfig(BaseModel):
    """Audit trail for data access."""

    enabled: bool = False
    # Appended to, never rewritten. Events also go to the `warp.audit` logger.
    file: str | None = None
    # Record the column names a caller filtered on (never the values).
    log_filter_columns: bool = True


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
    export: ExportConfig = Field(default_factory=ExportConfig)
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
    masking: MaskingConfig = Field(default_factory=MaskingConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
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


@dataclass(frozen=True)
class RuntimeEnv:
    """Process-level settings that come from the environment, not the YAML config."""

    app_env: str = "development"
    log_level: str = "INFO"
    log_format: str = ""  # "json" | "colored"; empty -> json in production, colored otherwise
    cors_origins: tuple[str, ...] = ("*",)
    config_path: str | None = None
    api_host: str = "0.0.0.0"  # noqa: S104 - a server binds all interfaces by default
    api_port: int = 8000
    api_workers: int = 1

    @property
    def is_production(self) -> bool:
        """Whether `APP_ENV=production`."""
        return self.app_env == "production"

    @property
    def log_json(self) -> bool:
        """Whether logs are emitted as JSON lines."""
        return self.log_format == "json" or (not self.log_format and self.is_production)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "RuntimeEnv":
        """Read the well-known `APP_ENV`, `LOG_*`, `CORS_ORIGINS`, `CONFIG_PATH`, `API_*` variables."""
        env = os.environ if environ is None else environ
        origins = tuple(o.strip() for o in env.get("CORS_ORIGINS", "*").split(",") if o.strip())
        return cls(
            app_env=env.get("APP_ENV", "development"),
            log_level=env.get("LOG_LEVEL", "INFO"),
            log_format=env.get("LOG_FORMAT", ""),
            cors_origins=origins or ("*",),
            config_path=env.get("CONFIG_PATH") or None,
            api_host=env.get("API_HOST", "0.0.0.0"),  # noqa: S104
            api_port=int(env.get("API_PORT", "8000")),
            api_workers=int(env.get("API_WORKERS", "1")),
        )


#: Below this a masking key is a passphrase somebody chose, which is itself
#: dictionary-attackable — the hole the key exists to close.
MIN_HASH_SECRET_LENGTH = 32


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

    if not cfg.auth.enabled and any(key.has_row_rules for key in cfg.auth.api_keys):
        violations.append(
            "auth.enabled is false while API keys carry row_filters: nobody would "
            "be identified, so the row rules would not apply to anything. Enable "
            "authentication or remove the row filters."
        )

    if cfg.masking.enabled and "hash" in cfg.masking.strategies:
        secret = cfg.masking.hash_secret or ""
        if not secret:
            violations.append(
                "masking rules use the 'hash' strategy with no masking.hash_secret: "
                "an unkeyed digest of an email or a national id is reversible by "
                "enumeration, so the column would not actually be masked. Set "
                "masking.hash_secret, or use redact/partial/last4/null."
            )
        elif len(secret) < MIN_HASH_SECRET_LENGTH:
            violations.append(
                f"masking.hash_secret is shorter than {MIN_HASH_SECRET_LENGTH} "
                f"characters: a guessable key is the same exposure as no key. "
                f"Use a generated secret."
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
