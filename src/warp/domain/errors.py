"""Custom exceptions for Auto CRUD API."""

from typing import Any


class WarpError(Exception):
    """Base exception for Auto CRUD API."""

    def __init__(self, message: str, details: dict[str, Any] | None = None, status_code: int = 500):
        """Initialize the exception with a message, details, and HTTP status."""
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to dictionary for API response."""
        return {"error": self.__class__.__name__, "message": self.message, "details": self.details}


class DatabaseConnectionError(WarpError):
    """Raised when database connection fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(
            message=message,
            details=details,
            status_code=503,  # Service Unavailable
        )


class DatabaseQueryError(WarpError):
    """Raised when a database query fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class ConfigurationError(WarpError):
    """Raised when configuration is invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class ValidationError(WarpError):
    """Raised when validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(
            message=message,
            details=details,
            status_code=400,  # Bad Request
        )


class NotFoundError(WarpError):
    """Raised when a resource is not found."""

    def __init__(self, resource: str, identifier: Any):
        """Build the error for a missing `resource` with the given identifier."""
        super().__init__(
            message=f"{resource} not found",
            details={"resource": resource, "identifier": str(identifier)},
            status_code=404,
        )


# --- Catalog / HAL exceptions ---


class CatalogError(WarpError):
    """Catalog storage or retrieval errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class CatalogNotFoundError(CatalogError):
    """Raised when a catalog is not found."""

    def __init__(self, db_name: str):
        """Build the error for a missing catalog `db_name`."""
        super().__init__(
            message=f"Catalog not found: {db_name}",
            details={"database": db_name},
        )
        self.db_name = db_name


class InvalidCatalogNameError(CatalogError):
    """Raised when a catalog/database name is not a safe storage identifier.

    Catalog names become directory names under the store's base path, so
    anything that is not a plain identifier (path separators, dot segments,
    leading underscores, control characters) is rejected before it can touch
    the filesystem.
    """

    def __init__(self, name: str):
        """Build the error for an unsafe catalog `name`."""
        super().__init__(
            message=(
                "Invalid catalog name: must start with a letter or digit and contain "
                "only letters, digits, '_' or '-' (max 64 characters)"
            ),
            details={"name": name},
        )
        self.status_code = 400  # Bad Request
        self.name = name


class TableNotFoundInCatalogError(CatalogError):
    """Raised when a table is not found in a catalog."""

    def __init__(self, table_name: str, db_name: str = ""):
        """Build the error for a table missing from a catalog."""
        msg = f"Table not found: {table_name}"
        if db_name:
            msg += f" in catalog {db_name}"
        super().__init__(
            message=msg,
            details={"table": table_name, "database": db_name},
        )
        self.table_name = table_name
        self.db_name = db_name


class ColumnNotFoundInCatalogError(CatalogError):
    """Raised when a column is not found in a table catalog."""

    def __init__(self, column_name: str, table_name: str, db_name: str = ""):
        """Build the error for a column missing from a table catalog."""
        msg = f"Column not found: {column_name} in table {table_name}"
        if db_name:
            msg += f" in catalog {db_name}"
        super().__init__(
            message=msg,
            details={"column": column_name, "table": table_name, "database": db_name},
        )
        self.column_name = column_name
        self.table_name = table_name
        self.db_name = db_name


class CatalogNotDraftError(CatalogError):
    """Raised when attempting to edit a catalog that is not in draft status."""

    def __init__(self, db_name: str):
        """Build the error for editing a catalog that is not in draft status."""
        super().__init__(
            message=f"Catalog is not in draft status: {db_name}. Only draft catalogs can be edited.",
            details={"database": db_name},
        )
        self.status_code = 409  # Conflict


class LLMError(WarpError):
    """LLM provider errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class LLMProviderNotFoundError(LLMError):
    """Raised when LLM provider is not available."""

    def __init__(self, provider: str):
        """Build the error for an unavailable LLM `provider`."""
        super().__init__(
            message=f"LLM provider not found: {provider}",
            details={"provider": provider},
        )
        self.provider = provider


class LLMGenerationError(LLMError):
    """Raised when LLM fails to generate content."""

    pass


class AnalysisError(WarpError):
    """Schema analysis errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class ExportError(WarpError):
    """Export related errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)


class I18nError(WarpError):
    """Internationalization errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize the error with a message and optional details payload."""
        super().__init__(message=message, details=details, status_code=500)
