# parse_sql prototype: notes toward a PR

Working notes kept on the branch so they travel with the code. Drop this file
(or fold it into the PR description) before opening the PR.

## One-paragraph summary

`ibis.parse_sql` was a 2022 prototype that handled a handful of SQL shapes and
silently mis-translated several common ones (DISTINCT, HAVING, OFFSET,
COUNT(DISTINCT)). This branch keeps its architecture (sqlglot optimizer ->
sqlglot planner -> Ibis) and fills in the converter so that every TPC-H query
and every TPC-DS query Ibis can express parses into an expression that returns
the same rows as DuckDB running the raw SQL. Correctness is gated by tests that
execute the parsed expression, not by decompiler snapshots.

## Scoreboard (rows compared against DuckDB)

| suite | before | after |
|---|---|---|
| TPC-H (22) | 5 | 22 |
| TPC-DS (99) | 34 (first measurement) | 90; the other 9 use ROLLUP, which Ibis cannot express |
| everyday constructs (52) | 15 of the first 36 | 51; OFFSET is dropped by sqlglot < 30's planner |
| silently wrong | 4 | 0 |

Tests: `ibis/backends/duckdb/tests/test_decompile_tpch.py`
(`test_parse_sql_tpch_results`, `test_parse_sql_tpcds_results`) and
`ibis/backends/duckdb/tests/test_parse_sql.py`. Run with the DuckDB backend
test environment (needs `ci/ibis-testing-data`).

## Architecture: why the planner stays

Before changing anything I measured the planner itself: it plans 22/22 TPC-H
and 99/99 TPC-DS, and it preserves DISTINCT, HAVING, windows and set
operations. Every silent-wrong case was a gap in the Ibis conversion code, not
information lost by the planner. Its step types (Scan, Join, Aggregate, Sort,
SetOperation) map almost one-to-one onto Ibis relational ops. A rewrite onto
the raw AST was not needed.

Costs of the planner, all worked around in `ibis/expr/sql.py`:

- Generated names are undocumented: `_a_N` (aggregate operands), `_g_N`
  (group keys), `_h` (HAVING), `_o_N` (order keys), `_w_N` is ours.
- Dependencies are stored in a `set`; conversion order must be forced or the
  choice of which scan becomes a `.view()` differs per run.
- `SetOperation` steps do not record which side is left; set operations are
  converted from the sqlglot expression instead.
- Nested derived tables are inlined into one step, dropping intermediate
  aliases; unknown aliases fall back to the relation being built.
- Window functions are lifted into an Aggregate step together with real
  aggregates; the converter splits them apart.
- The planner mistakes `x = (SELECT ...)` and `x = outer.y` for join keys.

## Optimizer rule set (`RULES`)

Dropped: `unnest_subqueries`, `merge_subqueries`, `eliminate_subqueries`
(they decorrelate subqueries into joins against generated derived tables;
Ibis expresses EXISTS / IN / scalar subqueries directly, which is both simpler
to translate and what a user would write), and `optimize_joins` (raises a
cycle error once a correlated subquery stays in a join condition).

Kept: `pushdown_predicates` (turns `FROM a, b WHERE a.k = b.k` into proper
join keys) and `pushdown_projections` (dropping it broke TPC-H q02; the price
is that every scan carries a `.select(...)`, which the decompiler then inlines
everywhere; that is a decompiler bug, see follow-ups).

## Design decisions, each with the query that forced it

- Second scan of the same base table or CTE becomes `.view()`: self joins
  (TPC-H q07, q21) and correlated subqueries over the outer table (q17).
- Subqueries are planned as-is, never re-optimized in isolation: re-running
  `optimize` on an inner SELECT loses the outer scope (q04, EXISTS cases).
- Catalog keeps base tables apart from bindings: a base table's name
  otherwise shadows the scan of it, which produced a silent wrong result on
  TPC-DS q75 and the "belongs to another relation" errors on ~20 queries.
- Column references are dereferenced against a stack of enclosing relations:
  a join member's field maps to the chain's column (or, for a key an inner
  equality makes redundant, to its counterpart), and the name is traced up
  through the projections and aggregates above the chain (TPC-DS q39, q47).
- Correlated conditions in an inner join's ON clause are applied as a filter
  over the finished chain; Ibis cannot address a join member from inside a
  subquery in a join predicate (TPC-H q02, q17; TPC-DS q10).
- Join collisions are suffixed `{name}_{alias}` instead of `_right`: chains
  that join the same CTE four times collide on `_right` (TPC-DS q04, q11).
- Set operands are aligned by position and coerced to a common type, as SQL
  does; Ibis matches by name (TPC-DS q14, q27, q36).
- SQL ranking functions are 1-based and the default frame of an ordered
  aggregate window ends at the current row; Ibis differs on both.

## Deviations a reviewer should know about

- `mean()` of a decimal column is typed as a decimal of the input scale in
  Ibis; DuckDB computes a double. Parsed averages are therefore rounded. The
  tests compare numbers to two decimals because of this. Open question: should
  the parser cast to float before averaging, or is this Ibis's semantics?
- Duplicate SQL output names get a `_1`, `_2` suffix; Ibis forbids them.
- `concat()` is translated with `coalesce(arg, '')` per argument because
  CONCAT skips NULLs where `||` propagates them (DuckDB semantics).

## Not done (follow-ups, in order)

1. Decompiler: no rules for ExistsSubquery, ScalarSubquery, InSubquery, cross
   joins, windows; and it inlines pushed-down `select`s instead of binding
   them to a variable. Only 6/22 TPC-H expressions decompile-and-re-execute.
   Issue #11810 (op vs method signature mismatch) is the structural fix.
2. ROLLUP / GROUPING SETS: an Ibis expression-model gap, not a parser gap.
3. Dialect functions beyond the ANSI-ish set in the tests.
4. Docs: `parse_sql` is not in the API reference; `docs/_quarto.yml` has a
   TODO for `decompile` "once the API is not experimental".

## Suggested PR split

1. Converter fixes with no rule-set change: DISTINCT, HAVING, COUNT(DISTINCT),
   OFFSET, CASE, NOT, IS NULL, LIKE, scalar functions, derived tables,
   aggregate projections. Make the silent-wrong cases correct first.
2. Rule set + correlated subqueries + self joins + the catalog/dereference
   model, with the TPC-H result test as the gate.
3. Set operations, CTEs, windows, FULL joins, date arithmetic, with the TPC-DS
   result test and the everyday-SQL test.
4. Decompiler work, separately.

Each step keeps the existing `ibis/expr/tests/test_sql.py` snapshots updated;
they change because the optimizer no longer merges subqueries.

## Related issues

#9529 (convert SQL to Ibis code, the tracking issue), #9979 (TPC-H), #9267
(COUNT(*)), #8597 (decompile returns SQL), #11810 (decompiler signatures),
#9254 (closed, "use sqlglot's AST").
