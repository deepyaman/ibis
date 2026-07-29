from __future__ import annotations

import pyarrow as pa
import pytest

import ibis
from ibis.backends.snowflake.converter import (
    PYARROW_JSON_TYPE,
    SnowflakePyArrowData,
    storage_schema,
)
from ibis.formats.pyarrow import PyArrowSchema

JSON_ENCODED = ["json", "array<int64>", "map<string, int64>", "struct<a: int64>"]


@pytest.mark.parametrize("dtype", JSON_ENCODED)
def test_convert_schema_wraps_json_encoded_types(dtype):
    schema = ibis.schema({"x": dtype})
    assert SnowflakePyArrowData.convert_schema(schema).field("x").type == (
        PYARROW_JSON_TYPE
    )


def test_convert_schema_matches_base_for_scalar_types():
    schema = ibis.schema(
        {"i": "int64", "s": "string", "f": "float64", "b": "boolean", "t": "timestamp"}
    )
    assert SnowflakePyArrowData.convert_schema(schema).equals(
        PyArrowSchema.from_ibis(schema)
    )


def test_convert_schema_preserves_nullability():
    schema = ibis.schema({"a": "!int64", "b": "int64", "c": "!json", "d": "json"})
    fields = SnowflakePyArrowData.convert_schema(schema)
    assert [field.nullable for field in fields] == [False, True, False, True]


def test_convert_schema_matches_convert_table_output():
    # `to_pyarrow_batches` declares the reader schema up front, and
    # `RecordBatchReader.from_batches` requires every batch to match it exactly
    schema = ibis.schema({"i": "int64", "js": "json", "arr": "array<int64>"})
    # what the connector hands back: JSON-encoded columns as strings
    raw = pa.table(
        {
            "i": pa.array([1, 2], pa.int8()),
            "js": pa.array(['{"a": 1}', "null"]),
            "arr": pa.array(["[1]", "[2]"]),
        }
    )
    converted = SnowflakePyArrowData.convert_table(raw, schema)
    assert converted.schema.equals(SnowflakePyArrowData.convert_schema(schema))


def test_storage_schema_unwraps_extension_types():
    schema = ibis.schema({"i": "int64", "js": "json", "arr": "array<int64>"})
    unwrapped = storage_schema(SnowflakePyArrowData.convert_schema(schema))
    assert unwrapped.types == [pa.int64(), pa.string(), pa.string()]


def test_storage_schema_is_noop_without_extension_types():
    schema = PyArrowSchema.from_ibis(ibis.schema({"i": "int64", "s": "string"}))
    assert storage_schema(schema).equals(schema)


def test_storage_schema_preserves_nullability():
    schema = SnowflakePyArrowData.convert_schema(
        ibis.schema({"a": "!json", "b": "json"})
    )
    assert [field.nullable for field in storage_schema(schema)] == [False, True]


def test_empty_table_survives_conversion():
    # the connector returns None for a zero-row result; the stand-in has to use
    # the storage types, because the extension wrapping rejects native nested
    # arrays as storage
    schema = ibis.schema(
        {"i": "int64", "arr": "array<int64>", "m": "map<string, int64>"}
    )
    empty = storage_schema(SnowflakePyArrowData.convert_schema(schema)).empty_table()
    converted = SnowflakePyArrowData.convert_table(empty, schema)
    assert len(converted) == 0
    assert converted.schema.equals(SnowflakePyArrowData.convert_schema(schema))


def test_csv_unwrapping_avoids_recordbatch_cast():
    # `RecordBatch.cast` only exists in pyarrow >= 16, and the snowflake extra
    # supports >= 10, so `to_csv` unwraps extension columns via `.storage`
    schema = ibis.schema({"i": "int64", "js": "json"})
    raw = pa.table({"i": pa.array([1]), "js": pa.array(['{"a": 1}'])})
    batch = SnowflakePyArrowData.convert_table(raw, schema).to_batches()[0]

    target = storage_schema(batch.schema)
    unwrapped = pa.RecordBatch.from_arrays(
        [
            column.storage if isinstance(column.type, pa.ExtensionType) else column
            for column in batch.columns
        ],
        schema=target,
    )
    assert unwrapped.schema.equals(target)
    assert unwrapped.column(1).to_pylist() == ['{"a": 1}']
