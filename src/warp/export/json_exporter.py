"""JSON catalog exporter."""

import json
from pathlib import Path

from warp.catalog.models import DatabaseCatalog
from warp.export.base import CatalogExporter


class JsonExporter(CatalogExporter):
    """Export catalog as JSON file."""

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
        if lang:
            data = self._filter_language(catalog, lang)
        else:
            data = catalog.model_dump(mode="json")

        return json.dumps(data, indent=2, ensure_ascii=False)

    def _filter_language(self, catalog: DatabaseCatalog, lang: str) -> dict:
        """Extract only the specified language from all LocalizedText fields."""
        data = catalog.model_dump(mode="json")
        data["description"] = catalog.description.get(lang)

        filtered_tables = {}
        for tname, table in catalog.tables.items():
            t = {
                "table_name": table.table_name,
                "description": table.description.get(lang),
                "human_name": table.human_name.get(lang),
                "primary_key": table.primary_key,
                "row_count": table.row_count,
                "tags": table.tags,
                "columns": [],
            }
            for col in table.columns:
                t["columns"].append({
                    "name": col.name,
                    "data_type": col.data_type,
                    "description": col.description.get(lang),
                    "semantic_type": col.semantic_type,
                    "nullable": col.nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "references": col.references,
                    "tags": col.tags,
                    "sample_values": col.sample_values,
                })
            filtered_tables[tname] = t

        data["tables"] = filtered_tables
        data["_language"] = lang
        return data
