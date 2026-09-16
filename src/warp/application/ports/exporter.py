"""Base exporter interface."""

from abc import ABC, abstractmethod
from pathlib import Path

from warp.domain.catalog import DatabaseCatalog


class CatalogExporter(ABC):
    """Abstract base class for catalog exporters."""

    @abstractmethod
    def export(
        self,
        catalog: DatabaseCatalog,
        output_path: str | Path,
        lang: str | None = None,
    ) -> Path:
        """Export a catalog to a file."""
        ...

    @abstractmethod
    def export_string(
        self,
        catalog: DatabaseCatalog,
        lang: str | None = None,
    ) -> str:
        """Export a catalog to a string."""
        ...
