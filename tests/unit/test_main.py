"""Tests for the FastAPI app factory, health/probe endpoints, and retry helper.

Also a regression guard: create_app() must not raise at build time. A route
whose return annotation is a union like ``dict | JSONResponse`` needs
``response_model=None`` or FastAPI errors while building the app.
"""
import pytest
from fastapi.testclient import TestClient

import warp.main as main_module
from warp.config.settings import Settings
from warp.core.exceptions import DatabaseConnectionError
from warp.main import connect_with_retry, create_app


class _Adapter:
    def __init__(self, name: str = "db", connected: bool = True, fail_times: int = 0):
        self.name = name
        self._connected = connected
        self._fail_times = fail_times
        self.connect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self._fail_times:
            raise RuntimeError("boom")


@pytest.fixture
def state():
    s = main_module.state
    saved = (s.settings, dict(s.databases), dict(s.schemas), s.is_ready)
    yield s
    s.settings, s.databases, s.schemas, s.is_ready = (
        saved[0], dict(saved[1]), dict(saved[2]), saved[3]
    )


def _client() -> TestClient:
    # Not used as a context manager, so the DB-connecting lifespan does not run.
    return TestClient(create_app())


def test_create_app_does_not_raise():
    # Regression guard for the response_model union issue.
    app = create_app()
    assert any(getattr(r, "path", None) == "/health" for r in app.routes)


class TestProbes:
    def test_live(self):
        assert _client().get("/live").json() == {"alive": True}

    def test_ready_true(self, state):
        state.is_ready = True
        r = _client().get("/ready")
        assert r.status_code == 200
        assert r.json()["ready"] is True

    def test_ready_false(self, state):
        state.is_ready = False
        r = _client().get("/ready")
        assert r.status_code == 503

    def test_health_healthy(self, state):
        state.databases = {"db": _Adapter(connected=True)}
        state.is_ready = True
        r = _client().get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_health_unhealthy_db(self, state):
        state.databases = {"db": _Adapter(connected=False)}
        state.is_ready = True
        r = _client().get("/health")
        assert r.status_code == 503
        assert r.json()["databases"]["db"] == "disconnected"

    def test_info(self, state, tmp_path):
        cfg = Settings()
        cfg.settings.catalog.storage_path = str(tmp_path)
        state.settings = cfg
        state.schemas = {}
        r = _client().get("/info")
        assert r.status_code == 200
        assert r.json()["name"] == "Warp Engine"


class TestConnectWithRetry:
    async def test_success(self):
        adapter = _Adapter()
        await connect_with_retry(adapter, max_retries=1)
        assert adapter.connect_calls == 1

    async def test_failure_raises(self):
        adapter = _Adapter(fail_times=5)
        with pytest.raises(DatabaseConnectionError):
            await connect_with_retry(adapter, max_retries=1)
