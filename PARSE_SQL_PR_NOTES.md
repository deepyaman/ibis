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
| decompile and re-execute, TPC-H | 6 | 22 |
| decompile and re-execute, TPC-DS | not measured | 90 (the ROLLUP 9 again) |
| decompile and re-execute, everyday | not measured | 51 |

Tests: `ibis/backends/duckdb/tests/test_decompile_tpch.py`
(`test_parse_sql_tpch_results`, `test_parse_sql_tpcds_results`, each with a
`parse` and a `roundtrip` variant) and
`ibis/backends/duckdb/tests/test_parse_sql.py`. Run with the DuckDB backend
test environment (needs `ci/ibis-testing-data`).

## Architecture: why the planner stays

> Superseded on 2026-10-20 by the AST converter; see "Phase A handoff" at the end.

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

> Superseded on 2026-10-20 by the AST converter; see "Phase A handoff" at the end.

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

## Decompiler

The decompiler is checked the same way: decompile the parsed expression, run
the generated code, execute the rebuilt expression, compare rows. Unit tests
in `ibis/expr/tests/test_decompile.py` additionally require the generated
code to be a fixed point (decompiling the rebuilt expression gives the same
text).

Decisions, each with the query that forced it:

- EXISTS renders as `(pred).any()`, the documented way to spell a correlated
  EXISTS; it needs the predicate to span exactly the inner and outer table,
  otherwise `(rel.count() > 0)` (TPC-H q04, q21, q22).
- A relation joined through a `JoinReference` is always bound to a variable:
  the reference is a pass-through, so its parent looked single-use and was
  inlined into every predicate and projection of the chain (TPC-H q02 was
  unreadable and raised).
- `and`/`or` were rendered as Python keywords, which raise on an Ibis
  expression; NOT rendered as `.not()`, a syntax error (q07, q12, q13, q19).
- `simplify` (used only by the decompiler) moved filters below projections
  that compute window functions, so the window saw filtered rows: TPC-DS
  q51, q53, q63 and q89 silently returned different rows. It also collapsed
  `table.select(all columns)` under a subquery into the bare table, which
  subquery ops reject (q95).
- A join link shared by two chains was bound to a variable, producing
  `joinlink = .inner_join(...)` (TPC-DS q10).
- Variable names come from op class names; `or`, `sum`, `not` are keywords or
  builtins and get a trailing underscore (TPC-DS q88).

Known rough edges: shared sub-expressions get class-name variables
(`invalues`, `windowfunction`, `subtract`); `ibis.literal(...)` is spelled
out for every list member of `isin`; `.name()` is used where `mutate(k=...)`
would read better. All cosmetic. Issue #11810 (op vs method signature
mismatch) remains the structural fix for the generic `value()` rule.

## Not done (follow-ups, in order)

1. ROLLUP / GROUPING SETS: an Ibis expression-model gap, not a parser gap.
2. Dialect functions beyond the ANSI-ish set in the tests.
3. Decompiled code readability (see rough edges above).
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
4. Decompiler: the `simplify` fix first (it is a correctness bug on its own),
   then the rendering rules with the round-trip tests as the gate.

Each step keeps the existing `ibis/expr/tests/test_sql.py` snapshots updated;
they change because the optimizer no longer merges subqueries.

## Related issues

#9529 (convert SQL to Ibis code, the tracking issue), #9979 (TPC-H), #9267
(COUNT(*)), #8597 (decompile returns SQL), #11810 (decompiler signatures),
#9254 (closed, "use sqlglot's AST").

## Phase A handoff: the AST converter (2026-10-20)

**Status.** Commits `57ac91f24` (`fix(decompile)`: date arithmetic and TRY_CAST rendering) and
`627a4db5d` (`feat(parse_sql): drop the planner, convert the AST`). `ibis/expr/sql.py` (1446 lines)
no longer imports `sqlglot.planner`. Gate, from the worktree with the pixi python:

    python -m pytest ibis/backends/duckdb/tests/test_parse_sql.py ibis/backends/duckdb/tests/test_decompile_tpch.py \
      ibis/expr/tests/test_decompile.py ibis/expr/tests/test_rewrites.py ibis/expr/tests/test_sql.py \
      -q --no-header -p no:cacheprovider

504 passed, 18 xfailed (the nine TPC-DS ROLLUP queries, parse and roundtrip, `strict=True`,
`raises=UnsupportedOperationError`). TPC-H 22/22 and TPC-DS 90/99 in both modes; 96 everyday queries
in both modes; 15 unsupported-construct cases raise as intended; OFFSET passes with no xfail. The two
claims above that the review refuted stand corrected: `pushdown_projections` was never needed, and the
planner did lose information (OFFSET, DISTINCT ON, QUALIFY, derived-table projections, set-op sides).

**Design** (the full write-up with the evidence for each rule is the plan file
`~/.claude/plans/pasted-content-id-af53-read-parse-sql-r-foamy-sparrow.md`):

- `RULES = (qualify, annotate_types, canonicalize)`, `optimize(..., isolate_tables=False)`. The
  per-table `(SELECT ... FROM t) AS t` wrappers came from `optimize()`'s table isolation, not from
  `pushdown_projections`. Measured on the three result suites, parse and roundtrip, 288 tests each:
  (A) this set, (B) + `simplify`, (C) + `normalize` + `pushdown_predicates` + `simplify`,
  (D) C + `pushdown_projections`: all four give 288 passed, 18 xfailed. The smallest set is kept.
- One `Scope` per SELECT (`ibis/expr/sql.py`): FROM/JOIN aliases map to columns of the relation under
  construction, which each clause replaces (table or chain, filtered relation, aggregate). A reference
  to an enclosing SELECT's alias resolves through the parent scope to that relation's field, so
  correlated subqueries, HAVING and ORDER BY all use the same lookup.
- Joins: members are addressed through the chain's output names (`_map_member`); an inner-join key
  dropped as equal to the same-named left column maps to that column. A WHERE conjunct that references
  two or more aliases, all bound so far including the newly comma-joined one, and no subquery becomes
  that link's join condition unless a later link is RIGHT/FULL (`_hoistable`). ON conjuncts holding a
  subquery are filtered after the chain for inner joins and rejected for outer joins. `rname` is
  `{name}_<alias>`.
- Aggregates: every `AggFunc` that is not a window function (with any `FILTER`) becomes a named metric
  before `aggregate`; GROUP BY expressions are named after the column or the matching select alias.
  Later clauses find both through a node-keyed memo in `convert()`; a qualified column that is not a
  group raises `IbisInputError`. `COUNT(*)` is `scope.rel.count()` on the relation being aggregated.
- Correlated scalar subqueries anchor on a relation the compiler keeps aliased; when the clause's
  relation is a Project/Filter/Sort/Distinct the converter `.view()`s it first (`_anchor`). EXISTS and
  IN block select fusion on their own and need nothing. No gate query exercises the view rule;
  `ibis/expr/tests/test_sql.py::test_correlated_scalar_subquery_over_a_derived_table_is_anchored` does.
- Projection: identity projections are elided; a plain projection on an unfinished join uses
  `Join.select`, anything with a window, aggregate or subquery projects on the finished table. ORDER BY
  keys that the projection does not carry fall back to an extended projection that is sorted and then
  narrowed. Set operations and CTEs as before, with `with_`/`from_` absorbed for sqlglot < 28 and
  `SetOperation` for < 26.

**Phase B** (mechanical, test-gated; the plan's section 9):

- B1 value layer: done in `627a4db5d`: NULL comparisons and arithmetic, TRY_CAST, `= ANY` / `<> ALL`,
  `FILTER (WHERE)`, named windows, parenthesised ORDER BY keys. Left from the review's list: EXTRACT
  units other than the ones that map to a method (`dow`, `doy`, `epoch`), `IS DISTINCT FROM`,
  STRING_AGG, BOOL_OR/BOOL_AND, `//`, DATE_DIFF, CURRENT_DATE, EXP/LOG, POSITION, SPLIT_PART,
  REGEXP_MATCHES, DATE_PART, CONCAT's NULL semantics per dialect, SUBSTRING with a non-positive start,
  ORDER BY on a FROM-less SELECT (currently ignored).
- B2 errors: `_reject_unsupported` and `test_parse_sql_unsupported` exist; audit what else escapes as a
  raw Ibis error (incomparable types, duplicate CTE names) and the wording.
- B3 test hygiene: the comparator (compare in order under a top-level ORDER BY, compare names, no
  blanket rounding; mark the decimal-mean queries against an issue for `ops.Mean`), move
  `assert_same_rows`/`roundtrip` out of the test module, build the TPC-DS catalog once per session.
- B4 hygiene: drop the `chore(sql)` notes commits before any PR; the review's PR order (simplify fix,
  decompile rules, harness, converter); decide the sqlglot floor (`_arg`, `_SET_OPERATION` absorb the
  renames; `Expression.walk(prune=)`, `sge.Filter`, `Join.method` must exist at the floor).
- B5 Ibis issues to file, repros from this session: (1) `merge_select_select` drops the outer alias of
  a correlated `ScalarSubquery`: `s = t.filter(t.a > 1).select(t.a); s.filter(s.a > u.filter(u.a ==
  s.a).a.mean())` compiles to a dangling `t2`; (2) `ir.Join.select` skips the projection rewrite:
  `t.join(u, t.a == u.a).select(t.a, r=ibis.row_number())` emits `ROW_NUMBER()` without `OVER`;
  (3) `Table.aggregate(having=...)` can call the nonexistent `ibis.util.get_name`; (4) an int64 column
  is "not comparable" to an int32 scalar subquery; (5) `ops.Mean` of `decimal(p, s)` is typed
  `decimal(p, s)` while engines return double.

Anything that changes the Scope, anchor, aggregate-memo or hoisting rules, or a TPC-DS failure whose
cause is not obvious, goes back to the planning model rather than being patched around.

**Known deviations.** Unaliased output columns are `_col_N` and quoted aliases are lower-cased by
sqlglot's `qualify`; every rescan of a table or CTE is a `.view()` (TPC-DS scans one table up to 15
times); `parse_sql` without `dialect` parses `ORDER BY x` as NULLS FIRST (sqlglot's generic default),
so pass the dialect; the `.select(<all columns>)` after each join in decompiled output is the
decompiler rendering the chain's own values, not a projection the converter adds.

## Phase B, session 1: B3, B1, B2 (2026-10-20)

**Commits.** `test(duckdb): tighten the parsed SQL result checks` (B3),
`feat(parse_sql): complete the everyday value layer` (B1), `fix(parse_sql): reject invalid and
unsupported SQL` (B2). Gate after each: 514, 558 and 605 passed, 18 xfailed (504 + the tests added).
Scope, anchor, aggregate-memo and hoisting rules untouched. Two decompiler files changed in B1 and B2
(`ibis/expr/decompile.py`: rendering rules for `delta`, `identical_to`, `day_of_week.index()`,
`find`, `split`, `lstrip`/`rstrip`, array indexing, `//`, `ibis.today()`); B4 may split them into
the decompile PR.

**B3, what the strict comparator showed.** `ibis/tests/util.py` now holds `decompile_roundtrip`,
`expose_sort_keys` and `assert_same_result`. Rows are compared in order under a top-level ORDER BY
(ties free; the keys are appended to the SQL DuckDB runs, or taken from the output for set
operations and `SELECT *` ordinals), names case-insensitively except `_col_N`, value kinds, and
values exactly (floats to 1e-9). The everyday suite is exact in both modes. The only difference in
the TPC suites is **decimal typing**, and it is wider than `ops.Mean`:

- `AVG(decimal(p, s))` is typed `decimal(p, s)` (TPC-H 1; TPC-DS 7, 9, 13, 26, 27, 28, 47, 53, 57,
  63, 89, the last three through `AVG(...) OVER`).
- `decimal(15, 2) * decimal(15, 2)` is typed `decimal(15, 2)` (`highest_precedence`), so
  `SUM(l_extendedprice * (1 - l_discount))` is `decimal(38, 2)` where DuckDB computes
  `DECIMAL(38, 4)` (TPC-H 3, 9, 10, 15, 19).
- Both are rounded to the declared scale by `PandasData.convert_Decimal` on `to_pandas`, and
  **`to_pyarrow` raises** `ArrowInvalid: Rescaling Decimal value would cause data loss` on the same
  expressions (repro: `t = ibis.table({"p": "decimal(15, 2)", "d": "decimal(15, 2)"}, name="t")`,
  `con.to_pyarrow(t.aggregate(s=(t.p * (1 - t.d)).sum()))` on DuckDB with a few rows).

The 17 queries are marked in `TPCH_NARROW_DECIMAL_QUERIES` / `TPCDS_NARROW_DECIMAL_QUERIES`
(`test_decompile_tpch.py`); `narrow_decimals=True` admits half a unit of the last place on those
columns and fails if none deviates any more. **B5 action:** file the issue (Mean typing *and* decimal
multiply scale, with the `to_pyarrow` repro) and put its number in the comment above those sets.

**B1.** EXTRACT dow/isodow/doy/week/epoch (+ DATE_PART), IS [NOT] DISTINCT FROM (also as a join
key; `x IS NOT DISTINCT FROM NULL` keeps its meaning), STRING_AGG (+ ORDER BY), BOOL_OR/BOOL_AND,
`//`, DATE_DIFF, CURRENT_DATE/CURRENT_TIMESTAMP/NOW/TODAY, EXP/LOG (bare LOG is base 10: sqlglot
parses it as LN where the dialect says so), POSITION/STRPOS, SPLIT_PART (SQL's `''` past the last
part, via `ifelse`), REGEXP_MATCHES, CONCAT NULL handling from sqlglot's `coalesce` flag (the `safe`
flag the converter read is set for every dialect), SUBSTRING rejects a literal start < 1, ORDER BY on
a FROM-less SELECT is kept. Known: `dow` numbering is Postgres/DuckDB's (Sunday 0).

**B2.** Silent misreads fixed by raising: aggregate in WHERE (was a whole-table scalar subquery),
window in WHERE/HAVING, `LIMIT n%`, set-operation sides of different widths (were zipped), TRIM of
characters, LTRIM/RTRIM (now `lstrip`/`rstrip`), BETWEEN SYMMETRIC, window `EXCLUDE`, several
statements (`parse_one` kept the first). Type errors from Ibis are `IbisInputError` naming the
fragment (`convert` wraps `TypeError`/`ValidationError`; WHERE/HAVING/ON go through `_boolean`).
Unknown units, nested aggregates, duplicate CTEs, unknown tables, parse errors: all
`IbisInputError`/`UnsupportedOperationError`. `_metric_units` now treats only the function of a
window as a window function (an aggregate inside PARTITION BY is a metric). `test_parse_sql_invalid`
(22) and `test_parse_sql_unsupported` (34) pin the messages.

**For B4 (sqlglot floor).** Classes registered at import since Phase A: `sge.SplitPart`,
`LogicalOr`, `LogicalAnd`, `NullSafeEQ`, `NullSafeNEQ`, `IntDiv`, `DateDiff`, `CurrentDate`,
`CurrentTimestamp`, `StrPosition`, `RegexpLike`, `Exp`, `Log`, `GroupConcat`, `Anonymous`, `Fetch`
(isinstance only). Args read by name: `Limit.limit_options`, `WindowSpec.exclude`,
`RegexpLike.flag`, `Concat.coalesce`, `Between.symmetric`, `Trim.position`.

**Pre-existing, outside the gate.** Three decompile snapshots under `ibis/backends/tests/sql/`
fail on the branch independent of this session (`test_compiler.py::test_table_drop_with_filter`,
`test_select_sql.py::test_where_analyze_scalar_op`, `::test_join_between_joins`: interval units and
join-chain rendering changed by the decompile commits); regenerate them in the decompile PR.

**Edge kept as a loud failure.** In an aggregate query, a window function whose argument is
structurally equal to a metric (`SUM(a) AS s, SUM(a) OVER () ... GROUP BY a`) hits the memo and
raises `IbisInputError` ("No reduction or analytic function ...") rather than translating; valid but
rare SQL, left for the memo's owner.

**Follow-ups after re-running the review's 184-query battery** (the review session's
`scratchpad/review/adversarial.py` and `adversarial_rt.py`, strict comparator, no rounding):
`1283a0e72` fix(parse_sql): truncate integer division like SQL (`a // b` was Ibis's floor; now
`(a - a % b) // b`, exact for negative operands), `051539ec3` fix(parse_sql): let an uncorrelated
EXISTS compile (a bare table under EXISTS is projected first; NATURAL/ASOF/POSITIONAL joins are
rejected before `qualify` fails on their columns). Gate: 611 passed, 18 xfailed.

| battery (184 queries) | ok | silently wrong | raise |
|---|---|---|---|
| main (planner, 2022 prototype) | 50 | 10 | 124 |
| planner branch at review time | 143 | 5 | 36 (roundtrip 138 / 5 / 41) |
| AST converter now | 176 | 1 | 7 (roundtrip 173 / 1 / 10) |

The one wrong result is the decimal typing above (`AVG(CAST(c AS DECIMAL(10, 2)))` → 2.12 for
2.125). The seven raises are deliberate `UnsupportedOperationError`s: DISTINCT ON, NATURAL JOIN,
`SUM(DISTINCT x)`, `SUBSTRING` with start 0 and -1, `YEAR(d)` (follow-up 2, dialect functions),
`> ALL (subquery)`. The three roundtrip-only raises are decompiler gaps the review already assigned
to the decompile PR: `PERCENT_RANK()`/`CUME_DIST()` render with no arguments, a nested EXISTS trips
the `.any()` unpack, `NULLIF` renders as `.null_if`.

## Planner vs AST converter, measured (2026-10-20)

Numbers for the planner column come from `PARSE_SQL_REVIEW.md` (the branch at `2036a63a8`); the AST
column is the branch at `051539ec3`. The battery scripts now travel with the branch as
`PARSE_SQL_BATTERY.py` and `PARSE_SQL_BATTERY_RT.py` (run from the worktree with
`PYTHONPATH=$PWD`); drop them with the notes before any PR.

| | Planner converter (branch at review time) | AST converter (branch now) |
|---|---|---|
| Review battery, 184 queries (ok / silently wrong / raise) | 143 / 5 / 36 | 176 / 1 / 7 |
| Same after a decompile round trip | 138 / 5 / 41 | 173 / 1 / 10 |
| `main` before the branch, for scale | 50 / 10 / 124 | |
| Gate | 392 passed, 20 xfailed | 611 passed, 18 xfailed |
| What the gate checks | sorted rows, names ignored, numbers rounded to 2 decimals | row order under ORDER BY, names, value kinds, exact values (floats 1e-9) |
| TPC-H / TPC-DS | 22/22, 90/99 | 22/22, 90/99 (the same 9 ROLLUP queries: an Ibis gap) |
| Everyday SQL cases | 54, OFFSET xfailed | 120, OFFSET passes |
| Unsupported / invalid SQL tests | 0 / 0 | 36 / 22 |
| Silent wrong results known | DISTINCT ON, QUALIFY, OFFSET dropped, `_h` alias dropped, `= NULL` as IS NULL, TRY_CAST as CAST, SUBSTRING from 0 or -1, decimal rounding hidden by the comparator | decimal typing only, flagged on 17 queries with a strict mark |
| Derived tables with computed columns | regressed against main | work |
| Failure mode for SQL it cannot handle | `KeyError: 's'`, `KeyError: ''`, `TypeError: <class 'sqlglot.expressions.MD5'>`, "Column 'channel' is not found" for ROLLUP | `UnsupportedOperationError` naming the construct, `IbisInputError` naming the SQL fragment |
| Optimizer rules | `pushdown_predicates` and `pushdown_projections` needed, `merge_subqueries` dropped | `qualify`, `annotate_types`, `canonicalize`; heavier sets measured equal |
| `ibis/expr/sql.py` | 1449 lines, about 400 undoing planner artifacts | 1703 lines, none undoing anything; the growth is functions and validation |
| sqlglot coupling | `import ibis` broke below 26, CTEs silently unbound below 28, OFFSET needed a planner from 30.18 | renames absorbed, OFFSET independent of the planner; classes registered at import still need the floor decision (B4) |
| Decompiled TPC-H q02 (Phase A review) | three `.view()`s, per-scan `.select(...)`, planner join shape | `join(..., keys)` for comma joins, `.any()` for EXISTS, no per-scan selects |

Open in both: ROLLUP and GROUPING SETS (expression model), the decimal typing (B5), the three
decompiler gaps above (decompile PR). Known AST edge: a window function whose argument is
structurally equal to a metric of the same aggregate query hits the memo and raises instead of
translating.

## Phase B, session 2: the review 2 fix session (2026-10-20)

**Commits** (on top of the review note, rebased onto `main` at `67dbda44c`, whose `uv.lock` is now
sqlglot 30.13.0): `fix(parse_sql): read the negate arg of NOT [I]LIKE` (B2), `fix(parse_sql): give
IS TRUE and IS FALSE no NULLs`, `fix(parse_sql): frame three window shapes like SQL`,
`fix(parse_sql): translate CONCAT_WS and skip NULLs`, `fix(decompile): keep literal left operands as
ibis`, `fix(parse_sql): type decimal literals like engines` (B3), `fix(parse_sql): accept COUNT(1)
and a bare OVER ()` (B4), `fix(ir): do not merge filters that hold a subquery`, `fix(decompile):
render set ops, unaries and EXISTS` (B5), `fix(parse_sql): import on sqlglot's declared floor`,
`ci: run the parse_sql tests at both sqlglot bounds` (B1). Every fix has a case in the gate
(`CASES`, `UNSUPPORTED`, `INVALID`, `roundtrip_cases` or `test_rewrites.py`) that failed before it.
Scope, anchor, aggregate-memo and hoisting rules untouched.

| | before (review 2) | after |
|---|---|---|
| Gate (pixi, sqlglot 28.8) | 611 passed, 18 xfailed | 672 passed, 18 xfailed |
| `PARSE_SQL_BATTERY.py` | ok=176 wrong=1 error=7 | ok=176 wrong=1 error=7 (unchanged: the decimal AVG and the seven deliberate raises) |
| `battery2.py parse` (ok / wrong / kind / ugly / raise) | 283 / 16 / 2 / 5 / 52 | 298 / 4 / 2 / 0 / 54 |
| `battery2.py roundtrip` | 269 / 18 / 2 / 11 / 52, 6 renamed columns | 298 / 4 / 2 / 0 / 54, 0 renamed |
| Wider suites (`ibis/backends/tests/sql ibis/expr/tests ibis/tests/expr`) | 3310 passed with 3 snapshots deselected | 3323 passed, 1 skipped, 3 xfailed, nothing deselected |
| Gate at sqlglot 27.29.0 / 30.24.0 | 27.29 ok; 30.24 TPC-H q13 and q16 wrong | 672 passed, 18 xfailed at both |
| Gate at sqlglot 23.6.3 (the floor of `>=23.4`) | `import ibis` fails | 633 passed, 39 skipped, 18 xfailed; TPC-H 22/22 |

The four remaining `battery2` wrongs are the three decimal-typing cases (`p * q`, `SUM(p * q)`,
`AVG(p)`: the B5 issue to file) and `SUBSTRING(b, a - 1, 1)`, a computed start before 1, which
the docstring now documents as following Ibis. The two extra raises are `LENGTH(NULL)` and
`SUM((SELECT COUNT(*) ...))`, now `IbisInputError` instead of `AttributeError`.

**What each fix did beyond the review's one-liner.**

- B2: sqlglot 30.8 parses `NOT LIKE` as `Like(negate=True)`; the rule reads the arg, which is
  absent before. `main`'s lock is past 30.8, so this would have shown up in CI.
- B3 decimals: a literal with a decimal point is `Decimal` typed by its digits (`1.10` is
  `decimal(3, 2)`), an exponent form stays float. Two typing rules came with it, in
  `_align_numeric`: float with decimal computes in float (Ibis ranks float64 below decimal, so
  `c * 0.5` on a float column would be narrowed on fetch), and two decimals Ibis cannot rank
  (`decimal(38, 2) * 0.0001000000`, which raised "Cannot compute precedence" in TPC-H q11 and
  TPC-DS 11, 12, 31, 36, 98) widen the narrower one to the wider precision and scale.
- B3 windows: a named window is merged with the inline parts (`OVER (w ORDER BY a)`); adding a
  PARTITION BY, a second ORDER BY or a frame over a named window that has one is `IbisInputError`,
  as in DuckDB and PostgreSQL (three `INVALID` cases).
- B4: `NTILE(n) OVER ()` translates, but its decompile round trip failed twice in this session
  with `SignatureValidationError ... order_by=(y,)` from `Value.over`, and 32 later runs of the same
  expression (16 fixed hash seeds, worktree and `main`) could not reproduce it. Left out of the
  `ranking_empty_window` case; worth an issue if it recurs.
- B5 literals: the decompiler now keeps `ibis.literal(...)` for any literal on the left of an
  infix operator (a plain value folds with a plain right operand, or swaps operands through the
  reflected operator), and a typed literal is never spliced bare (`('0.2', 'decimal' * t.a)` was
  the pre-existing rendering). `test_basic`'s `three`/`nine` now expect the expressions, and the
  TPC-H q03 snapshot gains `ibis.literal(1)`.
- B5 EXISTS: `decompile()` records, per `ExistsSubquery`, the parent of the Filter or Project whose
  values hold it; `(pred).any()` is rendered only when the predicates span exactly the inner relation
  and that parent and hold no subquery of their own, else `(rel.count() > 0)`. Nested EXISTS and
  the CTE self-EXISTS round-trip. `subsequent_filters` leaves a filter alone when a predicate holds
  a subquery. The first review's `InSubquery` fallback (replacing the `keep` context) is not done:
  it changes `ops.InSubquery` and the compilers' handling of a bare-table subquery, outside this
  session's list.
- B5 set ops / unaries: `distinct=` is rendered when it differs from the method's default
  (`union` False, `intersect`/`difference` True); `~x` and `-x` render parenthesized like binary
  operators, so every method call on them (`.name`, `.fill_null`) binds right. The three snapshots
  that the branch's earlier decompile commits left stale are regenerated in the same commit.
- B1: the five ranking classes and `SplitPart` are registered by name where `sge` has them (no
  version compared). sqlglot 23.6.3 also drops `* REPLACE` in `qualify` (a silent wrong column),
  so a probe at import checks whether `qualify` applies REPLACE and `parse_sql` rejects it where
  not. The CI job `test_sqlglot_bounds` (`ibis-main.yml`) locks sqlglot at the floor resolved from
  the pyproject spec (`uv pip compile --resolution lowest-direct`, 23.6.3 today) and at the latest
  release, downloads the test data and runs the gate. The two gate modules skip, below 27.11, what
  the oldest releases cannot parse or qualify (ranking functions, STRPOS, SPLIT_PART, LTRIM,
  STRING_AGG with a separator, SEMI/ANTI joins, `* REPLACE`, five unsupported-syntax cases, TPC-DS
  36, 44, 47, 49, 57); `parse_sql` raises for every one of them there, nothing is silently wrong.
  The 27.11 boundary is where the ranking classes appear; the other items' boundaries were not
  bisected. The job has not run in GitHub Actions yet; its `uv lock --upgrade-package` steps were
  tried locally on a copy of the project.

**Still open for the PR split (B6):** drop the `chore(sql)` commits, the `PARSE_SQL_*` files and
the battery scripts; the review's PR order; file the B5 issues (decimal typing with the
`to_pyarrow` repro, the `merge_select_select` alias, `Join.select`, `aggregate(having=)`,
int32 subquery comparability). Section 4 of review 2 (missing everyday functions) was out of
scope and is unchanged.
