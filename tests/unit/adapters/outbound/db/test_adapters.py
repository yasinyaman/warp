"""SQL construction safety tests for the PostgreSQL and MySQL adapters.

These exercise the identifier sanitizer and WHERE-clause builder directly
(no DB connection needed) to prove that:
- identifiers are whitelist-validated (injection rejected), and
- all values are parameterized (never interpolated into the SQL string).
"""

import pytest

from warp.adapters.outbound.db.mysql import MySQLAdapter
from warp.adapters.outbound.db.postgres import PostgreSQLAdapter

PG = PostgreSQLAdapter({"name": "t"})
MY = MySQLAdapter({"name": "t"})

INJECTION = [
    'x"; DROP TABLE users; --',
    "x; SELECT 1",
    "col--",
    "col name",
    "1col",
    "",
    "a` OR `1`=`1",
]


class TestSanitizeIdentifier:
    @pytest.mark.parametrize("adapter", [PG, MY])
    def test_accepts_valid(self, adapter):
        assert adapter._sanitize_identifier("user_id") == "user_id"

    @pytest.mark.parametrize("adapter", [PG, MY])
    @pytest.mark.parametrize("bad", INJECTION)
    def test_rejects_injection(self, adapter, bad):
        with pytest.raises(ValueError):
            adapter._sanitize_identifier(bad)


class TestPostgresWhereClause:
    @pytest.mark.parametrize(
        "op,expected",
        [
            ("eq", '"age" = $1'),
            ("ne", '"age" != $1'),
            ("gt", '"age" > $1'),
            ("gte", '"age" >= $1'),
            ("lt", '"age" < $1'),
            ("lte", '"age" <= $1'),
            ("like", '"age" ILIKE $1'),
        ],
    )
    def test_operators_parameterized(self, op, expected):
        clause, idx, params = PG._build_where_clause("age", op, 5, 1)
        assert clause == expected
        assert params == [5]
        assert idx == 2

    def test_in_uses_multiple_placeholders(self):
        clause, idx, params = PG._build_where_clause("id", "in", [1, 2, 3], 1)
        assert clause == '"id" IN ($1, $2, $3)'
        assert params == [1, 2, 3]
        assert idx == 4

    def test_is_null(self):
        clause, _, params = PG._build_where_clause("deleted_at", "is_null", True, 1)
        assert clause == '"deleted_at" IS NULL'
        assert params == []

    def test_malicious_value_is_parameterized_not_interpolated(self):
        payload = "1; DROP TABLE users; --"
        clause, _, params = PG._build_where_clause("status", "eq", payload, 1)
        assert clause == '"status" = $1'
        assert params == [payload]  # value bound, never in the SQL text
        assert "DROP" not in clause

    def test_malicious_column_rejected(self):
        with pytest.raises(ValueError):
            PG._build_where_clause("status; DROP TABLE users", "eq", 1, 1)


class TestMySQLWhereClause:
    @pytest.mark.parametrize(
        "op,expected",
        [
            ("eq", "`age` = %s"),
            ("ne", "`age` != %s"),
            ("gt", "`age` > %s"),
            ("like", "`age` LIKE %s"),
        ],
    )
    def test_operators_parameterized(self, op, expected):
        clause, params = MY._build_where_clause("age", op, 5)
        assert clause == expected
        assert params == [5]

    def test_in_uses_multiple_placeholders(self):
        clause, params = MY._build_where_clause(
            "id",
            "in",
            [1, 2],
        )
        assert clause == "`id` IN (%s, %s)"
        assert params == [1, 2]

    def test_is_null(self):
        clause, params = MY._build_where_clause("deleted_at", "is_null", False)
        assert clause == "`deleted_at` IS NOT NULL"
        assert params == []

    def test_malicious_value_is_parameterized(self):
        payload = "1; DROP TABLE users; --"
        clause, params = MY._build_where_clause("status", "eq", payload)
        assert clause == "`status` = %s"
        assert params == [payload]
        assert "DROP" not in clause

    def test_malicious_column_rejected(self):
        with pytest.raises(ValueError):
            MY._build_where_clause("status`; DROP", "eq", 1)
