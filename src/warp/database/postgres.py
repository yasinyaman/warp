"""
PostgreSQL database adapter implementation.
"""
from typing import Any

import asyncpg

from .base import DatabaseAdapter
from .identifiers import sanitize_identifier


class PostgreSQLAdapter(DatabaseAdapter):
    """PostgreSQL database adapter using asyncpg."""

    async def connect(self) -> None:
        """Create connection pool to PostgreSQL."""
        options = self.config.get("options", {})

        self._pool = await asyncpg.create_pool(
            host=self.config["host"],
            port=self.config["port"],
            database=self.config["database"],
            user=self.config["username"],
            password=self.config["password"],
            min_size=options.get("pool_min_size", 2),
            max_size=options.get("pool_size", 10),
            ssl=options.get("ssl", False) if options.get("ssl") else None
        )

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def get_tables(self) -> list[str]:
        """Get all table names from public schema."""
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [row["table_name"] for row in rows]

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Get detailed schema information for a table."""
        schema: dict[str, Any] = {
            "table_name": table,
            "columns": [],
            "primary_key": None,
            "foreign_keys": [],
            "indexes": []
        }

        async with self._pool.acquire() as conn:
            # Get column information
            columns_query = """
                SELECT
                    c.column_name,
                    c.data_type,
                    c.udt_name,
                    c.is_nullable,
                    c.column_default,
                    c.character_maximum_length,
                    c.numeric_precision,
                    c.numeric_scale
                FROM information_schema.columns c
                WHERE c.table_schema = 'public'
                  AND c.table_name = $1
                ORDER BY c.ordinal_position
            """
            columns = await conn.fetch(columns_query, table)

            for col in columns:
                schema["columns"].append({
                    "name": col["column_name"],
                    "type": col["data_type"],
                    "udt_name": col["udt_name"],
                    "nullable": col["is_nullable"] == "YES",
                    "default": col["column_default"],
                    "max_length": col["character_maximum_length"],
                    "precision": col["numeric_precision"],
                    "scale": col["numeric_scale"]
                })

            # Get primary key
            pk_query = """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = 'public'
                  AND tc.table_name = $1
                ORDER BY kcu.ordinal_position
            """
            pk_cols = await conn.fetch(pk_query, table)
            if pk_cols:
                pk_columns = [row["column_name"] for row in pk_cols]
                schema["primary_key"] = pk_columns[0] if len(pk_columns) == 1 else pk_columns

            # Get foreign keys
            fk_query = """
                SELECT
                    kcu.column_name,
                    ccu.table_name AS foreign_table,
                    ccu.column_name AS foreign_column,
                    tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND tc.table_name = $1
            """
            fks = await conn.fetch(fk_query, table)
            for fk in fks:
                schema["foreign_keys"].append({
                    "column": fk["column_name"],
                    "references_table": fk["foreign_table"],
                    "references_column": fk["foreign_column"],
                    "constraint_name": fk["constraint_name"]
                })

            # Get indexes
            idx_query = """
                SELECT
                    i.relname AS index_name,
                    a.attname AS column_name,
                    ix.indisunique AS is_unique,
                    ix.indisprimary AS is_primary
                FROM pg_class t
                JOIN pg_index ix ON t.oid = ix.indrelid
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
                WHERE t.relkind = 'r'
                  AND t.relname = $1
                  AND NOT ix.indisprimary
                ORDER BY i.relname, a.attnum
            """
            indexes = await conn.fetch(idx_query, table)

            # Group index columns
            idx_map = {}
            for idx in indexes:
                name = idx["index_name"]
                if name not in idx_map:
                    idx_map[name] = {
                        "name": name,
                        "columns": [],
                        "unique": idx["is_unique"]
                    }
                idx_map[name]["columns"].append(idx["column_name"])

            schema["indexes"] = list(idx_map.values())

        return schema

    async def execute_query(
        self,
        query: str,
        params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a raw SQL query."""
        async with self._pool.acquire() as conn:
            if params:
                # Convert named params to positional for asyncpg
                param_list = []
                param_idx = 1
                new_query = query
                for key, value in params.items():
                    new_query = new_query.replace(f":{key}", f"${param_idx}")
                    param_list.append(value)
                    param_idx += 1
                rows = await conn.fetch(new_query, *param_list)
            else:
                rows = await conn.fetch(query)

            return [dict(row) for row in rows]

    async def insert(
        self,
        table: str,
        data: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert a new record."""
        safe_table = self._sanitize_identifier(table)
        columns = [self._sanitize_identifier(c) for c in data]
        placeholders = [f"${i+1}" for i in range(len(columns))]
        values = list(data.values())

        query = f"""
            INSERT INTO "{safe_table}" ({', '.join(f'"{c}"' for c in columns)})
            VALUES ({', '.join(placeholders)})
            RETURNING *
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *values)
            return dict(row) if row else {}

    async def select(
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: dict[str, int] | None = None,
        sort: list[tuple[str, str]] | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Select records with filtering, pagination, and sorting."""
        safe_table = self._sanitize_identifier(table)

        # Build SELECT clause
        if columns:
            safe_cols = [self._sanitize_identifier(c) for c in columns]
            select_cols = ', '.join(f'"{c}"' for c in safe_cols)
        else:
            select_cols = "*"

        # Build WHERE clause
        where_clauses = []
        params = []
        param_idx = 1

        if filters:
            for col, op, val in filters:
                clause, param_idx, new_params = self._build_where_clause(
                    col, op, val, param_idx
                )
                where_clauses.append(clause)
                params.extend(new_params)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Build ORDER BY clause
        order_sql = ""
        if sort:
            safe_sort = [(self._sanitize_identifier(col), dir) for col, dir in sort]
            order_parts = [f'"{col}" {dir.upper()}' for col, dir in safe_sort]
            order_sql = f"ORDER BY {', '.join(order_parts)}"

        # Count query
        count_query = f'SELECT COUNT(*) FROM "{safe_table}" {where_sql}'
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)

            # Main query with pagination
            limit_sql = ""
            if pagination:
                limit = pagination.get("limit", 50)
                offset = pagination.get("offset", 0)
                limit_sql = f"LIMIT {limit} OFFSET {offset}"

            query = f'SELECT {select_cols} FROM "{safe_table}" {where_sql} {order_sql} {limit_sql}'
            rows = await conn.fetch(query, *params)

            return [dict(row) for row in rows], total

    async def select_by_id(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Select a single record by ID."""
        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)

        if columns:
            safe_cols = [self._sanitize_identifier(c) for c in columns]
            select_cols = ', '.join(f'"{c}"' for c in safe_cols)
        else:
            select_cols = "*"

        query = f'SELECT {select_cols} FROM "{safe_table}" WHERE "{safe_id_col}" = $1'

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, id_value)
            return dict(row) if row else None

    async def update(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an existing record."""
        if not data:
            return await self.select_by_id(table, id_column, id_value)

        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)

        set_clauses = []
        values = []
        for i, (col, val) in enumerate(data.items(), start=1):
            safe_col = self._sanitize_identifier(col)
            set_clauses.append(f'"{safe_col}" = ${i}')
            values.append(val)

        values.append(id_value)
        id_param = f"${len(values)}"

        query = f"""
            UPDATE "{safe_table}"
            SET {', '.join(set_clauses)}
            WHERE "{safe_id_col}" = {id_param}
            RETURNING *
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *values)
            return dict(row) if row else None

    async def delete(
        self,
        table: str,
        id_column: str,
        id_value: Any
    ) -> bool:
        """Delete a record by ID."""
        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)

        query = f'DELETE FROM "{safe_table}" WHERE "{safe_id_col}" = $1 RETURNING "{safe_id_col}"'

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, id_value)
            return row is not None

    def _sanitize_identifier(self, name: str) -> str:
        """Validate a SQL identifier (delegates to the shared sanitizer)."""
        return sanitize_identifier(name)

    def _build_where_clause(
        self,
        column: str,
        operator: str,
        value: Any,
        param_idx: int
    ) -> tuple[str, int, list[Any]]:
        """Build a WHERE clause component."""
        col = f'"{self._sanitize_identifier(column)}"'
        params = []

        if operator == "eq":
            clause = f"{col} = ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "ne":
            clause = f"{col} != ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "gt":
            clause = f"{col} > ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "gte":
            clause = f"{col} >= ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "lt":
            clause = f"{col} < ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "lte":
            clause = f"{col} <= ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "like":
            clause = f"{col} ILIKE ${param_idx}"
            params.append(value)
            param_idx += 1
        elif operator == "in":
            if isinstance(value, list | tuple):
                placeholders = [f"${param_idx + i}" for i in range(len(value))]
                clause = f"{col} IN ({', '.join(placeholders)})"
                params.extend(value)
                param_idx += len(value)
            else:
                clause = f"{col} = ${param_idx}"
                params.append(value)
                param_idx += 1
        elif operator == "is_null":
            clause = f"{col} IS NULL" if value else f"{col} IS NOT NULL"
        else:
            clause = f"{col} = ${param_idx}"
            params.append(value)
            param_idx += 1

        return clause, param_idx, params
