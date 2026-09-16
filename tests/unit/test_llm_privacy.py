"""Tests for LLM sample-data privacy: PII masking + cloud opt-in gating."""

from unittest.mock import MagicMock

import pytest

from warp.config.settings import Settings
from warp.enrichment.analyzer import EnrichedAnalyzer
from warp.enrichment.sample_reader import (
    ColumnStats,
    TableSamples,
    is_pii_column,
    mask_pii_samples,
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
