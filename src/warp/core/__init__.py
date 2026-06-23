"""Core building blocks: exceptions and logging."""

from .exceptions import (
    AutoCrudException,
    ConfigurationError,
    DatabaseConnectionError,
    DatabaseQueryError,
    NotFoundError,
    ValidationError,
)
from .logging import get_logger, setup_logging

__all__ = [
    "AutoCrudException",
    "DatabaseConnectionError",
    "DatabaseQueryError",
    "ConfigurationError",
    "ValidationError",
    "NotFoundError",
    "setup_logging",
    "get_logger",
]
