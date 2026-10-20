import ibis

customer = ibis.table(
    name="customer",
    schema={
        "c_custkey": "int64",
        "c_name": "string",
        "c_address": "string",
        "c_nationkey": "int32",
        "c_phone": "string",
        "c_acctbal": "decimal(15, 2)",
        "c_mktsegment": "string",
        "c_comment": "string",
    },
)
orders = ibis.table(
    name="orders",
    schema={
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
)
lineitem = ibis.table(
    name="lineitem",
    schema={
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
)
cast = ibis.literal("1995-03-15").cast("date")
f = customer.filter((customer.c_mktsegment == "BUILDING"))
f1 = orders.filter((orders.o_orderdate < cast))
f2 = lineitem.filter((lineitem.l_shipdate > cast))
p = f.select(f.c_custkey, f.c_mktsegment)
p1 = f1.select(f1.o_orderkey, f1.o_custkey, f1.o_orderdate, f1.o_shippriority)
p2 = f2.select(f2.l_orderkey, f2.l_extendedprice, f2.l_discount, f2.l_shipdate)
joinchain = (
    p.inner_join(p1, (p.c_custkey == p1.o_custkey))
    .inner_join(p2, (p1.o_orderkey == p2.l_orderkey))
    .select(
        p.c_custkey,
        p.c_mktsegment,
        p1.o_orderkey,
        p1.o_custkey,
        p1.o_orderdate,
        p1.o_shippriority,
        p2.l_orderkey,
        p2.l_extendedprice,
        p2.l_discount,
        p2.l_shipdate,
    )
)
agg = joinchain.aggregate(
    [(joinchain.l_extendedprice * ((1 - joinchain.l_discount))).sum().name("revenue")],
    by=[joinchain.l_orderkey, joinchain.o_orderdate, joinchain.o_shippriority],
)
p3 = agg.select(agg.l_orderkey, agg.revenue, agg.o_orderdate, agg.o_shippriority)

result = p3.order_by(p3.revenue.desc(), p3.o_orderdate.asc(nulls_first=True)).limit(10)
