"""Real-database fixtures (PostgreSQL 16, MySQL 8) via testcontainers.

Every test in this package is marked `integration`; the whole package is
skipped when Docker is not reachable.
"""

from collections.abc import AsyncIterator, Iterator

import pytest

from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.application.config import DatabaseConfig
from warp.application.ports.database import DatabaseGateway

POSTGRES_IMAGE = "postgres:16-alpine"
MYSQL_IMAGE = "mysql:8.0"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


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


async def _fresh_users_table(gateway: DatabaseGateway, dialect: str) -> None:
    await gateway.execute_query("DROP TABLE IF EXISTS users")
    if dialect == "postgresql":
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id SERIAL PRIMARY KEY, username VARCHAR(50) NOT NULL, "
            "zip VARCHAR(10), active BOOLEAN NOT NULL DEFAULT TRUE, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    else:
        await gateway.execute_query(
            "CREATE TABLE users ("
            "id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(50) NOT NULL, "
            "zip VARCHAR(10), active TINYINT(1) NOT NULL DEFAULT 1, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    await gateway.execute_query(
        "INSERT INTO users (username, zip, active) VALUES "
        "('alice', '00123', TRUE), ('bob', '90210', FALSE), ('carol', NULL, TRUE)"
    )


@pytest.fixture
async def pg_gateway(postgres_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    gateway = DatabaseFactory.create(postgres_config)
    await gateway.connect()
    try:
        await _fresh_users_table(gateway, "postgresql")
        yield gateway
    finally:
        await gateway.disconnect()


@pytest.fixture
async def mysql_gateway(mysql_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
    gateway = DatabaseFactory.create(mysql_config)
    await gateway.connect()
    try:
        await _fresh_users_table(gateway, "mysql")
        yield gateway
    finally:
        await gateway.disconnect()
