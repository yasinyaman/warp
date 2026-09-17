"""Tests for the FastAPI app factory, lifespan, probes and the /openapi.json policy.

The app is built with a container over fakes, so the real lifespan runs
(connect -> discover -> mount routers -> disconnect) without a database.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import MockDatabaseAdapter
from warp.adapters.inbound.http.app import (
    RuntimeContext,
    connect_with_retry,
    create_app,
    runtime_of,
)
from warp.adapters.inbound.http.auth import AuthManager
from warp.application.config import ApiKeyConfig, AuthConfig, DatabaseConfig, RuntimeEnv, Settings
from warp.domain.errors import ConfigurationError, DatabaseConnectionError
from warp.infrastructure.bootstrap import build_container


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


class FakeGatewayFactory:
    """Hands out one prepared mock gateway and records the configs it saw."""

    def __init__(self, gateway: MockDatabaseAdapter) -> None:
        self.gateway = gateway
        self.configs: list = []

    def create(self, config):
        self.configs.append(config)
        return self.gateway

    def get_supported_types(self):
        return ["postgresql"]


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        databases=[DatabaseConfig(name="testdb", type="postgresql", database="d", username="u")],
        settings={"catalog": {"storage_path": str(tmp_path / "catalogs")}, **overrides},
    )


def _app(container=None, env: RuntimeEnv | None = None) -> FastAPI:
    factory = (lambda: container) if container is not None else None
    return create_app(container_factory=factory, env=env or RuntimeEnv())


def _client(container=None, env=None) -> TestClient:
    # Not a context manager: the lifespan does not run.
    return TestClient(_app(container, env))


def test_create_app_does_not_raise():
    # Regression guard for the response_model union issue.
    app = _app()
    assert any(getattr(r, "path", None) == "/health" for r in app.routes)
    assert isinstance(runtime_of(app), RuntimeContext)


class TestProbes:
    def test_live(self):
        assert _client().get("/live").json() == {"alive": True}

    def test_ready_true(self):
        app = _app()
        runtime_of(app).is_ready = True
        r = TestClient(app).get("/ready")
        assert r.status_code == 200
        assert r.json()["ready"] is True

    def test_ready_false(self):
        assert _client().get("/ready").status_code == 503

    def test_health_healthy(self):
        app = _app()
        runtime_of(app).gateways = {"db": _Adapter(connected=True)}
        runtime_of(app).is_ready = True
        r = TestClient(app).get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_health_unhealthy_db(self):
        app = _app()
        runtime_of(app).gateways = {"db": _Adapter(connected=False)}
        runtime_of(app).is_ready = True
        r = TestClient(app).get("/health")
        assert r.status_code == 503
        assert r.json()["databases"]["db"] == "disconnected"

    def test_info_before_startup(self):
        r = _client().get("/info")
        assert r.status_code == 200
        assert r.json()["name"] == "Warp Engine"
        assert r.json()["catalog"]["catalog_count"] == 0


class TestLifespan:
    """The real startup/shutdown sequence over a fake gateway."""

    def test_full_startup_and_shutdown(self, tmp_path, mock_db_with_data):
        factory = FakeGatewayFactory(mock_db_with_data)
        container = build_container(_settings(tmp_path), gateway_factory=factory)
        app = _app(container)

        with TestClient(app) as c:
            runtime = runtime_of(app)
            assert runtime.is_ready and runtime.container is container
            assert [cfg.name for cfg in factory.configs] == ["testdb"]  # DatabaseConfig, not a dict
            assert "testdb" in runtime.schemas
            assert c.get("/health").json()["status"] == "healthy"
            assert c.get("/api/v1/users").status_code == 200  # CRUD router mounted
            assert c.get("/api/v1/catalog").status_code == 200  # catalog router mounted
            assert c.post("/api/v1/query/execute", json={"query": "SELECT 1"}).status_code == 403
            info = c.get("/info").json()
            assert info["databases"]["testdb"]["table_count"] == 2

        assert runtime_of(app).is_ready is False
        assert mock_db_with_data.is_connected is False  # disconnected on shutdown

    def test_info_has_capabilities_block(self, tmp_path, mock_db_with_data):
        container = build_container(
            _settings(tmp_path, enable_raw_query=False),
            gateway_factory=FakeGatewayFactory(mock_db_with_data),
        )
        with TestClient(_app(container)) as c:
            caps = c.get("/info").json()["capabilities"]
        assert caps["db_prefix"] == "always"
        assert caps["api_prefix"] == "/api/v1"
        assert caps["schema"] is True
        assert caps["raw_query"] is False
        assert caps["export"]["enabled"] is True
        assert {"json", "ndjson"} <= set(caps["export"]["formats"])
        assert "in" in caps["filter_ops"]

    def test_single_db_serves_prefixed_and_alias_routes(self, tmp_path, mock_db_with_data):
        container = build_container(
            _settings(tmp_path), gateway_factory=FakeGatewayFactory(mock_db_with_data)
        )
        app = _app(container)
        with TestClient(app) as c:
            # The bare prefix stays the documented location for a single database...
            assert c.get("/api/v1/users").status_code == 200
            assert c.post("/api/v1/query/execute", json={"query": "SELECT 1"}).status_code == 403
            # ...and the db-scoped prefix always works too (hidden alias).
            assert c.get("/api/v1/testdb/users").status_code == 200
            assert c.get("/api/v1/testdb/users/1").json()["username"] == "admin"
            r = c.post("/api/v1/testdb/query/execute", json={"query": "SELECT 1"})
            assert r.status_code == 403
            paths = c.get("/openapi.json").json()["paths"]
        assert "/api/v1/users" in paths
        assert "/api/v1/testdb/users" not in paths  # alias is not documented twice

    def test_multi_db_uses_scoped_prefix_only(self, tmp_path, mock_db_with_data):
        settings = Settings(
            databases=[
                DatabaseConfig(name="one", type="postgresql", database="d", username="u"),
                DatabaseConfig(name="two", type="postgresql", database="d", username="u"),
            ],
            settings={"catalog": {"storage_path": str(tmp_path / "catalogs")}},
        )
        container = build_container(settings, gateway_factory=FakeGatewayFactory(mock_db_with_data))
        with TestClient(_app(container)) as c:
            assert c.get("/api/v1/one/users").status_code == 200
            assert c.get("/api/v1/two/users").status_code == 200
            assert c.get("/api/v1/users").status_code == 404

    def test_missing_factory_refuses_to_start(self):
        with pytest.raises(ConfigurationError), TestClient(_app()):
            pass

    def test_production_refuses_unsafe_config(self, tmp_path, mock_db):
        container = build_container(
            _settings(tmp_path), gateway_factory=FakeGatewayFactory(mock_db)
        )
        app = _app(container, env=RuntimeEnv(app_env="production", cors_origins=("*",)))
        with pytest.raises(ConfigurationError, match="unsafe"), TestClient(app):
            pass

    def test_auth_enabled_guards_routes(self, tmp_path, mock_db_with_data):
        settings = _settings(
            tmp_path,
            auth={"enabled": True, "api_keys": [{"key": "k", "permissions": ["all"]}]},
        )
        container = build_container(settings, gateway_factory=FakeGatewayFactory(mock_db_with_data))
        with TestClient(_app(container)) as c:
            assert c.get("/api/v1/users").status_code == 401
            assert c.get("/api/v1/users", headers={"X-API-Key": "k"}).status_code == 200
            assert c.get("/health").status_code == 200  # public


class TestConnectWithRetry:
    async def test_success(self):
        adapter = _Adapter()
        await connect_with_retry(adapter, max_retries=1)
        assert adapter.connect_calls == 1

    async def test_failure_raises(self):
        adapter = _Adapter(fail_times=5)
        with pytest.raises(DatabaseConnectionError):
            await connect_with_retry(adapter, max_retries=1)


class TestOpenAPIRoute:
    """/openapi.json is an explicit route so the auth manager can guard it."""

    def test_served_when_auth_disabled(self):
        r = _client().get("/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["title"] == "Warp Engine"

    def _manager(self, public):
        return AuthManager(
            AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(key="reader-key", permissions=["read"]),
                    ApiKeyConfig(key="query-key", permissions=["query"]),
                ],
                public_paths=public,
            )
        )

    def test_requires_key_when_not_public(self):
        app = _app()
        runtime_of(app).auth_manager = self._manager(public=["/health"])
        c = TestClient(app)
        assert c.get("/openapi.json").status_code == 401
        assert c.get("/openapi.json", headers={"X-API-Key": "nope"}).status_code == 401
        assert c.get("/openapi.json", headers={"X-API-Key": "query-key"}).status_code == 403
        ok = c.get("/openapi.json", headers={"X-API-Key": "reader-key"})
        assert ok.status_code == 200 and "paths" in ok.json()

    def test_public_when_listed(self):
        app = _app()
        runtime_of(app).auth_manager = self._manager(public=["/health", "/openapi.json"])
        assert TestClient(app).get("/openapi.json").status_code == 200

    def test_production_hides_docs_but_keeps_spec_route(self):
        app = _app(env=RuntimeEnv(app_env="production"))
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/docs" not in paths
        assert "/openapi.json" in paths


class TestRuntimeEnv:
    def test_from_environ_defaults(self):
        env = RuntimeEnv.from_environ({})
        assert env.app_env == "development" and not env.is_production
        assert env.cors_origins == ("*",) and env.log_json is False

    def test_from_environ_values(self):
        env = RuntimeEnv.from_environ(
            {
                "APP_ENV": "production",
                "CORS_ORIGINS": "https://a, https://b,",
                "API_PORT": "9000",
                "CONFIG_PATH": "/etc/warp.yaml",
            }
        )
        assert env.is_production and env.log_json is True
        assert env.cors_origins == ("https://a", "https://b")
        assert env.api_port == 9000 and env.config_path == "/etc/warp.yaml"
        assert (
            RuntimeEnv.from_environ({"LOG_FORMAT": "colored", "APP_ENV": "production"}).log_json
            is False
        )
