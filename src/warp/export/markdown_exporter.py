"""Markdown catalog exporter.

Generates human-readable documentation from a DatabaseCatalog.
"""

from pathlib import Path

from warp.catalog.models import DatabaseCatalog, TableCatalogEntry
from warp.export.base import CatalogExporter


class MarkdownExporter(CatalogExporter):
    """Export catalog as Markdown documentation."""

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
        lang = lang or "en"
        lines: list[str] = []

        lines.append(f"# {catalog.database_name}")
        desc = catalog.description.get(lang)
        if desc:
            lines.append(f"\n{desc}")
        lines.append(f"\n**Type:** {catalog.database_type}")
        lines.append(f"**Tables:** {catalog.table_count}")
        lines.append(f"**Generated:** {catalog.generated_at}")
        lines.append("")

        lines.append("## Tables\n")
        for tname in sorted(catalog.tables.keys()):
            table = catalog.tables[tname]
            human = table.human_name.get(lang) or tname
            lines.append(f"- [{human}](#{tname})")
        lines.append("")

        for tname in sorted(catalog.tables.keys()):
            table = catalog.tables[tname]
            lines.extend(self._render_table(table, lang))
            lines.append("")

        return "\n".join(lines)

    def _render_table(self, table: TableCatalogEntry, lang: str) -> list[str]:
        """Render a single table section."""
        lines: list[str] = []

        human = table.human_name.get(lang) or table.table_name
        lines.append(f"## {human}")
        lines.append(f"\n**Table:** `{table.table_name}`")

        desc = table.description.get(lang)
        if desc:
            lines.append(f"\n{desc}")

        if table.row_count is not None:
            lines.append(f"\n**Row count:** ~{table.row_count:,}")

        if table.tags:
            lines.append(f"\n**Tags:** {', '.join(table.tags)}")

        lines.append("\n### Columns\n")
        lines.append("| Column | Type | Description | Semantic | Nullable |")
        lines.append("|--------|------|-------------|----------|----------|")

        for col in table.columns:
            col_desc = col.description.get(lang) or ""
            semantic = col.semantic_type or ""
            nullable = "Yes" if col.nullable else "No"
            name = col.name
            if col.is_primary_key:
                name = f"**{name}** (PK)"
            elif col.is_foreign_key:
                ref = f" -> {col.references}" if col.references else ""
                name = f"{name} (FK{ref})"

            lines.append(f"| {name} | {col.data_type} | {col_desc} | {semantic} | {nullable} |")

        if table.relationships:
            lines.append("\n### Relationships\n")
            for rel in table.relationships:
                rel_desc = rel.description.get(lang) or ""
                lines.append(
                    f"- `{rel.source_column}` -> "
                    f"`{rel.target_table}.{rel.target_column}` "
                    f"({rel.relationship_type})"
                )
                if rel_desc:
                    lines.append(f"  {rel_desc}")

        if table.indexes:
            lines.append("\n### Indexes\n")
            for idx in table.indexes:
                unique = " (UNIQUE)" if idx.unique else ""
                cols = ", ".join(idx.columns)
                lines.append(f"- `{idx.name}`: ({cols}){unique}")

        return lines


def get_exporter(format: str) -> CatalogExporter:
    """Get an exporter by format name."""
    from warp.export.json_exporter import JsonExporter
    from warp.export.yaml_exporter import YamlExporter

    exporters: dict[str, type[CatalogExporter]] = {
        "json": JsonExporter,
        "yaml": YamlExporter,
        "markdown": MarkdownExporter,
        "md": MarkdownExporter,
    }

    if format not in exporters:
        available = ", ".join(exporters.keys())
        raise ValueError(f"Unknown export format: {format}. Available: {available}")

    return exporters[format]()
