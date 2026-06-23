"""Catalog-aware query and strategy generation.

Uses catalog descriptions to help LLMs generate
better SQL queries and data strategies.
"""

import json
from typing import Any

from warp.catalog.models import DatabaseCatalog
from warp.core.logging import get_logger
from warp.llm.client import LLMClient

logger = get_logger(__name__)

QUERY_SYSTEM_PROMPT = """\
You are a SQL expert. You generate accurate, efficient SQL queries based on \
the database schema and catalog descriptions provided. Always consider:
- Table relationships and foreign keys
- Column semantic types
- Data types and constraints
{language_instruction}"""

QUERY_PROMPT = """\
Database: {database_name} ({database_type})

Schema context:
{schema_context}

User request: {user_request}

Generate a SQL query that satisfies the request. Return a JSON response:
{{
  "query": "SELECT ...",
  "explanation": "What this query does",
  "tables_used": ["table1", "table2"],
  "notes": "Any important considerations"
}}"""


class QueryStrategy:
    """Generates SQL queries using catalog context."""

    def __init__(
        self,
        catalog: DatabaseCatalog,
        llm_client: LLMClient,
        lang: str = "en",
    ):
        self.catalog = catalog
        self.llm_client = llm_client
        self.lang = lang

    async def generate_query(self, user_request: str) -> dict[str, Any]:
        """Generate a SQL query from a natural language request."""
        schema_context = self._build_schema_context()

        prompt = QUERY_PROMPT.format(
            database_name=self.catalog.database_name,
            database_type=self.catalog.database_type,
            schema_context=schema_context,
            user_request=user_request,
        )

        system_prompt = QUERY_SYSTEM_PROMPT.format(
            language_instruction=f"Explain in language: {self.lang}"
        )

        response = await self.llm_client.generate_json(
            prompt=prompt,
            system_prompt=system_prompt,
        )

        try:
            parsed: dict[str, Any] = json.loads(response)
            return parsed
        except json.JSONDecodeError:
            return {
                "query": response,
                "explanation": "Raw response (JSON parsing failed)",
                "tables_used": [],
                "notes": "",
            }

    def _build_schema_context(self) -> str:
        """Build a compact schema context string for the LLM."""
        parts = []

        for tname, table in self.catalog.tables.items():
            desc = table.description.get(self.lang)
            human = table.human_name.get(self.lang) or tname

            cols_info = []
            for col in table.columns:
                col_parts = [f"{col.name} {col.data_type}"]
                if col.is_primary_key:
                    col_parts.append("PK")
                if col.is_foreign_key and col.references:
                    col_parts.append(f"FK->{col.references}")
                if col.semantic_type:
                    col_parts.append(f"[{col.semantic_type}]")
                if not col.nullable:
                    col_parts.append("NOT NULL")
                cols_info.append(" ".join(col_parts))

            table_line = f"Table: {tname} ({human})"
            if desc:
                table_line += f" - {desc}"
            if table.row_count:
                table_line += f" (~{table.row_count} rows)"

            parts.append(table_line)
            for ci in cols_info:
                parts.append(f"  {ci}")

            for rel in table.relationships:
                rel_desc = rel.description.get(self.lang, "")
                parts.append(
                    f"  REL: {rel.source_column} -> "
                    f"{rel.target_table}.{rel.target_column} "
                    f"({rel.relationship_type}) {rel_desc}"
                )

            parts.append("")

        return "\n".join(parts)
