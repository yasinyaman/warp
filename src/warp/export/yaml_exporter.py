"""YAML catalog exporter."""

from pathlib import Path

import yaml

from warp.catalog.models import DatabaseCatalog
from warp.export.json_exporter import JsonExporter


class YamlExporter(JsonExporter):
    """Export catalog as YAML file.

    Inherits language filtering from JsonExporter.
    """

    def export(
        self,
        catalog: DatabaseCatalog,
        output_path: str | Path,
        lang: str | None = None,
    ) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        content = self.export_string(catalog, lang=lang)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return path

    def export_string(
        self,
        catalog: DatabaseCatalog,
        lang: str | None = None,
    ) -> str:
        data = self._filter_language(catalog, lang) if lang else catalog.model_dump(mode="json")

        return yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
