"""
MySQL database adapter implementation.
"""
from typing import Any, Dict, List, Optional, Tuple

import aiomysql

from .base import DatabaseAdapter
from .identifiers import sanitize_identifier


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter using aiomysql."""

    async def connect(self) -> None:
        """Create connection pool to MySQL."""
        options = self.config.get("options", {})

        self._pool = await aiomysql.create_pool(
            host=self.config["host"],
            port=self.config["port"],
            db=self.config["database"],
            user=self.config["username"],
            password=self.config["password"],
            minsize=options.get("pool_min_size", 2),
            maxsize=options.get("pool_size", 10),
            autocommit=True
        )

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def get_tables(self) -> List[str]:
        """Get all table names from the database."""
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, (self.config["database"],))
                rows = await cur.fetchall()
                return [row["TABLE_NAME"] for row in rows]

    async def get_table_schema(self, table: str) -> Dict[str, Any]:
        """Get detailed schema information for a table."""
        schema = {
            "table_name": table,
            "columns": [],
            "primary_key": None,
            "foreign_keys": [],
            "indexes": []
        }

        db_name = self.config["database"]

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # Get column information
                columns_query = """
                    SELECT
                        COLUMN_NAME,
                        DATA_TYPE,
                        COLUMN_TYPE,
                        IS_NULLABLE,
                        COLUMN_DEFAULT,
                        CHARACTER_MAXIMUM_LENGTH,
                        NUMERIC_PRECISION,
                        NUMERIC_SCALE,
                        COLUMN_KEY,
                        EXTRA
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                """
                await cur.execute(columns_query, (db_name, table))
                columns = await cur.fetchall()

                for col in columns:
                    schema["columns"].append({
                        "name": col["COLUMN_NAME"],
                        "type": col["DATA_TYPE"],
                        "full_type": col["COLUMN_TYPE"],
                        "nullable": col["IS_NULLABLE"] == "YES",
                        "default": col["COLUMN_DEFAULT"],
                        "max_length": col["CHARACTER_MAXIMUM_LENGTH"],
                        "precision": col["NUMERIC_PRECISION"],
                        "scale": col["NUMERIC_SCALE"],
                        "key": col["COLUMN_KEY"],
                        "extra": col["EXTRA"]
                    })

                    # Check for primary key
                    if col["COLUMN_KEY"] == "PRI":
                        if schema["primary_key"] is None:
                            schema["primary_key"] = col["COLUMN_NAME"]
                        elif isinstance(schema["primary_key"], str):
                            schema["primary_key"] = [schema["primary_key"], col["COLUMN_NAME"]]
                        else:
                            schema["primary_key"].append(col["COLUMN_NAME"])

                # Get foreign keys
                fk_query = """
                    SELECT
                        COLUMN_NAME,
                        REFERENCED_TABLE_NAME,
                        REFERENCED_COLUMN_NAME,
                        CONSTRAINT_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                """
                await cur.execute(fk_query, (db_name, table))
                fks = await cur.fetchall()

                for fk in fks:
                    schema["foreign_keys"].append({
                        "column": fk["COLUMN_NAME"],
                        "references_table": fk["REFERENCED_TABLE_NAME"],
                        "references_column": fk["REFERENCED_COLUMN_NAME"],
                        "constraint_name": fk["CONSTRAINT_NAME"]
                    })

                # Get indexes
                idx_query = """
                    SELECT
                        INDEX_NAME,
                        COLUMN_NAME,
                        NON_UNIQUE,
                        SEQ_IN_INDEX
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND INDEX_NAME != 'PRIMARY'
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """
                await cur.execute(idx_query, (db_name, table))
                indexes = await cur.fetchall()

                # Group index columns
                idx_map = {}
                for idx in indexes:
                    name = idx["INDEX_NAME"]
                    if name not in idx_map:
                        idx_map[name] = {
                            "name": name,
                            "columns": [],
                            "unique": idx["NON_UNIQUE"] == 0
                        }
                    idx_map[name]["columns"].append(idx["COLUMN_NAME"])

                schema["indexes"] = list(idx_map.values())

        return schema

    async def execute_query(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Execute a raw SQL query."""
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if params:
                    # Convert named params to positional for MySQL
                    param_list = []
                    new_query = query
                    for key, value in params.items():
                        new_query = new_query.replace(f":{key}", "%s")
                        param_list.append(value)
                    await cur.execute(new_query, param_list)
                else:
                    await cur.execute(query)

                rows = await cur.fetchall()
                return list(rows)

    async def insert(
        self,
        table: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Insert a new record."""
        safe_table = self._sanitize_identifier(table)
        columns = [self._sanitize_identifier(c) for c in data.keys()]
        placeholders = ["%s"] * len(columns)
        values = list(data.values())

        query = f"""
            INSERT INTO `{safe_table}` ({', '.join(f'`{c}`' for c in columns)})
            VALUES ({', '.join(placeholders)})
        """

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, values)
                last_id = cur.lastrowid

                # Fetch the inserted record
                if last_id:
                    await cur.execute(f"SELECT * FROM `{safe_table}` WHERE id = %s", (last_id,))
                    row = await cur.fetchone()
                    return row if row else data
                return data

    async def select(
        self,
        table: str,
        columns: Optional[List[str]] = None,
        filters: Optional[List[Tuple[str, str, Any]]] = None,
        pagination: Optional[Dict[str, int]] = None,
        sort: Optional[List[Tuple[str, str]]] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Select records with filtering, pagination, and sorting."""
        safe_table = self._sanitize_identifier(table)
        
        # Build SELECT clause
        if columns:
            safe_cols = [self._sanitize_identifier(c) for c in columns]
            select_cols = ', '.join(f'`{c}`' for c in safe_cols)
        else:
            select_cols = "*"

        # Build WHERE clause
        where_clauses = []
        params = []

        if filters:
            for col, op, val in filters:
                clause, new_params = self._build_where_clause(col, op, val)
                where_clauses.append(clause)
                params.extend(new_params)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Build ORDER BY clause
        order_sql = ""
        if sort:
            safe_sort = [(self._sanitize_identifier(col), dir) for col, dir in sort]
            order_parts = [f'`{col}` {dir.upper()}' for col, dir in safe_sort]
            order_sql = f"ORDER BY {', '.join(order_parts)}"

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # Count query
                count_query = f"SELECT COUNT(*) as cnt FROM `{safe_table}` {where_sql}"
                await cur.execute(count_query, params)
                count_row = await cur.fetchone()
                total = count_row["cnt"] if count_row else 0

                # Main query with pagination
                limit_sql = ""
                if pagination:
                    limit = pagination.get("limit", 50)
                    offset = pagination.get("offset", 0)
                    limit_sql = f"LIMIT {limit} OFFSET {offset}"

                query = f"SELECT {select_cols} FROM `{safe_table}` {where_sql} {order_sql} {limit_sql}"
                await cur.execute(query, params)
                rows = await cur.fetchall()

                return list(rows), total

    async def select_by_id(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        columns: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Select a single record by ID."""
        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)
        
        if columns:
            safe_cols = [self._sanitize_identifier(c) for c in columns]
            select_cols = ', '.join(f'`{c}`' for c in safe_cols)
        else:
            select_cols = "*"
            
        query = f"SELECT {select_cols} FROM `{safe_table}` WHERE `{safe_id_col}` = %s"

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, (id_value,))
                row = await cur.fetchone()
                return row

    async def update(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Update an existing record."""
        if not data:
            return await self.select_by_id(table, id_column, id_value)

        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)
        
        set_clauses = [f'`{self._sanitize_identifier(col)}` = %s' for col in data.keys()]
        values = list(data.values())
        values.append(id_value)

        query = f"""
            UPDATE `{safe_table}`
            SET {', '.join(set_clauses)}
            WHERE `{safe_id_col}` = %s
        """

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, values)

                if cur.rowcount > 0:
                    return await self.select_by_id(table, id_column, id_value)
                return None

    async def delete(
        self,
        table: str,
        id_column: str,
        id_value: Any
    ) -> bool:
        """Delete a record by ID."""
        safe_table = self._sanitize_identifier(table)
        safe_id_col = self._sanitize_identifier(id_column)
        
        query = f"DELETE FROM `{safe_table}` WHERE `{safe_id_col}` = %s"

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (id_value,))
                return cur.rowcount > 0

    def _sanitize_identifier(self, name: str) -> str:
        """Validate a SQL identifier (delegates to the shared sanitizer)."""
        return sanitize_identifier(name)

    def _build_where_clause(
        self,
        column: str,
        operator: str,
        value: Any
    ) -> Tuple[str, List[Any]]:
        """Build a WHERE clause component."""
        col = f'`{self._sanitize_identifier(column)}`'
        params = []

        if operator == "eq":
            clause = f"{col} = %s"
            params.append(value)
        elif operator == "ne":
            clause = f"{col} != %s"
            params.append(value)
        elif operator == "gt":
            clause = f"{col} > %s"
            params.append(value)
        elif operator == "gte":
            clause = f"{col} >= %s"
            params.append(value)
        elif operator == "lt":
            clause = f"{col} < %s"
            params.append(value)
        elif operator == "lte":
            clause = f"{col} <= %s"
            params.append(value)
        elif operator == "like":
            clause = f"{col} LIKE %s"
            params.append(value)
        elif operator == "in":
            if isinstance(value, (list, tuple)):
                placeholders = ", ".join(["%s"] * len(value))
                clause = f"{col} IN ({placeholders})"
                params.extend(value)
            else:
                clause = f"{col} = %s"
                params.append(value)
        elif operator == "is_null":
            clause = f"{col} IS NULL" if value else f"{col} IS NOT NULL"
        else:
            clause = f"{col} = %s"
            params.append(value)

        return clause, params
