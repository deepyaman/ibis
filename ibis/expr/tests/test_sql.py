from __future__ import annotations

import pytest

import ibis
import ibis.common.exceptions as com
import ibis.expr.operations as ops
import ibis.expr.types as ir

pytest.importorskip("black")

catalog = {
    "employee": {"first_name": "string", "last_name": "string", "id": "int64"},
    "call": {
        "start_time": "timestamp",
        "end_time": "timestamp",
        "employee_id": "int64",
        "call_outcome_id": "int64",
        "call_attempts": "int64",
    },
    "call_outcome": {"outcome_text": "string", "id": "int64"},
}


def test_parse_sql_basic_projection(snapshot):
    sql = "SELECT *, first_name as first FROM employee WHERE id < 5 ORDER BY id DESC"
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


@pytest.mark.parametrize("how", ["right", "left", "inner"])
def test_parse_sql_basic_join(how, snapshot):
    sql = f"""
SELECT
  *,
  first_name as first
FROM employee {how.upper()}
JOIN call ON
  employee.id = call.employee_id
WHERE
  id < 5
ORDER BY
  id DESC"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_limited_join(snapshot):
    sql = """
SELECT
  *,
  first_name as first
FROM employee
JOIN call ON
  employee.id = call.employee_id
LIMIT 3"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_multiple_joins(snapshot):
    sql = """
SELECT employee.*, call.*, call_outcome.outcome_text, call_outcome.id as id_right
FROM employee
JOIN call
  ON employee.id = call.employee_id
JOIN call_outcome
  ON call.call_outcome_id = call_outcome.id"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_basic_aggregation(snapshot):
    sql = """
SELECT
  employee_id,
  sum(call_attempts) AS attempts
FROM call
GROUP BY employee_id"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_basic_aggregation_with_join(snapshot):
    sql = """
SELECT
  id,
  sum(call_attempts) AS attempts
FROM employee
LEFT JOIN call
  ON employee.id = call.employee_id
GROUP BY id"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_aggregation_with_multiple_joins(snapshot):
    sql = """
SELECT
  t.employee_id,
  AVG(t.call_attempts) AS avg_attempts
FROM (
  SELECT * FROM employee JOIN call ON employee.id = call.employee_id
  JOIN call_outcome ON call.call_outcome_id = call_outcome.id
) AS t
GROUP BY t.employee_id"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_simple_reduction(snapshot):
    sql = """SELECT AVG(call_attempts) AS mean FROM call"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_scalar_subquery(snapshot):
    sql = """
SELECT *
FROM call
WHERE call_attempts > (
  SELECT avg(call_attempts) AS mean
  FROM call
)"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_join_subquery(snapshot):
    sql = """
SELECT *
FROM call
INNER JOIN (
  SELECT
    AVG(call.call_attempts) AS mean
  FROM call
) AS subq
  ON subq.mean < call.call_attempts"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_simple_select_count(snapshot):
    sql = """SELECT COUNT(first_name) FROM employee"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_table_alias(snapshot):
    sql = """SELECT e.* FROM employee AS e"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_join_with_filter(snapshot):
    sql = """
SELECT *, first_name as first FROM employee
LEFT JOIN call ON employee.id = call.employee_id
WHERE id < 5
ORDER BY id DESC"""
    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


def test_parse_sql_in_clause(snapshot):
    sql = """
SELECT first_name FROM employee
WHERE first_name IN ('Graham', 'John', 'Terry', 'Eric', 'Michael')"""

    expr = ibis.parse_sql(sql, catalog)
    code = ibis.decompile(expr, format=True)
    snapshot.assert_match(code, "decompiled.py")


tables = {
    "t": {"a": "int64", "b": "string", "c": "float64"},
    "u": {"a": "int64", "x": "string"},
    "v": {"a": "int64", "y": "string"},
}


def test_comma_join_conditions_become_join_keys():
    expr = ibis.parse_sql("SELECT t.a FROM t, u WHERE t.a = u.a AND t.c > 1", tables)
    (chain,) = expr.op().find(ops.JoinChain)
    (link,) = chain.rest
    assert link.how == "inner"
    (predicate,) = link.predicates
    assert isinstance(predicate, ops.Equals)
    # the condition on one table stays a filter, as written
    (filt,) = expr.op().find(ops.Filter)
    (predicate,) = filt.predicates
    assert isinstance(predicate, ops.Greater)


def test_comma_join_conditions_stay_filters_before_a_right_join():
    sql = "SELECT t.a FROM t, u RIGHT JOIN v ON u.a = v.a WHERE t.a = u.a"
    expr = ibis.parse_sql(sql, tables)
    (chain,) = expr.op().find(ops.JoinChain)
    first, second = chain.rest
    assert first.how == "cross"
    assert not first.predicates
    assert second.how == "right"
    (filt,) = expr.op().find(ops.Filter)
    (predicate,) = filt.predicates
    assert isinstance(predicate, ops.Equals)


def test_dropped_join_key_resolves_after_a_filter():
    sql = "SELECT u.a, u.x FROM t JOIN u ON t.a = u.a WHERE t.c > 1"
    expr = ibis.parse_sql(sql, tables)
    assert expr.columns == ("a", "x")
    assert "WHERE" in ibis.to_sql(expr)


def test_second_scan_of_a_table_is_a_view():
    expr = ibis.parse_sql(
        "SELECT t1.a FROM t AS t1 JOIN t AS t2 ON t1.a = t2.a", tables
    )
    assert expr.op().find(ops.SelfReference)


def test_correlated_scalar_subquery_over_a_derived_table_is_anchored():
    derived = "FROM (SELECT a, c FROM t WHERE a > 1) AS s"
    scalar = ibis.parse_sql(
        f"SELECT s.a {derived} WHERE s.c > (SELECT AVG(u.a) FROM u WHERE u.a = s.a)",
        tables,
    )
    # the derived table keeps its alias in the compiled SQL
    assert scalar.op().find(ops.SelfReference)
    exists = ibis.parse_sql(
        f"SELECT s.a {derived} WHERE EXISTS (SELECT 1 FROM u WHERE u.a = s.a)", tables
    )
    assert not exists.op().find(ops.SelfReference)


def test_duplicate_output_names_are_suffixed():
    expr = ibis.parse_sql("SELECT a, a FROM t", tables)
    assert expr.columns == ("a", "a_1")


def test_identity_projection_is_elided():
    expr = ibis.parse_sql("SELECT * FROM t WHERE a > 1", tables)
    assert isinstance(expr.op(), ops.Filter)


def test_join_projection_with_a_window_is_not_the_chain_output():
    joined = "FROM t JOIN u ON t.a = u.a"
    plain = ibis.parse_sql(f"SELECT t.a, u.x {joined}", tables)
    assert isinstance(plain.op(), ops.JoinChain)
    window = ibis.parse_sql(
        f"SELECT t.a, ROW_NUMBER() OVER (ORDER BY t.a) AS rn {joined}", tables
    )
    assert isinstance(window.op(), ops.Project)


def test_column_outside_group_by_is_rejected():
    with pytest.raises(com.IbisInputError, match="GROUP BY"):
        ibis.parse_sql("SELECT a, b FROM t GROUP BY a", tables)


def test_concat_null_handling_follows_the_dialect():
    sql = "SELECT CONCAT(b, 'x') AS bx FROM t"
    # CONCAT skips NULL arguments in DuckDB and propagates them in MySQL
    assert "COALESCE" in ibis.to_sql(ibis.parse_sql(sql, tables, dialect="duckdb"))
    assert "COALESCE" not in ibis.to_sql(ibis.parse_sql(sql, tables, dialect="mysql"))


def test_from_less_select_keeps_its_order_by():
    expr = ibis.parse_sql("SELECT 2 AS two, 1 AS one ORDER BY one", tables)
    assert isinstance(expr.op(), ops.Sort)


@pytest.mark.parametrize(
    "literal", ["1.5", "0.1", "100.0000", "-0.333", "1e2", "1.5E-3"]
)
@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "clickhouse"])
def test_non_integer_literal_is_float64(literal, dialect):
    expr = ibis.parse_sql(f"SELECT a * {literal} AS q FROM t", tables, dialect=dialect)
    assert expr.schema() == ibis.schema({"q": "float64"})
    assert "DECIMAL" not in ibis.to_sql(expr, dialect="duckdb").upper()


@pytest.mark.parametrize(
    "value",
    [
        "CASE WHEN a > 1 THEN c / 3 ELSE e END",
        "CASE a WHEN 1 THEN e ELSE c / 3 END",
        "IF(a > 1, e, c / 3)",
        "COALESCE(e, c / 3)",
        "NULLIF(e, c)",
        "GREATEST(e, c / 3)",
        "LEAST(c / 3, e)",
        "e * c",
    ],
)
@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "clickhouse"])
def test_float_and_decimal_operands_are_float64(value, dialect):
    tables = {"t": {"a": "int64", "c": "float64", "e": "decimal(3, 2)"}}
    expr = ibis.parse_sql(f"SELECT {value} AS x FROM t", tables, dialect=dialect)
    assert expr.schema() == ibis.schema({expr.columns[0]: "float64"})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c AS x FROM t UNION ALL SELECT e FROM t",
        "SELECT e AS x FROM t UNION ALL SELECT c FROM t",
        "SELECT e AS x FROM t INTERSECT SELECT c FROM t",
    ],
)
def test_set_operation_with_float_and_decimal_is_float64(sql):
    tables = {"t": {"c": "float64", "e": "decimal(3, 2)"}}
    expr = ibis.parse_sql(sql, tables, dialect="postgres")
    assert expr.schema() == ibis.schema({"x": "float64"})
    assert "DECIMAL" not in ibis.to_sql(expr, dialect="postgres").upper()


@pytest.mark.parametrize("dialect", [None, "duckdb", "postgres"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM T",
        "SELECT t.a FROM T AS t",
        "WITH X AS (SELECT a FROM t) SELECT a FROM x",
        "WITH x AS (SELECT a FROM t) SELECT a FROM X",
    ],
    ids=["table", "aliased_table", "cte_name", "cte_reference"],
)
def test_unquoted_names_are_compared_as_the_dialect_compares_them(sql, dialect):
    expr = ibis.parse_sql(sql, tables, dialect=dialect)
    assert expr.columns == ("a",)
    assert 'FROM "t" AS "t0"' in ibis.to_sql(expr, dialect="postgres", pretty=False)


@pytest.mark.parametrize(("dialect", "as_"), [("snowflake", "AS "), ("oracle", "")])
def test_uppercasing_dialect_resolves_a_lowercase_catalog(dialect, as_):
    expr = ibis.parse_sql("SELECT a, b FROM t WHERE a > 1", tables, dialect=dialect)
    # these engines report the unquoted names upper-cased
    assert expr.columns == ("A", "B")
    assert ibis.to_sql(expr, dialect=dialect, pretty=False) == (
        f'SELECT "t0"."a" AS "A", "t0"."b" AS "B" FROM "t" {as_}"t0" WHERE "t0"."a" > 1'
    )


def test_uppercasing_dialect_resolves_an_uppercase_catalog():
    catalog = {"T": {"A": "int64"}}
    expr = ibis.parse_sql("SELECT a FROM t", catalog, dialect="snowflake")
    assert expr.columns == ("A",)
    # the projection selects the whole table
    assert ibis.to_sql(expr, dialect="snowflake", pretty=False) == 'SELECT * FROM "T"'
    expr = ibis.parse_sql("SELECT a + 1 AS b FROM t", catalog, dialect="snowflake")
    sql = ibis.to_sql(expr, dialect="snowflake", pretty=False)
    assert sql == 'SELECT "t0"."A" + 1 AS "B" FROM "T" AS "t0"'


def test_postgres_resolves_an_uppercase_table_name():
    expr = ibis.parse_sql("SELECT A, b FROM T", tables, dialect="postgres")
    assert expr.columns == ("a", "b")
    sql = ibis.to_sql(expr, dialect="postgres", pretty=False)
    assert sql == 'SELECT "t0"."a", "t0"."b" FROM "t" AS "t0"'


@pytest.mark.parametrize(
    ("sql", "dialect", "table"),
    [
        ("SELECT a FROM nope", "duckdb", "nope"),
        ("SELECT a FROM nope", "snowflake", "nope"),
        # a quoted name is compared exactly, except by case-insensitive DuckDB
        ('SELECT a FROM "T"', "postgres", "T"),
        # ClickHouse compares identifiers case-sensitively
        ("SELECT a FROM T", "clickhouse", "T"),
        ("WITH x AS (SELECT a FROM t) SELECT a FROM y", "duckdb", "y"),
    ],
)
def test_unknown_table_is_named(sql, dialect, table):
    with pytest.raises(com.IbisInputError, match=f"Table '{table}' is not in"):
        ibis.parse_sql(sql, tables, dialect=dialect)


@pytest.mark.parametrize("dialect", [None, "duckdb"])
def test_cast_to_another_parameterization_is_kept(dialect):
    catalog = {"t": {"c": "decimal(10, 2)", "ts": "timestamp('UTC')"}}
    sql = (
        "SELECT CAST(c AS DECIMAL(18, 3)) AS x, CAST(ts AS TIMESTAMP) AS n, "
        "CAST(c AS DECIMAL(10, 2)) AS y FROM t"
    )
    expr = ibis.parse_sql(sql, catalog, dialect=dialect)
    assert expr.schema() == ibis.schema(
        {"x": "decimal(18, 3)", "n": "timestamp", "y": "decimal(10, 2)"}
    )
    # the cast to the column's own type is redundant
    assert ibis.to_sql(expr, dialect="duckdb", pretty=False) == (
        'SELECT CAST("t0"."c" AS DECIMAL(18, 3)) AS "x", '
        'CAST("t0"."ts" AS TIMESTAMP) AS "n", "t0"."c" AS "y" FROM "t" AS "t0"'
    )


def test_catalog_takes_a_table_expression():
    other = ibis.table({"a": "int64"}, name="other")
    expr = ibis.parse_sql("SELECT a FROM t WHERE a > 1", {"t": other})
    assert expr.schema() == ibis.schema({"a": "int64"})
    # the table is named by its catalog key
    sql = ibis.to_sql(expr, dialect="duckdb", pretty=False)
    assert sql == 'SELECT * FROM "t" AS "t0" WHERE "t0"."a" > 1'


@pytest.mark.parametrize(
    "condition", ["b IS NOT NULL", "b NOTNULL", "(a > 1) IS NOT TRUE"]
)
def test_negated_is_follows_the_dialect(condition):
    # sqlglot parses ``IS NOT`` as ``Is(negate=True)`` for postgres from 30.14
    # and as ``Not(Is)`` for duckdb
    sql = f"SELECT a FROM t WHERE {condition}"
    expected = ibis.parse_sql(sql.replace("NOTNULL", "IS NOT NULL"), tables)
    result = ibis.parse_sql(sql, tables, dialect="postgres")
    assert ibis.to_sql(result) == ibis.to_sql(expected)


@pytest.mark.parametrize("kind", ["SEMI", "ANTI"])
def test_right_semi_and_anti_joins_are_rejected(kind):
    # a ClickHouse RIGHT SEMI join keeps the rows of the right table, not the left
    sql = f"SELECT t.a FROM t RIGHT {kind} JOIN u ON t.a = u.a"
    with pytest.raises(com.UnsupportedOperationError, match=kind):
        ibis.parse_sql(sql, tables, dialect="clickhouse")
    # LEFT is the side of a semi or anti join that Ibis has
    left = ibis.parse_sql(sql.replace("RIGHT", "LEFT"), tables, dialect="clickhouse")
    assert left.columns == ("a",)


def test_typed_division_of_integers_is_integer_division():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (7, 7.0), (-7, -7.0)) v(a, c)")
    # Postgres's ``/`` divides integers to an integer truncated toward zero
    sql = "SELECT a / 2 AS h, 7 / 2 AS k, c / 2 AS f FROM t"
    expr = ibis.parse_sql(sql, tables, dialect="postgres")
    assert expr.schema()["h"].is_integer()
    assert expr.schema()["k"].is_integer()
    assert expr.schema()["f"].is_floating()
    result = con.execute(ibis.to_sql(expr, dialect="duckdb")).fetchall()
    assert sorted(result) == [(-3, 3, -3.5), (3, 3, 3.5)]
    # DuckDB's ``/`` is true division
    expr = ibis.parse_sql(sql, tables, dialect="duckdb")
    assert expr.schema()["h"].is_floating()


def test_round_with_truncation_is_rejected():
    with pytest.raises(com.UnsupportedOperationError, match="ROUND"):
        ibis.parse_sql("SELECT ROUND(c, 1, 1) AS r FROM t", tables, dialect="tsql")
    # a zero third argument rounds
    expr = ibis.parse_sql("SELECT ROUND(c, 1, 0) AS r FROM t", tables, dialect="tsql")
    assert isinstance(expr.op().values["r"], ops.Round)


def test_scalar_max_of_two_arguments_is_rejected():
    # SQLite's MAX of two arguments is the greater of them, not an aggregate
    with pytest.raises(com.UnsupportedOperationError, match="MAX"):
        ibis.parse_sql("SELECT MAX(a, c) AS m FROM t", tables, dialect="sqlite")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.*, u.x FROM t JOIN u ON t.a = u.a",
        "SELECT t.a, t.b FROM t SEMI JOIN u ON t.a = u.a",
    ],
)
def test_parse_sql_join_identity_is_table(sql):
    """A SELECT list that is the join's output gives a table, not a join."""
    catalog = {"t": {"a": "int64", "b": "string"}, "u": {"a": "int64", "x": "string"}}
    expr = ibis.parse_sql(sql, catalog, dialect="duckdb")
    assert not isinstance(expr, ir.Join)
    assert isinstance(expr, ir.Table)
