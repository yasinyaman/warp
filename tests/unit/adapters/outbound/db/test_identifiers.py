"""Tests for centralized SQL identifier sanitization/quoting."""

import pytest

from warp.adapters.outbound.db.dialect import MSSQL
from warp.adapters.outbound.db.identifiers import quote_identifier, sanitize_identifier
from warp.adapters.outbound.db.sample_reader import SampleReader


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


class TestQuoteIdentifierDialects:
    def test_sqlserver_uses_brackets(self):
        assert quote_identifier("users", "mssql") == "[users]"
        assert quote_identifier("users", "sqlserver") == "[users]"
        assert quote_identifier("users", MSSQL) == "[users]"

    def test_generic_odbc_and_unknown_use_ansi_quotes(self):
        assert quote_identifier("users", "odbc") == '"users"'
        assert quote_identifier("users", "sqlite") == '"users"'

    def test_sample_reader_quotes_for_sqlserver(self):
        reader = SampleReader(_NoopAdapter(), db_type="mssql", schema="dbo")
        assert reader._quote_identifier("users") == "[users]"
        with pytest.raises(ValueError):
            reader._quote_identifier("a]b")


class TestDialectExtraChars:
    """Oracle's legal identifier alphabet is wider, and its data dictionary uses it."""

    def test_oracle_allows_dollar_and_hash(self):
        # An identity column's sequence is generated as ISEQ$$_73346.
        assert sanitize_identifier("ISEQ$$_73346", "$#") == "ISEQ$$_73346"
        assert sanitize_identifier("SYS#TAB", "$#") == "SYS#TAB"

    def test_those_characters_stay_rejected_by_default(self):
        for name in ("ISEQ$$_73346", "SYS#TAB"):
            with pytest.raises(ValueError):
                sanitize_identifier(name)

    def test_the_first_character_is_still_a_letter_or_underscore(self):
        with pytest.raises(ValueError):
            sanitize_identifier("$leading", "$#")
        with pytest.raises(ValueError):
            sanitize_identifier("1abc", "$#")

    @pytest.mark.parametrize("name", ['a"b', "a;b", "a b", "a'b", "a--b", "a/*b*/"])
    def test_widening_never_admits_anything_that_could_escape_the_quotes(self, name):
        # The double quote in particular: it is the only character that could
        # terminate a quoted identifier, and no engine may allow it.
        with pytest.raises(ValueError):
            sanitize_identifier(name, "$#")

    def test_the_compiled_pattern_is_cached_per_alphabet(self):
        from warp.adapters.outbound.db.identifiers import _identifier_re

        assert _identifier_re("$#") is _identifier_re("$#")
        assert _identifier_re("") is not _identifier_re("$#")
