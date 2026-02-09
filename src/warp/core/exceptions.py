"""
Custom exceptions for Auto CRUD API.
"""
from typing import Any, Dict, Optional


class AutoCrudException(Exception):
    """Base exception for Auto CRUD API."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        status_code: int = 500
    ):
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API response."""
        return {
            "error": self.__class__.__name__,
            "message": self.message,
            "details": self.details
        }


class DatabaseConnectionError(AutoCrudException):
    """Raised when database connection fails."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            details=details,
            status_code=503  # Service Unavailable
        )


class DatabaseQueryError(AutoCrudException):
    """Raised when a database query fails."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            details=details,
            status_code=500
        )


class ConfigurationError(AutoCrudException):
    """Raised when configuration is invalid."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            details=details,
            status_code=500
        )


class ValidationError(AutoCrudException):
    """Raised when validation fails."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            details=details,
            status_code=400  # Bad Request
        )


class NotFoundError(AutoCrudException):
    """Raised when a resource is not found."""

    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            message=f"{resource} not found",
            details={"resource": resource, "identifier": str(identifier)},
            status_code=404
        )


# --- Catalog / HAL exceptions ---


class CatalogError(AutoCrudException):
    """Catalog storage or retrieval errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, details=details, status_code=500)


class CatalogNotFoundError(CatalogError):
    """Raised when a catalog is not found."""

    def __init__(self, db_name: str):
        super().__init__(
            message=f"Catalog not found: {db_name}",
            details={"database": db_name},
        )
        self.db_name = db_name


class TableNotFoundInCatalogError(CatalogError):
    """Raised when a table is not found in a catalog."""

    def __init__(self, table_name: str, db_name: str = ""):
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
        super().__init__(
            message=f"Catalog is not in draft status: {db_name}. Only draft catalogs can be edited.",
            details={"database": db_name},
        )
        self.status_code = 409  # Conflict


class LLMError(AutoCrudException):
    """LLM provider errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, details=details, status_code=500)


class LLMProviderNotFoundError(LLMError):
    """Raised when LLM provider is not available."""

    def __init__(self, provider: str):
        super().__init__(
            message=f"LLM provider not found: {provider}",
            details={"provider": provider},
        )
        self.provider = provider


class LLMGenerationError(LLMError):
    """Raised when LLM fails to generate content."""

    pass


class AnalysisError(AutoCrudException):
    """Schema analysis errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, details=details, status_code=500)


class ExportError(AutoCrudException):
    """Export related errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, details=details, status_code=500)


class I18nError(AutoCrudException):
    """Internationalization errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, details=details, status_code=500)
