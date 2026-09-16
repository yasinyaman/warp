"""Tests for enrichment modules: CommentReader and EnrichedAnalyzer."""

import json
from unittest.mock import AsyncMock

import pytest

from warp.config.settings import Settings
from warp.enrichment.analyzer import EnrichedAnalyzer
from warp.enrichment.comment_reader import CommentReader, TableComments
from warp.llm.client import LLMClient, LLMProvider

# ===========================================
# Mock Classes
# ===========================================


class MockLLMProvider(LLMProvider):
    """Mock LLM provider that returns predefined responses."""

    def __init__(self, response: str = ""):
        self._response = response

    async def generate(
        self,
        prompt,
        system_prompt=None,
        temperature=0.3,
        max_tokens=4096,
        response_format=None,
    ):
        return self._response

    async def close(self):
        pass


class MockAdapter:
    """Mock warp DatabaseAdapter."""

    def __init__(self):
        self.get_tables = AsyncMock(return_value=["users", "orders"])
        self.get_table_schema = AsyncMock()
        self.execute_query = AsyncMock(return_value=[])


# ===========================================
# CommentReader Fixtures
# ===========================================


@pytest.fixture
def mock_adapter():
    return MockAdapter()


@pytest.fixture
def pg_reader(mock_adapter):
    return CommentReader(
        adapter=mock_adapter,
        db_type="postgresql",
        schema="public",
    )


@pytest.fixture
def mysql_reader(mock_adapter):
    return CommentReader(
        adapter=mock_adapter,
        db_type="mysql",
        schema="mydb",
        database="mydb",
    )


# ===========================================
# CommentReader Tests
# ===========================================


class TestCommentReaderPostgreSQL:
    """Tests for PostgreSQL comment reading."""

    @pytest.mark.asyncio
    async def test_read_table_comment(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.return_value = [{"comment": "Registered user accounts"}]

        result = await pg_reader.read_table_comment("users")

        assert result == "Registered user accounts"
        mock_adapter.execute_query.assert_called_once()
        sql, params = mock_adapter.execute_query.call_args.args
        assert ":table_name" in sql and ":schema" in sql and "$1" not in sql
        assert params == {"table_name": "users", "schema": "public"}

    @pytest.mark.asyncio
    async def test_read_table_comment_none(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.return_value = [{"comment": None}]

        result = await pg_reader.read_table_comment("users")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_table_comment_empty(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.return_value = []

        result = await pg_reader.read_table_comment("users")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_column_comments(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.return_value = [
            {"column_name": "email", "comment": "User email address"},
            {"column_name": "status", "comment": "Account status: active/inactive"},
        ]

        result = await pg_reader.read_column_comments("users")

        assert result == {
            "email": "User email address",
            "status": "Account status: active/inactive",
        }

    @pytest.mark.asyncio
    async def test_read_column_comments_empty(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.return_value = []

        result = await pg_reader.read_column_comments("users")

        assert result == {}

    @pytest.mark.asyncio
    async def test_read_table_comments_combined(self, pg_reader, mock_adapter):
        # First call: table comment, second call: column comments
        mock_adapter.execute_query.side_effect = [
            [{"comment": "User accounts"}],
            [{"column_name": "email", "comment": "Email address"}],
        ]

        result = await pg_reader.read_table_comments("users")

        assert isinstance(result, TableComments)
        assert result.table_name == "users"
        assert result.table_comment == "User accounts"
        assert result.column_comments == {"email": "Email address"}

    @pytest.mark.asyncio
    async def test_read_all_comments_batch(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.side_effect = [
            # All table comments
            [
                {"table_name": "users", "comment": "User accounts"},
                {"table_name": "orders", "comment": "Customer orders"},
            ],
            # All column comments
            [
                {"table_name": "users", "column_name": "email", "comment": "Email"},
                {"table_name": "orders", "column_name": "total", "comment": "Order total"},
            ],
        ]

        result = await pg_reader.read_all_comments(["users", "orders"])

        assert len(result) == 2
        assert result["users"].table_comment == "User accounts"
        assert result["users"].column_comments["email"] == "Email"
        assert result["orders"].table_comment == "Customer orders"

    @pytest.mark.asyncio
    async def test_read_all_comments_filters_tables(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.side_effect = [
            [
                {"table_name": "users", "comment": "Users"},
                {"table_name": "orders", "comment": "Orders"},
                {"table_name": "products", "comment": "Products"},
            ],
            [
                {"table_name": "users", "column_name": "id", "comment": "PK"},
                {"table_name": "products", "column_name": "name", "comment": "Name"},
            ],
        ]

        # Only request users
        result = await pg_reader.read_all_comments(["users"])

        assert len(result) == 1
        assert "users" in result

    @pytest.mark.asyncio
    async def test_error_handling(self, pg_reader, mock_adapter):
        mock_adapter.execute_query.side_effect = Exception("Connection error")

        result = await pg_reader.read_table_comment("users")
        assert result is None

        result = await pg_reader.read_column_comments("users")
        assert result == {}


class TestCommentReaderMySQL:
    """Tests for MySQL comment reading."""

    @pytest.mark.asyncio
    async def test_read_table_comment(self, mysql_reader, mock_adapter):
        mock_adapter.execute_query.return_value = [{"comment": "Customer orders"}]

        result = await mysql_reader.read_table_comment("orders")

        assert result == "Customer orders"

    @pytest.mark.asyncio
    async def test_read_column_comments(self, mysql_reader, mock_adapter):
        mock_adapter.execute_query.return_value = [
            {"column_name": "total", "comment": "Order total amount"},
        ]

        result = await mysql_reader.read_column_comments("orders")

        assert result == {"total": "Order total amount"}


class TestCommentReaderUnsupportedDB:
    """Tests for unsupported database types."""

    @pytest.mark.asyncio
    async def test_unsupported_db_returns_none(self, mock_adapter):
        reader = CommentReader(adapter=mock_adapter, db_type="sqlite")

        assert await reader.read_table_comment("test") is None
        assert await reader.read_column_comments("test") == {}


# ===========================================
# EnrichedAnalyzer Fixtures
# ===========================================


@pytest.fixture
def analyzer_adapter():
    adapter = MockAdapter()
    adapter.get_table_schema.return_value = {
        "columns": [
            {"name": "id", "type": "integer", "nullable": False, "key": "PRI"},
            {"name": "email", "type": "varchar", "nullable": False, "max_length": 255},
            {"name": "name", "type": "varchar", "nullable": True, "max_length": 100},
            {"name": "status", "type": "varchar", "nullable": False, "max_length": 20},
            {"name": "created_at", "type": "timestamp", "nullable": False},
        ],
        "foreign_keys": [],
        "indexes": [
            {"name": "idx_email", "columns": ["email"], "unique": True},
        ],
        "primary_key": "id",
    }
    return adapter


@pytest.fixture
def llm_response():
    return json.dumps(
        {
            "table_description": {
                "en": "User accounts table",
                "tr": "Kullanıcı hesapları tablosu",
            },
            "table_human_name": {"en": "Users", "tr": "Kullanıcılar"},
            "table_tags": ["auth", "core"],
            "columns": {
                "id": {
                    "description": {"en": "Primary key identifier"},
                    "semantic_type": "id",
                    "tags": ["pk"],
                },
                "email": {
                    "description": {
                        "en": "User email address",
                        "tr": "Kullanıcı e-posta adresi",
                    },
                    "semantic_type": "email",
                    "tags": ["contact"],
                },
                "name": {
                    "description": {"en": "Full name", "tr": "Ad soyad"},
                    "semantic_type": "name",
                    "tags": [],
                },
                "status": {
                    "description": {"en": "Account status"},
                    "semantic_type": "status",
                    "tags": [],
                },
                "created_at": {
                    "description": {"en": "Account creation timestamp"},
                    "semantic_type": "date",
                    "tags": [],
                },
            },
            "relationships": [],
        }
    )


@pytest.fixture
def analyzer_config():
    return Settings(
        databases=[],
        settings={
            "analysis": {"sample_limit": 0, "include_row_count": False},
            "i18n": {
                "default_language": "en",
                "languages": ["en", "tr"],
                "translation_strategy": "single",
            },
            "llm": {"provider": "openai", "model": "gpt-4o-mini"},
            "catalog": {"auto_cross_reference": False},
        },
    )


# ===========================================
# EnrichedAnalyzer Tests
# ===========================================


class TestEnrichedAnalyzer:
    """Tests for the main analyzer orchestrator."""

    @pytest.mark.asyncio
    async def test_analyze_single_table(self, analyzer_adapter, llm_response, analyzer_config):
        provider = MockLLMProvider(response=llm_response)
        client = LLMClient(provider=provider)

        analyzer = EnrichedAnalyzer(
            adapter=analyzer_adapter,
            config=analyzer_config,
            llm_client=client,
            db_type="postgresql",
            database_name="testdb",
        )

        catalog = await analyzer.analyze(table_names=["users"])

        assert catalog.database_name == "testdb"
        assert catalog.table_count == 1

        users = catalog.get_table("users")
        assert users is not None
        assert users.description.get("en") == "User accounts table"
        assert users.description.get("tr") == "Kullanıcı hesapları tablosu"
        assert users.human_name.get("en") == "Users"
        assert "auth" in users.tags

        # Check columns
        email_col = users.get_column("email")
        assert email_col is not None
        assert email_col.semantic_type == "email"
        assert email_col.description.get("en") == "User email address"

        id_col = users.get_column("id")
        assert id_col is not None
        assert id_col.is_primary_key

    @pytest.mark.asyncio
    async def test_analyze_all_tables(self, analyzer_adapter, llm_response, analyzer_config):
        provider = MockLLMProvider(response=llm_response)
        client = LLMClient(provider=provider)

        analyzer = EnrichedAnalyzer(
            adapter=analyzer_adapter,
            config=analyzer_config,
            llm_client=client,
            database_name="testdb",
        )

        catalog = await analyzer.analyze()

        # Should analyze both tables returned by get_tables()
        assert catalog.table_count == 2
        assert analyzer_adapter.get_tables.called

    @pytest.mark.asyncio
    async def test_analyze_with_excluded_tables(self, analyzer_adapter, llm_response):
        config = Settings(
            databases=[],
            settings={
                "analysis": {
                    "sample_limit": 0,
                    "excluded_tables": ["orders"],
                },
                "catalog": {"auto_cross_reference": False},
            },
        )
        provider = MockLLMProvider(response=llm_response)
        client = LLMClient(provider=provider)

        analyzer = EnrichedAnalyzer(
            adapter=analyzer_adapter,
            config=config,
            llm_client=client,
            database_name="testdb",
        )

        catalog = await analyzer.analyze()
        assert catalog.table_count == 1
        assert catalog.get_table("users") is not None
        assert catalog.get_table("orders") is None

    @pytest.mark.asyncio
    async def test_analyze_llm_json_error_fallback(self, analyzer_adapter, analyzer_config):
        """When LLM returns invalid JSON, should fall back to basic entry."""
        provider = MockLLMProvider(response="not valid json {{{")
        client = LLMClient(provider=provider)

        analyzer = EnrichedAnalyzer(
            adapter=analyzer_adapter,
            config=analyzer_config,
            llm_client=client,
            database_name="testdb",
        )

        # Per-table fallback: invalid JSON -> basic (schema-only) entry.
        # (analyze() additionally raises AnalysisError only when *all* tables
        # fail LLM; full schema-only catalog fallback is a Phase 3 item.)
        entry, llm_ok = await analyzer._analyze_table(table_name="users")

        assert llm_ok is False
        assert entry is not None
        assert entry.table_name == "users"
        assert len(entry.columns) == 5

    @pytest.mark.asyncio
    async def test_analyze_with_foreign_keys(self, analyzer_adapter, analyzer_config):
        analyzer_adapter.get_table_schema.return_value = {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False, "key": "PRI"},
                {"name": "user_id", "type": "integer", "nullable": False},
                {"name": "total", "type": "decimal", "nullable": False},
            ],
            "foreign_keys": [
                {
                    "column": "user_id",
                    "references_table": "users",
                    "references_column": "id",
                }
            ],
            "indexes": [],
            "primary_key": "id",
        }

        llm_resp = json.dumps(
            {
                "table_description": {"en": "Customer orders"},
                "table_human_name": {"en": "Orders"},
                "table_tags": ["billing"],
                "columns": {
                    "id": {"description": {"en": "Order ID"}, "semantic_type": "id"},
                    "user_id": {
                        "description": {"en": "Reference to user"},
                        "semantic_type": "id",
                    },
                    "total": {
                        "description": {"en": "Order total amount"},
                        "semantic_type": "amount",
                    },
                },
                "relationships": [
                    {
                        "source_column": "user_id",
                        "target_table": "users",
                        "target_column": "id",
                        "relationship_type": "many-to-one",
                        "description": {"en": "Order belongs to a user"},
                    }
                ],
            }
        )

        provider = MockLLMProvider(response=llm_resp)
        client = LLMClient(provider=provider)

        analyzer = EnrichedAnalyzer(
            adapter=analyzer_adapter,
            config=analyzer_config,
            llm_client=client,
            database_name="testdb",
        )

        catalog = await analyzer.analyze(table_names=["orders"])
        orders = catalog.get_table("orders")

        assert orders is not None
        assert len(orders.foreign_keys) == 1
        assert orders.foreign_keys[0].references_table == "users"

        user_id_col = orders.get_column("user_id")
        assert user_id_col is not None
        assert user_id_col.is_foreign_key
        assert user_id_col.references == "users.id"

        assert len(orders.relationships) == 1
        assert orders.relationships[0].relationship_type == "many-to-one"


class TestParseLocalized:
    """Tests for _parse_localized helper."""

    def test_dict_input(self):
        result = EnrichedAnalyzer._parse_localized({"en": "Hello", "tr": "Merhaba"})
        assert result.get("en") == "Hello"
        assert result.get("tr") == "Merhaba"

    def test_string_input(self):
        result = EnrichedAnalyzer._parse_localized("Hello")
        assert result.get("en") == "Hello"

    def test_none_input(self):
        result = EnrichedAnalyzer._parse_localized(None)
        assert result.is_empty

    def test_empty_dict(self):
        result = EnrichedAnalyzer._parse_localized({})
        assert result.is_empty
