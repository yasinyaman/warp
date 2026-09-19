"""
Tests for schema models and analyzer.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

import pytest

from warp.application.services.schema_discovery import DB_TYPE_MAPPING, SchemaAnalyzer
from warp.domain.schema import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    IndexSchema,
    TableSchema,
)

# ===========================================
# ColumnSchema Tests
# ===========================================


class TestColumnSchema:
    """Tests for ColumnSchema model."""

    def test_minimal_column(self):
        """Test column with minimal fields."""
        col = ColumnSchema(name="id", type="integer")

        assert col.name == "id"
        assert col.type == "integer"
        assert col.nullable is True  # default
        assert col.default is None

    def test_full_column(self):
        """Test column with all fields."""
        col = ColumnSchema(
            name="price",
            type="decimal",
            full_type="decimal(10,2)",
            nullable=False,
            default="0.00",
            max_length=None,
            precision=10,
            scale=2,
        )

        assert col.precision == 10
        assert col.scale == 2
        assert col.nullable is False


# ===========================================
# ForeignKeySchema Tests
# ===========================================


class TestForeignKeySchema:
    """Tests for ForeignKeySchema model."""

    def test_foreign_key(self):
        """Test foreign key definition."""
        fk = ForeignKeySchema(
            column="user_id",
            references_table="users",
            references_column="id",
            constraint_name="fk_orders_user",
        )

        assert fk.column == "user_id"
        assert fk.references_table == "users"
        assert fk.references_column == "id"


# ===========================================
# IndexSchema Tests
# ===========================================


class TestIndexSchema:
    """Tests for IndexSchema model."""

    def test_simple_index(self):
        """Test simple index."""
        idx = IndexSchema(name="idx_users_email", columns=["email"], unique=True)

        assert idx.name == "idx_users_email"
        assert idx.columns == ["email"]
        assert idx.unique is True

    def test_composite_index(self):
        """Test composite index."""
        idx = IndexSchema(
            name="idx_orders_user_status", columns=["user_id", "status"], unique=False
        )

        assert len(idx.columns) == 2


# ===========================================
# TableSchema Tests
# ===========================================


class TestTableSchema:
    """Tests for TableSchema model."""

    @pytest.fixture
    def users_schema(self):
        """Create a sample users table schema."""
        return TableSchema(
            table_name="users",
            columns=[
                ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
                ColumnSchema(name="username", type="varchar", nullable=False),
                ColumnSchema(name="email", type="varchar", nullable=False),
                ColumnSchema(name="status", type="varchar", nullable=True, default="'active'"),
                ColumnSchema(
                    name="created_at", type="timestamp", nullable=True, default="CURRENT_TIMESTAMP"
                ),
            ],
            primary_key="id",
            foreign_keys=[],
            indexes=[IndexSchema(name="idx_email", columns=["email"], unique=True)],
        )

    def test_key_columns_single(self, users_schema):
        """A single-column key is still a key: one entry, not a bare string."""
        assert users_schema.key_columns == ("id",)
        assert users_schema.has_key is True

    def test_key_columns_composite_keeps_every_column(self):
        """The whole key travels.

        This used to answer `"order_id"` — the first column — and every
        key-addressed statement was built from it alone, so `DELETE` on one
        line reached every line of the order.
        """
        schema = TableSchema(
            table_name="order_items", columns=[], primary_key=["order_id", "product_id"]
        )
        assert schema.key_columns == ("order_id", "product_id")
        assert schema.has_composite_pk is True
        assert schema.has_key is True

    def test_a_table_with_no_key_says_so(self):
        """No key means no key-addressed routes, not a fabricated ``id``."""
        schema = TableSchema(table_name="logs", columns=[], primary_key=None)
        assert schema.key_columns == ()
        assert schema.has_key is False

    def test_get_column(self, users_schema):
        """Test getting column by name."""
        col = users_schema.get_column("email")
        assert col is not None
        assert col.name == "email"

        # Non-existent
        col = users_schema.get_column("nonexistent")
        assert col is None

    def test_get_column_names(self, users_schema):
        """Test getting all column names."""
        names = users_schema.get_column_names()
        assert names == ["id", "username", "email", "status", "created_at"]

    def test_get_required_columns(self, users_schema):
        """Test getting required columns (non-nullable without default)."""
        required = users_schema.get_required_columns()

        # id is auto_increment, so not required for insert
        # username and email are required
        assert "username" in required
        assert "email" in required
        assert "id" not in required  # auto_increment
        assert "status" not in required  # has default
        assert "created_at" not in required  # has default

    def test_get_insertable_columns(self, users_schema):
        """Test getting insertable columns."""
        insertable = users_schema.get_insertable_columns()

        assert "username" in insertable
        assert "email" in insertable
        assert "status" in insertable
        assert "id" not in insertable  # auto_increment


# ===========================================
# DatabaseSchema Tests
# ===========================================


class TestDatabaseSchema:
    """Tests for DatabaseSchema model."""

    def test_empty_schema(self):
        """Test empty database schema."""
        schema = DatabaseSchema(database_name="test")
        assert schema.database_name == "test"
        assert schema.tables == {}

    def test_get_table(self):
        """Test getting table by name."""
        schema = DatabaseSchema(database_name="test")
        users_table = TableSchema(table_name="users", columns=[])
        schema.tables["users"] = users_table

        assert schema.get_table("users") is users_table
        assert schema.get_table("nonexistent") is None

    def test_get_table_names(self):
        """Test getting all table names."""
        schema = DatabaseSchema(database_name="test")
        schema.tables["users"] = TableSchema(table_name="users", columns=[])
        schema.tables["orders"] = TableSchema(table_name="orders", columns=[])

        names = schema.get_table_names()
        assert set(names) == {"users", "orders"}


# ===========================================
# SchemaAnalyzer Tests
# ===========================================


class TestSchemaAnalyzer:
    """Tests for SchemaAnalyzer."""

    @pytest.fixture
    def analyzer(self, mock_db_with_data):
        """Create analyzer with mock database."""
        return SchemaAnalyzer(mock_db_with_data)

    @pytest.mark.asyncio
    async def test_analyze_returns_database_schema(self, analyzer):
        """Test analyze returns DatabaseSchema."""
        schema = await analyzer.analyze()

        assert isinstance(schema, DatabaseSchema)
        assert "users" in schema.tables
        assert "products" in schema.tables

    @pytest.mark.asyncio
    async def test_analyze_excludes_tables(self, mock_db_with_data):
        """Test excluded tables are not analyzed."""
        analyzer = SchemaAnalyzer(mock_db_with_data, excluded_tables=["products"])
        schema = await analyzer.analyze()

        assert "users" in schema.tables
        assert "products" not in schema.tables

    @pytest.mark.asyncio
    async def test_analyze_table(self, analyzer):
        """Test analyzing single table."""
        table_schema = await analyzer.analyze_table("users")

        assert table_schema.table_name == "users"
        assert table_schema.primary_key == "id"


class TestSchemaAnalyzerPydanticModels:
    """Tests for Pydantic model generation."""

    @pytest.fixture
    def analyzer(self, mock_db_with_data):
        """Create analyzer with mock database."""
        return SchemaAnalyzer(mock_db_with_data)

    def test_generate_base_model(self, analyzer, sample_table_schema):
        """Test generating base Pydantic model."""
        Model = analyzer.generate_pydantic_model(sample_table_schema)

        # Check model was created
        assert Model is not None
        assert Model.__name__ == "TestTable"

        # Check fields exist
        fields = Model.model_fields
        assert "id" in fields
        assert "name" in fields
        assert "price" in fields

    def test_generate_create_model(self, analyzer, sample_table_schema):
        """Test generating create model (without auto fields)."""
        # Add auto_increment to id
        sample_table_schema.columns[0].extra = "auto_increment"

        Model = analyzer.generate_pydantic_model(sample_table_schema, for_create=True)

        assert Model.__name__ == "TestTableCreate"

        # id should not be in create model
        fields = Model.model_fields
        assert "id" not in fields
        assert "name" in fields

    def test_generate_create_model_skips_identity_and_computed(self, analyzer, sample_table_schema):
        """SQL Server identity/computed columns are never client-supplied."""
        sample_table_schema.columns[0].extra = "identity"
        sample_table_schema.columns.append(
            ColumnSchema(name="total", type="money", nullable=True, extra="computed")
        )

        Model = analyzer.generate_pydantic_model(sample_table_schema, for_create=True)

        fields = Model.model_fields
        assert "id" not in fields and "total" not in fields
        assert "name" in fields

    def test_generate_update_model(self, analyzer, sample_table_schema):
        """Test generating update model (all optional)."""
        Model = analyzer.generate_pydantic_model(sample_table_schema, for_update=True)

        assert Model.__name__ == "TestTableUpdate"

        # All fields should be optional
        fields = Model.model_fields
        for field_info in fields.values():
            # In Pydantic v2, check if default is None
            assert field_info.default is None

    def test_request_models_forbid_unknown_fields(self, analyzer, sample_table_schema):
        import pydantic

        models = analyzer.generate_crud_models(sample_table_schema)
        with pytest.raises(pydantic.ValidationError):
            models["create"](bogus=1)
        with pytest.raises(pydantic.ValidationError):
            models["update"](bogus=1)
        # Response/base models keep pydantic's default and tolerate extra keys.
        assert models["create"].model_config.get("extra") == "forbid"
        assert models["update"].model_config.get("extra") == "forbid"
        assert models["response"].model_config.get("extra") != "forbid"
        assert models["base"].model_config.get("extra") != "forbid"

    def test_generate_crud_models(self, analyzer, sample_table_schema):
        """Test generating all CRUD models at once."""
        models = analyzer.generate_crud_models(sample_table_schema)

        assert "base" in models
        assert "create" in models
        assert "update" in models
        assert "response" in models

    def test_model_caching(self, analyzer, sample_table_schema):
        """Test that models are cached."""
        Model1 = analyzer.generate_pydantic_model(sample_table_schema)
        Model2 = analyzer.generate_pydantic_model(sample_table_schema)

        # Should be the same object (cached)
        assert Model1 is Model2

    def test_custom_model_name(self, analyzer, sample_table_schema):
        """Test custom model name."""
        Model = analyzer.generate_pydantic_model(sample_table_schema, model_name="CustomName")

        assert Model.__name__ == "CustomName"


class TestDBTypeMapping:
    """Tests for database type to Python type mapping."""

    def test_integer_types(self):
        """Test integer type mappings."""
        assert DB_TYPE_MAPPING["integer"] is int
        assert DB_TYPE_MAPPING["bigint"] is int
        assert DB_TYPE_MAPPING["smallint"] is int
        assert DB_TYPE_MAPPING["int"] is int

    def test_float_types(self):
        """Test float type mappings."""
        assert DB_TYPE_MAPPING["real"] is float
        assert DB_TYPE_MAPPING["double precision"] is float
        assert DB_TYPE_MAPPING["numeric"] is float
        assert DB_TYPE_MAPPING["decimal"] is float

    def test_string_types(self):
        """Test string type mappings."""
        assert DB_TYPE_MAPPING["character varying"] is str
        assert DB_TYPE_MAPPING["varchar"] is str
        assert DB_TYPE_MAPPING["text"] is str
        assert DB_TYPE_MAPPING["uuid"] is UUID  # drivers return uuid.UUID objects

    def test_boolean_types(self):
        """Test boolean type mappings."""
        assert DB_TYPE_MAPPING["boolean"] is bool
        assert DB_TYPE_MAPPING["bit"] is bool

    def test_json_types(self):
        """Test JSON type mappings."""
        assert DB_TYPE_MAPPING["json"] is Any  # any JSON value (drivers may return text)
        assert DB_TYPE_MAPPING["jsonb"] is Any

    def test_datetime_types(self):
        """Test datetime type mappings (stored as strings)."""
        assert DB_TYPE_MAPPING["timestamp"] is datetime  # what asyncpg/aiomysql return
        assert DB_TYPE_MAPPING["date"] is date
        assert DB_TYPE_MAPPING["datetime"] is datetime


class TestPascalCaseConversion:
    """Tests for snake_case to PascalCase conversion."""

    def test_simple_conversion(self):
        """Test simple snake_case conversion."""
        assert SchemaAnalyzer._to_pascal_case("users") == "Users"
        assert SchemaAnalyzer._to_pascal_case("order_items") == "OrderItems"
        assert SchemaAnalyzer._to_pascal_case("user_profile_settings") == "UserProfileSettings"

    def test_single_word(self):
        """Test single word conversion."""
        assert SchemaAnalyzer._to_pascal_case("user") == "User"

    def test_already_capitalized(self):
        """Test already capitalized words."""
        assert SchemaAnalyzer._to_pascal_case("User") == "User"


class TestTypeKind:
    """Coarse kinds used for id parsing and typed filters."""

    def test_kinds(self):
        from warp.domain.sql_types import python_type_for, type_kind

        assert type_kind("integer") == "int"
        assert type_kind("bigserial") == "int"
        assert type_kind("numeric") == "float"
        assert type_kind("boolean") == "bool"
        assert type_kind("character varying") == "str"
        assert type_kind("uuid") == "uuid"
        assert type_kind("timestamp with time zone") == "datetime"
        assert type_kind("date") == "date"
        assert type_kind("time") == "time"
        assert type_kind("jsonb") == "json"
        assert type_kind("bytea") == "bytes"
        assert type_kind("USER-DEFINED", udt_name="_int4") == "list"
        assert type_kind("ARRAY", udt_name="_text") == "list"
        assert type_kind("USER-DEFINED", udt_name="mood_enum") == "str"  # PG enum/domain
        assert type_kind("tinyint", full_type="tinyint(1)") == "bool"
        assert type_kind("tinyint", full_type="tinyint(4)") == "int"
        assert type_kind("interval") == "str"  # unknown -> text passthrough
        assert python_type_for("point") is str


class TestSQLServerTypes:
    def test_sql_server_and_odbc_types_are_classified(self):
        from warp.domain.sql_types import python_type_for, type_kind

        assert python_type_for("nvarchar") is str
        assert python_type_for("uniqueidentifier") is UUID
        assert python_type_for("datetime2") is datetime
        assert python_type_for("datetimeoffset") is datetime
        assert type_kind("bit") == "bool"
        assert type_kind("money") == "float"
        assert type_kind("smallmoney") == "float"
        assert type_kind("rowversion") == "bytes"
        assert type_kind("varbinary") == "bytes"
        assert type_kind("xml") == "str"
        assert type_kind("number") == "float"
        assert type_kind("varchar2") == "str"
        assert type_kind("raw") == "bytes"


class TestOracleTypes:
    """Oracle bakes precision into the type name, unlike every other engine here."""

    def test_parameterized_type_names_resolve(self):
        from warp.domain.sql_types import python_type_for, type_kind

        assert type_kind("TIMESTAMP(6)") == "datetime"
        assert type_kind("TIMESTAMP(6) WITH TIME ZONE") == "datetime"
        assert type_kind("TIMESTAMP(9) WITH LOCAL TIME ZONE") == "datetime"
        assert python_type_for("NUMBER(10,0)") is float
        assert type_kind("VARCHAR2(255)") == "str"
        assert type_kind("RAW(16)") == "bytes"

    def test_interval_types_pass_through_as_text(self):
        from warp.domain.sql_types import type_kind

        assert type_kind("INTERVAL DAY(2) TO SECOND(6)") == "str"
        assert type_kind("INTERVAL YEAR(4) TO MONTH") == "str"

    def test_plain_oracle_types(self):
        from warp.domain.sql_types import type_kind

        assert type_kind("NUMBER") == "float"
        assert type_kind("BINARY_DOUBLE") == "float"
        assert type_kind("BINARY_FLOAT") == "float"
        assert type_kind("CLOB") == "str"
        assert type_kind("NCLOB") == "str"
        assert type_kind("BFILE") == "bytes"
        assert type_kind("ROWID") == "str"

    def test_an_unknown_parameterized_type_still_degrades_to_text(self):
        from warp.domain.sql_types import type_kind

        assert type_kind("SDO_GEOMETRY(4)") == "str"
