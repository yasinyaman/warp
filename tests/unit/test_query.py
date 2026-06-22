"""Tests for the raw SQL query validator."""
import pytest

from warp.api.query import QueryValidator


class TestQueryValidator:
    def test_allows_simple_select(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT * FROM users WHERE id = :id") is True

    def test_allows_single_trailing_semicolon(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT 1;") is True
        assert v.validate("SELECT 1 ;  ") is True

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users; SELECT * FROM secrets",
            "SELECT 1; DROP TABLE users",
            "SELECT 1;DELETE FROM users",
            "SELECT 1; SELECT 2;",
        ],
    )
    def test_rejects_multiple_statements(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="Multiple SQL statements"):
            v.validate(query)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users -- comment",
            "SELECT /* x */ 1",
            "DROP TABLE users",
            "DELETE FROM users",
        ],
    )
    def test_rejects_dangerous_or_non_whitelisted(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError):
            v.validate(query)

    def test_enforces_whitelist_start(self):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="must start with"):
            v.validate("UPDATE users SET x = 1")

    def test_whitelist_is_configurable(self):
        v = QueryValidator(["SELECT", "INSERT"])
        assert v.validate("INSERT INTO t (a) VALUES (:a)") is True
