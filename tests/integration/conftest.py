"""Real-database fixtures (PostgreSQL 16, MySQL 8, SQL Server 2022) via testcontainers.

Every test in this package is marked `integration`; the whole package is
skipped when Docker is not reachable. SQL Server additionally needs the
Microsoft ODBC driver on the host and an x86-64 Docker engine (the image has
no arm64 build), so it is skipped with a reason when either is missing; CI
sets WARP_REQUIRE_MSSQL=1 to turn that skip into a failure. To run the SQL
Server tests against a server you already have (e.g. from an Apple Silicon
machine), set WARP_MSSQL_HOST (and optionally WARP_MSSQL_PORT / _USER /
_PASSWORD / _DATABASE); no container is started then.
"""

import os
import platform
from collections.abc import AsyncIterator, Iterator

import pytest

from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.application.config import DatabaseConfig
from warp.application.ports.database import DatabaseGateway

POSTGRES_IMAGE = "postgres:16-alpine"
MYSQL_IMAGE = "mysql:8.0"
MSSQL_IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
MSSQL_DATABASE = "warp"
# Throwaway password for the test container; SQL Server enforces complexity.
MSSQL_PASSWORD = "Warp_Integr4tion!"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def _mssql_driver() -> str | None:
    """Name of an installed 'ODBC Driver NN for SQL Server', or None."""
    try:
        import pyodbc
    except Exception:  # not installed, or the wheel cannot find unixODBC
        return None
    drivers = [name for name in pyodbc.drivers() if "SQL Server" in name]
    return sorted(drivers)[-1] if drivers else None


def _mssql_unavailable(reason: str) -> None:
    if os.environ.get("WARP_REQUIRE_MSSQL") == "1":
        pytest.fail(f"SQL Server integration tests are required (WARP_REQUIRE_MSSQL=1): {reason}")
    pytest.skip(f"SQL Server: {reason}")


@pytest.fixture(scope="session")
def postgres_config() -> Iterator[DatabaseConfig]:
    if not _docker_available():
        pytest.skip("Docker is not available")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE) as pg:
        yield DatabaseConfig(
            name="pg",
            type="postgresql",
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            database=pg.dbname,
            username=pg.username,
            password=pg.password,
        )


@pytest.fixture(scope="session")
def mysql_config() -> Iterator[DatabaseConfig]:
    if not _docker_available():
        pytest.skip("Docker is not available")
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(MYSQL_IMAGE) as my:
        yield DatabaseConfig(
            name="my",
            type="mysql",
            host=my.get_container_host_ip(),
            port=int(my.get_exposed_port(3306)),
            database=my.dbname,
            username=my.username,
            password=my.password,
        )


def _external_mssql_config(driver: str) -> DatabaseConfig | None:
    """A SQL Server the developer already runs (WARP_MSSQL_HOST), or None."""
    host = os.environ.get("WARP_MSSQL_HOST")
    if not host:
        return None
    return DatabaseConfig(
        name="ms",
        type="mssql",
        host=host,
        port=int(os.environ.get("WARP_MSSQL_PORT", "1433")),
        database=os.environ.get("WARP_MSSQL_DATABASE", MSSQL_DATABASE),
        username=os.environ.get("WARP_MSSQL_USER", "sa"),
        password=os.environ.get("WARP_MSSQL_PASSWORD", MSSQL_PASSWORD),
        options={"driver": driver, "trust_server_certificate": True},
    )


@pytest.fixture(scope="session")
def mssql_config() -> Iterator[DatabaseConfig]:
    driver = _mssql_driver()
    if driver is None:
        _mssql_unavailable("no 'ODBC Driver NN for SQL Server' is installed (pyodbc.drivers())")
    external = _external_mssql_config(driver)
    if external is not None:
        yield external
        return
    if not _docker_available():
        pytest.skip("Docker is not available")
    required = os.environ.get("WARP_REQUIRE_MSSQL") == "1"
    if platform.machine() in ("arm64", "aarch64") and not required:
        pytest.skip(
            "SQL Server: the image is x86-64 only "
            "(set WARP_REQUIRE_MSSQL=1 to try it under amd64 emulation)"
        )
    from testcontainers.community.mssql import SqlServerContainer

    container = SqlServerContainer(MSSQL_IMAGE, password=MSSQL_PASSWORD).maybe_emulate_amd64()
    try:
        container.start()
    except Exception as e:
        _mssql_unavailable(f"container failed to start: {e}")
    try:
        # A dedicated database keeps other sessions' temp tables (tempdb) out of
        # schema discovery.
        exit_code, output = container.exec(
            [
                "bash",
                "-c",
                f"/opt/mssql-tools*/bin/sqlcmd -C -U SA -P '{MSSQL_PASSWORD}' "
                f"-Q \"IF DB_ID('{MSSQL_DATABASE}') IS NULL CREATE DATABASE {MSSQL_DATABASE}\"",
            ]
        )
        assert exit_code == 0, output
        yield DatabaseConfig(
            name="ms",
            type="mssql",
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(1433)),
            database=MSSQL_DATABASE,
            username=container.username,
            password=container.password,
            # The container presents a self-signed certificate; Driver 18 encrypts by default.
            options={"driver": driver, "trust_server_certificate": True},
        )
    finally:
        container.stop()


async def _fresh_users_table(gateway: DatabaseGateway, dialect: str) -> None:
    await gateway.execute_query("DROP TABLE IF EXISTS users")
    if dialect == "postgresql":
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id SERIAL PRIMARY KEY, username VARCHAR(50) NOT NULL, "
            "zip VARCHAR(10), active BOOLEAN NOT NULL DEFAULT TRUE, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    elif dialect == "mssql":
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id INT IDENTITY(1,1) PRIMARY KEY, username NVARCHAR(50) NOT NULL, "
            "zip NVARCHAR(10), active BIT NOT NULL DEFAULT 1, "
            "created_at DATETIME2 DEFAULT SYSUTCDATETIME())"
        )
    else:
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(50) NOT NULL, "
            "zip VARCHAR(10), active TINYINT(1) NOT NULL DEFAULT 1, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    yes, no = ("1", "0") if dialect == "mssql" else ("TRUE", "FALSE")
    await gateway.execute_query(
        "INSERT INTO users (username, zip, active) VALUES "
        f"('alice', '00123', {yes}), ('bob', '90210', {no}), ('carol', NULL, {yes})"
    )


async def _connected(config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    gateway = DatabaseFactory.create(config)
    await gateway.connect()
    try:
        await _fresh_users_table(gateway, config.type)
        yield gateway
    finally:
        await gateway.disconnect()


@pytest.fixture
async def pg_gateway(postgres_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    async for gateway in _connected(postgres_config):
        yield gateway


@pytest.fixture
async def mysql_gateway(mysql_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    async for gateway in _connected(mysql_config):
        yield gateway


@pytest.fixture
async def mssql_gateway(mssql_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    async for gateway in _connected(mssql_config):
        yield gateway
