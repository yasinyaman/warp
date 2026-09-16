"""Tests for the raw SQL query validator and endpoint."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warp.api.query import QueryValidator, create_query_router


class TestQueryValidator:
    def test_allows_simple_select(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT * FROM users WHERE id = :id") is True

    def test_allows_single_trailing_semicolon(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT 1;") is True
        assert v.validate("SELECT 1 ;  ") is True

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users; SELECT * FROM secrets",
            "SELECT 1; DROP TABLE users",
            "SELECT 1;DELETE FROM users",
            "SELECT 1; SELECT 2;",
        ],
    )
    def test_rejects_multiple_statements(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="Multiple SQL statements"):
            v.validate(query)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users -- comment",
            "SELECT /* x */ 1",
            "DROP TABLE users",
            "DELETE FROM users",
        ],
    )
    def test_rejects_dangerous_or_non_whitelisted(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError):
            v.validate(query)

    def test_enforces_whitelist_start(self):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="must start with"):
            v.validate("UPDATE users SET x = 1")

    def test_whitelist_is_configurable(self):
        v = QueryValidator(["SELECT", "INSERT"])
        assert v.validate("INSERT INTO t (a) VALUES (:a)") is True


class _DB:
    """Minimal adapter for the query endpoint."""

    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    async def execute_query(self, query, params=None):
        if self._error:
            raise self._error
        return self._rows


def _client(db, enabled=True, whitelist=None):
    app = FastAPI()
    app.include_router(
        create_query_router(db=db, whitelist=whitelist or ["SELECT"], enabled=enabled)
    )
    return TestClient(app)


class TestQueryEndpoint:
    def test_execute_select(self):
        client = _client(_DB(rows=[{"id": 1}]))
        r = client.post("/query/execute", json={"query": "SELECT * FROM users"})
        assert r.status_code == 200
        assert r.json()["row_count"] == 1

    def test_disabled_returns_403(self):
        client = _client(_DB(), enabled=False)
        r = client.post("/query/execute", json={"query": "SELECT 1"})
        assert r.status_code == 403

    def test_multi_statement_rejected(self):
        client = _client(_DB())
        r = client.post("/query/execute", json={"query": "SELECT 1; DROP TABLE users"})
        assert r.status_code == 400

    def test_non_whitelisted_rejected(self):
        client = _client(_DB())
        r = client.post("/query/execute", json={"query": "DELETE FROM users"})
        assert r.status_code == 400

    def test_db_error_does_not_leak_details(self):
        client = _client(_DB(error=RuntimeError("relation secret_table does not exist")))
        r = client.post("/query/execute", json={"query": "SELECT * FROM x"})
        assert r.status_code == 500
        assert "secret_table" not in r.text
        assert r.json()["detail"] == "Query execution failed"

    def test_allowed_commands_endpoint(self):
        client = _client(_DB(), whitelist=["SELECT", "INSERT"])
        r = client.get("/query/allowed-commands")
        assert r.status_code == 200
        assert "SELECT" in r.json()["allowed_commands"]


class TestParameterBindingErrors:
    def test_missing_named_param_is_400(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from warp.api.query import create_query_router

        class _RaisingDB:
            async def execute_query(self, query, params=None):
                # What the real adapters raise when a :name has no value.
                raise ValueError("Missing value for query parameter :status")

        app = FastAPI()
        app.include_router(create_query_router(db=_RaisingDB(), enabled=True))
        r = TestClient(app).post(
            "/query/execute", json={"query": "SELECT * FROM users WHERE status = :status"}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Missing value for query parameter :status"
