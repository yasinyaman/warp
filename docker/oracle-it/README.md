# Running the Oracle integration tests on Apple Silicon

Oracle publishes no macOS ARM64 ODBC driver, so the tests cannot run on the
host the way the SQL Server ones can. They run in this Linux arm64 image
instead, which installs the same Instant Client ODBC driver CI uses.

```bash
# 1. An Oracle to test against
docker run -d --name warp-oracle-it \
  -e ORACLE_PASSWORD=Warp_Integr4tion1 \
  -e APP_USER=warp -e APP_USER_PASSWORD=Warp_Integr4tion1 \
  gvenzl/oracle-free:23-slim-faststart
# wait for "DATABASE IS READY TO USE!" in `docker logs warp-oracle-it`

# 2. The test runner
docker build -t warp-oracle-it docker/oracle-it

# 3. The tests, pointed at the container via WARP_ORACLE_HOST
docker run --rm --network container:warp-oracle-it -v "$PWD:/app:ro" -w /app \
  -e WARP_REQUIRE_ORACLE=1 -e PYTEST_ADDOPTS="-p no:cacheprovider" \
  -e WARP_ORACLE_HOST=127.0.0.1 -e WARP_ORACLE_PORT=1521 \
  -e WARP_ORACLE_SERVICE=FREEPDB1 \
  -e WARP_ORACLE_USER=warp -e WARP_ORACLE_PASSWORD=Warp_Integr4tion1 \
  warp-oracle-it python -m pytest tests/integration/test_oracle_adapter.py -m integration -q
```

`--network container:` puts the runner in the Oracle container's network
namespace, so no ports need publishing. On x86-64 CI none of this is needed:
the driver installs on the runner and testcontainers starts Oracle itself.
