"""Catalog analysis use case.

Combines schema discovery, DB comments, sample data, cross-references,
and LLM generation to produce a complete DatabaseCatalog.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from warp.application.config import Settings
from warp.application.localization import LocalizationManager
from warp.application.ports.database import DatabaseGateway
from warp.application.ports.metadata import CommentSource, SampleSource
from warp.application.ports.text_generation import TextGenerator
from warp.application.prompts import (
    build_system_prompt,
    build_table_analysis_prompt,
    build_translation_prompt,
)
from warp.application.services.catalog_review import CatalogReviewService
from warp.application.services.cross_reference import CrossReferenceService
from warp.domain.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    ForeignKeyInfo,
    IndexInfo,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from warp.domain.errors import AnalysisError
from warp.domain.samples import TableSamples, mask_pii_samples, samples_for_storage

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    """Outcome of `CatalogAnalysisService.analyze()` beyond the catalog itself."""

    catalog: DatabaseCatalog
    failed_tables: list[str] = field(default_factory=list)  # dropped: schema read failed
    fallback_tables: list[str] = field(default_factory=list)  # kept, but without LLM output
    llm_successes: int = 0

    @property
    def has_problems(self) -> bool:
        """Whether any table was dropped or built without the LLM."""
        return bool(self.failed_tables or self.fallback_tables)


class CatalogAnalysisService:
    """Use case: analyze a database and produce an enriched `DatabaseCatalog`.

    Pipeline:
    1. List tables through the database gateway
    2. Read DB comments (CommentSource) and sample data (SampleSource)
    3. Add cross-reference context from other catalogs (CrossReferenceService)
    4. Generate descriptions via the TextGenerator (LLM)
    5. Build the catalog and hand it to the review service (draft / approve)

    Every collaborator is injected; the composition root wires the adapters.
    """

    def __init__(  # noqa: PLR0913 - keyword-only; wired by the composition root
        self,
        *,
        gateway: DatabaseGateway,
        config: Settings,
        text_generator: TextGenerator,
        comments: CommentSource | None = None,
        samples: SampleSource | None = None,
        review: CatalogReviewService | None = None,
        cross_reference: CrossReferenceService | None = None,
        localization: LocalizationManager | None = None,
        db_type: str = "postgresql",
        database_name: str = "",
    ):
        """Store the collaborators; `review=None` means the catalog is not persisted."""
        self.gateway = gateway
        self.config = config
        self.text_generator = text_generator
        self.comments = comments
        self.samples = samples
        self.review = review
        self.cross_reference = cross_reference
        self.i18n = localization or LocalizationManager.from_config(config)
        self.db_type = db_type
        self.database_name = database_name
        self.report: AnalysisReport | None = None

    async def analyze(  # noqa: C901, PLR0912, PLR0915
        self,
        table_names: list[str] | None = None,
        auto_approve: bool = False,
    ) -> DatabaseCatalog:
        """Analyze database and generate enriched catalog.

        If a previous catalog exists with user_overrides, those overrides
        are extracted before regeneration and re-applied afterward, so
        user edits survive across LLM re-analysis.
        """
        logger.info(f"Starting database analysis: {self.database_name} ({self.db_type})")

        # Extract existing user overrides before regeneration
        existing_overrides: dict[str, dict[str, Any]] = {}
        if self.review:
            existing_overrides = self.review.extract_overrides(self.database_name)
            if existing_overrides:
                logger.info(f"Preserved user overrides for {len(existing_overrides)} table(s)")

        try:
            all_db_tables = await self.gateway.get_tables()

            if table_names is None:
                excluded = set(self.config.settings.analysis.excluded_tables)
                table_names = [t for t in all_db_tables if t not in excluded]
            else:
                # Normalize user-provided table names against actual DB names
                # (handles case-sensitivity: e.g. "Categories" -> "categories")
                db_table_lookup = {t.lower(): t for t in all_db_tables}
                resolved: list[str] = []
                for name in table_names:
                    actual = db_table_lookup.get(name.lower())
                    if actual:
                        if actual != name:
                            logger.info(f"Table name resolved: '{name}' -> '{actual}'")
                        resolved.append(actual)
                    else:
                        logger.warning(
                            f"Table '{name}' not found in database "
                            f"(available: {', '.join(all_db_tables[:20])})"
                        )
                table_names = resolved

                if not table_names:
                    raise AnalysisError("None of the requested tables were found in the database.")

            logger.info(f"Tables to analyze: {len(table_names)}")

            all_comments = (
                await self.comments.read_all_comments(table_names) if self.comments else {}
            )

            table_entries: dict[str, TableCatalogEntry] = {}
            llm_successes = 0
            failed_tables: list[str] = []  # dropped from the catalog entirely
            fallback_tables: list[str] = []  # present, but schema-only (no LLM output)

            for table_name in table_names:
                try:
                    entry, llm_ok = await self._analyze_table(
                        table_name=table_name,
                        table_comment=all_comments.get(table_name),
                    )
                    table_entries[table_name] = entry
                    if llm_ok:
                        llm_successes += 1
                        logger.info(f"Table analyzed: {table_name}")
                    else:
                        fallback_tables.append(table_name)
                        logger.warning(f"Table analyzed without LLM (fallback): {table_name}")
                except Exception as e:
                    failed_tables.append(table_name)
                    logger.error(f"Failed to analyze table {table_name}: {e}")

            problems = len(failed_tables) + len(fallback_tables)
            if problems > 0 and llm_successes == 0 and table_names:
                raise AnalysisError(
                    f"LLM generation failed for all {problems} table(s). "
                    "Check your LLM provider API key and quota. "
                    "You can switch provider in settings.llm.provider "
                    "(openai, anthropic, gemini, ollama)."
                )

            if fallback_tables:
                logger.warning(
                    f"LLM failed for {len(fallback_tables)}/{len(table_names)} table(s); "
                    f"schema-only entries created: {', '.join(fallback_tables)}"
                )
            if failed_tables:
                logger.error(
                    f"{len(failed_tables)} table(s) could not be read and were left out of "
                    f"the catalog: {', '.join(failed_tables)}"
                )

            catalog = DatabaseCatalog(
                database_name=self.database_name,
                database_type=self.db_type,
                tables=table_entries,
                languages=self.i18n.languages,
                llm_provider=self.config.settings.llm.provider,
                llm_model=self.config.settings.llm.model,
                status=CatalogStatus.draft,
            )

            if self.i18n.is_multilingual and self.i18n.translation_strategy == "multi":
                catalog = await self._translate_catalog(catalog)

            if self.review:
                self.review.save_as_draft(catalog)

                # Re-apply user overrides from previous catalog
                if existing_overrides:
                    catalog = self.review.apply_overrides(self.database_name, existing_overrides)
                    logger.info("User overrides re-applied after regeneration")

                if auto_approve:
                    self.review.approve_catalog(self.database_name)
                    catalog.status = CatalogStatus.approved

            self.report = AnalysisReport(
                catalog=catalog,
                failed_tables=failed_tables,
                fallback_tables=fallback_tables,
                llm_successes=llm_successes,
            )
            logger.info(
                f"Analysis complete: {self.database_name} "
                f"({catalog.table_count} tables, {catalog.languages})"
            )

            return catalog

        except AnalysisError:
            raise
        except Exception as e:
            raise AnalysisError(f"Database analysis failed: {e}") from e

    async def _analyze_table(
        self,
        table_name: str,
        table_comment: Any | None = None,
    ) -> tuple[TableCatalogEntry, bool]:
        """Analyze a single table and generate descriptions.

        Returns:
            Tuple of (TableCatalogEntry, llm_success). llm_success is False
            when the entry was built without LLM (fallback mode).
        """
        schema_data = await self.gateway.get_table_schema(table_name)

        columns = schema_data.get("columns", [])
        foreign_keys = schema_data.get("foreign_keys", [])
        indexes = schema_data.get("indexes", [])
        primary_key = schema_data.get("primary_key")

        samples = None
        if self.samples is not None and self.config.settings.analysis.sample_limit > 0:
            samples = await self.samples.read_table_samples(
                table_name=table_name,
                sample_limit=self.config.settings.analysis.sample_limit,
                include_row_count=self.config.settings.analysis.include_row_count,
            )

        cross_ref_context = ""
        if self.cross_reference and self.cross_reference.has_references():
            col_names = [c.get("name", "") for c in columns if isinstance(c, dict)]
            if not col_names and columns:
                col_names = [c.name if hasattr(c, "name") else str(c) for c in columns]
            cross_ref_context = self.cross_reference.get_context_for_table(table_name, col_names)

        db_table_comment = None
        db_column_comments: dict[str, str] = {}
        if table_comment:
            db_table_comment = table_comment.table_comment
            db_column_comments = table_comment.column_comments

        col_dicts = self._columns_to_dicts(columns)
        fk_dicts = self._fks_to_dicts(foreign_keys)
        idx_dicts = self._indexes_to_dicts(indexes)

        if self.i18n.translation_strategy == "single":
            prompt_languages = self.i18n.languages
        else:
            prompt_languages = [self.i18n.default_language]

        prompt = build_table_analysis_prompt(
            table_name=table_name,
            database_name=self.database_name,
            database_type=self.db_type,
            columns=col_dicts,
            foreign_keys=fk_dicts,
            indexes=idx_dicts,
            table_comment=db_table_comment,
            column_comments=db_column_comments,
            samples=self._samples_for_llm(samples),
            cross_reference_context=cross_ref_context,
            languages=prompt_languages,
        )

        system_prompt = build_system_prompt(
            languages=prompt_languages,
            language_instruction=self.i18n.get_language_instruction()
            if self.i18n.is_multilingual and self.i18n.translation_strategy == "single"
            else "",
        )

        # Common args for _build_basic_entry (used in multiple fallback paths)
        basic_args = (
            table_name,
            col_dicts,
            fk_dicts,
            idx_dicts,
            primary_key,
            samples,
            db_table_comment,
            db_column_comments,
        )

        # Attempt LLM generation; fall back to basic entry on failure
        logger.debug(
            f"[{table_name}] Sending LLM prompt ({len(prompt)} chars, "
            f"system={len(system_prompt)} chars)"
        )
        try:
            llm_response = await self.text_generator.generate_json(
                prompt=prompt,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.warning(f"LLM generation failed for {table_name}, using fallback: {e}")
            return self._build_basic_entry(*basic_args), False

        logger.debug(
            f"[{table_name}] LLM response ({len(llm_response)} chars): "
            f"{llm_response[:500]}{'...' if len(llm_response) > 500 else ''}"
        )

        try:
            result = json.loads(llm_response)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM JSON response for {table_name}: {e}")
            return self._build_basic_entry(*basic_args), False

        try:
            return self._build_enriched_entry(
                table_name=table_name,
                result=result,
                col_dicts=col_dicts,
                fk_dicts=fk_dicts,
                idx_dicts=idx_dicts,
                primary_key=primary_key,
                samples=samples,
                db_table_comment=db_table_comment,
                db_column_comments=db_column_comments,
            ), True
        except Exception as e:
            logger.warning(
                f"LLM returned unexpected structure for {table_name}, using fallback: {e}"
            )
            return self._build_basic_entry(*basic_args), False

    def _samples_for_llm(self, samples: TableSamples | None) -> TableSamples | None:
        """Apply privacy controls to sample data before it is sent to the LLM.

        - Cloud providers (openai/anthropic/gemini) receive no raw samples
          unless ``analysis.share_samples_with_cloud_llm`` is enabled.
        - Otherwise, PII-looking column values are masked when
          ``analysis.mask_pii_samples`` is enabled.

        The unmodified ``samples`` are still used to populate the catalog.
        """
        if samples is None:
            return None

        analysis = self.config.settings.analysis
        provider = self.config.settings.llm.provider.lower()

        if self.config.settings.llm.is_cloud_provider and not analysis.share_samples_with_cloud_llm:
            logger.info(
                f"Not sending sample data to cloud LLM provider '{provider}' "
                "(analysis.share_samples_with_cloud_llm is disabled)"
            )
            return None

        if analysis.mask_pii_samples:
            return mask_pii_samples(samples, analysis.pii_column_patterns)

        return samples

    def _build_enriched_entry(  # noqa: C901, PLR0913, PLR0917, PLR0912
        self,
        table_name: str,
        result: dict[str, Any],
        col_dicts: list[dict[str, Any]],
        fk_dicts: list[dict[str, Any]],
        idx_dicts: list[dict[str, Any]],
        primary_key: Any,
        samples: Any,
        db_table_comment: str | None,
        db_column_comments: dict[str, str],
    ) -> TableCatalogEntry:
        """Build a TableCatalogEntry from LLM result."""
        # Guard: result must be a dict
        if not isinstance(result, dict):
            raise ValueError(f"LLM result is not a dict: {type(result).__name__}")

        table_desc = self._parse_localized(result.get("table_description", {}))
        table_human = self._parse_localized(result.get("table_human_name", {}))

        llm_columns_raw = result.get("columns", {})
        # Guard: columns can arrive in many shapes from small LLMs
        llm_columns: dict[str, Any] = {}
        if isinstance(llm_columns_raw, dict):
            llm_columns = llm_columns_raw
        elif isinstance(llm_columns_raw, list):
            # Try to convert list of dicts with "name" key to a dict
            for item in llm_columns_raw:
                if isinstance(item, dict) and "name" in item:
                    llm_columns[item["name"]] = item
        elif isinstance(llm_columns_raw, str):
            logger.warning(
                f"LLM returned columns as plain string for {table_name}, ignoring LLM column data"
            )
        else:
            logger.warning(
                f"LLM returned unexpected columns type for {table_name}: "
                f"{type(llm_columns_raw).__name__}, ignoring LLM column data"
            )

        column_entries = []

        for col in col_dicts:
            col_name = col["name"]
            llm_col = llm_columns.get(col_name, {})

            # Normalize: small LLMs may return a plain string instead of a dict
            # e.g. {"id": "integer"} or {"name": "category name"}
            if isinstance(llm_col, str):
                # Treat the string as the column description
                llm_col = {"description": {self.i18n.default_language: llm_col}}

            is_pk = col.get("key") == "PRI"
            if not is_pk and primary_key:
                if isinstance(primary_key, str):
                    is_pk = col_name == primary_key
                elif isinstance(primary_key, list):
                    is_pk = col_name in primary_key

            is_fk = any(fk["column"] == col_name for fk in fk_dicts)
            fk_ref = None
            for fk in fk_dicts:
                if fk["column"] == col_name:
                    fk_ref = f"{fk['references_table']}.{fk['references_column']}"
                    break

            sample_vals = []
            if samples and col_name in samples.column_samples:
                seen = set()
                for v in samples.column_samples[col_name]:
                    if v is not None and str(v) not in seen:
                        seen.add(str(v))
                        sample_vals.append(v)
                        if len(sample_vals) >= 5:
                            break

            col_desc = self._parse_localized(llm_col.get("description", {}))
            db_comment = db_column_comments.get(col_name)
            semantic_type = llm_col.get("semantic_type")

            # Privacy: PII columns (by name pattern or LLM semantic type) keep
            # no sample values in the stored catalog, which is re-published
            # through the draft API and the OpenAPI spec.
            analysis_cfg = self.config.settings.analysis
            sample_vals = samples_for_storage(
                col_name,
                semantic_type,
                sample_vals,
                mask_pii=analysis_cfg.mask_pii_samples,
                patterns=analysis_cfg.pii_column_patterns,
            )

            column_entries.append(
                ColumnCatalogEntry(
                    name=col_name,
                    data_type=col["type"],
                    full_type=col.get("full_type"),
                    description=col_desc,
                    semantic_type=semantic_type,
                    nullable=col.get("nullable", True),
                    is_primary_key=is_pk,
                    is_foreign_key=is_fk,
                    references=fk_ref,
                    default_value=str(col["default"]) if col.get("default") else None,
                    tags=llm_col.get("tags", []),
                    sample_values=sample_vals,
                    db_comment=db_comment,
                    generated_description=col_desc,
                )
            )

        relationships = []
        raw_rels = result.get("relationships", [])
        if not isinstance(raw_rels, list):
            raw_rels = []
        for rel in raw_rels:
            if not isinstance(rel, dict):
                continue
            relationships.append(
                RelationshipInfo(
                    source_column=rel.get("source_column", ""),
                    target_table=rel.get("target_table", ""),
                    target_column=rel.get("target_column", ""),
                    relationship_type=rel.get("relationship_type", "many-to-one"),
                    description=self._parse_localized(rel.get("description", {})),
                )
            )

        return TableCatalogEntry(
            table_name=table_name,
            description=table_desc,
            human_name=table_human,
            columns=column_entries,
            primary_key=primary_key,
            foreign_keys=[ForeignKeyInfo(**fk) for fk in fk_dicts],
            indexes=[IndexInfo(**idx) for idx in idx_dicts],
            relationships=relationships,
            row_count=samples.row_count if samples else None,
            tags=result.get("table_tags", []) if isinstance(result.get("table_tags"), list) else [],
            db_comment=db_table_comment,
            generated_description=table_desc,
        )

    def _build_basic_entry(
        self,
        table_name: str,
        col_dicts: list[dict[str, Any]],
        fk_dicts: list[dict[str, Any]],
        idx_dicts: list[dict[str, Any]],
        primary_key: Any,
        samples: Any,
        db_table_comment: str | None,
        db_column_comments: dict[str, str],
    ) -> TableCatalogEntry:
        """Build a basic entry without LLM descriptions (fallback)."""
        column_entries = []
        for col in col_dicts:
            col_name = col["name"]
            db_comment = db_column_comments.get(col_name)
            desc = LocalizedText()
            if db_comment:
                desc = LocalizedText(texts={self.i18n.default_language: db_comment})

            column_entries.append(
                ColumnCatalogEntry(
                    name=col_name,
                    data_type=col["type"],
                    full_type=col.get("full_type"),
                    description=desc,
                    nullable=col.get("nullable", True),
                    db_comment=db_comment,
                )
            )

        table_desc = LocalizedText()
        if db_table_comment:
            table_desc = LocalizedText(texts={self.i18n.default_language: db_table_comment})

        return TableCatalogEntry(
            table_name=table_name,
            description=table_desc,
            columns=column_entries,
            primary_key=primary_key,
            foreign_keys=[ForeignKeyInfo(**fk) for fk in fk_dicts],
            indexes=[IndexInfo(**idx) for idx in idx_dicts],
            row_count=samples.row_count if samples else None,
            db_comment=db_table_comment,
        )

    async def _translate_catalog(self, catalog: DatabaseCatalog) -> DatabaseCatalog:
        """Translate catalog descriptions to missing languages."""
        source_lang = self.i18n.default_language

        for table in catalog.tables.values():
            for lang in self.i18n.languages:
                if lang == source_lang:
                    continue
                source_text = table.description.get(source_lang)
                if source_text and not table.description.texts.get(lang):
                    translated = await self._translate_text(source_text, source_lang, lang)
                    if translated:
                        table.description.set(lang, translated)

                source_name = table.human_name.get(source_lang)
                if source_name and not table.human_name.texts.get(lang):
                    translated = await self._translate_text(source_name, source_lang, lang)
                    if translated:
                        table.human_name.set(lang, translated)

                for col in table.columns:
                    source_col_desc = col.description.get(source_lang)
                    if source_col_desc and not col.description.texts.get(lang):
                        translated = await self._translate_text(source_col_desc, source_lang, lang)
                        if translated:
                            col.description.set(lang, translated)

        return catalog

    async def _translate_text(self, text: str, source_lang: str, target_lang: str) -> str | None:
        """Translate a single text using LLM."""
        if not text.strip():
            return None
        try:
            prompt = build_translation_prompt(text, source_lang, target_lang)
            return await self.text_generator.generate(prompt=prompt)
        except Exception as e:
            logger.warning(f"Translation failed ({source_lang}->{target_lang}): {e}")
            return None

    @staticmethod
    def _parse_localized(value: Any) -> LocalizedText:
        """Parse a value into LocalizedText."""
        if isinstance(value, dict):
            return LocalizedText(texts={k: str(v) for k, v in value.items()})
        elif isinstance(value, str):
            return LocalizedText(texts={"en": value})
        return LocalizedText()

    @staticmethod
    def _columns_to_dicts(columns: Any) -> list[dict[str, Any]]:
        """Convert column objects to dicts."""
        result = []
        for col in columns:
            if isinstance(col, dict):
                result.append(col)
            elif hasattr(col, "model_dump"):
                result.append(col.model_dump())
            elif hasattr(col, "__dict__"):
                result.append(col.__dict__)
            else:
                result.append({"name": str(col), "type": "unknown"})
        return result

    @staticmethod
    def _fks_to_dicts(foreign_keys: Any) -> list[dict[str, Any]]:
        """Convert FK objects to dicts."""
        result = []
        for fk in foreign_keys:
            if isinstance(fk, dict):
                result.append(fk)
            elif hasattr(fk, "model_dump"):
                result.append(fk.model_dump())
            elif hasattr(fk, "__dict__"):
                result.append(fk.__dict__)
        return result

    @staticmethod
    def _indexes_to_dicts(indexes: Any) -> list[dict[str, Any]]:
        """Convert index objects to dicts."""
        result = []
        for idx in indexes:
            if isinstance(idx, dict):
                result.append(idx)
            elif hasattr(idx, "model_dump"):
                result.append(idx.model_dump())
            elif hasattr(idx, "__dict__"):
                result.append(idx.__dict__)
        return result
