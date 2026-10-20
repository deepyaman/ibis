"""`parse_sql` on everyday SQL, checked against DuckDB running the same SQL."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

import ibis
import ibis.common.exceptions as com

pytest.importorskip("duckdb")

CATALOG = {
    "t": {"a": "int64", "b": "string", "c": "float64", "d": "date", "ts": "timestamp"},
    "u": {"a": "int64", "x": "string"},
}

T_ROWS = """
VALUES
  (1, 'foo', 1.5, DATE '2024-01-01', TIMESTAMP '2024-01-01 10:00:00'),
  (2, 'bar', 2.5, DATE '2024-02-01', TIMESTAMP '2024-02-02 11:00:00'),
  (3, NULL, NULL, DATE '2024-03-01', TIMESTAMP '2024-03-03 12:00:00'),
  (2, 'baz', 4.0, DATE '2024-04-01', TIMESTAMP '2024-04-04 13:00:00')
"""
U_ROWS = "VALUES (1, 'p'), (2, 'q'), (9, 'r')"


@pytest.fixture(scope="module")
def duck():
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t AS SELECT * FROM ({T_ROWS}) v(a, b, c, d, ts)")
    con.execute(f"CREATE TABLE u AS SELECT * FROM ({U_ROWS}) v(a, x)")
    return con


def _kind(col: pd.Series) -> str:
    """Classify a result column for comparison purposes."""
    if not col.notna().any():
        return "null"
    if pd.api.types.is_datetime64_any_dtype(col) or (
        col.dtype == object and col.dropna().map(lambda v: hasattr(v, "year")).all()
    ):
        return "datetime"
    if pd.api.types.is_bool_dtype(col):
        return "string"
    if pd.api.types.is_numeric_dtype(col) or (
        col.dtype == object
        and pd.to_numeric(col, errors="coerce").notna().sum() == col.notna().sum()
    ):
        return "numeric"
    return "string"


def _normalize(col: pd.Series, kind: str) -> pd.Series:
    if kind == "datetime":
        return pd.to_datetime(col).dt.strftime("%Y-%m-%d %H:%M:%S").astype(object)
    if kind == "numeric":
        return pd.to_numeric(col).astype(float)
    return col.astype(object).where(col.notna(), None).astype(str)


def assert_same_rows(result: pd.DataFrame, expected: pd.DataFrame) -> None:
    """Compare by position and value: SQL output names and row order are free.

    Numbers are compared to two decimals: Ibis types the mean of a decimal
    column as a decimal of the same scale where DuckDB computes a double.
    """
    assert result.shape == expected.shape
    left, right = pd.DataFrame(), pd.DataFrame()
    for i, (rcol, ecol) in enumerate(zip(result.columns, expected.columns)):
        kinds = {_kind(result[rcol]), _kind(expected[ecol])} - {"null"}
        kind = (
            "numeric"
            if kinds == {"numeric"}
            else kinds.pop()
            if len(kinds) == 1
            else "string"
        )
        left[i] = _normalize(result[rcol], kind)
        right[i] = _normalize(expected[ecol], kind)
    numeric = [c for c in left.columns if left[c].dtype == float]
    for frame in (left, right):
        frame[numeric] = frame[numeric].round(2)
    left = left.sort_values(list(left.columns), kind="stable").reset_index(drop=True)
    right = right.sort_values(list(right.columns), kind="stable").reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, check_exact=False, atol=0.011)


CASES = {
    "select_where": "SELECT a, b FROM t WHERE a > 1",
    "count_star": "SELECT COUNT(*) AS n FROM t",
    "count_star_group_by": "SELECT b, COUNT(*) AS n FROM t GROUP BY b",
    "distinct": "SELECT DISTINCT a FROM t",
    "having": "SELECT a, SUM(c) AS s FROM t GROUP BY a HAVING SUM(c) > 2",
    "order_limit_offset": "SELECT a FROM t ORDER BY a LIMIT 2 OFFSET 1",
    "case_when": "SELECT CASE WHEN a > 1 THEN 'big' ELSE 'small' END AS sz FROM t",
    "not_is_null": "SELECT a FROM t WHERE NOT (b IS NULL)",
    "is_null": "SELECT a FROM t WHERE b IS NULL",
    "like": "SELECT a FROM t WHERE b LIKE 'ba%'",
    "between": "SELECT a FROM t WHERE a BETWEEN 1 AND 2",
    "coalesce": "SELECT COALESCE(b, 'none') AS b2 FROM t",
    "string_functions": "SELECT UPPER(b) AS ub, LENGTH(b) AS lb, SUBSTRING(b, 1, 2) AS sb FROM t",
    "date_functions": "SELECT EXTRACT(year FROM d) AS y, DATE_TRUNC('month', ts) AS m FROM t",
    "date_arithmetic": "SELECT a FROM t WHERE d >= DATE '2024-02-01' AND d < DATE '2024-02-01' + INTERVAL '1' MONTH",
    "cast": "SELECT CAST(a AS VARCHAR) AS s FROM t",
    "arithmetic_alias": "SELECT a * 2 + 1 AS z, c / 2 AS h FROM t",
    "window_row_number": "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY a) AS rn FROM t",
    "window_cumulative_sum": "SELECT a, SUM(c) OVER (ORDER BY a) AS cs FROM t",
    "window_rows_frame": "SELECT a, SUM(c) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS s FROM t",
    "window_range_frame": "SELECT a, SUM(c) OVER (PARTITION BY b ORDER BY a RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM t",
    "window_lag_first_value": "SELECT a, LAG(a) OVER (ORDER BY a) AS p, FIRST_VALUE(b) OVER (PARTITION BY b ORDER BY a) AS f FROM t",
    "window_rank": "SELECT a, RANK() OVER (ORDER BY c DESC) AS r, DENSE_RANK() OVER (ORDER BY a) AS d FROM t",
    "window_ntile": "SELECT a, NTILE(2) OVER (ORDER BY a) AS n FROM t",
    "window_over_aggregate": "SELECT a, SUM(c) AS s, AVG(SUM(c)) OVER () AS avg_s FROM t GROUP BY a",
    "cte": "WITH x AS (SELECT a, c FROM t WHERE a > 1) SELECT a, SUM(c) AS s FROM x GROUP BY a",
    "cte_with_union": "WITH x AS (SELECT a FROM t UNION ALL SELECT a FROM u) SELECT a, COUNT(*) AS n FROM x GROUP BY a",
    "cte_referenced_twice": "WITH x AS (SELECT a, c FROM t WHERE a > 1) SELECT x1.a, x2.c FROM x AS x1 JOIN x AS x2 ON x1.a = x2.a",
    "derived_table": "SELECT s.a FROM (SELECT a FROM t WHERE a > 1) AS s",
    "derived_union": "SELECT s.a FROM (SELECT a FROM t UNION SELECT a FROM u) AS s WHERE s.a > 1",
    "union_all": "SELECT a FROM t UNION ALL SELECT a FROM u",
    "union": "SELECT a FROM t UNION SELECT a FROM u",
    "union_order_limit": "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY a LIMIT 3",
    "except": "SELECT a FROM t EXCEPT SELECT a FROM u",
    "intersect": "SELECT a FROM t INTERSECT SELECT a FROM u",
    "in_subquery": "SELECT a FROM t WHERE a IN (SELECT a FROM u)",
    "exists": "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)",
    "not_exists": "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a)",
    "correlated_scalar_subquery": "SELECT a FROM t WHERE c > (SELECT AVG(c) FROM t AS t2 WHERE t2.a = t.a)",
    "scalar_subquery": "SELECT a FROM t WHERE c > (SELECT AVG(c) FROM t)",
    "join": "SELECT t.a, u.x FROM t JOIN u ON t.a = u.a",
    "left_join_anti": "SELECT t.a, u.x FROM t LEFT JOIN u ON t.a = u.a WHERE u.x IS NULL",
    "self_join": "SELECT t1.a, t2.b FROM t AS t1 JOIN t AS t2 ON t1.a = t2.a",
    "cross_join": "SELECT t.a, u.x FROM t CROSS JOIN u",
    "count_distinct": "SELECT COUNT(DISTINCT a) AS n FROM t",
    "reductions": "SELECT AVG(c) AS m, MIN(a) AS lo, MAX(a) AS hi FROM t",
    "stddev_variance": "SELECT STDDEV(c) AS s, VAR_POP(c) AS v FROM t",
    "or": "SELECT a FROM t WHERE a = 1 OR b = 'baz'",
    "select_without_from": "SELECT 1 AS one, 'x' AS s",
    "order_by_desc_nulls_last": "SELECT a, c FROM t ORDER BY c DESC NULLS LAST",
    "group_by_expression": "SELECT a % 2 AS parity, COUNT(*) AS n FROM t GROUP BY a % 2",
    "aggregate_of_expression": "SELECT SUM(c * 2) AS s FROM t",
    "math_functions": "SELECT ROUND(c, 1) AS r, ABS(a - 2) AS ab, GREATEST(a, 2) AS g FROM t",
    "concat": "SELECT b || '_' || b AS bb, CONCAT(b, 'x') AS bx FROM t",
    "derived_computed": "SELECT s.z FROM (SELECT a + c AS z FROM t) AS s",
    "derived_where_alias": "SELECT s.aa FROM (SELECT a AS aa FROM t) AS s WHERE s.aa > 1",
    "nested_derived": "SELECT q.z FROM (SELECT s.z + 1 AS z FROM (SELECT a AS z FROM t) AS s) AS q",
    "derived_with_limit": "SELECT s.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) AS s",
    "scalar_sub_select": "SELECT a, (SELECT MAX(a) FROM u) AS m FROM t",
    "corr_scalar_select": "SELECT a, (SELECT MAX(u.a) FROM u WHERE u.a = t.a) AS m FROM t",
    "corr_scalar_select_after_where": "SELECT a, (SELECT COUNT(*) FROM u WHERE u.a = t.a) AS n FROM t WHERE c > 1",
    "count_star_arith": "SELECT SUM(c) / COUNT(*) AS r FROM t",
    "order_by_nonprojected": "SELECT a FROM t ORDER BY c DESC",
    "order_by_alias": "SELECT a + 1 AS x FROM t ORDER BY x",
    "order_by_ordinal": "SELECT a, b FROM t ORDER BY 2, 1",
    "order_by_agg_not_selected": "SELECT a FROM t GROUP BY a ORDER BY SUM(c)",
    "having_alias": "SELECT a, SUM(c) AS s FROM t GROUP BY a HAVING s > 2",
    "having_count_not_selected": "SELECT b FROM t GROUP BY b HAVING COUNT(*) > 1",
    "having_corr_sub": "SELECT a, SUM(c) AS s FROM t GROUP BY a HAVING SUM(c) > (SELECT AVG(u.a) FROM u WHERE u.a = t.a)",
    "select_corr_after_group": "SELECT a, (SELECT COUNT(*) FROM u WHERE u.a = t.a) AS n FROM t GROUP BY a",
    "group_by_expression_alias": "SELECT a % 2 AS parity, COUNT(*) AS n FROM t GROUP BY parity",
    "group_by_ordinal": "SELECT a % 2, COUNT(*) FROM t GROUP BY 1",
    "dup_names": "SELECT a, a FROM t",
    "dup_alias_select": "SELECT a, COUNT(*) AS a FROM t GROUP BY a",
    "eq_null": "SELECT a FROM t WHERE b = NULL",
    "null_arith": "SELECT a + NULL AS z FROM t",
    "try_cast": "SELECT TRY_CAST(b AS INTEGER) AS i FROM t",
    "using": "SELECT a, x FROM t JOIN u USING (a)",
    "semi": "SELECT a FROM t SEMI JOIN u ON t.a = u.a",
    "anti": "SELECT a FROM t ANTI JOIN u ON t.a = u.a",
    "right_join": "SELECT t.a, u.x FROM t RIGHT JOIN u ON t.a = u.a",
    "full_join": "SELECT t.a, u.x FROM t FULL OUTER JOIN u ON t.a = u.a",
    "select_star_join": "SELECT * FROM t JOIN u ON t.a = u.a",
    "window_filter_derived": "SELECT * FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t) AS s WHERE rn = 1",
    "window_order_by_dropped": "SELECT a, SUM(c) OVER (PARTITION BY b) AS s FROM t ORDER BY b",
    "subq_in_join_source": "SELECT t.a, s.m FROM t JOIN (SELECT a, MAX(c) AS m FROM t GROUP BY a) AS s ON t.a = s.a WHERE s.m > 1",
    "join_on_subquery_eq": "SELECT t.a FROM t JOIN u ON t.a = u.a AND t.c > (SELECT AVG(c) FROM t)",
    "three_way_comma": "SELECT t.a, t2.b FROM t, u, t AS t2 WHERE t.a = u.a AND u.a = t2.a AND t2.c > 1",
    "comma_join_where_or": "SELECT t.a FROM t, u WHERE (t.a = u.a AND t.c > 1) OR (t.a = u.a + 1 AND t.c < 1)",
    "left_join_filter_right": "SELECT t.a FROM t LEFT JOIN u ON t.a = u.a WHERE u.x = 'p'",
    "named_window": "SELECT a, SUM(c) OVER w AS s FROM t WINDOW w AS (PARTITION BY b ORDER BY a)",
    "count_star_window_group": "SELECT a, COUNT(*) OVER () AS n FROM t GROUP BY a",
    "filter_where_agg": "SELECT SUM(c) FILTER (WHERE a > 1) AS s FROM t",
    "any_subquery": "SELECT a FROM t WHERE a = ANY (SELECT a FROM u)",
    "distinct_order_by": "SELECT DISTINCT a FROM t ORDER BY a",
    "cte_corr_scalar": "WITH x AS (SELECT a, c FROM t WHERE a > 1) SELECT a FROM x WHERE c > (SELECT AVG(u.a) FROM u WHERE u.a = x.a)",
    "exists_join_inner": "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM u JOIN t AS t2 ON u.a = t2.a WHERE t2.c = t.c)",
}

UNSUPPORTED = {
    "distinct_on": ("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b", "DISTINCT ON"),
    "qualify": (
        "SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t QUALIFY rn = 1",
        "QUALIFY",
    ),
    "rollup": ("SELECT a, COUNT(*) FROM t GROUP BY ROLLUP (a)", "ROLLUP"),
    "cube": ("SELECT a, b, COUNT(*) FROM t GROUP BY CUBE (a, b)", "CUBE"),
    "grouping_sets": (
        "SELECT a, b, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), (b))",
        "GROUPING SETS",
    ),
    "natural_join": ("SELECT * FROM t NATURAL JOIN u", "(?i)join"),
    "lateral": (
        "SELECT t.a, l.z FROM t, LATERAL (SELECT t.a + 1 AS z) AS l",
        "(?i)lateral",
    ),
    "values": ("SELECT * FROM (VALUES (1, 'a')) AS v(n, s)", "(?i)values"),
    "unnest": ("SELECT * FROM UNNEST([1, 2]) AS x(v)", "(?i)unnest"),
    "recursive_cte": (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
        "RECURSIVE",
    ),
    "sum_distinct": ("SELECT SUM(DISTINCT a) FROM t", "(?i)distinct"),
    "limit_subquery": ("SELECT a FROM t LIMIT (SELECT 1)", "LIMIT"),
    "outer_join_subquery_on": (
        "SELECT t.a FROM t LEFT JOIN u ON t.a = u.a AND t.c > (SELECT AVG(c) FROM t)",
        "outer join",
    ),
    "unknown_function": ("SELECT MD5(b) FROM t", "MD5"),
    "any_lt": ("SELECT a FROM t WHERE a < ANY (SELECT a FROM u)", "ANY"),
}


def roundtrip(expr):
    """Decompile `expr` to Python, run that code, and return what it builds."""
    code = ibis.decompile(expr)
    namespace = {}
    exec(code, {}, namespace)  # noqa: S102
    return namespace["result"]


@pytest.mark.parametrize("sql", list(CASES.values()), ids=list(CASES))
def test_parse_sql_matches_duckdb(duck, sql):
    expr = ibis.parse_sql(sql, CATALOG, dialect="duckdb")

    expected = duck.execute(sql).df()
    result = ibis.duckdb.from_connection(duck).to_pandas(expr)

    assert_same_rows(result, expected)


@pytest.mark.parametrize("sql", list(CASES.values()), ids=list(CASES))
def test_decompile_roundtrip_matches_duckdb(duck, sql):
    """Parsed SQL decompiles to Python that rebuilds an equivalent expression."""
    expr = roundtrip(ibis.parse_sql(sql, CATALOG, dialect="duckdb"))

    expected = duck.execute(sql).df()
    result = ibis.duckdb.from_connection(duck).to_pandas(expr)

    assert_same_rows(result, expected)


@pytest.mark.parametrize(
    ("sql", "message"), list(UNSUPPORTED.values()), ids=list(UNSUPPORTED)
)
def test_parse_sql_unsupported(sql, message):
    """SQL `parse_sql` cannot translate raises instead of dropping a clause."""
    with pytest.raises(com.UnsupportedOperationError, match=message):
        ibis.parse_sql(sql, CATALOG, dialect="duckdb")
