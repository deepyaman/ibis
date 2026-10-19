from __future__ import annotations

import contextlib
import operator
from functools import singledispatch

import sqlglot as sg
import sqlglot.expressions as sge
import sqlglot.optimizer as sgo
import sqlglot.planner as sgp
from public import public

import ibis
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.schema as sch
import ibis.expr.types as ir
from ibis.backends.sql.datatypes import SqlglotType
from ibis.util import experimental

# The sqlglot optimizer rules we run before planning.
#
# ``unnest_subqueries``, ``merge_subqueries`` and ``eliminate_subqueries``
# rewrite correlated subqueries into joins against generated derived tables
# (``_u_0``, ``partsupp_2``, ...). Ibis can express correlated subqueries
# directly (``EXISTS`` -> ``ExistsSubquery``, scalar subqueries ->
# ``ScalarSubquery``), so keeping them intact yields a much more direct
# translation. ``optimize_joins`` fails with a cycle error when a correlated
# subquery remains in a join condition, so it is dropped as well.
_DROPPED_RULES = frozenset(
    {
        "unnest_subqueries",
        "merge_subqueries",
        "eliminate_subqueries",
        "optimize_joins",
    }
)
RULES = tuple(rule for rule in sgo.RULES if rule.__name__ not in _DROPPED_RULES)


class Catalog(dict[str, sch.Schema]):
    """A catalog of tables and their schemas."""

    typemap = {
        dt.Int8: "tinyint",
        dt.Int16: "smallint",
        dt.Int32: "int",
        dt.Int64: "bigint",
        dt.Float16: "halffloat",
        dt.Float32: "float",
        dt.Float64: "double",
        dt.Decimal: "decimal",
        dt.Boolean: "boolean",
        dt.JSON: "json",
        dt.Interval: "interval",
        dt.Timestamp: "datetime",
        dt.Date: "date",
        dt.Binary: "varbinary",
        dt.String: "varchar",
        dt.Array: "array",
        dt.Map: "map",
        dt.UUID: "uuid",
        dt.Struct: "struct",
    }

    def __init__(
        self,
        *args,
        seen: set[str] | None = None,
        aliases: dict[str, ir.Table] | None = None,
        outer: tuple[ir.Table, ...] = (),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # Every scanned alias in the query, shared by reference. Later steps
        # (a sort over a join, say) refer to columns by the alias they were
        # scanned under, which is not a step name. First registration wins so
        # an alias reused inside a subquery does not shadow the outer one.
        self.aliases: dict[str, ir.Table] = {} if aliases is None else aliases
        # Base table names that have already been scanned somewhere in the
        # query. Shared (by reference) by every overlay of this catalog so a
        # second scan of the same base table (a self join, or a correlated
        # subquery over the outer table) becomes a distinct ``.view()``.
        self.seen: set[str] = set() if seen is None else seen
        # The stack of relations that conditions and projections currently
        # apply to, innermost last. Column references are dereferenced to
        # their fields so that outer references inside correlated subqueries
        # point at the enclosing join chain rather than at one of its (not
        # directly addressable) members.
        self.outer: tuple[ir.Table, ...] = outer

    def to_sqlglot_dtype(self, dtype: dt.DataType) -> str:
        if dtype.is_geospatial():
            return dtype.geotype
        else:
            default = dtype.__class__.__name__.lower()
            return self.typemap.get(type(dtype), default)

    def to_sqlglot_schema(self, schema: sch.Schema) -> dict[str, str]:
        return {name: self.to_sqlglot_dtype(dtype) for name, dtype in schema.items()}

    def to_sqlglot(self):
        return {
            name: self.to_sqlglot_schema(table.schema()) for name, table in self.items()
        }

    def bind(self, **tables) -> Catalog:
        """Return a new catalog with additional name -> table bindings."""
        return Catalog(
            {**self, **tables}, seen=self.seen, aliases=self.aliases, outer=self.outer
        )

    def with_outer(self, outer: ir.Table) -> Catalog:
        """Return a new catalog whose column references resolve against ``outer``."""
        return Catalog(
            self, seen=self.seen, aliases=self.aliases, outer=(*self.outer, outer)
        )

    def register(self, alias: str, table: ir.Table) -> None:
        self.aliases.setdefault(alias, table)

    def resolve(self, alias: str) -> ir.Table:
        try:
            return self[alias]
        except KeyError:
            return self.aliases[alias]

    def dereference(self, field: ir.Value) -> ir.Value:
        """Resolve ``field`` to the equivalent field of the outer join chain."""
        if not isinstance(op := field.op(), ops.Field):
            return field
        for outer in reversed(self.outer):
            # find the join chain the outer relation is built on, if any
            chain = outer.op()
            while not isinstance(chain, ops.JoinChain):
                if (chain := getattr(chain, "parent", None)) is None:
                    break
            else:
                for name, value in chain.values.items():
                    if (
                        isinstance(value, ops.Field)
                        and isinstance(value.rel, ops.JoinReference)
                        and value.rel.parent == op.rel
                        and value.name == op.name
                        and name in outer.columns
                    ):
                        return outer[name]
        return field

    def overlay(self, step):
        updates = {dep.name: convert(dep, catalog=self) for dep in step.dependencies}
        for name, table in updates.items():
            self.register(name, table)
        return self.bind(**updates)


def apply_limit(table, step):
    """Applies a LIMIT, if applicable."""

    offset = getattr(step, "offset", None)
    if not isinstance(step.limit, int):
        if isinstance(offset, int):
            return table.limit(None, offset=offset)
        return table

    return table.limit(step.limit, offset=offset if isinstance(offset, int) else 0)


def apply_projections(table, step, catalog):
    """Applies a SELECT projection, if applicable."""

    if not step.projections:
        return table

    projs = [convert(proj, catalog=catalog) for proj in step.projections]
    return table.select(projs)


def apply_condition(table, condition, catalog):
    """Applies a WHERE/HAVING condition, if applicable."""
    if condition is None:
        return table
    pred = convert(condition, catalog=catalog)
    if isinstance(pred, ir.BooleanScalar) and pred.op() == ops.Literal(
        True, dt.boolean
    ):
        return table
    return table.filter(pred)


@singledispatch
def convert(step, catalog):
    raise TypeError(type(step))


@convert.register(sgp.Scan)
def convert_scan(scan, catalog):
    source = scan.source
    alias = source.alias_or_name

    if scan.dependencies:
        # the source is a CTE or a derived table (``FROM (SELECT ...) AS s``);
        # the planner names the dependency after *its* source, not the alias
        (dep,) = scan.dependencies
        table = convert(dep, catalog=catalog)
    else:
        name = source.name
        table = catalog[name]
        if name in catalog.seen:
            table = table.view()
        catalog.seen.add(name)

    # make the alias visible to correlated subqueries in the condition
    catalog = catalog.bind(**{alias: table})

    table = apply_condition(table, scan.condition, catalog)
    catalog = catalog.bind(**{scan.name: table})
    table = apply_projections(table, scan, catalog)
    catalog.register(alias, table)
    catalog.register(scan.name, table)
    table = apply_limit(table, scan)

    return table


def qualify_projections(projections, groups):
    # The sqlglot planner will (sometimes) alias projections to the aggregate
    # that precedes it.
    #
    # - Sort: lineitem (132849388268768)
    #   Context:
    #     Key:
    #       - "l_returnflag"
    #       - "l_linestatus"
    #   Projections:
    #     - lineitem._g0 AS "l_returnflag"
    #     - lineitem._g1 AS "l_linestatus"
    #     <snip>
    #   Dependencies:
    #   - Aggregate: lineitem (132849388268864)
    #     Context:
    #       Aggregations:
    #         <snip>
    #       Group:
    #         - "lineitem"."l_returnflag"  <-- this is _g0
    #         - "lineitem"."l_linestatus"  <-- this is _g1
    #         <snip>
    #
    #  These aliases are stored in a dictionary in the aggregate `groups`, so if
    #  those are pulled out beforehand then we can use them to replace the
    #  aliases in the projections.

    def transformer(node):
        if (
            isinstance(node, sge.Column)
            and (name := node.name).startswith("_g")
            and isinstance(group := groups.get(name), sge.Column)
        ):
            # plain column groups keep their own name in the aggregate output;
            # computed groups are emitted under the ``_gN`` name instead
            return group
        return node

    projects = [project.transform(transformer) for project in projections]

    return projects


@convert.register(sgp.Sort)
def convert_sort(sort, catalog):
    catalog = catalog.overlay(sort)

    table = catalog[sort.name]
    catalog = catalog.with_outer(table)

    if sort.key:
        keys = [convert(key, catalog=None) for key in sort.key]
        table = table.order_by(keys)

    if sort.projections:
        groups = {}
        # group definitions that may be used in projections are defined
        # in the aggregate in dependencies...
        for dep in sort.dependencies:
            if (group := getattr(dep, "group", None)) is not None:
                groups |= group
        projs = [
            convert(proj, catalog=catalog)
            for proj in qualify_projections(sort.projections, groups)
        ]
        table = table.select(projs)

    table = apply_limit(table, sort)

    return table


_join_types = {
    "": "inner",
    "LEFT": "left",
    "RIGHT": "right",
}


@convert.register(sgp.Join)
def convert_join(join, catalog):
    catalog = catalog.overlay(join)

    left_name = join.source_name or join.name
    left_table = catalog[left_name]
    deferred = []

    for right_name, desc in join.joins.items():
        right_table = catalog[right_name]
        join_kind = _join_types[desc["side"]]

        predicate = None
        if desc["join_key"]:
            for left_key, right_key in zip(desc["source_key"], desc["join_key"]):
                if (
                    left_key.find(sge.Select) is not None
                    or right_key.find(sge.Select) is not None
                ):
                    # ``x = (SELECT ...)`` is an equality the planner mistook
                    # for a join key; it is a (correlated) filter
                    deferred.append(sge.EQ(this=left_key, expression=right_key))
                    continue
                left_key = convert(left_key, catalog=catalog)
                right_key = convert(right_key, catalog=catalog)
                if predicate is None:
                    predicate = left_key == right_key
                else:
                    predicate &= left_key == right_key

        if (condition := desc.get("condition")) is not None:
            if join_kind == "inner" and condition.find(sge.Select) is not None:
                deferred.append(condition)
            else:
                condition = convert(condition, catalog=catalog.with_outer(left_table))
                if not _is_true(condition):
                    predicate = (
                        condition if predicate is None else predicate & condition
                    )

        if predicate is None:
            left_table = left_table.cross_join(right_table)
        else:
            left_table = left_table.join(
                right_table, predicates=predicate, how=join_kind
            )

    catalog = catalog.with_outer(left_table)
    for condition in deferred:
        left_table = apply_condition(left_table, condition, catalog)
    left_table = apply_condition(left_table, join.condition, catalog)
    left_table = apply_projections(left_table, join, catalog)
    left_table = apply_limit(left_table, join)

    catalog[left_name] = left_table
    catalog.register(join.name, left_table)

    return left_table


def _is_true(expr) -> bool:
    return isinstance(expr, ir.BooleanScalar) and expr.op() == ops.Literal(
        True, dt.boolean
    )


def replace_operands(agg):
    # The sqlglot planner will pull out computed operands into a separate
    # section and alias them #
    # e.g.
    # Context:
    #   Aggregations:
    #     - SUM("_a_0") AS "sum_disc_price"
    #   Operands:
    #     - "lineitem"."l_extendedprice" * (1 - "lineitem"."l_discount") AS _a_0
    #
    # For the purposes of decompiling, we want these to be inline, so here we
    # replace those new aliases with the parsed sqlglot expression
    operands = {operand.alias: operand.this for operand in agg.operands}

    def transformer(node):
        if isinstance(node, sge.Column) and node.name in operands.keys():
            return operands[node.name]
        return node

    aggs = [item.transform(transformer) for item in agg.aggregations]

    agg.aggregations = aggs

    return agg


@convert.register(sgp.Aggregate)
def convert_aggregate(agg, catalog):
    catalog = catalog.overlay(agg)

    agg = replace_operands(agg)

    table = catalog[agg.source]
    catalog = catalog.with_outer(table)
    groups = [
        convert(g, catalog=catalog)
        if isinstance(g, sge.Column)
        else convert(g, catalog=catalog).name(name)
        for name, g in agg.group.items()
    ]
    metrics = [convert(a, catalog=catalog) for a in agg.aggregations]

    if metrics or groups:
        table = table.aggregate(metrics, by=groups)

    # the aggregated table is referenced by the step name (or by its source's
    # name) in HAVING and in the projections
    catalog = catalog.bind(**{agg.name: table, agg.source: table}).with_outer(table)

    table = apply_condition(table, agg.condition, catalog)

    if agg.projections:
        projs = [
            convert(proj, catalog=catalog)
            for proj in qualify_projections(agg.projections, agg.group)
        ]
        # ``_h`` is the planner's name for the HAVING predicate, it is not
        # part of the user's SELECT list
        projs = [p for p in projs if p.get_name() != "_h"]
        table = table.select(projs)

    table = apply_limit(table, agg)

    return table


@convert.register(sge.Select)
def convert_select(select, catalog):
    """Plan and convert a nested SELECT (a subquery) against ``catalog``.

    The subquery was already qualified and optimized as part of the whole
    query, so it is planned as is; re-optimizing it in isolation would lose
    the outer scope that correlated columns refer to.
    """
    plan = sgp.Plan(select)
    return convert(plan.root, catalog=catalog)


@convert.register(sge.Subquery)
def convert_subquery(subquery, catalog):
    return convert_select(subquery.this, catalog)


def _as_scalar_subquery(table):
    (name,) = table.columns
    return table[name].as_scalar()


@convert.register(sge.Exists)
def convert_exists(exists, catalog):
    inner = exists.this
    if isinstance(inner, sge.Subquery):
        inner = inner.this
    table = convert_select(inner, catalog)
    return ops.ExistsSubquery(table.op()).to_expr()


@convert.register(sge.Literal)
def convert_literal(literal, catalog):
    value = literal.this
    if literal.is_int:
        value = int(value)
    elif literal.is_number:
        value = float(value)
    return ibis.literal(value)


@convert.register(sge.Boolean)
def convert_boolean(boolean, catalog):
    return ibis.literal(boolean.this)


@convert.register(sge.Null)
def convert_null(null, catalog):
    return ibis.null()


@convert.register(sge.Alias)
def convert_alias(alias, catalog):
    this = convert(alias.this, catalog=catalog)
    return this.name(alias.alias)


@convert.register(sge.Column)
def convert_column(column, catalog):
    table = catalog.resolve(column.table)
    return catalog.dereference(table[column.name])


@convert.register(sge.Ordered)
def convert_ordered(ordered, catalog):
    this = ibis._[ordered.this.name]
    desc = ordered.args.get("desc", False)  # not exposed as an attribute
    nulls_first = ordered.args.get("nulls_first", False)
    return (
        ibis.desc(this, nulls_first=nulls_first)
        if desc
        else ibis.asc(this, nulls_first=nulls_first)
    )


_unary_operations = {
    sge.Paren: lambda x: x,
    sge.Not: operator.invert,
    sge.Neg: operator.neg,
}


@convert.register(sge.Unary)
def convert_unary(unary, catalog):
    op = _unary_operations[type(unary)]
    this = convert(unary.this, catalog=catalog)
    return op(this)


_binary_operations = {
    sge.LT: operator.lt,
    sge.LTE: operator.le,
    sge.GT: operator.gt,
    sge.GTE: operator.ge,
    sge.EQ: operator.eq,
    sge.NEQ: operator.ne,
    sge.Add: operator.add,
    sge.Sub: operator.sub,
    sge.Mul: operator.mul,
    sge.Div: operator.truediv,
    sge.Mod: operator.mod,
    sge.Pow: operator.pow,
    sge.And: operator.and_,
    sge.Or: operator.or_,
}


@convert.register(sge.Binary)
def convert_binary(binary, catalog):
    op = _binary_operations[type(binary)]
    this = convert(binary.this, catalog=catalog)
    expr = convert(binary.expression, catalog=catalog)

    # a (possibly parenthesized) subquery operand is a scalar subquery
    if isinstance(this, ir.Table):
        this = _as_scalar_subquery(this)
    if isinstance(expr, ir.Table):
        expr = _as_scalar_subquery(expr)

    # the planner writes join conditions as ``TRUE AND <condition>``
    if isinstance(binary, sge.And):
        if _is_true(this):
            return expr
        if _is_true(expr):
            return this

    return op(this, expr)


@convert.register(sge.Is)
def convert_is(is_, catalog):
    this = convert(is_.this, catalog=catalog)
    if isinstance(is_.expression, sge.Null):
        return this.isnull()
    return this == convert(is_.expression, catalog=catalog)


@convert.register(sge.Like)
def convert_like(like, catalog):
    this = convert(like.this, catalog=catalog)
    return this.like(convert(like.expression, catalog=catalog))


@convert.register(sge.ILike)
def convert_ilike(like, catalog):
    this = convert(like.this, catalog=catalog)
    return this.ilike(convert(like.expression, catalog=catalog))


@convert.register(sge.Between)
def convert_between(between, catalog):
    this = convert(between.this, catalog=catalog)
    low = convert(between.args["low"], catalog=catalog)
    high = convert(between.args["high"], catalog=catalog)
    return this.between(low, high)


_reduction_methods = {
    sge.Max: "max",
    sge.Min: "min",
    sge.Quantile: "quantile",
    sge.Sum: "sum",
    sge.Avg: "mean",
    sge.ArrayAgg: "collect",
}


@convert.register(sge.AggFunc)
def convert_reduction(reduction, catalog):
    method = _reduction_methods[type(reduction)]
    this = convert(reduction.this, catalog=catalog)
    return getattr(this, method)()


@convert.register(sge.Count)
def convert_count(count, catalog):
    this = count.this
    if this is None or isinstance(this, sge.Star):
        return ibis._.count()
    if isinstance(this, sge.Distinct):
        (this,) = this.expressions
        return convert(this, catalog=catalog).nunique()
    return convert(this, catalog=catalog).count()


@convert.register(sge.In)
def convert_in(in_, catalog):
    this = convert(in_.this, catalog=catalog)
    if (query := in_.args.get("query")) is not None:
        table = convert(query, catalog=catalog)
        (name,) = table.columns
        return this.isin(table[name])
    candidates = [convert(expression, catalog) for expression in in_.expressions]
    return this.isin(candidates)


@convert.register(sge.Cast)
def convert_cast(cast, catalog):
    this = convert(cast.this, catalog)
    to = convert(cast.to, catalog)

    return this.cast(to)


@convert.register(sge.DataType)
def convert_datatype(datatype, catalog):
    return SqlglotType().to_ibis(datatype)


@convert.register(sge.Case)
def convert_case(case, catalog):
    branches = [
        (convert(if_.this, catalog=catalog), convert(if_.args["true"], catalog=catalog))
        for if_ in case.args["ifs"]
    ]
    default = case.args.get("default")
    else_ = None if default is None else convert(default, catalog=catalog)
    if (base := case.this) is not None:
        return convert(base, catalog=catalog).cases(*branches, else_=else_)
    return ibis.cases(*branches, else_=else_)


@convert.register(sge.Substring)
def convert_substring(substring, catalog):
    this = convert(substring.this, catalog=catalog)
    # SQL substrings are 1-based
    start = convert(substring.args["start"], catalog=catalog) - 1
    if (length := substring.args.get("length")) is not None:
        return this.substr(start, convert(length, catalog=catalog))
    return this.substr(start)


@convert.register(sge.Extract)
def convert_extract(extract, catalog):
    unit = extract.this.name.lower()
    this = convert(extract.expression, catalog=catalog)
    return getattr(this, unit)()


@convert.register(sge.Nullif)
def convert_nullif(nullif, catalog):
    this = convert(nullif.this, catalog=catalog)
    return this.nullif(convert(nullif.expression, catalog=catalog))


@convert.register(sge.Coalesce)
def convert_coalesce(coalesce, catalog):
    args = [convert(coalesce.this, catalog=catalog)] + [
        convert(e, catalog=catalog) for e in coalesce.expressions
    ]
    return ibis.coalesce(*args)


@convert.register(sge.Upper)
def convert_upper(upper, catalog):
    return convert(upper.this, catalog=catalog).upper()


@convert.register(sge.Lower)
def convert_lower(lower, catalog):
    return convert(lower.this, catalog=catalog).lower()


@convert.register(sge.Length)
def convert_length(length, catalog):
    return convert(length.this, catalog=catalog).length()


@public
@experimental
def parse_sql(sqlstring, catalog, dialect=None):
    """Parse a SQL string into an Ibis expression.

    Parameters
    ----------
    sqlstring : str
        SQL string to parse
    catalog : dict
        A dictionary mapping table names to either schemas or ibis table expressions.
        If a schema is passed, a table expression will be created using the schema.
    dialect : str, optional
        The SQL dialect to use with sqlglot to parse the query string.

    Returns
    -------
    expr : ir.Expr

    """
    catalog = Catalog(
        {name: ibis.table(schema, name=name) for name, schema in catalog.items()}
    )

    expr = sg.parse_one(sqlstring, dialect)
    tree = sgo.optimize(expr, catalog.to_sqlglot(), rules=RULES)
    plan = sgp.Plan(tree)

    return convert(plan.root, catalog=catalog)


class SQLString(str):
    """Object to hold a formatted SQL string.

    Syntax highlights in Jupyter notebooks.
    """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({str(self)!r})"

    def _repr_markdown_(self) -> str:
        return f"```sql\n{self!s}\n```"

    def _repr_pretty_(self, p, cycle) -> str:
        output = str(self)
        try:
            from pygments import highlight
            from pygments.formatters import TerminalFormatter
            from pygments.lexers import SqlLexer
        except ImportError:
            pass
        else:
            with contextlib.suppress(Exception):
                output = highlight(
                    code=output,
                    lexer=SqlLexer(),
                    formatter=TerminalFormatter(),
                )

        # strip trailing newline
        p.text(output.strip())


@public
def to_sql(
    expr: ir.Expr, dialect: str | None = None, pretty: bool = True, **kwargs
) -> SQLString:
    """Return the formatted SQL string for an expression.

    Parameters
    ----------
    expr
        Ibis expression.
    dialect
        SQL dialect to use for compilation.
    pretty
        Whether to use pretty formatting.
    kwargs
        Scalar parameters

    Returns
    -------
    str
        Formatted SQL string

    Examples
    --------
    >>> import ibis
    >>> t = ibis.table({"a": "int", "b": "int"}, name="t")
    >>> expr = t.mutate(c=t.a + t.b)
    >>> ibis.to_sql(expr)  # doctest: +SKIP
    SELECT
      "t0"."a",
      "t0"."b",
      "t0"."a" + "t0"."b" AS "c"
    FROM "t" AS "t0"

    You can also specify the SQL dialect to use for compilation:
    >>> ibis.to_sql(expr, dialect="mysql")  # doctest: +SKIP
    SELECT
      `t0`.`a`,
      `t0`.`b`,
      `t0`.`a` + `t0`.`b` AS `c`
    FROM `t` AS `t0`

    See Also
    --------
    [`Table.compile()`](./expression-tables.qmd#ibis.expr.types.relations.Table.compile)

    """
    import ibis.backends.sql.compilers as sc

    # try to infer from a non-str expression or if not possible fallback to
    # the default pretty dialect for expressions
    if dialect is None:
        try:
            compiler_provider = expr._find_backend(use_default=True)
        except com.IbisError:
            # default to duckdb for SQL compilation because it supports the
            # widest array of ibis features for SQL backends
            compiler_provider = sc.duckdb
    else:
        try:
            compiler_provider = getattr(sc, dialect)
        except AttributeError as e:
            raise ValueError(f"Unknown dialect {dialect}") from e

    if (compiler := getattr(compiler_provider, "compiler", None)) is None:
        raise NotImplementedError(f"{compiler_provider} is not a SQL backend")

    out = compiler.to_sqlglot(expr.unbind(), **kwargs)
    queries = out if isinstance(out, list) else [out]
    dialect = compiler.dialect
    sql = ";\n".join(query.sql(dialect=dialect, pretty=pretty) for query in queries)
    return SQLString(sql)
