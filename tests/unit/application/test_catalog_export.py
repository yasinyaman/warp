"""Tests for CatalogExportService."""

import pytest

from warp.adapters.outbound.export.registry import default_exporters
from warp.application.services.catalog_export import CatalogExportService
from warp.domain.catalog import DatabaseCatalog, TableCatalogEntry
from warp.domain.errors import UnsupportedExportFormatError


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog(database_name="db", tables={"t": TableCatalogEntry(table_name="t")})


def test_render_and_export(tmp_path):
    service = CatalogExportService(default_exporters())
    assert set(service.formats) == {"json", "yaml", "markdown", "md"}
    assert '"database_name": "db"' in service.render(_catalog(), "json")
    path = service.export(_catalog(), "yaml", tmp_path / "c.yaml")
    assert path.exists()
    assert service.media_type("json") == "application/json"
    assert service.media_type("md") == "text/markdown"


def test_unknown_format_is_a_400():
    service = CatalogExportService(default_exporters())
    with pytest.raises(UnsupportedExportFormatError) as exc:
        service.get("xml")
    assert exc.value.status_code == 400
    assert "xml" in exc.value.message
