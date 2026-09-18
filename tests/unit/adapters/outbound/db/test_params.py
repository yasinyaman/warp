"""Tests for named-parameter binding (`:name` -> `$N` / `%s`)."""

import pytest

from warp.adapters.outbound.db.dialect import MSSQL, POSTGRESQL
from warp.adapters.outbound.db.params import bind_named_params


class TestPostgreSQL:
    def test_basic(self):
        sql, args = bind_named_params(
            "SELECT 1 WHERE a = :a AND b = :b", {"a": 1, "b": 2}, "postgresql"
        )
        assert sql == "SELECT 1 WHERE a = $1 AND b = $2"
        assert args == [1, 2]

    def test_prefix_names_do_not_collide(self):
        sql, args = bind_named_params(
            "WHERE id = :id AND id_type = :id_type", {"id": 1, "id_type": "x"}, "postgresql"
        )
        assert sql == "WHERE id = $1 AND id_type = $2"
        assert args == [1, "x"]

    def test_repeated_name_reuses_placeholder(self):
        sql, args = bind_named_params("SELECT :v, :v WHERE x = :v", {"v": 7}, "postgresql")
        assert sql == "SELECT $1, $1 WHERE x = $1"
        assert args == [7]

    def test_cast_operator_untouched(self):
        sql, args = bind_named_params("SELECT :n::int, x::text", {"n": "5"}, "postgresql")
        assert sql == "SELECT $1::int, x::text"
        assert args == ["5"]

    def test_string_literals_are_skipped(self):
        sql, args = bind_named_params(
            "SELECT ':not_a_param', 'it''s :x' WHERE a = :a", {"a": 1}, "postgresql"
        )
        assert sql == "SELECT ':not_a_param', 'it''s :x' WHERE a = $1"
        assert args == [1]

    def test_order_follows_first_appearance_not_dict_order(self):
        sql, args = bind_named_params("WHERE b = :b AND a = :a", {"a": "A", "b": "B"}, "postgresql")
        assert sql == "WHERE b = $1 AND a = $2"
        assert args == ["B", "A"]


class TestMySQL:
    def test_repeated_name_repeats_value(self):
        sql, args = bind_named_params("SELECT :v, :v", {"v": 7}, "mysql")
        assert sql == "SELECT %s, %s"
        assert args == [7, 7]

    def test_percent_is_escaped_only_with_params(self):
        sql, args = bind_named_params("SELECT 'a%b' WHERE x LIKE :p", {"p": "%z%"}, "mysql")
        assert sql == "SELECT 'a%%b' WHERE x LIKE %s"
        assert args == ["%z%"]
        # No parameters -> the driver does no formatting -> nothing to escape.
        assert bind_named_params("SELECT 'a%b'", None, "mysql") == ("SELECT 'a%b'", [])


class TestErrors:
    def test_no_params_returns_query_unchanged(self):
        assert bind_named_params("SELECT 1", None, "postgresql") == ("SELECT 1", [])
        assert bind_named_params("SELECT 1", {}, "mysql") == ("SELECT 1", [])
        # Literals and casts are still not placeholders.
        assert bind_named_params("SELECT ':x', y::int", None, "postgresql") == (
            "SELECT ':x', y::int",
            [],
        )

    def test_placeholder_without_any_params_is_missing(self):
        with pytest.raises(ValueError, match="Missing value for query parameter :x"):
            bind_named_params("SELECT :x", None, "postgresql")
        with pytest.raises(ValueError, match="Missing value for query parameter :x"):
            bind_named_params("SELECT :x", {}, "mysql")

    def test_missing_value_raises(self):
        with pytest.raises(ValueError, match="Missing value for query parameter :b"):
            bind_named_params("WHERE a = :a AND b = :b", {"a": 1}, "postgresql")

    def test_unused_value_raises(self):
        with pytest.raises(ValueError, match="Unused query parameter"):
            bind_named_params("WHERE a = :a", {"a": 1, "typo": 2}, "mysql")

    def test_unknown_dialect(self):
        with pytest.raises(ValueError, match="dialect"):
            bind_named_params("SELECT 1", {"a": 1}, "sqlite")


class TestQmark:
    def test_repeated_name_repeats_value(self):
        sql, args = bind_named_params("SELECT :v, :v WHERE x = :v", {"v": 7}, "mssql")
        assert sql == "SELECT ?, ? WHERE x = ?"
        assert args == [7, 7, 7]

    def test_percent_is_not_escaped(self):
        sql, args = bind_named_params("SELECT 'a%b' WHERE x LIKE :p", {"p": "%z%"}, "odbc")
        assert sql == "SELECT 'a%b' WHERE x LIKE ?"
        assert args == ["%z%"]

    def test_casts_and_literals_still_skipped(self):
        sql, args = bind_named_params("SELECT ':x', y::int, :a", {"a": 1}, "sqlserver")
        assert sql == "SELECT ':x', y::int, ?"
        assert args == [1]

    def test_accepts_dialect_object(self):
        assert bind_named_params("WHERE a = :a", {"a": 1}, MSSQL) == ("WHERE a = ?", [1])
        assert bind_named_params("WHERE a = :a", {"a": 1}, POSTGRESQL) == ("WHERE a = $1", [1])
