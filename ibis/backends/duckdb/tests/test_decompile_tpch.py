from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from pytest import param

import ibis
from ibis.backends.duckdb.tests.test_parse_sql import assert_same_rows
from ibis.backends.tests.tpc.conftest import compare_tpc_results
from ibis.formats.pandas import PandasData

tpch_catalog = {
    "lineitem": {
        "l_orderkey": "int64",
        "l_partkey": "int64",
        "l_suppkey": "int64",
        "l_linenumber": "int32",
        "l_quantity": "decimal(15, 2)",
        "l_extendedprice": "decimal(15, 2)",
        "l_discount": "decimal(15, 2)",
        "l_tax": "decimal(15, 2)",
        "l_returnflag": "string",
        "l_linestatus": "string",
        "l_shipdate": "date",
        "l_commitdate": "date",
        "l_receiptdate": "date",
        "l_shipinstruct": "string",
        "l_shipmode": "string",
        "l_comment": "string",
    },
    "customer": {
        "c_custkey": "int64",
        "c_name": "string",
        "c_address": "string",
        "c_nationkey": "int32",
        "c_phone": "string",
        "c_acctbal": "decimal(15, 2)",
        "c_mktsegment": "string",
        "c_comment": "string",
    },
    "orders": {
        "o_orderkey": "int64",
        "o_custkey": "int64",
        "o_orderstatus": "string",
        "o_totalprice": "decimal(15, 2)",
        "o_orderdate": "date",
        "o_orderpriority": "string",
        "o_clerk": "string",
        "o_shippriority": "int32",
        "o_comment": "string",
    },
    "part": {
        "p_partkey": "int64",
        "p_name": "string",
        "p_mfgr": "string",
        "p_brand": "string",
        "p_type": "string",
        "p_size": "int32",
        "p_container": "string",
        "p_retailprice": "decimal(15, 2)",
        "p_comment": "string",
    },
    "partsupp": {
        "ps_partkey": "int64",
        "ps_suppkey": "int64",
        "ps_availqty": "int32",
        "ps_supplycost": "decimal(15, 2)",
        "ps_comment": "string",
    },
    "supplier": {
        "s_suppkey": "int64",
        "s_name": "string",
        "s_address": "string",
        "s_nationkey": "int32",
        "s_phone": "string",
        "s_acctbal": "decimal(15, 2)",
        "s_comment": "string",
    },
    "nation": {
        "n_nationkey": "int32",
        "n_name": "string",
        "n_regionkey": "int32",
        "n_comment": "string",
    },
    "region": {"r_regionkey": "int32", "r_name": "string", "r_comment": "string"},
}

root = Path(__file__).absolute().parents[3]

SQL_QUERY_PATH = root / "backends" / "tests" / "tpc" / "queries" / "duckdb" / "h"
TPCDS_QUERY_PATH = root / "backends" / "tests" / "tpc" / "queries" / "duckdb" / "ds"

# Ibis has no ROLLUP / GROUPING SETS
TPCDS_ROLLUP_QUERIES = frozenset({5, 14, 18, 22, 67, 70, 77, 80, 86})


@contextmanager
def set_database(con, db):
    olddb = con.current_database
    con.raw_sql(f"USE {db}")
    yield
    con.raw_sql(f"USE {olddb}")


@pytest.mark.parametrize(
    "tpch_query",
    [
        param(1, id="tpch01"),
        param(3, id="tpch03"),
    ],
)
def test_parse_sql_tpch(tpch_query, snapshot, con):
    tpch_query_file = SQL_QUERY_PATH / f"{tpch_query:02d}.sql"
    with open(tpch_query_file) as f:
        sql = f.read()

    expr = ibis.parse_sql(sql, tpch_catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "out_tpch.py")

    # Import just-created snapshot
    SNAPSHOT_MODULE = f"ibis.backends.duckdb.tests.snapshots.test_decompile_tpch.test_parse_sql_tpch.tpch{tpch_query:02d}.out_tpch"
    module = importlib.import_module(SNAPSHOT_MODULE)

    with set_database(con, "tpch"):
        # Get results from executing SQL directly on DuckDB
        expected_df = con.con.execute(sql).df()
        # Get results from decompiled ibis query
        result_df = con.to_pandas(module.result)

    # Then set the expected columns so we can coerce the datatypes
    # of the pandas dataframe correctly
    expected_df.columns = result_df.columns

    expected_df = PandasData.convert_table(expected_df, module.result.schema())

    compare_tpc_results(result_df, expected_df)


@pytest.mark.parametrize("tpch_query", range(1, 23), ids=lambda q: f"tpch{q:02d}")
def test_parse_sql_tpch_results(tpch_query, con):
    """Every TPC-H query parses into an expression that computes the same result."""
    sql = (SQL_QUERY_PATH / f"{tpch_query:02d}.sql").read_text()

    expr = ibis.parse_sql(sql, tpch_catalog, dialect="duckdb")

    with set_database(con, "tpch"):
        expected_df = con.con.execute(sql).df()
        result_df = con.to_pandas(expr)

    expected_df.columns = result_df.columns
    expected_df = PandasData.convert_table(expected_df, expr.schema())

    compare_tpc_results(result_df, expected_df)


@pytest.mark.parametrize(
    "tpcds_query",
    [
        param(
            q,
            id=f"tpcds{q:02d}",
            marks=[pytest.mark.xfail(reason="requires rollup")]
            if q in TPCDS_ROLLUP_QUERIES
            else [],
        )
        for q in range(1, 100)
    ],
)
def test_parse_sql_tpcds_results(tpcds_query, con):
    """Every TPC-DS query parses into an expression that computes the same result."""
    sql = (TPCDS_QUERY_PATH / f"{tpcds_query:02d}.sql").read_text()

    with set_database(con, "tpcds"):
        catalog = {name: con.table(name).schema() for name in con.list_tables()}
        expr = ibis.parse_sql(sql, catalog, dialect="duckdb")
        expected_df = con.con.execute(sql).df()
        result_df = con.to_pandas(expr)

    # SQL output names and (for ties) row order are free, values are not
    assert_same_rows(result_df, expected_df)
