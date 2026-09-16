"""Tests for CRUD mass-assignment protection (read-only column rejection)."""

import pytest

from warp.api.crud import CRUDOperations
from warp.core.exceptions import ValidationError
from warp.schema.models import ColumnSchema, TableSchema


def _schema():
    return TableSchema(
        table_name="users",
        columns=[
            ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
            ColumnSchema(name="username", type="varchar", nullable=False),
            ColumnSchema(name="is_admin", type="boolean", nullable=True, default="false"),
            ColumnSchema(name="created_at", type="timestamp", nullable=True, default="now()"),
        ],
        primary_key="id",
    )


class _FakeDB:
    def __init__(self):
        self.inserted = None
        self.updated = None

    async def insert(self, table, data):
        self.inserted = data
        return {"id": 1, **data}

    async def update(self, table, id_column, id_value, data):
        self.updated = data
        return {"id": id_value, **data}

    async def select_by_id(self, table, id_column, id_value, columns=None):
        return {"id": id_value}


@pytest.fixture
def crud():
    return CRUDOperations(_FakeDB(), _schema(), readonly_columns=["created_at", "updated_at"])


class TestCreate:
    async def test_rejects_auto_generated_pk(self, crud):
        with pytest.raises(ValidationError):
            await crud.create({"username": "x", "id": 999})

    async def test_rejects_readonly_column(self, crud):
        with pytest.raises(ValidationError):
            await crud.create({"username": "x", "created_at": "2020-01-01"})

    async def test_rejects_unknown_column(self, crud):
        with pytest.raises(ValidationError):
            await crud.create({"username": "x", "role": "root"})

    async def test_allows_writable_columns(self, crud):
        await crud.create({"username": "x", "is_admin": True})
        assert crud.db.inserted == {"username": "x", "is_admin": True}


class TestUpdate:
    async def test_rejects_pk_change(self, crud):
        with pytest.raises(ValidationError):
            await crud.update(1, {"username": "y", "id": 999})

    async def test_rejects_readonly_column(self, crud):
        with pytest.raises(ValidationError):
            await crud.update(1, {"created_at": "2020-01-01"})

    async def test_allows_writable_columns(self, crud):
        await crud.update(1, {"username": "y"})
        assert crud.db.updated == {"username": "y"}


class TestConfigurableProtection:
    async def test_sensitive_column_protected_via_config(self):
        crud = CRUDOperations(_FakeDB(), _schema(), readonly_columns=["is_admin"])
        with pytest.raises(ValidationError):
            await crud.create({"username": "x", "is_admin": True})
