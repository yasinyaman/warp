"""OpenAPI spec enricher using catalog data.

Injects catalog descriptions into warp's OpenAPI spec
so that downstream tools (LLMs, code generators) get rich context about
every table, column, relationship, and data shape.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from warp.domain.catalog import ColumnCatalogEntry, DatabaseCatalog, TableCatalogEntry

logger = logging.getLogger(__name__)


class OpenAPIEnricher:
    """Enriches warp's OpenAPI spec with catalog descriptions."""

    def __init__(
        self, catalog: DatabaseCatalog, lang: str = "en", *, include_examples: bool = False
    ):
        """Store the catalog, target language and example policy.

        Args:
            catalog: Catalog whose descriptions are injected into the spec.
            lang: Language used for descriptions.
            include_examples: Whether real sample values from the database are
                written into the spec (`example`, `examples`,
                `x-llm-context.examples`). Off by default because the spec is
                frequently reachable without credentials.
        """
        self.catalog = catalog
        self.lang = lang
        self.include_examples = include_examples

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def enrich(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Enrich an OpenAPI spec with full catalog context."""
        paths = spec.get("paths", {})
        enriched_count = 0
        catalog_tables = list(self.catalog.tables.keys())

        logger.debug(f"Enriching OpenAPI: {len(paths)} paths, catalog tables: {catalog_tables}")

        # --- Enrich paths (operations) ---
        for path, methods in paths.items():
            table_name = self._extract_table_from_path(path)
            if not table_name:
                continue

            table = self._find_table(table_name)
            if not table:
                logger.debug(f"No catalog match for path '{path}' (extracted: '{table_name}')")
                continue

            enriched_count += 1
            self._enrich_path_operations(path, methods, table)

        # --- Enrich component schemas ---
        self._enrich_schemas(spec)

        # --- Add x-llm-context top-level extension ---
        self._add_llm_context(spec)

        logger.info(
            f"OpenAPI spec enriched: {self.catalog.database_name} "
            f"({enriched_count}/{len(paths)} paths matched)"
        )

        return spec

    def enrich_file(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
    ) -> Path:
        """Enrich an OpenAPI spec file."""
        input_path = Path(input_path)
        if output_path is None:
            output_path = input_path.with_stem(f"{input_path.stem}.enriched")
        output_path = Path(output_path)

        with open(input_path) as f:
            spec = json.load(f)

        enriched = self.enrich(spec)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(enriched, f, indent=2, ensure_ascii=False)

        return output_path

    # ------------------------------------------------------------------
    # Path-level enrichment
    # ------------------------------------------------------------------

    def _enrich_path_operations(
        self, path: str, methods: dict[str, Any], table: TableCatalogEntry
    ) -> None:
        """Enrich all operations under a single path."""
        table.description.get(self.lang)
        human_name = table.human_name.get(self.lang) or table.table_name

        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue

            # --- Summary ---
            existing_summary = operation.get("summary", "")
            if human_name and human_name != table.table_name and human_name not in existing_summary:
                operation["summary"] = f"{existing_summary} ({human_name})"

            # --- Description: build rich markdown block ---
            rich_desc = self._build_operation_description(table, method)
            existing_desc = operation.get("description", "")
            if rich_desc and rich_desc not in existing_desc:
                if existing_desc:
                    operation["description"] = f"{rich_desc}\n\n---\n\n{existing_desc}"
                else:
                    operation["description"] = rich_desc

            # --- x-llm-context on each operation ---
            operation["x-llm-context"] = self._build_operation_llm_context(table, method)

            self._enrich_parameters(operation, table)
            self._enrich_request_body(operation, table)

    def _build_operation_description(  # noqa: C901
        self, table: TableCatalogEntry, method: str
    ) -> str:
        """Build a rich markdown description block for an operation."""
        parts: list[str] = []
        human_name = table.human_name.get(self.lang) or table.table_name
        table_desc = table.description.get(self.lang)

        if table_desc:
            parts.append(f"**{human_name}**: {table_desc}")

        if table.tags:
            parts.append(f"**Tags**: {', '.join(table.tags)}")

        if table.row_count is not None:
            parts.append(f"**Approximate rows**: {table.row_count:,}")

        # Relationships section
        if table.relationships:
            rel_lines = ["**Relationships**:"]
            for rel in table.relationships:
                rel_desc = rel.description.get(self.lang, "") if rel.description else ""
                rel_line = (
                    f"  - `{rel.source_column}` → "
                    f"`{rel.target_table}.{rel.target_column}` "
                    f"({rel.relationship_type})"
                )
                if rel_desc:
                    rel_line += f" — {rel_desc}"
                rel_lines.append(rel_line)
            parts.append("\n".join(rel_lines))

        # Column overview for GET list operations
        if method.lower() == "get" and table.columns:
            col_lines = ["**Columns**:"]
            for col in table.columns:
                col_desc = col.description.get(self.lang)
                flags = []
                if col.is_primary_key:
                    flags.append("PK")
                if col.is_foreign_key:
                    flags.append(f"FK → {col.references}")
                if col.semantic_type:
                    flags.append(f"semantic: {col.semantic_type}")
                if not col.nullable:
                    flags.append("required")

                flag_str = f" [{', '.join(flags)}]" if flags else ""
                desc_str = f" — {col_desc}" if col_desc else ""
                col_lines.append(f"  - `{col.name}` ({col.data_type}){flag_str}{desc_str}")
            parts.append("\n".join(col_lines))

        return "\n\n".join(parts)

    def _build_operation_llm_context(self, table: TableCatalogEntry, method: str) -> dict[str, Any]:
        """Build structured x-llm-context for an operation."""
        ctx: dict[str, Any] = {
            "table": table.table_name,
            "human_name": table.human_name.get(self.lang) or table.table_name,
            "description": table.description.get(self.lang) or "",
            "tags": table.tags,
            "row_count": table.row_count,
        }

        ctx["columns"] = []
        for col in table.columns:
            col_ctx: dict[str, Any] = {
                "name": col.name,
                "type": col.data_type,
                "description": col.description.get(self.lang) or "",
                "nullable": col.nullable,
                "is_primary_key": col.is_primary_key,
            }
            if col.is_foreign_key:
                col_ctx["foreign_key"] = col.references
            if col.semantic_type:
                col_ctx["semantic_type"] = col.semantic_type
            if col.tags:
                col_ctx["tags"] = col.tags
            if self.include_examples and col.sample_values:
                col_ctx["examples"] = col.sample_values[:5]
            ctx["columns"].append(col_ctx)

        if table.relationships:
            ctx["relationships"] = [
                {
                    "from": rel.source_column,
                    "to": f"{rel.target_table}.{rel.target_column}",
                    "type": rel.relationship_type,
                    "description": rel.description.get(self.lang, "") if rel.description else "",
                }
                for rel in table.relationships
            ]

        return ctx

    # ------------------------------------------------------------------
    # Parameter & request body enrichment
    # ------------------------------------------------------------------

    def _enrich_parameters(self, operation: dict[str, Any], table: TableCatalogEntry) -> None:
        """Add column descriptions to query/path parameters."""
        params = operation.get("parameters", [])
        for param in params:
            param_name = param.get("name", "")

            col_name = self._extract_column_from_param(param_name)
            if not col_name:
                continue

            col = table.get_column(col_name)
            if not col:
                continue

            col_desc = col.description.get(self.lang)
            enriched_desc = self._build_column_description(col)

            if enriched_desc:
                existing = param.get("description", "")
                if col_desc and col_desc not in existing:
                    param["description"] = enriched_desc

            # Add examples
            if self.include_examples and col.sample_values and not param.get("examples"):
                param["examples"] = {
                    f"example_{i}": {"value": v} for i, v in enumerate(col.sample_values[:3])
                }

    def _enrich_request_body(self, operation: dict[str, Any], table: TableCatalogEntry) -> None:
        """Add column descriptions and metadata to request body properties."""
        body = operation.get("requestBody", {})
        content = body.get("content", {})
        json_content = content.get("application/json", {})
        schema_ref = json_content.get("schema", {})

        # Direct properties (inline schema)
        self._enrich_schema_properties(schema_ref, table)

    def _enrich_schema_properties(self, schema: dict[str, Any], table: TableCatalogEntry) -> None:
        """Enrich properties within a schema dict."""
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            col = table.get_column(prop_name)
            if not col:
                continue

            enriched = self._build_column_description(col)
            if enriched:
                existing = prop_schema.get("description", "")
                if enriched not in existing:
                    prop_schema["description"] = enriched

            # Add example values
            if self.include_examples and col.sample_values and "example" not in prop_schema:
                prop_schema["example"] = col.sample_values[0]

    # ------------------------------------------------------------------
    # Component schema enrichment
    # ------------------------------------------------------------------

    def _enrich_schemas(self, spec: dict[str, Any]) -> None:  # noqa: C901, PLR0912
        """Enrich component schemas with full catalog info."""
        components = spec.get("components", {})
        schemas = components.get("schemas", {})
        enriched_schema_count = 0

        for schema_name, schema in schemas.items():
            extracted = self._extract_table_from_schema_name(schema_name)
            if not extracted:
                continue

            table = self._find_table(extracted)
            if not table:
                continue

            enriched_schema_count += 1

            # Schema-level description
            table_desc = table.description.get(self.lang)
            if table_desc:
                existing = schema.get("description", "")
                if table_desc not in existing:
                    schema["description"] = (
                        f"{table_desc}\n\n{existing}" if existing else table_desc
                    )

            # Schema-level x-llm-context
            schema["x-llm-context"] = {
                "table": table.table_name,
                "tags": table.tags,
                "row_count": table.row_count,
            }

            # Property-level descriptions + metadata
            properties = schema.get("properties", {})
            for prop_name, prop_schema in properties.items():
                col = table.get_column(prop_name)
                if not col:
                    continue

                enriched = self._build_column_description(col)
                if enriched:
                    existing = prop_schema.get("description", "")
                    if enriched not in existing:
                        prop_schema["description"] = enriched

                # Example value
                if self.include_examples and col.sample_values and "example" not in prop_schema:
                    prop_schema["example"] = col.sample_values[0]

                # x-llm-context per property
                col_meta: dict[str, Any] = {}
                if col.semantic_type:
                    col_meta["semantic_type"] = col.semantic_type
                if col.is_primary_key:
                    col_meta["is_primary_key"] = True
                if col.is_foreign_key:
                    col_meta["foreign_key"] = col.references
                if col.tags:
                    col_meta["tags"] = col.tags
                if self.include_examples and col.sample_values:
                    col_meta["examples"] = col.sample_values[:5]
                if col_meta:
                    prop_schema["x-llm-context"] = col_meta

        logger.debug(f"Schema enrichment: {enriched_schema_count}/{len(schemas)} schemas matched")

    # ------------------------------------------------------------------
    # Top-level LLM context
    # ------------------------------------------------------------------

    def _add_llm_context(self, spec: dict[str, Any]) -> None:
        """Add x-llm-context top-level extension with full DB overview."""
        tables_overview = []
        for tname, table in self.catalog.tables.items():
            t_ctx: dict[str, Any] = {
                "name": tname,
                "human_name": table.human_name.get(self.lang) or tname,
                "description": table.description.get(self.lang) or "",
                "tags": table.tags,
                "row_count": table.row_count,
                "column_count": len(table.columns),
                "columns": [
                    {
                        "name": c.name,
                        "type": c.data_type,
                        "description": c.description.get(self.lang) or "",
                        **({"semantic_type": c.semantic_type} if c.semantic_type else {}),
                        **({"foreign_key": c.references} if c.is_foreign_key else {}),
                        "is_primary_key": c.is_primary_key,
                        "nullable": c.nullable,
                        **(
                            {"examples": c.sample_values[:3]}
                            if self.include_examples and c.sample_values
                            else {}
                        ),
                    }
                    for c in table.columns
                ],
            }
            if table.relationships:
                t_ctx["relationships"] = [
                    {
                        "from": r.source_column,
                        "to": f"{r.target_table}.{r.target_column}",
                        "type": r.relationship_type,
                    }
                    for r in table.relationships
                ]
            tables_overview.append(t_ctx)

        spec["x-llm-context"] = {
            "database": self.catalog.database_name,
            "database_type": self.catalog.database_type,
            "provider": self.catalog.llm_provider,
            "model": self.catalog.llm_model,
            "table_count": self.catalog.table_count,
            "tables": tables_overview,
        }

    # ------------------------------------------------------------------
    # Column description builder
    # ------------------------------------------------------------------

    def _build_column_description(self, col: ColumnCatalogEntry) -> str:
        """Build a rich description string for a column."""
        parts: list[str] = []

        col_desc = col.description.get(self.lang)
        if col_desc:
            parts.append(col_desc)

        meta: list[str] = []
        if col.is_primary_key:
            meta.append("Primary Key")
        if col.is_foreign_key and col.references:
            meta.append(f"Foreign Key → {col.references}")
        if col.semantic_type:
            meta.append(f"Semantic type: {col.semantic_type}")
        if not col.nullable:
            meta.append("Required")

        if meta:
            parts.append(f"[{', '.join(meta)}]")

        if self.include_examples and col.sample_values:
            examples = ", ".join(str(v) for v in col.sample_values[:3])
            parts.append(f"Examples: {examples}")

        return " | ".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_table(self, name: str) -> TableCatalogEntry | None:
        """Find a table in catalog by name (case-insensitive)."""
        table = self.catalog.get_table(name)
        if table:
            return table
        for cat_table in self.catalog.tables:
            if cat_table.lower() == name.lower():
                return self.catalog.get_table(cat_table)
        return None

    def _extract_table_from_path(self, path: str) -> str | None:
        """Extract table name from an API path.

        Handles both single-db and multi-db URL patterns:
          /api/v1/{table}          -> table
          /api/v1/{db_name}/{table} -> table
        """
        parts = path.strip("/").split("/")

        if len(parts) < 3 or parts[0] != "api" or parts[1] != "v1":
            return None

        segment = parts[2]

        if segment in ("catalog", "query"):
            return None

        # Multi-db layout: /api/v1/{db_name}/{table}/...
        if len(parts) >= 4 and segment == self.catalog.database_name:
            table_segment = parts[3]
            if table_segment in ("query",):
                return None
            return table_segment

        return segment

    def _extract_table_from_schema_name(self, schema_name: str) -> str | None:
        """Extract table name from an OpenAPI schema name.

        Handles fully-qualified names:
          warp__schema__analyzer__OrderItemsCreate__1 -> order_items
        """
        name = schema_name

        if "__" in name:
            parts = name.split("__")
            for part in reversed(parts):
                if part and part[0].isupper():
                    name = part
                    break
            else:
                return None

        for suffix in ("Create", "Update", "Response", "List", "Patch"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break

        if not name:
            return None

        snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()
        return snake

    @staticmethod
    def _extract_column_from_param(param_name: str) -> str | None:
        """Extract column name from filter parameter name."""
        if param_name.startswith("filter["):
            parts = param_name.split("[")
            if len(parts) >= 2:
                return parts[1].rstrip("]")
        if "[" not in param_name and param_name not in (
            "sort",
            "limit",
            "offset",
            "page",
            "per_page",
            "fields",
        ):
            return param_name
        return None
