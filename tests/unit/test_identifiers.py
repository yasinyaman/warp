"""Tests for centralized SQL identifier sanitization/quoting."""

import pytest

from warp.database.identifiers import quote_identifier, sanitize_identifier
from warp.enrichment.sample_reader import SampleReader


class TestSanitizeIdentifier:
    @pytest.mark.parametrize("name", ["users", "user_id", "_private", "Col1", "a", "A1_b2"])
    def test_accepts_valid(self, name):
        assert sanitize_identifier(name) == name

    @pytest.mark.parametrize(
        "bad",
        [
            'users"; DROP TABLE users; --',
            "users; SELECT 1",
            "users--",
            "col name",
            "col-name",
            "ta.ble",
            "1col",
            "col)",
            "",
            'a" OR "1"="1',
        ],
    )
    def test_rejects_injection(self, bad):
        with pytest.raises(ValueError):
            sanitize_identifier(bad)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            sanitize_identifier(None)  # type: ignore[arg-type]


class TestQuoteIdentifier:
    def test_postgres_uses_double_quotes(self):
        assert quote_identifier("users", "postgresql") == '"users"'

    def test_mysql_uses_backticks(self):
        assert quote_identifier("users", "mysql") == "`users`"

    def test_quoting_validates(self):
        with pytest.raises(ValueError):
            quote_identifier('a"; DROP TABLE x; --', "postgresql")


class _NoopAdapter:
    """Adapter that records the SQL it is asked to run."""

    def __init__(self):
        self.queries: list[str] = []

    async def execute_query(self, query, params=None):
        self.queries.append(query)
        return []


class TestSampleReaderHardening:
    def test_quote_identifier_rejects_malicious(self):
        reader = SampleReader(_NoopAdapter(), db_type="postgresql")
        with pytest.raises(ValueError):
            reader._quote_identifier('users"; DROP TABLE users; --')

    async def test_read_samples_does_not_execute_injected_sql(self):
        adapter = _NoopAdapter()
        reader = SampleReader(adapter, db_type="postgresql")
        # A malicious table name is rejected internally and handled gracefully:
        # no SQL containing the payload is ever sent to the adapter.
        result = await reader.read_samples('users"; DROP TABLE users; --')
        assert result == {}
        assert all("DROP TABLE" not in q for q in adapter.queries)

    async def test_read_samples_builds_quoted_query_for_valid_table(self):
        adapter = _NoopAdapter()
        reader = SampleReader(adapter, db_type="postgresql")
        await reader.read_samples("users", limit=3)
        assert adapter.queries
        assert '"public"."users"' in adapter.queries[0]
