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
    employee.select(employee.id)
    .left_join(
        call.select(call.employee_id, call.call_attempts),
        (
            employee.select(employee.id).id
            == call.select(call.employee_id, call.call_attempts).employee_id
        ),
    )
    .select(
        employee.select(employee.id).id,
        call.select(call.employee_id, call.call_attempts).employee_id,
        call.select(call.employee_id, call.call_attempts).call_attempts,
    )
)

result = joinchain.aggregate(
    [joinchain.call_attempts.sum().name("attempts")], by=[joinchain.id]
)
