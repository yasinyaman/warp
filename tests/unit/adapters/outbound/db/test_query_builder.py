"""Tests for the dialect-aware SafeQueryBuilder (single source of CRUD SQL)."""

import pytest

from warp.adapters.outbound.db.dialect import MSSQL
from warp.adapters.outbound.db.query_builder import SafeQueryBuilder

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
            "users",
            ["id", "name"],
            [("status", "eq", "x")],
            {"limit": 10, "offset": 5},
            [("id", "desc")],
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
        assert sql == ('UPDATE "users" SET "name" = $1, "age" = $2 WHERE "id" = $3 RETURNING *')
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


class TestStreamSelect:
    """A streamed read has no COUNT and no paging, and caps rows per dialect."""

    def test_pg_select_with_filter_sort_and_limit(self):
        sql, params = PG.build_stream_select(
            "users", ["id", "name"], [("status", "eq", "x")], [("id", "desc")], limit=10
        )
        assert sql == (
            'SELECT "id", "name" FROM "users" WHERE "status" = $1 '
            'ORDER BY "id" DESC LIMIT 10 OFFSET 0'
        )
        assert params == ["x"]
        assert "COUNT" not in sql.upper()

    def test_mysql_star_without_clauses(self):
        sql, params = MY.build_stream_select("t", None, None, None)
        assert sql == "SELECT * FROM `t`"
        assert params == []

    def test_sql_server_uses_offset_fetch(self):
        sql, _ = MS.build_stream_select("t", None, None, [("id", "asc")], limit=10)
        assert sql == "SELECT * FROM [t] ORDER BY [id] ASC OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
        # OFFSET/FETCH needs an ORDER BY, so one is supplied when the caller has none.
        unsorted, _ = MS.build_stream_select("t", None, None, None, limit=5)
        assert unsorted == (
            "SELECT * FROM [t] ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY"
        )

    def test_no_limit_means_no_pagination_tail(self):
        sql, _ = PG.build_stream_select("t", None, [("a", "in", [1, 2])], None, limit=None)
        assert sql == 'SELECT * FROM "t" WHERE "a" IN ($1, $2)'
        assert "OFFSET" not in sql and "LIMIT" not in sql
        assert MS.build_stream_select("t", None, None, None)[0] == "SELECT * FROM [t]"

    def test_identifiers_validated(self):
        with pytest.raises(ValueError):
            PG.build_stream_select('t"; DROP', None, None, None)
        with pytest.raises(ValueError):
            PG.build_stream_select("t", ["id; DROP TABLE x"], None, None)

    def test_the_row_cap_is_coerced_to_an_integer(self):
        # The cap is the only value interpolated into the statement.
        assert MY.build_stream_select("t", None, None, None, limit=7)[0].endswith(
            "LIMIT 7 OFFSET 0"
        )
        with pytest.raises((TypeError, ValueError)):
            MY.build_stream_select("t", None, None, None, limit="7; DROP TABLE t")  # type: ignore[arg-type]


MS = SafeQueryBuilder("mssql")


class TestSQLServer:
    def test_primitives(self):
        assert MS.quote("users") == "[users]"
        assert MS.supports_returning is True
        assert MS.returning_style == "output"
        assert PG.returning_style == "returning"
        assert MY.returning_style == "refetch"

    def test_builder_accepts_dialect_object_and_aliases(self):
        assert SafeQueryBuilder(MSSQL).dialect is MSSQL
        assert SafeQueryBuilder("sqlserver").dialect is MSSQL

    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError, match="dialect"):
            SafeQueryBuilder("sqlite")

    def test_where_uses_qmark_and_like(self):
        where, idx, params = MS.build_where(
            [("a", "eq", 1), ("b", "like", "x%"), ("c", "in", [1, 2])]
        )
        assert where == "WHERE [a] = ? AND [b] LIKE ? AND [c] IN (?, ?)"
        assert params == [1, "x%", 1, 2]
        assert idx == 5

    def test_insert_output_before_values(self):
        sql, params = MS.build_insert("users", {"name": "a", "email": "b"})
        assert sql == "INSERT INTO [users] ([name], [email]) OUTPUT INSERTED.* VALUES (?, ?)"
        assert params == ["a", "b"]

    def test_insert_without_returning(self):
        sql, _ = MS.build_insert("users", {"name": "a"}, returning=False)
        assert sql == "INSERT INTO [users] ([name]) VALUES (?)"
        pg_sql, _ = PG.build_insert("users", {"name": "a"}, returning=False)
        assert pg_sql == 'INSERT INTO "users" ("name") VALUES ($1)'

    def test_update_output(self):
        sql, params = MS.build_update("users", "id", 5, {"name": "x", "age": 3})
        assert sql == "UPDATE [users] SET [name] = ?, [age] = ? OUTPUT INSERTED.* WHERE [id] = ?"
        assert params == ["x", 3, 5]
        plain, _ = MS.build_update("users", "id", 5, {"name": "x"}, returning=False)
        assert plain == "UPDATE [users] SET [name] = ? WHERE [id] = ?"

    def test_delete_output(self):
        sql, params = MS.build_delete("users", "id", 9)
        assert sql == "DELETE FROM [users] OUTPUT DELETED.[id] WHERE [id] = ?"
        assert params == [9]
        plain, _ = MS.build_delete("users", "id", 9, returning=False)
        assert plain == "DELETE FROM [users] WHERE [id] = ?"

    def test_select_offset_fetch_with_sort(self):
        count, select, params = MS.build_select(
            "users",
            ["id"],
            [("status", "eq", "x")],
            {"limit": 10, "offset": 5},
            [("id", "desc")],
        )
        assert count == "SELECT COUNT(*) AS cnt FROM [users] WHERE [status] = ?"
        assert select == (
            "SELECT [id] FROM [users] WHERE [status] = ? "
            "ORDER BY [id] DESC OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY"
        )
        assert params == ["x"]

    def test_select_offset_fetch_injects_dummy_order(self):
        _, select, _ = MS.build_select("users", None, None, {"limit": 10, "offset": 0}, None)
        assert select == (
            "SELECT * FROM [users] ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
        )

    def test_select_without_pagination(self):
        _, select, _ = MS.build_select("users", None, None, None, [("id", "asc")])
        assert select == "SELECT * FROM [users] ORDER BY [id] ASC"

    def test_select_no_double_spaces_without_order(self):
        _, select, _ = PG.build_select("t", None, [("a", "eq", 1)], {"limit": 5, "offset": 0}, None)
        assert select == 'SELECT * FROM "t" WHERE "a" = $1 LIMIT 5 OFFSET 0'

    def test_select_by_id(self):
        sql, params = MS.build_select_by_id("users", "id", 7, None)
        assert sql == "SELECT * FROM [users] WHERE [id] = ?"
        assert params == [7]
