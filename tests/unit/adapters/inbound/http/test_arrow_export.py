"""Type mapping and IPC encoding of the Arrow export helpers."""

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pyarrow as pa
import pytest

from warp.adapters.inbound.http.arrow_export import (
    ExportSerializationError,
    _kind,
    arrow_available,
    arrow_ipc_stream,
    arrow_schema_for,
    to_record_batch,
)
from warp.domain.schema import ColumnSchema, TableSchema


def col(type_: str, **kw) -> ColumnSchema:
    return ColumnSchema(name=kw.pop("name", "c"), type=type_, **kw)


@pytest.mark.parametrize(
    ("column", "kind"),
    [
        (col("smallint", udt_name="int2"), "int16"),
        (col("tinyint"), "int16"),
        (col("integer", udt_name="int4"), "int32"),
        (col("int"), "int32"),
        (col("bigint", udt_name="int8"), "int64"),
        (col("numeric", precision=12, scale=2), "decimal"),
        (col("decimal"), "decimal"),
        (col("real"), "float32"),
        (col("double precision", udt_name="float8"), "float64"),
        (col("double"), "float64"),
        (col("boolean"), "bool"),
        (col("tinyint", full_type="tinyint(1)"), "bool"),
        (col("timestamp with time zone", udt_name="timestamptz"), "timestamptz"),
        (col("timestamp without time zone"), "timestamp"),
        (col("datetime"), "timestamp"),
        (col("date"), "date"),
        (col("time without time zone"), "time"),
        (col("bytea"), "binary"),
        (col("blob"), "binary"),
        (col("ARRAY", udt_name="_int4"), "array"),
        (col("text"), "string"),
        (col("uuid"), "string"),
        (col("jsonb"), "string"),
        (col("USER-DEFINED", udt_name="mood"), "string"),
        (col("interval"), "string"),
    ],
)
def test_kind_mapping(column, kind):
    assert _kind(column) == kind


def test_arrow_available_here():
    assert arrow_available() is True


class TestSchema:
    def test_types(self):
        schema = arrow_schema_for(
            [
                col("integer", name="id", udt_name="int4"),
                col("numeric", name="amount", precision=10, scale=2),
                col("numeric", name="free"),  # no precision → text
                col("timestamp with time zone", name="ts", udt_name="timestamptz"),
                col("timestamp", name="naive"),
                col("date", name="d"),
                col("time", name="t"),
                col("bytea", name="b"),
                col("text", name="s"),
            ]
        )
        assert schema.field("id").type == pa.int32()
        assert schema.field("amount").type == pa.decimal128(10, 2)
        assert schema.field("free").type == pa.string()
        assert schema.field("ts").type == pa.timestamp("us", tz="UTC")
        assert schema.field("naive").type == pa.timestamp("us")
        assert schema.field("d").type == pa.date32()
        assert schema.field("t").type == pa.time64("us")
        assert schema.field("b").type == pa.binary()
        assert schema.field("s").type == pa.string()
        assert all(f.nullable for f in schema)

    def test_huge_precision_decimal_is_text(self):
        schema = arrow_schema_for([col("numeric", name="n", precision=50, scale=10)])
        assert schema.field("n").type == pa.string()


class TestRecordBatch:
    def test_coercions(self):
        columns = [
            col("integer", name="id", udt_name="int4"),
            col("numeric", name="amount", precision=10, scale=2),
            col("boolean", name="flag"),
            col("timestamp with time zone", name="ts", udt_name="timestamptz"),
            col("jsonb", name="doc"),
            col("uuid", name="u"),
            col("interval", name="iv"),
            col("ARRAY", name="tags", udt_name="_text"),
            col("bytea", name="b"),
        ]
        schema = arrow_schema_for(columns)
        plus_two = timezone(timedelta(hours=2))
        rows = [
            {
                "id": 1,
                "amount": Decimal("1.50"),
                "flag": 1,
                "ts": datetime(2024, 1, 1, 12, tzinfo=plus_two),
                "doc": {"a": [1, 2]},
                "u": UUID("12345678-1234-5678-1234-567812345678"),
                "iv": timedelta(seconds=5),
                "tags": ["x", "y"],
                "b": bytearray(b"\x00\x01"),
            },
            {
                "id": "2",
                "amount": 2.25,
                "flag": False,
                "ts": datetime(2024, 1, 1, 12),  # naive → assumed UTC
                "doc": None,
                "u": None,
                "iv": None,
                "tags": None,
                "b": None,
            },
        ]
        batch = to_record_batch(rows, schema, columns)
        assert batch.num_rows == 2
        assert batch.column("id").to_pylist() == [1, 2]
        assert batch.column("amount").to_pylist() == [Decimal("1.50"), Decimal("2.25")]
        assert batch.column("flag").to_pylist() == [True, False]
        ts = batch.column("ts").to_pylist()
        assert ts[0] == datetime(2024, 1, 1, 10, tzinfo=UTC)
        assert ts[1] == datetime(2024, 1, 1, 12, tzinfo=UTC)
        assert json.loads(batch.column("doc").to_pylist()[0]) == {"a": [1, 2]}
        assert batch.column("u").to_pylist() == ["12345678-1234-5678-1234-567812345678", None]
        assert batch.column("iv").to_pylist() == ["0:00:05", None]
        assert json.loads(batch.column("tags").to_pylist()[0]) == ["x", "y"]
        assert batch.column("b").to_pylist() == [b"\x00\x01", None]

    def test_missing_keys_are_null(self):
        columns = [col("integer", name="id"), col("text", name="s")]
        schema = arrow_schema_for(columns)
        batch = to_record_batch([{"id": 1}], schema, columns)
        assert batch.column("s").to_pylist() == [None]

    def test_string_column_falls_back_to_str(self):
        columns = [col("text", name="s")]
        schema = arrow_schema_for(columns)

        class Weird:
            def __str__(self) -> str:
                return "weird"

        batch = to_record_batch([{"s": Weird()}], schema, columns)
        assert batch.column("s").to_pylist() == ["weird"]

    def test_non_string_column_raises(self):
        columns = [col("integer", name="id", udt_name="int4")]
        schema = arrow_schema_for(columns)
        with pytest.raises(ExportSerializationError, match="'id'"):
            to_record_batch([{"id": "not a number"}], schema, columns)
        with pytest.raises(ExportSerializationError):
            to_record_batch([{"id": 2**40}], schema, columns)  # overflows int32

    def test_date_and_time_pass_through(self):
        columns = [col("date", name="d"), col("time", name="t")]
        schema = arrow_schema_for(columns)
        batch = to_record_batch([{"d": date(2024, 5, 6), "t": time(7, 8, 9)}], schema, columns)
        assert batch.column("d").to_pylist() == [date(2024, 5, 6)]
        assert batch.column("t").to_pylist() == [time(7, 8, 9)]


async def _batches(*batches):
    for batch in batches:
        yield batch


class TestIpcStream:
    @pytest.mark.asyncio
    async def test_round_trip_with_columns_and_empty_batches(self):
        table = TableSchema(
            table_name="t",
            columns=[
                col("integer", name="id", udt_name="int4"),
                col("text", name="name"),
                col("boolean", name="ok"),
            ],
        )
        chunks = [
            chunk
            async for chunk in arrow_ipc_stream(
                _batches([{"id": 1, "name": "a", "ok": True}], [], [{"id": 2, "name": None}]),
                table,
                ["name", "id"],
            )
        ]
        assert len(chunks) == 4  # schema (+ empty batch), batch, batch, EOS
        assert pa.ipc.open_stream(chunks[0]).read_all().num_rows == 0
        result = pa.ipc.open_stream(b"".join(chunks)).read_all()
        assert result.schema.names == ["name", "id"]
        assert result.column("id").to_pylist() == [1, 2]
        assert result.column("name").to_pylist() == ["a", None]

    @pytest.mark.asyncio
    async def test_schema_is_first_chunk(self):
        table = TableSchema(table_name="t", columns=[col("integer", name="id")])
        stream = arrow_ipc_stream(_batches([{"id": 1}]), table)
        first = await stream.__anext__()
        assert pa.ipc.read_schema(pa.BufferReader(first)).names == ["id"]
        # Drain the generator so the writer is closed cleanly.
        async for _ in stream:
            pass
