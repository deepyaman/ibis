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
joinchain = (
    employee.filter((employee.id < 5))
    .left_join(call, (employee.filter((employee.id < 5)).id == call.employee_id))
    .select(
        employee.filter((employee.id < 5)).first_name,
        employee.filter((employee.id < 5)).last_name,
        employee.filter((employee.id < 5)).id,
        call.start_time,
        call.end_time,
        call.employee_id,
        call.call_outcome_id,
        call.call_attempts,
    )
)
s = joinchain.order_by(joinchain.id.desc())

result = s.select(
    s.first_name,
    s.last_name,
    s.id,
    s.start_time,
    s.end_time,
    s.employee_id,
    s.call_outcome_id,
    s.call_attempts,
    s.first_name.name("first"),
)
