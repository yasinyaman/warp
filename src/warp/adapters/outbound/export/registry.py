"""Default exporter registry."""

from warp.adapters.outbound.export.json import JsonExporter
from warp.adapters.outbound.export.markdown import MarkdownExporter
from warp.adapters.outbound.export.yaml import YamlExporter
from warp.application.ports.exporter import CatalogExporter


def default_exporters() -> dict[str, CatalogExporter]:
    """Exporters keyed by format name."""
    markdown = MarkdownExporter()
    return {"json": JsonExporter(), "yaml": YamlExporter(), "markdown": markdown, "md": markdown}
