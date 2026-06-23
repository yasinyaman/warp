"""MCP Server enricher using catalog data.

Enriches stargate MCPServer tool/resource descriptions
with catalog-aware context so LLMs can make better decisions.
"""

from typing import Any

from warp.catalog.models import DatabaseCatalog, TableCatalogEntry
from warp.core.logging import get_logger

logger = get_logger(__name__)


class MCPEnricher:
    """Enriches stargate MCPServer with catalog descriptions."""

    def __init__(self, catalog: DatabaseCatalog, lang: str = "en"):
        """Store the catalog and target language for enrichment."""
        self.catalog = catalog
        self.lang = lang

    def enrich(self, server: Any) -> Any:
        """Enrich an MCPServer with catalog descriptions."""
        tools = getattr(server, "tools", [])
        enriched_tools = 0
        for tool in tools:
            if self._enrich_tool(tool):
                enriched_tools += 1

        resources = getattr(server, "resources", [])
        enriched_resources = 0
        for resource in resources:
            if self._enrich_resource(resource):
                enriched_resources += 1

        logger.info(
            f"MCP server enriched: {enriched_tools} tools, {enriched_resources} resources"
        )

        return server

    def _enrich_tool(self, tool: Any) -> bool:
        """Enrich a single MCPTool with catalog context."""
        http_path = getattr(tool, "http_path", "")
        tool_name = getattr(tool, "name", "")

        table_name = self._extract_table_name(http_path, tool_name)
        if not table_name:
            return False

        table = self.catalog.get_table(table_name)
        if not table:
            return False

        table_desc = table.description.get(self.lang)
        human_name = table.human_name.get(self.lang) or table_name
        existing_desc = getattr(tool, "description", "")

        parts = []
        if existing_desc:
            parts.append(existing_desc)

        if table_desc:
            parts.append(f"Table: {human_name} - {table_desc}")

        col_hints = self._build_column_hints(table)
        if col_hints:
            parts.append(f"Columns: {col_hints}")

        rel_hints = self._build_relationship_hints(table)
        if rel_hints:
            parts.append(f"Relations: {rel_hints}")

        if len(parts) > 1:
            tool.description = " | ".join(parts)
            self._enrich_input_schema(tool, table)
            return True

        return False

    def _enrich_resource(self, resource: Any) -> bool:
        """Enrich a single MCPResource with catalog context."""
        uri = getattr(resource, "uri", "")
        resource_name = getattr(resource, "name", "")

        table_name = self._extract_table_name(uri, resource_name)
        if not table_name:
            return False

        table = self.catalog.get_table(table_name)
        if not table:
            return False

        table_desc = table.description.get(self.lang)
        if table_desc:
            existing = getattr(resource, "description", "")
            if existing:
                resource.description = f"{existing} | {table_desc}"
            else:
                resource.description = table_desc
            return True

        return False

    def _enrich_input_schema(self, tool: Any, table: TableCatalogEntry) -> None:
        """Add column descriptions to tool's input_schema properties."""
        schema = getattr(tool, "input_schema", {})
        if not isinstance(schema, dict):
            return

        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue

            col = table.get_column(prop_name)
            if col:
                col_desc = col.description.get(self.lang)
                if col_desc and not prop_schema.get("description"):
                    prop_schema["description"] = col_desc

    def _build_column_hints(self, table: TableCatalogEntry) -> str:
        """Build a compact column description string."""
        hints = []
        for col in table.columns:
            if col.semantic_type and col.semantic_type != "null":
                hints.append(f"{col.name}({col.semantic_type})")
            elif col.is_primary_key:
                hints.append(f"{col.name}(pk)")
            elif col.is_foreign_key:
                ref = f"->{col.references}" if col.references else ""
                hints.append(f"{col.name}(fk{ref})")
        return ", ".join(hints[:8])

    def _build_relationship_hints(self, table: TableCatalogEntry) -> str:
        """Build compact relationship hints."""
        hints = []
        for rel in table.relationships:
            rel_desc = rel.description.get(self.lang)
            if rel_desc:
                hints.append(rel_desc)
            else:
                hints.append(
                    f"{rel.source_column}->{rel.target_table}.{rel.target_column}"
                )
        return "; ".join(hints[:4])

    @staticmethod
    def _extract_table_name(path: str, name: str) -> str | None:
        """Extract table name from HTTP path or tool/resource name."""
        if path:
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1":
                return parts[2]
            if len(parts) >= 1:
                for part in reversed(parts):
                    if not part.startswith("{"):
                        return part

        if name:
            for prefix in ("list_", "get_", "create_", "update_", "delete_"):
                if name.startswith(prefix):
                    remainder = name[len(prefix):]
                    if remainder.endswith("_by_id"):
                        remainder = remainder[:-6]
                    return remainder

        return None
