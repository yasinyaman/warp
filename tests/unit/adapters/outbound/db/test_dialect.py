"""Tests for the Dialect descriptors and registry."""

import logging

import pytest

from warp.adapters.outbound.db.dialect import (
    DIALECTS,
    MSSQL,
    MYSQL,
    ODBC,
    POSTGRESQL,
    CommentQueries,
    Dialect,
    dialect_or_generic,
    get_dialect,
)


class TestRegistry:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("postgresql", POSTGRESQL),
            ("postgres", POSTGRESQL),
            ("MySQL", MYSQL),
            ("mariadb", MYSQL),
            ("mssql", MSSQL),
            ("SQLServer", MSSQL),
            ("odbc", ODBC),
        ],
    )
    def test_get_dialect_by_name(self, name, expected):
        assert get_dialect(name) is expected

    def test_get_dialect_passes_objects_through(self):
        assert get_dialect(MSSQL) is MSSQL

    def test_unknown_dialect_is_an_error(self):
        with pytest.raises(ValueError, match="Unsupported SQL dialect"):
            get_dialect("sqlite")

    def test_dialect_or_generic_falls_back_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert dialect_or_generic("sqlite") is ODBC
        assert "sqlite" in caplog.text
        assert dialect_or_generic("postgres") is POSTGRESQL
        assert dialect_or_generic(MYSQL) is MYSQL

    def test_registered_dialects_are_immutable(self):
        for dialect in DIALECTS.values():
            with pytest.raises(AttributeError):
                dialect.name = "x"  # type: ignore[misc]


class TestPlaceholdersAndQuoting:
    def test_placeholder_styles(self):
        assert POSTGRESQL.placeholder(3) == "$3"
        assert MYSQL.placeholder(3) == "%s"
        assert MSSQL.placeholder(3) == "?"
        assert ODBC.placeholder(1) == "?"

    def test_quote_chars(self):
        assert POSTGRESQL.quote("users") == '"users"'
        assert MYSQL.quote("users") == "`users`"
        assert MSSQL.quote("users") == "[users]"
        assert ODBC.quote("users") == '"users"'

    @pytest.mark.parametrize("bad", ["a]b", "a]; DROP TABLE x; --", "col name", ""])
    def test_quote_validates(self, bad):
        with pytest.raises(ValueError):
            MSSQL.quote(bad)


class TestDefaultSchema:
    def test_dialect_defaults(self):
        assert POSTGRESQL.default_schema("mydb") == "public"
        assert MYSQL.default_schema("mydb") == "mydb"
        assert MSSQL.default_schema("mydb") == "dbo"
        assert ODBC.default_schema("mydb") == ""

    def test_configured_schema_wins(self):
        assert MSSQL.default_schema("mydb", {"schema": "sales"}) == "sales"
        assert POSTGRESQL.default_schema("mydb", {"schema": "app"}) == "app"
        assert MSSQL.default_schema("mydb", {"schema": ""}) == "dbo"
        assert MYSQL.default_schema("mydb", {"pool_size": 3}) == "mydb"


class TestPagination:
    def test_limit_offset(self):
        assert POSTGRESQL.order_and_limit("", None) == ""
        assert POSTGRESQL.order_and_limit('ORDER BY "id" ASC', None) == 'ORDER BY "id" ASC'
        assert POSTGRESQL.order_and_limit("", 10, 5) == "LIMIT 10 OFFSET 5"
        assert (
            MYSQL.order_and_limit("ORDER BY `id` ASC", 10, 5)
            == "ORDER BY `id` ASC LIMIT 10 OFFSET 5"
        )

    def test_offset_fetch_requires_an_order(self):
        assert MSSQL.order_and_limit("", None) == ""
        assert MSSQL.order_and_limit("ORDER BY [id] DESC", None) == "ORDER BY [id] DESC"
        assert MSSQL.order_and_limit("ORDER BY [id] DESC", 10, 5) == (
            "ORDER BY [id] DESC OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY"
        )
        assert MSSQL.order_and_limit("", 10, 0) == (
            "ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
        )

    def test_sample_select(self):
        assert POSTGRESQL.sample_select('"public"."t"', 5) == 'SELECT * FROM "public"."t" LIMIT 5'
        assert MYSQL.sample_select("`t`", 1) == "SELECT * FROM `t` LIMIT 1"
        assert MSSQL.sample_select("[dbo].[t]", 5) == "SELECT TOP (5) * FROM [dbo].[t]"


class TestCatalogQueries:
    def test_postgres_and_mysql_scope(self):
        assert POSTGRESQL.comment_queries is not None
        assert POSTGRESQL.comment_queries.scope_param == "schema"
        assert MYSQL.comment_queries is not None
        assert MYSQL.comment_queries.scope_param == "database"
        assert POSTGRESQL.row_count_sql is not None
        assert ":schema" in POSTGRESQL.row_count_sql
        assert MYSQL.row_count_sql is not None
        assert ":table_name" in MYSQL.row_count_sql

    def test_sqlserver_catalog_queries(self):
        assert MSSQL.comment_queries is not None
        assert MSSQL.comment_queries.scope_param == "schema"
        for sql in (
            MSSQL.comment_queries.table,
            MSSQL.comment_queries.columns,
            MSSQL.comment_queries.all_tables,
            MSSQL.comment_queries.all_columns,
        ):
            assert "sys.extended_properties" in sql and "MS_Description" in sql
            assert "CAST(ep.value AS nvarchar(max))" in sql
        assert MSSQL.row_count_sql is not None
        assert "sys.partitions" in MSSQL.row_count_sql
        # Every statement binds cleanly to qmark placeholders.
        from warp.adapters.outbound.db.params import bind_named_params

        for sql in (
            MSSQL.comment_queries.table,
            MSSQL.comment_queries.columns,
            MSSQL.row_count_sql,
        ):
            bound, args = bind_named_params(sql, {"schema": "dbo", "table_name": "t"}, MSSQL)
            assert bound.count("?") == 2 and args == ["dbo", "t"]
        for sql in (MSSQL.comment_queries.all_tables, MSSQL.comment_queries.all_columns):
            bound, args = bind_named_params(sql, {"schema": "dbo"}, MSSQL)
            assert bound.count("?") == 1 and args == ["dbo"]

    def test_generic_has_no_catalog_intelligence(self):
        assert ODBC.comment_queries is None
        assert ODBC.row_count_sql is None

    def test_custom_dialect(self):
        custom = Dialect(
            name="x",
            placeholder_style="qmark",
            quote_chars=('"', '"'),
            like_operator="LIKE",
            returning_style="refetch",
            limit_style="limit_offset",
            comment_queries=CommentQueries(
                table="t", columns="c", all_tables="at", all_columns="ac"
            ),
        )
        assert custom.comment_queries is not None
        assert custom.comment_queries.scope_param == "schema"
        assert get_dialect(custom) is custom
        assert custom.escape_percent is False
        assert custom.schema_qualified is True


class TestPaginationIsAlwaysInteger:
    """Pagination is the only value interpolated into SQL, so it must be a number."""

    @pytest.mark.parametrize("dialect", [POSTGRESQL, MYSQL, MSSQL])
    def test_a_non_numeric_limit_is_rejected(self, dialect):
        with pytest.raises((TypeError, ValueError)):
            dialect.order_and_limit("", "7; DROP TABLE t")  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            dialect.order_and_limit("", 7, "0; DROP TABLE t")  # type: ignore[arg-type]

    @pytest.mark.parametrize("dialect", [POSTGRESQL, MYSQL, MSSQL])
    def test_a_numeric_string_is_coerced(self, dialect):
        sql = dialect.order_and_limit("", "7", "3")  # type: ignore[arg-type]
        assert "7" in sql and "3" in sql and ";" not in sql

    @pytest.mark.parametrize("dialect", [POSTGRESQL, MYSQL, MSSQL])
    def test_a_sample_row_cap_is_an_integer_too(self, dialect):
        assert "5" in dialect.sample_select("[t]", "5")  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            dialect.sample_select("[t]", "5; DROP TABLE t")  # type: ignore[arg-type]
