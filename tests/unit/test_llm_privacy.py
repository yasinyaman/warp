"""Tests for LLM sample-data privacy: PII masking + cloud opt-in gating."""

from unittest.mock import MagicMock

import pytest

from warp.config.settings import Settings
from warp.enrichment.analyzer import EnrichedAnalyzer
from warp.enrichment.sample_reader import (
    ColumnStats,
    TableSamples,
    is_pii_column,
    is_pii_semantic_type,
    mask_pii_samples,
    samples_for_storage,
)


def _samples():
    return TableSamples(
        table_name="users",
        row_count=3,
        column_samples={
            "id": [1, 2, 3],
            "email": ["a@x.com", "b@x.com", "c@x.com"],
            "phone_number": ["+90 555", "+90 556", "+90 557"],
            "username": ["alice", "bob", "carol"],
        },
        column_stats={
            "email": ColumnStats(column_name="email", sample_values=["a@x.com"]),
            "username": ColumnStats(column_name="username", sample_values=["alice"]),
        },
    )


class TestPIIHelpers:
    @pytest.mark.parametrize("name", ["email", "user_email", "phone", "ssn", "password", "api_key"])
    def test_detects_pii(self, name):
        assert is_pii_column(name)

    @pytest.mark.parametrize("name", ["id", "username", "status", "created_at"])
    def test_ignores_non_pii(self, name):
        assert not is_pii_column(name)

    def test_mask_replaces_only_pii_columns(self):
        masked = mask_pii_samples(_samples())
        assert masked.column_samples["email"] == ["***", "***", "***"]
        assert masked.column_samples["phone_number"] == ["***", "***", "***"]
        assert masked.column_samples["username"] == ["alice", "bob", "carol"]
        assert masked.column_samples["id"] == [1, 2, 3]
        assert masked.column_stats["email"].sample_values == ["***"]
        assert masked.column_stats["username"].sample_values == ["alice"]

    def test_mask_does_not_mutate_original(self):
        original = _samples()
        mask_pii_samples(original)
        assert original.column_samples["email"] == ["a@x.com", "b@x.com", "c@x.com"]


def _analyzer(provider, *, share=False, mask=True):
    config = Settings()
    config.settings.llm.provider = provider
    config.settings.analysis.share_samples_with_cloud_llm = share
    config.settings.analysis.mask_pii_samples = mask
    return EnrichedAnalyzer(
        adapter=MagicMock(),
        config=config,
        llm_client=MagicMock(),
        db_type="postgresql",
    )


class TestCloudGating:
    def test_cloud_provider_default_sends_nothing(self):
        analyzer = _analyzer("openai")
        assert analyzer._samples_for_llm(_samples()) is None

    def test_cloud_provider_opt_in_sends_masked(self):
        analyzer = _analyzer("anthropic", share=True)
        result = analyzer._samples_for_llm(_samples())
        assert result is not None
        assert result.column_samples["email"] == ["***", "***", "***"]
        assert result.column_samples["username"] == ["alice", "bob", "carol"]

    def test_local_provider_sends_masked_by_default(self):
        analyzer = _analyzer("ollama")
        result = analyzer._samples_for_llm(_samples())
        assert result is not None
        assert result.column_samples["email"] == ["***", "***", "***"]

    def test_masking_can_be_disabled_for_local(self):
        analyzer = _analyzer("ollama", mask=False)
        result = analyzer._samples_for_llm(_samples())
        assert result.column_samples["email"] == ["a@x.com", "b@x.com", "c@x.com"]

    def test_none_samples_pass_through(self):
        analyzer = _analyzer("ollama")
        assert analyzer._samples_for_llm(None) is None


class TestStoragePrivacy:
    """PII never reaches the stored catalog (which feeds the draft API + OpenAPI)."""

    def test_pii_semantic_types(self):
        assert is_pii_semantic_type("email")
        assert is_pii_semantic_type("Phone")
        assert not is_pii_semantic_type("amount")
        assert not is_pii_semantic_type(None)

    def test_samples_for_storage_drops_pii_by_name(self):
        assert samples_for_storage("user_email", None, ["a@x.com"]) == []
        assert samples_for_storage("username", None, ["alice"]) == ["alice"]

    def test_samples_for_storage_drops_pii_by_semantic_type(self):
        assert samples_for_storage("contact", "email", ["a@x.com"]) == []
        assert samples_for_storage("contact", "phone", ["+90 555"]) == []
        assert samples_for_storage("contact", "status", ["active"]) == ["active"]

    def test_samples_for_storage_respects_mask_flag(self):
        assert samples_for_storage("email", "email", ["a@x.com"], mask_pii=False) == ["a@x.com"]

    def _entry(self, analyzer):
        result = {
            "columns": {
                "id": {"semantic_type": "id"},
                "email": {"semantic_type": "email"},
                "contact": {"semantic_type": "phone"},
                "username": {"semantic_type": "name"},
                "status": {"semantic_type": "status"},
            }
        }
        samples = TableSamples(
            table_name="users",
            row_count=3,
            column_samples={
                "id": [1, 2],
                "email": ["a@x.com"],
                "contact": ["+90 555"],
                "username": ["alice"],
                "status": ["active"],
            },
        )
        return analyzer._build_enriched_entry(
            table_name="users",
            result=result,
            col_dicts=[
                {"name": "id", "type": "integer"},
                {"name": "email", "type": "varchar"},
                {"name": "contact", "type": "varchar"},
                {"name": "username", "type": "varchar"},
                {"name": "status", "type": "varchar"},
            ],
            fk_dicts=[],
            idx_dicts=[],
            primary_key="id",
            samples=samples,
            db_table_comment=None,
            db_column_comments={},
        )

    def test_enriched_entry_keeps_no_pii_samples(self):
        entry = self._entry(_analyzer("ollama"))
        by_name = {c.name: c.sample_values for c in entry.columns}
        assert by_name["email"] == []  # name pattern
        assert by_name["contact"] == []  # semantic type "phone"
        assert by_name["username"] == []  # semantic type "name"
        assert by_name["status"] == ["active"]
        assert by_name["id"] == [1, 2]

    def test_enriched_entry_keeps_samples_when_masking_disabled(self):
        entry = self._entry(_analyzer("ollama", mask=False))
        by_name = {c.name: c.sample_values for c in entry.columns}
        assert by_name["email"] == ["a@x.com"]
        assert by_name["contact"] == ["+90 555"]
