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
p = employee.select(employee.id)
p1 = call.select(call.employee_id, call.call_attempts)
joinchain = p.left_join(p1, (p.id == p1.employee_id)).select(
    p.id, p1.employee_id, p1.call_attempts
)

result = joinchain.aggregate(
    [joinchain.call_attempts.sum().name("attempts")], by=[joinchain.id]
)
