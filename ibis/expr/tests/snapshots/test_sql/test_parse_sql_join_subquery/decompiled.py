import ibis

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
selfreference = call.view()
agg = call.aggregate([call.call_attempts.mean().name("mean")])

result = selfreference.inner_join(agg, (selfreference.call_attempts > agg.mean)).select(
    selfreference.start_time,
    selfreference.end_time,
    selfreference.employee_id,
    selfreference.call_outcome_id,
    selfreference.call_attempts,
    agg.mean,
)
