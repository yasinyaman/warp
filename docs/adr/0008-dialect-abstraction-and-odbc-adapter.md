# 8. Dialect abstraction and the ODBC (SQL Server) adapter

- Status: Accepted
- Date: 2026-09-17

## Context

Warp supported PostgreSQL (asyncpg) and MySQL (aiomysql). Everything that
differs between the two — placeholder style (`$1` vs `%s`), identifier quoting,
`ILIKE` vs `LIKE`, `RETURNING`, `LIMIT/OFFSET`, the default schema and the
catalog queries for comments and row counts — was expressed as
`if dialect == "postgresql"` branches spread over six modules. Adding a third
engine meant touching all of them.

SQL Server was the requested third engine. There is no maintained native
asyncio driver for it; the standard route is ODBC (`pyodbc`, wrapped for
asyncio by `aioodbc`) with Microsoft's `msodbcsql18` driver. ODBC also opens the
door to other data sources (Oracle, DB2, …) through the same code path.

## Decision

1. **One `Dialect` object per engine** (`adapters/outbound/db/dialect.py`), a
   frozen dataclass holding the placeholder style, quote characters, `LIKE`
   operator, how written rows are returned (`returning` / `output` / `refetch`),
   pagination style (`LIMIT/OFFSET` or `OFFSET … FETCH`), percent escaping,
   default schema and the comment / row-count catalog queries. The query
   builder, the named-parameter binder, identifier quoting, the metadata
   readers and the composition root consume the `Dialect`; none of them branch
   on a database-type string any more. PostgreSQL and MySQL SQL is unchanged
   byte for byte.
2. **A single `ODBCAdapter`** with two profiles selected by the config `type`:
   - `mssql` / `sqlserver` — first-class: introspection through
     `INFORMATION_SCHEMA` and `sys.*` (identity/computed markers, keys,
     indexes), writes that return the row with `OUTPUT` (falling back per table
     to `SCOPE_IDENTITY()` + re-select when triggers make `OUTPUT` illegal,
     error 334), `TOP`/`OFFSET … FETCH`, a `datetimeoffset` output converter,
     comments from `MS_Description` extended properties and row counts from
     `sys.partitions`. Covered by real-database integration tests in CI.
   - `odbc` — best effort: ANSI quoting, `?` placeholders, introspection via
     the ODBC catalog functions, re-select after writes, no catalog
     intelligence. Unit-tested only; documented as such.
3. **Packaging**: `aioodbc` lives in an `odbc` extra and is imported lazily in
   `connect()`, so the factory and the unit tests run without it. The ODBC
   driver is a system package: the Docker image installs `msodbcsql18` by
   default (`--build-arg WITH_MSSQL_ODBC=0` opts out), CI installs it on the
   Ubuntu runner, developers install it locally (`brew install msodbcsql18` /
   apt). Both hashed lock files include `aioodbc`/`pyodbc`.
4. **Tests**: SQL Server integration tests run in CI (`WARP_REQUIRE_MSSQL=1`
   turns "unavailable" into a failure) and auto-skip on machines without the
   driver or without an x86-64 Docker engine (the SQL Server image has no
   arm64 build). `WARP_MSSQL_HOST` points them at an existing server instead.

## Consequences

- Adding an engine is now one `Dialect` entry plus, when the driver's
  execution model differs, one adapter; the SQL helpers need no changes.
- `ColumnSchema.is_auto_generated` gives create models and required-column
  checks one definition of "the database fills this in" (auto-increment,
  serial, identity, computed). PostgreSQL identity columns are now recognised
  too.
- SQL Server identifiers are bracket-quoted through the same strict
  sanitizer, so the identifier-injection guarantees are unchanged.
- The generic profile cannot detect identity columns unless the driver reports
  them in `TYPE_NAME`, and `DELETE` costs an extra `SELECT` because rowcounts
  may be unreported; both are acceptable for a best-effort path.
- `aioodbc` 0.5's `statistics()` wrapper drops pyodbc's mandatory table
  argument; the adapter works around it and degrades to "no index metadata"
  rather than failing.
