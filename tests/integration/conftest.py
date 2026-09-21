"""Real-database fixtures (PostgreSQL 16, MySQL 8, SQL Server 2022, Oracle 23)
via testcontainers.

Every test in this package is marked `integration`; the whole package is
skipped when Docker is not reachable.

The two ODBC engines additionally need a driver on the host, so they skip with
a reason when one is missing; CI sets WARP_REQUIRE_MSSQL=1 / WARP_REQUIRE_ORACLE=1
to turn those skips into failures. To run them against a server you already
have, set WARP_MSSQL_HOST / WARP_ORACLE_HOST (and optionally the matching
_PORT / _USER / _PASSWORD / _DATABASE / _SERVICE); no container is started then.

SQL Server's image is x86-64 only, so it is skipped on Apple Silicon unless
required. Oracle Free publishes arm64 builds, so it runs natively there.
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
# gvenzl/oracle-free is the practical choice: Oracle's own image needs a
# licence click-through. `-faststart` trades image size for startup time, and
# unlike SQL Server there are arm64 builds.
ORACLE_IMAGE = "gvenzl/oracle-free:23-slim-faststart"
ORACLE_SERVICE = "FREEPDB1"
ORACLE_USER = "warp"
ORACLE_PASSWORD = "Warp_Integr4tion1"


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


def _oracle_driver() -> str | None:
    """Name of an installed Oracle ODBC driver, or None."""
    try:
        import pyodbc
    except Exception:  # not installed, or the wheel cannot find unixODBC
        return None
    drivers = [name for name in pyodbc.drivers() if "Oracle" in name]
    return sorted(drivers)[-1] if drivers else None


def _oracle_unavailable(reason: str) -> None:
    if os.environ.get("WARP_REQUIRE_ORACLE") == "1":
        pytest.fail(f"Oracle integration tests are required (WARP_REQUIRE_ORACLE=1): {reason}")
    pytest.skip(f"Oracle: {reason}")


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


def _external_oracle_config(driver: str) -> DatabaseConfig | None:
    """An Oracle the developer already runs (WARP_ORACLE_HOST), or None."""
    host = os.environ.get("WARP_ORACLE_HOST")
    if not host:
        return None
    return DatabaseConfig(
        name="ora",
        type="oracle",
        host=host,
        port=int(os.environ.get("WARP_ORACLE_PORT", "1521")),
        database=os.environ.get("WARP_ORACLE_SERVICE", ORACLE_SERVICE),
        username=os.environ.get("WARP_ORACLE_USER", ORACLE_USER),
        password=os.environ.get("WARP_ORACLE_PASSWORD", ORACLE_PASSWORD),
        options={"driver": driver},
    )


@pytest.fixture(scope="session")
def oracle_config() -> Iterator[DatabaseConfig]:
    driver = _oracle_driver()
    if driver is None:
        _oracle_unavailable("no Oracle ODBC driver is installed (pyodbc.drivers())")
    external = _external_oracle_config(driver)
    if external is not None:
        yield external
        return
    if not _docker_available():
        pytest.skip("Docker is not available")
    from testcontainers.community.oracle import OracleDbContainer

    container = OracleDbContainer(
        ORACLE_IMAGE, username=ORACLE_USER, password=ORACLE_PASSWORD, dbname=ORACLE_SERVICE
    )
    try:
        container.start()
    except Exception as e:
        _oracle_unavailable(f"container failed to start: {e}")
    try:
        yield DatabaseConfig(
            name="ora",
            type="oracle",
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(1521)),
            database=ORACLE_SERVICE,
            username=ORACLE_USER,
            password=ORACLE_PASSWORD,
            options={"driver": driver},
        )
    finally:
        container.stop()


async def _fresh_users_table(gateway: DatabaseGateway, dialect: str) -> None:
    if dialect != "oracle":
        await gateway.execute_query("DROP TABLE IF EXISTS users")
    if dialect == "postgresql":
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id SERIAL PRIMARY KEY, username VARCHAR(50) NOT NULL, "
            "zip VARCHAR(10), active BOOLEAN NOT NULL DEFAULT TRUE, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    elif dialect == "oracle":
        # Oracle has no DROP TABLE IF EXISTS and no boolean before 23ai's
        # (still driver-dependent) BOOLEAN, so: PL/SQL drop and NUMBER(1).
        # CASCADE CONSTRAINTS: a leftover child table from an earlier run would
        # otherwise make this ORA-02449. -942 is "table does not exist".
        await gateway.execute_query(
            "BEGIN EXECUTE IMMEDIATE 'DROP TABLE users CASCADE CONSTRAINTS'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
        )
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "username VARCHAR2(50) NOT NULL, zip VARCHAR2(10), "
            "active NUMBER(1) DEFAULT 1 NOT NULL, "
            "created_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
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
    yes, no = ("1", "0") if dialect in ("mssql", "oracle") else ("TRUE", "FALSE")
    rows = f"('alice', '00123', {yes}), ('bob', '90210', {no}), ('carol', NULL, {yes})"
    if dialect == "oracle":
        # Oracle's INSERT takes a single VALUES tuple.
        for row in (
            f"('alice', '00123', {yes})",
            f"('bob', '90210', {no})",
            f"('carol', NULL, {yes})",
        ):
            await gateway.execute_query(f"INSERT INTO users (username, zip, active) VALUES {row}")
        return
    await gateway.execute_query(f"INSERT INTO users (username, zip, active) VALUES {rows}")


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


@pytest.fixture
async def oracle_gateway(oracle_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    async for gateway in _connected(oracle_config):
        yield gateway
