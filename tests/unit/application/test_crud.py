"""
Tests for CRUD operations.
"""

import pytest

from warp.application.services.crud import CRUDOperations
from warp.domain.pagination import PaginationParams
from warp.domain.schema import ColumnSchema, TableSchema


class TestCRUDOperations:
    """Tests for CRUDOperations class."""

    @pytest.fixture
    def table_schema(self):
        """Create a test table schema."""
        return TableSchema(
            table_name="users",
            columns=[
                ColumnSchema(name="id", type="integer", nullable=False),
                ColumnSchema(name="username", type="varchar", nullable=False),
                ColumnSchema(name="email", type="varchar", nullable=False),
                ColumnSchema(name="status", type="varchar", nullable=True, default="active"),
            ],
            primary_key="id",
        )

    @pytest.fixture
    def crud(self, mock_db_with_data, table_schema):
        """Create CRUD operations instance."""
        return CRUDOperations(db=mock_db_with_data, table_schema=table_schema)

    @pytest.mark.asyncio
    async def test_get_all_returns_paginated_response(self, crud):
        """Test get_all returns PaginatedResponse."""
        response = await crud.get_all()

        assert hasattr(response, "items")
        assert hasattr(response, "total")
        assert hasattr(response, "limit")
        assert hasattr(response, "offset")

    @pytest.mark.asyncio
    async def test_get_all_with_pagination(self, crud):
        """Test get_all with pagination."""
        pagination = PaginationParams(limit=2, offset=0)
        response = await crud.get_all(pagination=pagination)

        assert response.limit == 2
        assert response.offset == 0
        assert len(response.items) <= 2

    @pytest.mark.asyncio
    async def test_get_by_id_found(self, crud):
        """Test get_by_id when record exists."""
        record = await crud.get_by_id({"id": 1})

        assert record is not None
        assert record["id"] == 1

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, crud):
        """Test get_by_id when record doesn't exist."""
        record = await crud.get_by_id({"id": 9999})

        assert record is None

    @pytest.mark.asyncio
    async def test_create(self, crud):
        """Test creating a new record."""
        data = {"username": "newuser", "email": "new@test.com", "status": "active"}

        record = await crud.create(data)

        assert record is not None
        assert record["username"] == "newuser"
        assert "id" in record

    @pytest.mark.asyncio
    async def test_update(self, crud):
        """Test updating a record."""
        data = {"status": "inactive"}

        record = await crud.update({"id": 1}, data)

        assert record is not None
        assert record["status"] == "inactive"

    @pytest.mark.asyncio
    async def test_update_not_found(self, crud):
        """Test updating non-existent record."""
        record = await crud.update({"id": 9999}, {"status": "inactive"})

        assert record is None

    @pytest.mark.asyncio
    async def test_update_empty_data(self, crud):
        """Test update with empty data returns existing record."""
        record = await crud.update({"id": 1}, {})

        assert record is not None
        assert record["id"] == 1

    @pytest.mark.asyncio
    async def test_delete(self, crud):
        """Test deleting a record."""
        # First verify it exists
        exists_before = await crud.exists({"id": 1})
        assert exists_before is True

        # Delete
        deleted = await crud.delete({"id": 1})
        assert deleted is True

        # Verify deleted
        exists_after = await crud.exists({"id": 1})
        assert exists_after is False

    @pytest.mark.asyncio
    async def test_delete_not_found(self, crud):
        """Test deleting non-existent record."""
        deleted = await crud.delete({"id": 9999})

        assert deleted is False

    @pytest.mark.asyncio
    async def test_exists_true(self, crud):
        """Test exists returns True for existing record."""
        exists = await crud.exists({"id": 1})

        assert exists is True

    @pytest.mark.asyncio
    async def test_exists_false(self, crud):
        """Test exists returns False for non-existent record."""
        exists = await crud.exists({"id": 9999})

        assert exists is False

    @pytest.mark.asyncio
    async def test_count(self, crud):
        """Test counting records."""
        count = await crud.count()

        assert count == 3  # Based on mock data

    @pytest.mark.asyncio
    async def test_count_with_filters(self, mock_db_with_data, table_schema):
        """Test counting with filters."""
        crud = CRUDOperations(mock_db_with_data, table_schema)

        # Note: Mock doesn't implement filtering, but we test the interface
        count = await crud.count(filters=[("status", "eq", "active")])

        assert isinstance(count, int)


class TestCRUDWithColumns:
    """Tests for CRUD operations with column selection."""

    @pytest.fixture
    def table_schema(self):
        """Create a test table schema."""
        return TableSchema(
            table_name="products",
            columns=[
                ColumnSchema(name="id", type="integer", nullable=False),
                ColumnSchema(name="name", type="varchar", nullable=False),
                ColumnSchema(name="price", type="decimal", nullable=False),
                ColumnSchema(name="status", type="varchar", nullable=True),
            ],
            primary_key="id",
        )

    @pytest.fixture
    def crud(self, mock_db_with_data, table_schema):
        """Create CRUD operations for products."""
        return CRUDOperations(db=mock_db_with_data, table_schema=table_schema)

    @pytest.mark.asyncio
    async def test_get_all_with_columns(self, crud):
        """Test get_all with specific columns."""
        response = await crud.get_all(columns=["id", "name"])

        assert response is not None
        # Mock doesn't filter columns, but interface is tested

    @pytest.mark.asyncio
    async def test_get_by_id_with_columns(self, crud):
        """Test get_by_id with specific columns."""
        record = await crud.get_by_id({"id": 1}, columns=["id", "name"])

        assert record is not None


class TestCRUDNullableHandling:
    """Tests for nullable column handling in CRUD."""

    @pytest.fixture
    def table_schema(self):
        """Create schema with nullable and non-nullable columns."""
        return TableSchema(
            table_name="test_table",
            columns=[
                ColumnSchema(name="id", type="integer", nullable=False),
                ColumnSchema(name="required_field", type="varchar", nullable=False),
                ColumnSchema(name="optional_field", type="varchar", nullable=True),
            ],
            primary_key="id",
        )

    @pytest.fixture
    def crud(self, mock_db, table_schema):
        """Create CRUD instance."""
        mock_db.add_mock_table(
            "test_table", {"table_name": "test_table", "columns": [], "primary_key": "id"}, []
        )
        return CRUDOperations(mock_db, table_schema)

    def test_is_nullable_true(self, crud):
        """Test _is_nullable for nullable column."""
        assert crud._is_nullable("optional_field") is True

    def test_is_nullable_false(self, crud):
        """Test _is_nullable for non-nullable column."""
        assert crud._is_nullable("required_field") is False

    def test_is_nullable_unknown_column(self, crud):
        """Test _is_nullable for unknown column returns True."""
        assert crud._is_nullable("unknown_column") is True
