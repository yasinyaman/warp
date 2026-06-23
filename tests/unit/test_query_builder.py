"""Tests for the dialect-aware SafeQueryBuilder (single source of CRUD SQL)."""
import pytest

from warp.database.query_builder import SafeQueryBuilder

PG = SafeQueryBuilder("postgresql")
MY = SafeQueryBuilder("mysql")


class TestPrimitives:
    def test_quoting(self):
        assert PG.quote("users") == '"users"'
        assert MY.quote("users") == "`users`"

    def test_returning_support(self):
        assert PG.supports_returning is True
        assert MY.supports_returning is False

    def test_invalid_identifier_rejected(self):
        with pytest.raises(ValueError):
            PG.quote("a; DROP TABLE x")


class TestWhere:
    def test_pg_indexed_placeholders(self):
        where, idx, params = PG.build_where([("a", "eq", 1), ("b", "gt", 2)])
        assert where == 'WHERE "a" = $1 AND "b" > $2'
        assert params == [1, 2]
        assert idx == 3

    def test_mysql_positional_placeholders(self):
        where, _, params = MY.build_where([("a", "eq", 1), ("b", "gt", 2)])
        assert where == "WHERE `a` = %s AND `b` > %s"
        assert params == [1, 2]

    def test_like_is_dialect_specific(self):
        pg_clause, _, _ = PG.where_clause("a", "like", "x%", 1)
        my_clause, _, _ = MY.where_clause("a", "like", "x%", 1)
        assert "ILIKE" in pg_clause
        assert "LIKE" in my_clause and "ILIKE" not in my_clause

    def test_in_multiple(self):
        clause, idx, params = PG.where_clause("id", "in", [1, 2, 3], 1)
        assert clause == '"id" IN ($1, $2, $3)'
        assert params == [1, 2, 3]
        assert idx == 4
        clause2, _, params2 = MY.where_clause("id", "in", [1, 2], 1)
        assert clause2 == "`id` IN (%s, %s)"
        assert params2 == [1, 2]

    def test_in_scalar_falls_back_to_eq(self):
        clause, _, params = PG.where_clause("id", "in", 5, 1)
        assert clause == '"id" = $1'
        assert params == [5]

    def test_is_null(self):
        c1, _, p1 = PG.where_clause("d", "is_null", True, 1)
        c2, _, _ = PG.where_clause("d", "is_null", False, 1)
        assert c1 == '"d" IS NULL'
        assert p1 == []
        assert c2 == '"d" IS NOT NULL'

    def test_unknown_operator_defaults_eq(self):
        clause, _, params = MY.where_clause("c", "weird", 7, 1)
        assert clause == "`c` = %s"
        assert params == [7]

    def test_empty_filters(self):
        where, idx, params = PG.build_where([])
        assert where == ""
        assert params == []
        assert idx == 1


class TestStatements:
    def test_insert_pg_has_returning(self):
        sql, params = PG.build_insert("users", {"name": "a", "email": "b"})
        assert sql == 'INSERT INTO "users" ("name", "email") VALUES ($1, $2) RETURNING *'
        assert params == ["a", "b"]

    def test_insert_mysql_no_returning(self):
        sql, params = MY.build_insert("users", {"name": "a"})
        assert sql == "INSERT INTO `users` (`name`) VALUES (%s)"
        assert params == ["a"]

    def test_select_pg(self):
        count, select, params = PG.build_select(
            "users", ["id", "name"], [("status", "eq", "x")],
            {"limit": 10, "offset": 5}, [("id", "desc")],
        )
        assert count == 'SELECT COUNT(*) AS cnt FROM "users" WHERE "status" = $1'
        assert select == (
            'SELECT "id", "name" FROM "users" WHERE "status" = $1 '
            'ORDER BY "id" DESC LIMIT 10 OFFSET 5'
        )
        assert params == ["x"]

    def test_select_star_no_clauses(self):
        count, select, params = MY.build_select("t", None, None, None, None)
        assert count == "SELECT COUNT(*) AS cnt FROM `t`"
        assert select == "SELECT * FROM `t`"
        assert params == []

    def test_update_pg(self):
        sql, params = PG.build_update("users", "id", 5, {"name": "x", "age": 3})
        assert sql == (
            'UPDATE "users" SET "name" = $1, "age" = $2 WHERE "id" = $3 RETURNING *'
        )
        assert params == ["x", 3, 5]

    def test_update_mysql(self):
        sql, params = MY.build_update("users", "id", 5, {"name": "x"})
        assert sql == "UPDATE `users` SET `name` = %s WHERE `id` = %s"
        assert params == ["x", 5]

    def test_delete_pg_returns_id(self):
        sql, params = PG.build_delete("users", "id", 9)
        assert sql == 'DELETE FROM "users" WHERE "id" = $1 RETURNING "id"'
        assert params == [9]

    def test_delete_mysql(self):
        sql, params = MY.build_delete("users", "id", 9)
        assert sql == "DELETE FROM `users` WHERE `id` = %s"
        assert params == [9]

    def test_select_by_id(self):
        sql, params = PG.build_select_by_id("users", "id", 7, ["id", "name"])
        assert sql == 'SELECT "id", "name" FROM "users" WHERE "id" = $1'
        assert params == [7]

    def test_injection_in_identifier_rejected(self):
        with pytest.raises(ValueError):
            PG.build_insert("users", {'name"; DROP TABLE x; --': 1})
