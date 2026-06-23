"""Database adapters, the shared SQL builder, and identifier safety."""

from .base import DatabaseAdapter
from .factory import DatabaseFactory

__all__ = ["DatabaseAdapter", "DatabaseFactory"]
