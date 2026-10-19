from __future__ import annotations

import collections
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
        bases: dict[str, ir.Table] | None = None,
        seen: set[str] | None = None,
        aliases: dict[str, ir.Table] | None = None,
        outer: tuple[ir.Table, ...] = (),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # The base tables of the query, by name. They are kept apart from the
        # bindings (``self``) so that a base table's name does not shadow the
        # scan of it a step is actually working with.
        self.bases: dict[str, ir.Table] = {} if bases is None else bases
        # Every scanned alias in the query, shared by reference. Later steps
        # (a sort over a join, say) refer to columns by the alias they were
        # scanned under, which is not a step name. The same alias may be
        # scanned in several scopes (``store`` in two CTEs); column resolution
        # picks the candidate that belongs to the relation being built.
        self.aliases: dict[str, list[ir.Table]] = {} if aliases is None else aliases
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
            name: self.to_sqlglot_schema(table.schema())
            for name, table in self.bases.items()
        }

    def bind(self, **tables) -> Catalog:
        """Return a new catalog with additional name -> table bindings."""
        return Catalog(
            {**self, **tables},
            bases=self.bases,
            seen=self.seen,
            aliases=self.aliases,
            outer=self.outer,
        )

    def with_outer(self, outer: ir.Table) -> Catalog:
        """Return a new catalog whose column references resolve against ``outer``."""
        return Catalog(
            self,
            bases=self.bases,
            seen=self.seen,
            aliases=self.aliases,
            outer=(*self.outer, outer),
        )

    def register(self, alias: str, table: ir.Table) -> None:
        tables = self.aliases.setdefault(alias, [])
        if not any(known.op() == table.op() for known in tables):
            tables.append(table)

    def candidates(self, alias: str) -> list[ir.Table]:
        """The relations ``alias`` may refer to, most specific first."""
        tables = []
        if alias in self:
            tables.append(self[alias])
        tables.extend(self.aliases.get(alias, ()))
        if not tables and self.outer:
            # the planner inlines nested derived tables into one step, which
            # drops their intermediate aliases; those columns live on the
            # relation the step is working on
            tables.append(self.outer[-1])
        if not tables:
            raise KeyError(alias)
        return tables

    def column(self, alias: str, name: str) -> ir.Value:
        """Resolve ``alias.name``, preferring the relation currently being built."""
        if not alias:
            return self.dereference(self.outer[-1][name])
        if alias in self:
            # a binding made by the step being converted (or an enclosing one)
            # is authoritative; the registry is for aliases a step never saw
            if name in (table := self[alias]).columns:
                return self.dereference(table[name])
            if self.outer and name in self.outer[-1].columns:
                # a SELECT-list alias referenced through the step's name
                return self.outer[-1][name]
            raise com.IbisTypeError(f"Column {name!r} is not found in {alias!r}")
        fallback = None
        for table in self.candidates(alias):
            if name not in table.columns:
                continue
            field = table[name]
            if (resolved := self.dereference(field)) is not field:
                return resolved
            if fallback is None:
                fallback = field
        if fallback is None:
            raise com.IbisTypeError(
                f"Column {name!r} is not found in any relation named {alias!r}"
            )
        return fallback

    def dereference(self, field: ir.Value) -> ir.Value:
        """Resolve ``field`` to the equivalent field of the relation being built.

        A field of a join chain's member relation becomes the chain's field;
        a field of a relation the current one is built on (the chain under an
        aggregate, say) becomes the current relation's field, following any
        renames the relations in between apply.
        """
        if not isinstance(op := field.op(), ops.Field):
            return field
        for outer in reversed(self.outer):
            # the relations ``outer`` is built on, down to a join chain if any
            lineage = []
            node = outer.op()
            while node is not None and not isinstance(node, ops.JoinChain):
                lineage.append(node)
                node = getattr(node, "parent", None)
            if node is not None:
                lineage.append(node)
            if op.rel in lineage:
                index, name = lineage.index(op.rel), op.name
            elif node is not None and (name := _chain_column(node, op.rel, op.name)):
                index = len(lineage) - 1
            else:
                continue
            name = _trace_name(lineage[:index], lineage[index], name)
            if name is not None and name in outer.columns:
                return outer[name]
        return field

    def overlay(self, step):
        # dependencies are kept in a set; convert them in the order the query
        # names them so that which scan of a table becomes the ``.view()`` is
        # stable from run to run
        order = []
        if isinstance(step, sgp.Join):
            order = [step.source_name or step.name, *step.joins]
        deps = sorted(
            step.dependencies,
            key=lambda dep: (
                order.index(dep.name) if dep.name in order else len(order),
                dep.name or "",
            ),
        )
        updates = {dep.name: convert(dep, catalog=self) for dep in deps}
        for name, table in updates.items():
            self.register(name, table)
        return self.bind(**updates)


def _is_member_field(value, rel, name) -> bool:
    """Whether ``value`` is the field ``name`` of the join member ``rel``."""
    return (
        isinstance(value, ops.Field)
        and value.name == name
        and (
            value.rel == rel
            or (isinstance(value.rel, ops.JoinReference) and value.rel.parent == rel)
        )
    )


def _chain_column(chain, rel, name) -> str | None:
    """The output column of ``chain`` holding field ``name`` of member ``rel``.

    A column of the right table that an inner equality predicate makes equal
    to a column of the left is dropped from the chain, so it is looked up
    through the other side of that predicate.
    """
    for out, value in chain.values.items():
        if _is_member_field(value, rel, name):
            return out
    for link in chain.rest:
        for pred in link.predicates:
            if not (
                isinstance(pred, ops.Equals)
                and isinstance(pred.left, ops.Field)
                and isinstance(pred.right, ops.Field)
            ):
                continue
            for this, other in ((pred.left, pred.right), (pred.right, pred.left)):
                if _is_member_field(this, rel, name):
                    for out, value in chain.values.items():
                        if value == other:
                            return out
    return None


def _trace_name(above, rel, name) -> str | None:
    """Follow column ``name`` of ``rel`` up through the relations ``above`` it.

    ``above`` lists the relations from the outermost down to the child of
    ``rel``; each may pass the column through or rename it.
    """
    for node in reversed(above):
        field = ops.Field(rel, name)
        if isinstance(node, ops.Project):
            name = next((k for k, v in node.values.items() if v == field), None)
        elif isinstance(node, ops.Aggregate):
            name = next(
                (
                    k
                    for k, v in (*node.groups.items(), *node.metrics.items())
                    if v == field
                ),
                None,
            )
        elif name not in node.schema:
            name = None
        if name is None:
            return None
        rel = node
    return name


def apply_limit(table, step):
    """Applies a LIMIT, if applicable."""

    offset = getattr(step, "offset", None)
    if not isinstance(step.limit, int):
        if isinstance(offset, int):
            return table.limit(None, offset=offset)
        return table

    return table.limit(step.limit, offset=offset if isinstance(offset, int) else 0)


def _unique_names(values):
    """SQL allows duplicate output names, Ibis does not: suffix the repeats."""
    seen = collections.Counter()
    out = []
    for value in values:
        name = value.get_name()
        if (n := seen[name]) > 0:
            value = value.name(f"{name}_{n}")
        seen[name] += 1
        out.append(value)
    return out


def apply_projections(table, step, catalog):
    """Applies a SELECT projection, if applicable."""

    if not step.projections:
        return table

    projs = [convert(proj, catalog=catalog) for proj in step.projections]
    return table.select(_unique_names(projs))


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

    if source is None:
        # ``SELECT 1 AS x`` has no FROM clause
        values = {}
        for proj in scan.projections:
            value = convert(proj, catalog=catalog)
            op = value.op()
            values[value.get_name()] = op.arg if isinstance(op, ops.Alias) else op
        return apply_limit(ops.DummyTable(values).to_expr(), scan)

    alias = source.alias_or_name

    if isinstance(source, sge.Subquery):
        # a derived table: ``FROM (SELECT ...) AS s``
        table = convert(source.this, catalog=catalog)
    else:
        name = source.name
        # a CTE (bound) or a base table
        table = catalog[name] if name in catalog else catalog.bases[name]
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

    projected = None
    if sort.projections:
        groups = {}
        # group definitions that may be used in projections are defined
        # in the aggregate in dependencies...
        for dep in sort.dependencies:
            if (group := getattr(dep, "group", None)) is not None:
                groups |= group
        projs = _unique_names(
            [
                convert(proj, catalog=catalog)
                for proj in qualify_projections(sort.projections, groups)
            ]
        )
        projected = table.select(projs)

    if sort.key:
        names = [key.this.name for key in sort.key if isinstance(key.this, sge.Column)]
        simple = (
            projected is not None
            and len(names) == len(sort.key)
            and len(set(names)) == len(names)
            and all(name in projected.columns for name in names)
            and all(key.this.table in ("", sort.name) for key in sort.key)
        )
        if simple:
            # ``ORDER BY`` refers to the SELECT list by name
            keys = [convert(key, catalog=None) for key in sort.key]
            table = projected.order_by(keys)
        elif projected is not None:
            # the keys may mix SELECT-list aliases, columns the SELECT list
            # drops, and qualified columns of a join member
            extended = table.mutate(projs)
            keys = [
                convert(key, catalog=catalog.with_outer(extended)) for key in sort.key
            ]
            table = extended.order_by(keys).select([proj.get_name() for proj in projs])
        else:
            keys = [convert(key, catalog=catalog.with_outer(table)) for key in sort.key]
            table = table.order_by(keys)
    elif projected is not None:
        table = projected

    table = apply_limit(table, sort)

    return table


_join_types = {
    "": "inner",
    "LEFT": "left",
    "RIGHT": "right",
    "FULL": "outer",
}


@convert.register(sgp.Join)
def convert_join(join, catalog):
    catalog = catalog.overlay(join)

    left_name = join.source_name or join.name
    left_table = catalog[left_name]
    deferred = []
    local = {left_name, join.name, *join.joins}

    def is_correlated(expression) -> bool:
        return any(
            column.table and column.table not in local
            for column in expression.find_all(sge.Column)
        )

    for right_name, desc in join.joins.items():
        right_table = catalog[right_name]
        join_kind = _join_types[desc["side"]]

        predicate = None
        if desc["join_key"]:
            for left_key, right_key in zip(desc["source_key"], desc["join_key"]):
                if (
                    left_key.find(sge.Select) is not None
                    or right_key.find(sge.Select) is not None
                    or is_correlated(left_key)
                    or is_correlated(right_key)
                ):
                    # ``x = (SELECT ...)`` or ``x = outer.y`` is an equality
                    # the planner mistook for a join key; it is a filter
                    deferred.append(sge.EQ(this=left_key, expression=right_key))
                    continue
                left_key = convert(left_key, catalog=catalog)
                right_key = convert(right_key, catalog=catalog)
                if predicate is None:
                    predicate = left_key == right_key
                else:
                    predicate &= left_key == right_key

        if (condition := desc.get("condition")) is not None:
            if join_kind == "inner" and (
                condition.find(sge.Select) is not None or is_correlated(condition)
            ):
                deferred.append(condition)
            else:
                condition = convert(condition, catalog=catalog.with_outer(left_table))
                if not _is_true(condition):
                    predicate = (
                        condition if predicate is None else predicate & condition
                    )

        # a chain may join the same relation several times, so colliding
        # column names are suffixed with the (unique) alias, not ``_right``
        rname = f"{{name}}_{right_name}"
        if predicate is None:
            left_table = left_table.cross_join(right_table, rname=rname)
        else:
            left_table = left_table.join(
                right_table, predicates=predicate, how=join_kind, rname=rname
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


def _is_window(expression) -> bool:
    return expression.find(sge.Window) is not None


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
    #
    # Window functions are lifted into the aggregate step as well. Everything
    # a window item aggregates has to be computed by the aggregate first, so
    # aggregate functions outside a window and aggregate operands used inside
    # one become named metrics, and the item itself is evaluated over the
    # aggregated relation (``SUM(x) * 100 / SUM(SUM(x)) OVER (...)``).
    operands = {operand.alias: operand.this for operand in agg.operands}

    def inline(node):
        if isinstance(node, sge.Column) and node.name in operands.keys():
            return operands[node.name]
        return node

    def has_aggregate(expression) -> bool:
        return expression.find(sge.AggFunc) is not None

    aggs = []
    extra = {}

    def split(node):
        if isinstance(node, sge.AggFunc) and node.find_ancestor(sge.Window) is None:
            name = f"_w_{len(extra)}"
            extra[name] = sge.alias_(node.transform(inline), name, quoted=True)
            return sge.column(name, quoted=True)
        if isinstance(node, sge.Column) and node.name in operands.keys():
            operand = operands[node.name]
            if has_aggregate(operand):
                extra.setdefault(node.name, sge.alias_(operand, node.name, quoted=True))
                return node
            return operand
        return node

    for item in agg.aggregations:
        if _is_window(item):
            aggs.append(item.transform(split))
        else:
            aggs.append(item.transform(inline))
    aggs.extend(extra.values())

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
    windows = [a for a in agg.aggregations if _is_window(a)]
    metrics = [
        convert(a, catalog=catalog) for a in agg.aggregations if not _is_window(a)
    ]

    if metrics or groups:
        table = table.aggregate(metrics, by=groups)

    # the aggregated table is referenced by the step name (or by its source's
    # name) in HAVING and in the projections
    catalog = catalog.bind(**{agg.name: table, agg.source: table}).with_outer(table)

    if windows:
        table = table.mutate([convert(w, catalog=catalog) for w in windows])
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
@convert.register(sge.SetOperation)
def convert_select(select, catalog):
    """Convert a (possibly nested) SELECT or set operation against ``catalog``.

    The expression was already qualified and optimized as part of the whole
    query, so it is planned as is; re-optimizing it in isolation would lose
    the outer scope that correlated columns refer to.
    """
    if (with_ := select.args.get("with_")) is not None:
        # bind the CTEs by name, then plan the query without them so that
        # references to a CTE are plain scans resolved through the catalog
        for cte in with_.expressions:
            catalog = catalog.bind(**{cte.alias: convert(cte.this, catalog=catalog)})
        select = select.copy()
        select.set("with_", None)

    if isinstance(select, sge.SetOperation):
        return convert_set_operation(select, catalog)

    # the planner's SetOperation step does not preserve which side is which,
    # so derived tables built from a set operation are converted here and
    # replaced by a reference to the converted table
    if any(
        _is_inlined_source(source, select)
        for source in select.find_all(sge.Subquery)
        if isinstance(source.this, sge.SetOperation)
    ):
        select = select.copy()
        for source in list(select.find_all(sge.Subquery)):
            if isinstance(source.this, sge.SetOperation) and _is_inlined_source(
                source, select
            ):
                alias = source.alias_or_name
                catalog = catalog.bind(**{alias: convert(source.this, catalog=catalog)})
                source.replace(sge.to_table(alias))

    plan = sgp.Plan(select)
    return convert(plan.root, catalog=catalog)


_set_operations = {
    sge.Union: "union",
    sge.Intersect: "intersect",
    sge.Except: "difference",
}


def _align_set_operands(left, right):
    """SQL set operations match columns by position; Ibis matches by name."""
    from ibis.expr.datatypes.cast import highest_precedence

    left_values, right_values = [], []
    for name, rname in zip(left.columns, right.columns):
        lcol, rcol = left[name], right[rname]
        target = highest_precedence([lcol.type(), rcol.type()])
        if lcol.type() != target:
            lcol = lcol.cast(target)
        if rcol.type() != target:
            rcol = rcol.cast(target)
        left_values.append(lcol.name(name))
        right_values.append(rcol.name(name))
    return left.select(left_values), right.select(right_values)


def _is_inlined_source(subquery, select) -> bool:
    """Whether the planner inlines ``subquery`` into the plan of ``select``.

    Derived tables (``FROM (...) AS s``) are inlined, however deeply nested;
    subqueries in expressions (``IN (...)``, ``EXISTS (...)``) are planned
    separately when that expression is converted.
    """
    node = subquery
    while node is not select:
        if isinstance(node, sge.Subquery) and not isinstance(
            node.parent, (sge.From, sge.Join)
        ):
            return False
        if (node := node.parent) is None:
            return False
    return True


def convert_set_operation(setop, catalog):
    left = convert(setop.left, catalog=catalog)
    right = convert(setop.right, catalog=catalog)
    if left.schema() != right.schema():
        left, right = _align_set_operands(left, right)
    method = _set_operations[type(setop)]
    distinct = bool(setop.args.get("distinct"))
    table = getattr(left, method)(right, distinct=distinct)

    if (order := setop.args.get("order")) is not None:
        table = table.order_by(
            [convert(key, catalog=None) for key in order.expressions]
        )

    limit = setop.args.get("limit")
    offset = setop.args.get("offset")
    if limit is not None or offset is not None:
        table = table.limit(
            None if limit is None else int(limit.expression.name),
            offset=0 if offset is None else int(offset.expression.name),
        )

    return table


@convert.register(sge.Subquery)
def convert_subquery(subquery, catalog):
    return convert_select(subquery.this, catalog)


def _scalar(value):
    """A (possibly parenthesized) subquery used as a value is a scalar subquery."""
    if isinstance(value, ir.Table):
        (name,) = value.columns
        return value[name].as_scalar()
    return value


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
    # unqualified columns are the planner's generated ones (``_a_0``, ``_o_0``)
    return catalog.column(column.table, column.name)


@convert.register(sge.Ordered)
def convert_ordered(ordered, catalog):
    if catalog is None:
        # sort steps refer to the columns of the sorted relation by name
        this = ibis._[ordered.this.name]
    else:
        this = convert(ordered.this, catalog=catalog)
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

    this = _scalar(this)
    expr = _scalar(expr)

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


@convert.register(sge.Stddev)
@convert.register(sge.StddevSamp)
def convert_stddev_samp(reduction, catalog):
    return convert(reduction.this, catalog=catalog).std(how="sample")


@convert.register(sge.StddevPop)
def convert_stddev_pop(reduction, catalog):
    return convert(reduction.this, catalog=catalog).std(how="pop")


@convert.register(sge.Variance)
def convert_variance_samp(reduction, catalog):
    return convert(reduction.this, catalog=catalog).var(how="sample")


@convert.register(sge.VariancePop)
def convert_variance_pop(reduction, catalog):
    return convert(reduction.this, catalog=catalog).var(how="pop")


@convert.register(sge.ApproxDistinct)
def convert_approx_distinct(reduction, catalog):
    return convert(reduction.this, catalog=catalog).approx_nunique()


_ranking_functions = {
    sge.RowNumber: "row_number",
    sge.Rank: "rank",
    sge.DenseRank: "dense_rank",
    sge.PercentRank: "percent_rank",
    sge.CumeDist: "cume_dist",
}


@convert.register(sge.RowNumber)
@convert.register(sge.Rank)
@convert.register(sge.DenseRank)
@convert.register(sge.PercentRank)
@convert.register(sge.CumeDist)
def convert_ranking(func, catalog):
    result = getattr(ibis, _ranking_functions[type(func)])()
    if isinstance(func, (sge.RowNumber, sge.Rank, sge.DenseRank)):
        # SQL's ranking functions are 1-based, Ibis's are 0-based
        return result + 1
    return result


@convert.register(sge.Ntile)
def convert_ntile(ntile, catalog):
    # SQL's NTILE is 1-based, Ibis's is 0-based
    return ibis.ntile(convert(ntile.this, catalog=catalog)) + 1


@convert.register(sge.Lag)
@convert.register(sge.Lead)
def convert_lag_lead(func, catalog):
    this = convert(func.this, catalog=catalog)
    offset = func.args.get("offset")
    default = func.args.get("default")
    method = "lag" if isinstance(func, sge.Lag) else "lead"
    return getattr(this, method)(
        None if offset is None else convert(offset, catalog=catalog),
        None if default is None else convert(default, catalog=catalog),
    )


@convert.register(sge.FirstValue)
def convert_first_value(func, catalog):
    return convert(func.this, catalog=catalog).first()


@convert.register(sge.LastValue)
def convert_last_value(func, catalog):
    return convert(func.this, catalog=catalog).last()


@convert.register(sge.NthValue)
def convert_nth_value(func, catalog):
    this = convert(func.this, catalog=catalog)
    # SQL's NTH_VALUE is 1-based, ``nth`` is 0-based
    return this.nth(convert(func.args["offset"], catalog=catalog) - 1)


def _window_bound(value, side):
    """Translate a window frame bound into Ibis's signed offset convention."""
    if value is None:
        return None
    text = value if isinstance(value, str) else value.name
    if text.upper() == "UNBOUNDED":
        return None
    if text.upper() == "CURRENT ROW":
        return 0
    offset = int(text)
    return -offset if (side or "").upper() == "PRECEDING" else offset


@convert.register(sge.Window)
def convert_window(window, catalog):
    func = convert(window.this, catalog=catalog)
    group_by = [
        convert(part, catalog=catalog) for part in window.args.get("partition_by") or ()
    ]
    order_by = []
    if (order := window.args.get("order")) is not None:
        order_by = [convert(key, catalog=catalog) for key in order.expressions]

    kwargs = {}
    if (spec := window.args.get("spec")) is not None:
        start = _window_bound(spec.args.get("start"), spec.args.get("start_side"))
        end = _window_bound(spec.args.get("end"), spec.args.get("end_side"))
        kind = (spec.args.get("kind") or "ROWS").upper()
        kwargs["rows" if kind == "ROWS" else "range"] = (start, end)
    elif order_by and isinstance(window.this, sge.AggFunc):
        # SQL's default frame for an ordered aggregate window ends at the
        # current row (and its peers); Ibis's default is the whole partition
        kwargs["range"] = (None, 0)

    return func.over(ibis.window(group_by=group_by, order_by=order_by, **kwargs))


_truncation_units = {
    "year": "Y",
    "quarter": "Q",
    "month": "M",
    "week": "W",
    "day": "D",
    "hour": "h",
    "minute": "m",
    "second": "s",
    "millisecond": "ms",
    "microsecond": "us",
}


@convert.register(sge.TimestampTrunc)
@convert.register(sge.DateTrunc)
def convert_truncate(trunc, catalog):
    this = convert(trunc.this, catalog=catalog)
    unit = trunc.args["unit"].name.lower().removesuffix("s")
    return this.truncate(_truncation_units[unit])


@convert.register(sge.Round)
def convert_round(func, catalog):
    this = convert(func.this, catalog=catalog)
    if (decimals := func.args.get("decimals")) is not None:
        return this.round(convert(decimals, catalog=catalog))
    return this.round()


@convert.register(sge.Abs)
def convert_abs(func, catalog):
    return convert(func.this, catalog=catalog).abs()


@convert.register(sge.Floor)
def convert_floor(func, catalog):
    return convert(func.this, catalog=catalog).floor()


@convert.register(sge.Ceil)
def convert_ceil(func, catalog):
    return convert(func.this, catalog=catalog).ceil()


@convert.register(sge.Sqrt)
def convert_sqrt(func, catalog):
    return convert(func.this, catalog=catalog).sqrt()


@convert.register(sge.Ln)
def convert_ln(func, catalog):
    return convert(func.this, catalog=catalog).ln()


@convert.register(sge.Greatest)
def convert_greatest(func, catalog):
    return ibis.greatest(
        convert(func.this, catalog=catalog),
        *(convert(e, catalog=catalog) for e in func.expressions),
    )


@convert.register(sge.Least)
def convert_least(func, catalog):
    return ibis.least(
        convert(func.this, catalog=catalog),
        *(convert(e, catalog=catalog) for e in func.expressions),
    )


@convert.register(sge.Concat)
def convert_concat(func, catalog):
    args = [convert(e, catalog=catalog) for e in func.expressions]
    if func.args.get("safe"):
        # CONCAT() skips NULL arguments where ``||`` propagates them
        args = [ibis.coalesce(arg, "") for arg in args]
    first, *rest = args
    for arg in rest:
        first = first + arg
    return first


@convert.register(sge.DPipe)
def convert_dpipe(func, catalog):
    return convert(func.this, catalog=catalog) + convert(
        func.expression, catalog=catalog
    )


@convert.register(sge.Trim)
def convert_trim(func, catalog):
    return convert(func.this, catalog=catalog).strip()


@convert.register(sge.Initcap)
def convert_initcap(func, catalog):
    return convert(func.this, catalog=catalog).capitalize()


@convert.register(sge.If)
def convert_if(func, catalog):
    cond = convert(func.this, catalog=catalog)
    true = convert(func.args["true"], catalog=catalog)
    false = func.args.get("false")
    return cond.ifelse(true, None if false is None else convert(false, catalog=catalog))


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
        (
            _scalar(convert(if_.this, catalog=catalog)),
            _scalar(convert(if_.args["true"], catalog=catalog)),
        )
        for if_ in case.args["ifs"]
    ]
    default = case.args.get("default")
    else_ = None if default is None else _scalar(convert(default, catalog=catalog))
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


_interval_units = {
    "year": "Y",
    "quarter": "Q",
    "month": "M",
    "week": "W",
    "day": "D",
    "hour": "h",
    "minute": "m",
    "second": "s",
}


@convert.register(sge.DateAdd)
@convert.register(sge.DateSub)
def convert_date_add(func, catalog):
    this = convert(func.this, catalog=catalog)
    unit = func.args.get("unit")
    unit = "D" if unit is None else _interval_units[unit.name.lower().removesuffix("s")]
    if isinstance(func.expression, sge.Literal):
        interval = ibis.interval(int(func.expression.name), unit)
    else:
        interval = convert(func.expression, catalog=catalog).as_interval(unit)
    return this - interval if isinstance(func, sge.DateSub) else this + interval


@convert.register(sge.Interval)
def convert_interval(interval, catalog):
    unit = _interval_units[interval.args["unit"].name.lower().removesuffix("s")]
    if isinstance(interval.this, sge.Literal):
        return ibis.interval(int(interval.this.name), unit)
    return convert(interval.this, catalog=catalog).as_interval(unit)


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
        bases={name: ibis.table(schema, name=name) for name, schema in catalog.items()}
    )

    expr = sg.parse_one(sqlstring, dialect)
    tree = sgo.optimize(expr, catalog.to_sqlglot(), dialect=dialect, rules=RULES)

    return convert(tree, catalog=catalog)


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
