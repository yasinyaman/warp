"""LLM prompt templates for database catalog description generation.

Contains structured prompts for:
- Table description generation
- Column description + semantic type detection
- Relationship description
- Multi-language generation
"""

from typing import Any

from warp.enrichment.sample_reader import TableSamples

SYSTEM_PROMPT_BASE = """\
You are a database documentation expert. Your task is to analyze database \
table and column structures, understand their purpose, and generate clear, \
concise descriptions that help developers and AI agents understand the data.

Guidelines:
- Be concise but informative
- Identify the business purpose of tables and columns
- Detect semantic types (email, phone, amount, status, URL, etc.)
- Note relationships and their meaning
- Use domain-specific terminology where appropriate
{language_instruction}"""


TABLE_ANALYSIS_PROMPT = """\
Analyze the following database table and provide descriptions.

Table: {table_name}
Database: {database_name}
Database Type: {database_type}

Columns:
{columns_text}

Foreign Keys:
{foreign_keys_text}

Indexes:
{indexes_text}

{db_comments_text}

{sample_data_text}

{cross_reference_text}

Provide a JSON response with the following structure:
{{
  "table_description": {table_desc_format},
  "table_human_name": {table_name_format},
  "table_tags": ["tag1", "tag2"],
  "columns": {{
    "column_name": {{
      "description": {col_desc_format},
      "semantic_type": "email|phone|amount|status|url|date|id|name|address|flag|count|percentage|code|json|null",
      "tags": ["tag1"]
    }}
  }},
  "relationships": [
    {{
      "source_column": "col",
      "target_table": "table",
      "target_column": "col",
      "relationship_type": "many-to-one|one-to-one|one-to-many",
      "description": {rel_desc_format}
    }}
  ]
}}

Important:
- For semantic_type, choose the most specific match or "null" if unclear
- Base descriptions on column names, types, sample data, and context
- If DB comments exist, use them as the primary source and enhance
- Keep descriptions under 200 characters
- Tags should categorize the table's domain (e.g., "auth", "billing", "inventory")
"""


def format_columns_text(columns: list[dict[str, Any]]) -> str:
    """Format column info for the prompt."""
    lines = []
    for col in columns:
        parts = [
            f"  - {col['name']}: {col.get('full_type') or col['type']}",
        ]
        if not col.get("nullable", True):
            parts.append("NOT NULL")
        if col.get("key") == "PRI":
            parts.append("PRIMARY KEY")
        if col.get("default"):
            parts.append(f"DEFAULT {col['default']}")
        if col.get("max_length"):
            parts.append(f"max_length={col['max_length']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines) if lines else "  (no columns)"


def format_foreign_keys_text(fks: list[dict[str, Any]]) -> str:
    """Format foreign key info for the prompt."""
    if not fks:
        return "  (none)"
    lines = []
    for fk in fks:
        lines.append(
            f"  - {fk['column']} -> {fk['references_table']}.{fk['references_column']}"
        )
    return "\n".join(lines)


def format_indexes_text(indexes: list[dict[str, Any]]) -> str:
    """Format index info for the prompt."""
    if not indexes:
        return "  (none)"
    lines = []
    for idx in indexes:
        unique = "UNIQUE " if idx.get("unique") else ""
        cols = ", ".join(idx["columns"])
        lines.append(f"  - {unique}{idx['name']}: ({cols})")
    return "\n".join(lines)


def format_db_comments_text(
    table_comment: str | None,
    column_comments: dict[str, str],
) -> str:
    """Format existing DB comments for the prompt."""
    if not table_comment and not column_comments:
        return "DB Comments: (none)"

    parts = ["Existing DB Comments (use these as primary source):"]
    if table_comment:
        parts.append(f"  Table: {table_comment}")
    for col_name, comment in column_comments.items():
        parts.append(f"  {col_name}: {comment}")
    return "\n".join(parts)


def format_sample_data_text(samples: TableSamples | None) -> str:
    """Format sample data for the prompt."""
    if not samples or not samples.column_samples:
        return "Sample Data: (none available)"

    parts = [f"Sample Data (row_count ≈ {samples.row_count or 'unknown'}):"]
    for col_name, values in samples.column_samples.items():
        formatted_values = []
        for v in values[:5]:
            s = str(v)
            if len(s) > 50:
                s = s[:47] + "..."
            formatted_values.append(s)
        parts.append(f"  {col_name}: {formatted_values}")

    if samples.column_stats:
        parts.append("Column Stats:")
        for col_name, stats in samples.column_stats.items():
            stat_parts = []
            if stats.distinct_count is not None:
                stat_parts.append(f"distinct={stats.distinct_count}")
            if stats.null_count is not None:
                stat_parts.append(f"nulls={stats.null_count}")
            if stat_parts:
                parts.append(f"  {col_name}: {', '.join(stat_parts)}")

    return "\n".join(parts)


def _lang_format(languages: list[str]) -> str:
    """Create JSON format hint for language-aware fields."""
    if len(languages) <= 1:
        lang = languages[0] if languages else "en"
        return f'{{"{lang}": "description here"}}'
    parts = ", ".join(f'"{lang}": "..."' for lang in languages)
    return "{" + parts + "}"


def build_table_analysis_prompt(
    table_name: str,
    database_name: str,
    database_type: str,
    columns: list[dict[str, Any]],
    foreign_keys: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
    table_comment: str | None = None,
    column_comments: dict[str, str] | None = None,
    samples: TableSamples | None = None,
    cross_reference_context: str = "",
    languages: list[str] | None = None,
) -> str:
    """Build the complete table analysis prompt."""
    langs = languages or ["en"]
    lang_fmt = _lang_format(langs)

    return TABLE_ANALYSIS_PROMPT.format(
        table_name=table_name,
        database_name=database_name,
        database_type=database_type,
        columns_text=format_columns_text(columns),
        foreign_keys_text=format_foreign_keys_text(foreign_keys),
        indexes_text=format_indexes_text(indexes),
        db_comments_text=format_db_comments_text(
            table_comment, column_comments or {}
        ),
        sample_data_text=format_sample_data_text(samples),
        cross_reference_text=(
            f"Cross-reference from other catalogs:\n{cross_reference_context}"
            if cross_reference_context
            else ""
        ),
        table_desc_format=lang_fmt,
        table_name_format=lang_fmt,
        col_desc_format=lang_fmt,
        rel_desc_format=lang_fmt,
    )


def build_system_prompt(
    languages: list[str] | None = None,
    language_instruction: str = "",
) -> str:
    """Build the system prompt with language instructions."""
    if not language_instruction:
        langs = languages or ["en"]
        if len(langs) == 1:
            language_instruction = f"\nRespond in language code: {langs[0]}"
        else:
            lang_list = ", ".join(langs)
            language_instruction = (
                f"\nProvide all descriptions in these languages: {lang_list}. "
                f"Use language codes as JSON keys."
            )

    return SYSTEM_PROMPT_BASE.format(language_instruction=language_instruction)


TRANSLATION_PROMPT = """\
Translate the following database catalog description from {source_lang} to {target_lang}.
Keep technical terms accurate. Database and programming terms should remain in English.
Only output the translation, nothing else.

Text: {text}"""


def build_translation_prompt(
    text: str, source_lang: str, target_lang: str
) -> str:
    """Build a translation prompt."""
    return TRANSLATION_PROMPT.format(
        text=text,
        source_lang=source_lang,
        target_lang=target_lang,
    )
