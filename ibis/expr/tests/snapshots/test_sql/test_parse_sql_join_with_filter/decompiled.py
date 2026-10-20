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
f = employee.filter((employee.id < 5))
joinchain = f.left_join(call, (f.id == call.employee_id)).select(
    f.first_name,
    f.last_name,
    f.id,
    call.start_time,
    call.end_time,
    call.employee_id,
    call.call_outcome_id,
    call.call_attempts,
    f.first_name.name("first"),
)

result = joinchain.order_by(joinchain.id.desc())
