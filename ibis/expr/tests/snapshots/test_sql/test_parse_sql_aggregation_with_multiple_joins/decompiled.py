import ibis

employee = ibis.table(
    name="employee",
    schema={"first_name": "string", "last_name": "string", "id": "int64"},
)
call = ibis.table(
    name="call",
    schema={
        "start_time": "timestamp",
        "end_time": "timestamp",
        "employee_id": "int64",
        "call_outcome_id": "int64",
        "call_attempts": "int64",
    },
)
call_outcome = ibis.table(
    name="call_outcome", schema={"outcome_text": "string", "id": "int64"}
)
p = employee.select(employee.id)
p1 = call.select(call.employee_id, call.call_outcome_id, call.call_attempts)
p2 = call_outcome.select(call_outcome.id)
joinchain = (
    p.inner_join(p1, (p.id == p1.employee_id))
    .inner_join(p2, (p1.call_outcome_id == p2.id))
    .select(p1.employee_id, p1.call_attempts)
)

result = joinchain.aggregate(
    [joinchain.call_attempts.mean().name("avg_attempts")], by=[joinchain.employee_id]
)
