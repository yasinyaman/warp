"""
Pytest configuration and shared fixtures.
"""
import asyncio
import os
import sys
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ===========================================
# Event Loop Fixture
# ===========================================

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ===========================================
# Mock Database Adapter
# ===========================================

class MockDatabaseAdapter:
    """Mock database adapter for testing."""

    def __init__(self, config: dict[str, Any] = None):
        self.config = config or {}
        self.name = config.get("name", "test_db") if config else "test_db"
        self._pool = True  # Simulate connected state
        self._tables: dict[str, list[dict]] = {}
        self._schemas: dict[str, dict] = {}

    async def connect(self) -> None:
        """Simulate connection."""
        self._pool = True

    async def disconnect(self) -> None:
        """Simulate disconnection."""
        self._pool = None

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    async def get_tables(self) -> list[str]:
        """Return mock tables."""
        return list(self._tables.keys())

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Return mock schema."""
        return self._schemas.get(table, {
            "table_name": table,
            "columns": [],
            "primary_key": "id",
            "foreign_keys": [],
            "indexes": []
        })

    async def execute_query(self, query: str, params: dict = None) -> list[dict]:
        """Execute mock query."""
        return []

    async def insert(self, table: str, data: dict) -> dict:
        """Mock insert."""
        if table not in self._tables:
            self._tables[table] = []

        # Auto-generate ID
        new_id = len(self._tables[table]) + 1
        record = {"id": new_id, **data}
        self._tables[table].append(record)
        return record

    async def select(
        self,
        table: str,
        columns: list[str] = None,
        filters: list = None,
        pagination: dict = None,
        sort: list = None
    ) -> tuple:
        """Mock select."""
        records = self._tables.get(table, [])
        total = len(records)

        # Apply pagination
        if pagination:
            limit = pagination.get("limit", 50)
            offset = pagination.get("offset", 0)
            records = records[offset:offset + limit]

        return records, total

    async def select_by_id(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        columns: list[str] = None
    ):
        """Mock select by ID."""
        records = self._tables.get(table, [])
        for record in records:
            if record.get(id_column) == id_value:
                return record
        return None

    async def update(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        data: dict
    ):
        """Mock update."""
        records = self._tables.get(table, [])
        for i, record in enumerate(records):
            if record.get(id_column) == id_value:
                self._tables[table][i] = {**record, **data}
                return self._tables[table][i]
        return None

    async def delete(self, table: str, id_column: str, id_value: Any) -> bool:
        """Mock delete."""
        records = self._tables.get(table, [])
        for i, record in enumerate(records):
            if record.get(id_column) == id_value:
                self._tables[table].pop(i)
                return True
        return False

    def add_mock_table(self, name: str, schema: dict, data: list[dict] = None):
        """Helper to add mock table with schema and data."""
        self._schemas[name] = schema
        self._tables[name] = data or []


@pytest.fixture
def mock_db():
    """Create a mock database adapter."""
    return MockDatabaseAdapter()


@pytest.fixture
def mock_db_with_data():
    """Create a mock database with sample data."""
    db = MockDatabaseAdapter()

    # Add users table
    users_schema = {
        "table_name": "users",
        "columns": [
            {"name": "id", "type": "integer", "nullable": False, "default": None},
            {"name": "username", "type": "varchar", "nullable": False, "default": None},
            {"name": "email", "type": "varchar", "nullable": False, "default": None},
            {"name": "status", "type": "varchar", "nullable": True, "default": "active"},
        ],
        "primary_key": "id",
        "foreign_keys": [],
        "indexes": []
    }
    users_data = [
        {"id": 1, "username": "admin", "email": "admin@test.com", "status": "active"},
        {"id": 2, "username": "user1", "email": "user1@test.com", "status": "active"},
        {"id": 3, "username": "user2", "email": "user2@test.com", "status": "inactive"},
    ]
    db.add_mock_table("users", users_schema, users_data)

    # Add products table
    products_schema = {
        "table_name": "products",
        "columns": [
            {"name": "id", "type": "integer", "nullable": False, "default": None},
            {"name": "name", "type": "varchar", "nullable": False, "default": None},
            {"name": "price", "type": "decimal", "nullable": False, "default": None},
            {"name": "status", "type": "varchar", "nullable": True, "default": "draft"},
        ],
        "primary_key": "id",
        "foreign_keys": [],
        "indexes": []
    }
    products_data = [
        {"id": 1, "name": "Product A", "price": 100.00, "status": "active"},
        {"id": 2, "name": "Product B", "price": 200.00, "status": "active"},
        {"id": 3, "name": "Product C", "price": 50.00, "status": "draft"},
    ]
    db.add_mock_table("products", products_schema, products_data)

    return db


# ===========================================
# Sample Data Fixtures
# ===========================================

@pytest.fixture
def sample_user_data():
    """Sample user data for testing."""
    return {
        "username": "testuser",
        "email": "test@example.com",
        "status": "active"
    }


@pytest.fixture
def sample_product_data():
    """Sample product data for testing."""
    return {
        "name": "Test Product",
        "price": 99.99,
        "status": "active"
    }


@pytest.fixture
def sample_table_schema():
    """Sample table schema for testing."""
    from warp.schema.models import ColumnSchema, TableSchema

    return TableSchema(
        table_name="test_table",
        columns=[
            ColumnSchema(name="id", type="integer", nullable=False),
            ColumnSchema(name="name", type="varchar", nullable=False),
            ColumnSchema(name="description", type="text", nullable=True),
            ColumnSchema(name="price", type="decimal", nullable=False),
            ColumnSchema(name="is_active", type="boolean", nullable=True, default="true"),
            ColumnSchema(name="created_at", type="timestamp", nullable=True),
        ],
        primary_key="id",
        foreign_keys=[],
        indexes=[]
    )


# ===========================================
# Config Fixtures
# ===========================================

@pytest.fixture
def sample_config_dict():
    """Sample configuration dictionary."""
    return {
        "databases": [
            {
                "name": "test_db",
                "type": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "testdb",
                "username": "test",
                "password": "test123"
            }
        ],
        "settings": {
            "auto_discover_tables": True,
            "excluded_tables": ["migrations"],
            "pagination": {
                "default_limit": 50,
                "max_limit": 1000
            },
            "enable_raw_query": True,
            "raw_query_whitelist": ["SELECT"],
            "api_prefix": "/api/v1"
        }
    }


@pytest.fixture
def temp_config_file(tmp_path, sample_config_dict):
    """Create a temporary config file."""
    import yaml

    config_file = tmp_path / "database.yaml"
    with open(config_file, "w") as f:
        yaml.dump(sample_config_dict, f)

    return str(config_file)


# ===========================================
# FastAPI Test Client Fixtures
# ===========================================

@pytest.fixture
def app():
    """Create a test FastAPI application."""

    app = FastAPI(title="Test App")

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
