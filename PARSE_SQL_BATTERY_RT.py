"""Try to break parse_sql: run SQL on DuckDB and via parse_sql, compare STRICTLY."""
import sys, math, datetime, decimal, traceback
import duckdb, pandas as pd, ibis

CATALOG = {
    "t": {"a": "int64", "b": "string", "c": "float64", "d": "date", "ts": "timestamp"},
    "u": {"a": "int64", "x": "string"},
    # first column nullable, to catch COUNT(*) -> COUNT(first col)
    "n": {"k": "string", "v": "int64"},
    "dup": {"id": "int64", "name": "string"},
    "dup2": {"id": "int64", "name": "string"},
}
con = duckdb.connect()
con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
  (1, 'foo', 1.5, DATE '2024-01-01', TIMESTAMP '2024-01-01 10:00:00'),
  (2, 'bar', 2.5, DATE '2024-02-01', TIMESTAMP '2024-02-02 11:00:00'),
  (3, NULL, NULL, DATE '2024-03-01', TIMESTAMP '2024-03-03 12:00:00'),
  (2, 'baz', 4.0, DATE '2024-04-01', TIMESTAMP '2024-04-04 13:00:00'),
  (NULL, 'nul', 0.5, DATE '2024-05-01', TIMESTAMP '2024-05-05 14:00:00')) v(a, b, c, d, ts)""")
con.execute("CREATE TABLE u AS SELECT * FROM (VALUES (1, 'p'), (2, 'q'), (9, 'r'), (NULL, 's')) v(a, x)")
con.execute("CREATE TABLE n AS SELECT * FROM (VALUES (NULL, 1), ('a', 2), (NULL, 3)) v(k, v)")
con.execute("CREATE TABLE dup AS SELECT * FROM (VALUES (1, 'one'), (2, 'two')) v(id, name)")
con.execute("CREATE TABLE dup2 AS SELECT * FROM (VALUES (1, 'uno'), (3, 'tres')) v(id, name)")
ib = ibis.duckdb.from_connection(con)

def norm(v):
    if v is None or v is pd.NA or v is pd.NaT: return None
    if isinstance(v, (bool,)): return v
    if isinstance(v, (int,)) and not isinstance(v, bool): return float(v)
    if isinstance(v, float) and math.isnan(v): return None
    if isinstance(v, decimal.Decimal): return float(v)
    if isinstance(v, (pd.Timestamp, datetime.datetime, datetime.date)): return str(v)[:19]
    if hasattr(v, "item"): v = v.item()
    if isinstance(v, float) and math.isnan(v): return None
    if isinstance(v, int) and not isinstance(v, bool): return float(v)
    if isinstance(v, datetime.timedelta): return v.days
    return v

def rows(df):
    out = []
    for r in df.itertuples(index=False):
        out.append(tuple(norm(x) for x in r))
    return sorted(out, key=lambda r: tuple((x is None, str(x) if not isinstance(x, (int, float)) else "", x if isinstance(x, (int, float)) else 0) for x in r))

def close(a, b):
    if a is None or b is None: return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        try: return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
        except Exception: return False
    return a == b

def same(r1, r2):
    return len(r1) == len(r2) and all(len(x) == len(y) and all(close(p, q) for p, q in zip(x, y)) for x, y in zip(r1, r2))

CASES = dict(
    count_star_nullable_first_col="SELECT COUNT(*) AS n FROM n",
    count_star_group="SELECT v % 2 AS p, COUNT(*) AS n FROM n GROUP BY v % 2",
    count_col="SELECT COUNT(k) AS n FROM n",
    count_star_where="SELECT COUNT(*) AS n FROM n WHERE v > 1",
    not_in_values_null="SELECT a FROM t WHERE a NOT IN (1, 2)",
    not_in_subquery_with_null="SELECT a FROM t WHERE a NOT IN (SELECT a FROM u)",
    in_subquery_with_null="SELECT a FROM t WHERE a IN (SELECT a FROM u)",
    having_no_group="SELECT SUM(c) AS s FROM t HAVING SUM(c) > 1",
    having_no_group_false="SELECT SUM(c) AS s FROM t HAVING SUM(c) > 1000",
    distinct_on="SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b",
    distinct_multi="SELECT DISTINCT a, b FROM t",
    limit_no_order="SELECT COUNT(*) AS n FROM (SELECT a FROM t LIMIT 2) s",
    join_using="SELECT a, b, x FROM t JOIN u USING (a)",
    join_using_left="SELECT a, x FROM t LEFT JOIN u USING (a)",
    natural_join="SELECT a, b, x FROM t NATURAL JOIN u",
    subquery_in_on="SELECT t.a, u.x FROM t JOIN u ON t.a = u.a AND u.a IN (SELECT a FROM u WHERE a < 5)",
    scalar_subquery_in_select="SELECT a, (SELECT MAX(a) FROM u) AS m FROM t",
    correlated_in_select="SELECT a, (SELECT COUNT(*) FROM u WHERE u.a = t.a) AS cnt FROM t",
    correlated_in_select_sum="SELECT t.a, (SELECT SUM(u.a) FROM u WHERE u.a <= t.a) AS s FROM t",
    nested_cte="WITH x AS (SELECT a, c FROM t), y AS (SELECT a, c * 2 AS c2 FROM x) SELECT a, SUM(c2) AS s FROM y GROUP BY a",
    cte_chain_three="WITH x AS (SELECT a FROM t), y AS (SELECT a FROM x WHERE a > 1), z AS (SELECT a FROM y UNION ALL SELECT a FROM x) SELECT a, COUNT(*) AS n FROM z GROUP BY a",
    dup_col_names="SELECT dup.id, dup2.id FROM dup JOIN dup2 ON dup.id = dup2.id",
    dup_col_names_outer="SELECT dup.id, dup2.id, dup.name, dup2.name FROM dup LEFT JOIN dup2 ON dup.id = dup2.id",
    full_outer="SELECT dup.id, dup2.id FROM dup FULL OUTER JOIN dup2 ON dup.id = dup2.id",
    full_outer_coalesce="SELECT COALESCE(dup.id, dup2.id) AS id FROM dup FULL OUTER JOIN dup2 ON dup.id = dup2.id",
    right_join="SELECT dup.id, dup2.id FROM dup RIGHT JOIN dup2 ON dup.id = dup2.id",
    mixed_setops="SELECT a FROM t UNION SELECT a FROM u EXCEPT SELECT a FROM u WHERE a = 9",
    setop_precedence="SELECT a FROM t INTERSECT SELECT a FROM u UNION ALL SELECT 9",
    union_distinct_then_all="SELECT a FROM t UNION ALL (SELECT a FROM u UNION SELECT a FROM u)",
    union_type_coerce="SELECT a FROM t UNION ALL SELECT c FROM t",
    window_frame_rows="SELECT a, SUM(c) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND 1 FOLLOWING) AS s FROM t",
    window_frame_range_following="SELECT a, SUM(c) OVER (ORDER BY a RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS s FROM t",
    window_no_order_agg="SELECT a, SUM(c) OVER (PARTITION BY b) AS s FROM t",
    window_order_no_frame_max="SELECT a, MAX(c) OVER (ORDER BY a) AS m FROM t",
    window_count_star="SELECT a, COUNT(*) OVER (PARTITION BY a) AS n FROM t",
    window_in_where_subquery="SELECT a FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t) s WHERE rn <= 2",
    window_then_filter_then_window="SELECT a, rn, ROW_NUMBER() OVER (ORDER BY a DESC) AS rn2 FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t) s WHERE rn > 1",
    window_avg_decimal_free="SELECT a, AVG(a) OVER (ORDER BY a) AS av FROM t",
    window_lag_default="SELECT a, LAG(a, 1, -1) OVER (ORDER BY a) AS p FROM t",
    window_lead_2="SELECT a, LEAD(a, 2) OVER (ORDER BY a) AS p FROM t",
    window_nth="SELECT a, NTH_VALUE(a, 2) OVER (ORDER BY a) AS p FROM t",
    window_last_value="SELECT a, LAST_VALUE(a) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS p FROM t",
    window_rank_ties="SELECT a, RANK() OVER (ORDER BY a) AS r, DENSE_RANK() OVER (ORDER BY a) AS d, PERCENT_RANK() OVER (ORDER BY a) AS pr, CUME_DIST() OVER (ORDER BY a) AS cd FROM t",
    window_ntile3="SELECT a, NTILE(3) OVER (ORDER BY a) AS n FROM t",
    order_by_expr="SELECT a, b FROM t ORDER BY a * -1, b",
    order_by_ordinal="SELECT a, b FROM t ORDER BY 2 DESC, 1",
    order_by_alias_expr="SELECT a * 2 AS dbl FROM t ORDER BY dbl DESC",
    order_by_dropped="SELECT b FROM t ORDER BY a",
    order_by_nulls_first="SELECT a FROM t ORDER BY a NULLS FIRST",
    order_by_agg_not_in_select="SELECT b FROM t GROUP BY b ORDER BY SUM(c) DESC",
    group_by_ordinal="SELECT b, COUNT(*) AS n FROM t GROUP BY 1",
    group_by_alias="SELECT a % 2 AS p, COUNT(*) AS n FROM t GROUP BY p",
    group_by_null_key="SELECT a, COUNT(*) AS n FROM t GROUP BY a",
    having_alias="SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING n > 1",
    having_complex="SELECT a, SUM(c) AS s FROM t GROUP BY a HAVING SUM(c) > 1 AND COUNT(*) >= 1",
    agg_distinct_sum="SELECT SUM(DISTINCT a) AS s FROM t",
    agg_avg_int="SELECT AVG(a) AS m FROM t",
    agg_filter="SELECT SUM(c) FILTER (WHERE a > 1) AS s FROM t",
    agg_count_distinct_two="SELECT COUNT(DISTINCT a) AS na, COUNT(DISTINCT b) AS nb FROM t",
    agg_min_max_string="SELECT MIN(b) AS lo, MAX(b) AS hi FROM t",
    agg_bool="SELECT BOOL_OR(a > 2) AS o, BOOL_AND(a > 0) AS an FROM t",
    agg_string_agg="SELECT STRING_AGG(b, ',' ORDER BY b) AS s FROM t",
    agg_in_expression="SELECT SUM(c) / COUNT(*) AS avg FROM t",
    agg_nested_case="SELECT SUM(CASE WHEN a > 1 THEN c ELSE 0 END) AS s FROM t",
    case_searched_null="SELECT CASE WHEN b IS NULL THEN 'n' WHEN a > 2 THEN 'big' END AS k FROM t",
    case_simple="SELECT CASE a WHEN 1 THEN 'one' WHEN 2 THEN 'two' ELSE 'many' END AS k FROM t",
    case_simple_null="SELECT CASE b WHEN NULL THEN 'n' ELSE 'x' END AS k FROM t",
    is_not_null="SELECT a FROM t WHERE b IS NOT NULL",
    is_distinct_from="SELECT a FROM t WHERE b IS DISTINCT FROM 'foo'",
    is_not_distinct_from="SELECT a FROM t WHERE b IS NOT DISTINCT FROM NULL",
    is_true="SELECT a FROM t WHERE (a > 1) IS TRUE",
    not_like="SELECT a FROM t WHERE b NOT LIKE 'ba%'",
    ilike="SELECT a FROM t WHERE b ILIKE 'BA%'",
    not_between="SELECT a FROM t WHERE a NOT BETWEEN 1 AND 2",
    not_in_with_null_list="SELECT a FROM t WHERE a NOT IN (1, NULL)",
    in_with_null_list="SELECT a FROM t WHERE a IN (1, NULL)",
    null_compare="SELECT a FROM t WHERE b = NULL",
    null_arith="SELECT a + NULL AS x FROM t",
    bool_literal="SELECT a FROM t WHERE TRUE",
    bool_false="SELECT a FROM t WHERE FALSE",
    where_and_or_prec="SELECT a FROM t WHERE a = 1 OR a = 2 AND b = 'baz'",
    where_not_and="SELECT a FROM t WHERE NOT (a = 1 AND b = 'foo')",
    int_div="SELECT a / 2 AS h, a // 2 AS i, a % 2 AS m FROM t",
    int_div_neg="SELECT -a // 2 AS i, -a % 2 AS m FROM t",
    power="SELECT POWER(a, 2) AS p, a ^ 2 AS p2 FROM t",
    string_concat_null="SELECT b || 'x' AS c1, CONCAT(b, 'x') AS c2, CONCAT(b, NULL, 'y') AS c3 FROM t",
    substring_no_len="SELECT SUBSTRING(b, 2) AS s FROM t",
    substring_zero="SELECT SUBSTRING(b, 0, 2) AS s FROM t",
    substring_neg="SELECT SUBSTRING(b, -1, 2) AS s FROM t",
    substr_func="SELECT SUBSTR(b, 1, 1) AS s FROM t",
    string_funcs2="SELECT TRIM(' x '), LTRIM(' x '), RTRIM(' x '), REVERSE(b), REPLACE(b, 'a', 'o'), LEFT(b, 1), RIGHT(b, 1), REPEAT(b, 2), LPAD(b, 5, '*'), STRPOS(b, 'a'), INITCAP(b), CONTAINS(b, 'a'), STARTS_WITH(b, 'b') FROM t",
    string_position="SELECT POSITION('a' IN b) AS p FROM t",
    string_split="SELECT SPLIT_PART(b, 'a', 1) AS s FROM t",
    regex="SELECT a FROM t WHERE REGEXP_MATCHES(b, '^b')",
    cast_decimal="SELECT CAST(c AS DECIMAL(10, 1)) AS d FROM t",
    cast_int_from_float="SELECT CAST(c AS INTEGER) AS i FROM t",
    cast_str_to_int="SELECT CAST('12' AS INTEGER) AS i",
    cast_colon="SELECT c::INTEGER AS i FROM t",
    try_cast="SELECT TRY_CAST(b AS INTEGER) AS i FROM t",
    date_parts="SELECT EXTRACT(month FROM d) AS m, EXTRACT(day FROM d) AS dd, EXTRACT(dow FROM d) AS dow, EXTRACT(doy FROM d) AS doy, EXTRACT(epoch FROM ts) AS ep FROM t",
    date_part_func="SELECT DATE_PART('year', d) AS y, YEAR(d) AS y2, MONTH(d) AS m, DAYOFWEEK(d) AS dw FROM t",
    date_diff="SELECT DATE_DIFF('day', d, DATE '2024-06-01') AS dd, DATEDIFF('month', d, DATE '2024-06-01') AS md FROM t",
    date_sub_days="SELECT d - INTERVAL '10' DAY AS d2, d + INTERVAL 1 YEAR AS d3 FROM t",
    date_minus_date="SELECT DATE '2024-06-01' - d AS diff FROM t",
    date_trunc_week="SELECT DATE_TRUNC('week', d) AS w, DATE_TRUNC('quarter', ts) AS q FROM t",
    timestamp_cast="SELECT CAST(d AS TIMESTAMP) AS ts2, CAST(ts AS DATE) AS d2 FROM t",
    current_date_cmp="SELECT a FROM t WHERE d < CURRENT_DATE",
    interval_mult="SELECT d + INTERVAL '1' MONTH * 2 AS d2 FROM t",
    date_literal_cmp="SELECT a FROM t WHERE d BETWEEN '2024-02-01' AND '2024-03-31'",
    exists_uncorrelated="SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a > 100)",
    exists_uncorrelated_true="SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u)",
    exists_two_conditions="SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a AND u.x = 'p')",
    exists_with_agg="SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a GROUP BY u.a HAVING COUNT(*) > 0)",
    exists_nested_two_levels="SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a AND EXISTS (SELECT 1 FROM n WHERE n.v = u.a))",
    exists_or="SELECT a FROM t WHERE a = 3 OR EXISTS (SELECT 1 FROM u WHERE u.a = t.a)",
    exists_in_select="SELECT a, EXISTS (SELECT 1 FROM u WHERE u.a = t.a) AS e FROM t",
    exists_self="SELECT a FROM t AS t1 WHERE EXISTS (SELECT 1 FROM t AS t2 WHERE t2.a = t1.a AND t2.b <> t1.b)",
    exists_three_tables="SELECT t.a FROM t JOIN u ON t.a = u.a WHERE EXISTS (SELECT 1 FROM n WHERE n.v = t.a AND n.v = u.a)",
    not_exists_correlated_on_two="SELECT t.a, u.x FROM t JOIN u ON t.a = u.a WHERE NOT EXISTS (SELECT 1 FROM n WHERE n.v = u.a)",
    scalar_sub_correlated_select_and_where="SELECT a, (SELECT MIN(u.a) FROM u WHERE u.a >= t.a) AS m FROM t WHERE a > (SELECT AVG(a) FROM u)",
    scalar_sub_in_case="SELECT a, CASE WHEN a > (SELECT AVG(a) FROM u) THEN 'hi' ELSE 'lo' END AS k FROM t",
    scalar_sub_arith="SELECT a - (SELECT MIN(a) FROM u) AS diff FROM t",
    scalar_sub_both_sides="SELECT a FROM t WHERE (SELECT MIN(a) FROM u) < (SELECT MAX(a) FROM u) AND a > 1",
    in_subquery_correlated="SELECT a FROM t WHERE b IN (SELECT x FROM u WHERE u.a = t.a)",
    in_subquery_expr="SELECT a FROM t WHERE a + 1 IN (SELECT a FROM u)",
    in_subquery_with_distinct="SELECT a FROM t WHERE a IN (SELECT DISTINCT a FROM u)",
    any_subquery="SELECT a FROM t WHERE a = ANY (SELECT a FROM u)",
    all_subquery="SELECT a FROM t WHERE a > ALL (SELECT a FROM u WHERE a < 3)",
    derived_with_agg="SELECT s.b, s.n FROM (SELECT b, COUNT(*) AS n FROM t GROUP BY b) s WHERE s.n > 0",
    derived_nested_twice="SELECT z.a FROM (SELECT y.a FROM (SELECT a FROM t WHERE a > 1) y WHERE y.a < 3) z",
    derived_nested_alias_ref="SELECT z.a2 FROM (SELECT y.a * 2 AS a2 FROM (SELECT a FROM t WHERE a > 1) y) z",
    derived_join_two="SELECT s1.a, s2.x FROM (SELECT a FROM t) s1 JOIN (SELECT a, x FROM u) s2 ON s1.a = s2.a",
    derived_join_agg="SELECT t.a, s.n FROM t JOIN (SELECT a, COUNT(*) AS n FROM u GROUP BY a) s ON t.a = s.a",
    derived_limit_inner="SELECT s.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) s ORDER BY s.a",
    derived_distinct="SELECT COUNT(*) AS n FROM (SELECT DISTINCT a FROM t) s",
    derived_window="SELECT s.a FROM (SELECT a, RANK() OVER (ORDER BY c) AS r FROM t) s WHERE s.r = 1",
    cte_self_join="WITH x AS (SELECT a, b FROM t) SELECT x1.a, x2.b FROM x x1 JOIN x x2 ON x1.a = x2.a + 1",
    cte_used_in_subquery="WITH x AS (SELECT a FROM u) SELECT a FROM t WHERE a IN (SELECT a FROM x)",
    cte_used_in_exists="WITH x AS (SELECT a FROM u) SELECT a FROM t WHERE EXISTS (SELECT 1 FROM x WHERE x.a = t.a)",
    cte_with_agg_join="WITH agg AS (SELECT a, SUM(c) AS s FROM t GROUP BY a) SELECT t.a, agg.s FROM t JOIN agg ON t.a = agg.a",
    cte_shadow_table="WITH t AS (SELECT a FROM u) SELECT a FROM t",
    cte_window="WITH w AS (SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t) SELECT a FROM w WHERE rn = 1",
    three_way_join="SELECT t.a, u.x, n.v FROM t JOIN u ON t.a = u.a JOIN n ON n.v = u.a",
    join_filter_right_after="SELECT t.a FROM t LEFT JOIN u ON t.a = u.a WHERE u.a > 1",
    join_filter_in_on_left="SELECT t.a, u.x FROM t LEFT JOIN u ON t.a = u.a AND u.x = 'p'",
    join_filter_in_on_inner_nonequi="SELECT t.a, u.a FROM t JOIN u ON t.a < u.a",
    join_or_predicate="SELECT t.a, u.a FROM t JOIN u ON t.a = u.a OR t.a = u.a + 1",
    join_expr_key="SELECT t.a, u.a FROM t JOIN u ON t.a + 1 = u.a",
    join_comma_where="SELECT t.a, u.x FROM t, u WHERE t.a = u.a",
    join_comma_no_where="SELECT COUNT(*) AS n FROM t, u",
    join_same_table_twice_inner="SELECT t1.a, t2.a, t3.a FROM t t1 JOIN t t2 ON t1.a = t2.a JOIN t t3 ON t2.a = t3.a",
    join_self_left="SELECT t1.a, t2.b FROM t t1 LEFT JOIN t t2 ON t1.a = t2.a + 1",
    join_null_keys="SELECT t.a, u.a FROM t JOIN u ON t.a IS NOT DISTINCT FROM u.a",
    join_select_star="SELECT * FROM t JOIN u ON t.a = u.a",
    join_select_star_dup="SELECT * FROM dup JOIN dup2 ON dup.id = dup2.id",
    semi_join_in="SELECT t.a FROM t WHERE t.a IN (SELECT u.a FROM u WHERE u.x <> 's')",
    anti_join_not_in="SELECT t.a FROM t WHERE t.a NOT IN (SELECT u.a FROM u WHERE u.a IS NOT NULL)",
    group_by_two_order_by_count="SELECT a, b, COUNT(*) AS n FROM t GROUP BY a, b ORDER BY n DESC, a, b",
    group_by_all_then_window="SELECT a, SUM(c) AS s, RANK() OVER (ORDER BY SUM(c) DESC) AS r FROM t GROUP BY a",
    agg_over_agg_partition="SELECT b, SUM(c) AS s, SUM(SUM(c)) OVER (PARTITION BY a) AS ps, a FROM t GROUP BY a, b",
    window_agg_same_alias_as_col="SELECT a, SUM(a) OVER () AS a2 FROM t",
    limit_zero="SELECT a FROM t LIMIT 0",
    limit_in_union_branch="(SELECT a FROM t ORDER BY a LIMIT 1) UNION ALL (SELECT a FROM u ORDER BY a DESC LIMIT 1)",
    select_literal_only="SELECT 1 + 1 AS two",
    select_literal_types="SELECT 1.5 AS f, 'x' AS s, TRUE AS b, NULL AS nl, DATE '2020-01-01' AS d",
    select_star_order="SELECT * FROM t ORDER BY a DESC NULLS LAST, b",
    select_expr_reuse="SELECT a + 1 AS a1, (a + 1) * 2 AS a2 FROM t",
    select_alias_shadow_col="SELECT a AS b, b AS a FROM t",
    select_alias_same_as_col="SELECT a + 1 AS a FROM t WHERE a > 1",
    select_column_twice="SELECT a, a FROM t",
    select_column_twice_agg="SELECT a, COUNT(*) AS a FROM t GROUP BY a",
    where_on_alias_in_derived="SELECT s.z FROM (SELECT a + c AS z FROM t) s WHERE s.z > 3",
    quoted_identifiers='SELECT "a", "b" FROM "t" WHERE "a" > 1',
    mixed_case_alias='SELECT a AS "MyCol" FROM t',
    coalesce_three="SELECT COALESCE(NULL, b, 'z') AS s FROM t",
    nullif="SELECT NULLIF(a, 2) AS x FROM t",
    ifnull="SELECT IFNULL(b, 'z') AS s FROM t",
    greatest_least_null="SELECT GREATEST(a, 2, NULL) AS g, LEAST(c, 1.0) AS l FROM t",
    if_func="SELECT IF(a > 1, 'y', 'n') AS k FROM t",
    round_half="SELECT ROUND(2.5) AS r1, ROUND(3.5) AS r2, ROUND(c, 0) AS r3 FROM t",
    floor_ceil_neg="SELECT FLOOR(-c) AS f, CEIL(-c) AS c2 FROM t",
    sqrt_ln_exp="SELECT SQRT(c) AS s, LN(c) AS l, EXP(1) AS e, LOG(100) AS lg FROM t",
    sum_decimal_literal="SELECT SUM(c * 0.1) AS s FROM t",
    avg_of_cast_decimal="SELECT AVG(CAST(c AS DECIMAL(10,2))) AS m FROM t",
    avg_group_decimal="SELECT a, AVG(CAST(c AS DECIMAL(10,2))) AS m FROM t GROUP BY a",
)

ok = mism = err = 0
for name, sql in CASES.items():
    try:
        expected = con.execute(sql).df()
    except Exception as e:
        print(f"SKIP   {name}: duckdb rejects: {e.__class__.__name__}: {str(e)[:80]}")
        continue
    try:
        expr = ibis.parse_sql(sql, CATALOG, dialect="duckdb"); code = ibis.decompile(expr); ns = {}; exec(code, {}, ns); expr = ns["result"]
        result = ib.to_pandas(expr)
    except Exception as e:
        err += 1
        msg = str(e).replace("\n", " ")[:140]
        print(f"ERROR  {name}: {e.__class__.__name__}: {msg}")
        continue
    r1, r2 = rows(result), rows(expected)
    if same(r1, r2):
        ok += 1
        print(f"ok     {name}")
    else:
        mism += 1
        print(f"WRONG  {name}\n   sql:      {sql}\n   expected: {r2[:6]}\n   got:      {r1[:6]}\n   cols exp {list(expected.columns)} got {list(result.columns)}")
print(f"\nok={ok} wrong={mism} error={err}")
