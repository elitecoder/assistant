"""Checks for: a task id the record can't hold is left out of the record, and the typing goes ahead."""

SE = "src/assistant/session_effects.py"
T = "tests/test_session_effects.py::"
P = "tests/test_session_effects_pulse.py::"

CHECKS = (
    (
        "a task id of another shape is left out of the record, not refused",
        SE,
        '    if task and effects.TARGET_KEYS["task"].fullmatch(task):  # a task like `td-auto-1a2b` has no id in a record',
        '    if task:  # a task like `td-auto-1a2b` has no id in a record',
        T + "test_a_task_id_the_record_cant_hold_is_left_out_and_the_typing_goes_ahead",
    ),
    (
        "a pulse task with an auto id is typed into and not parked",
        SE,
        '    if task and effects.TARGET_KEYS["task"].fullmatch(task):  # a task like `td-auto-1a2b` has no id in a record',
        '    if task:  # a task like `td-auto-1a2b` has no id in a record',
        P + "test_a_pulse_task_with_an_auto_id_is_typed_into_recorded_and_not_parked",
    ),
    (
        "a task with the record's id shape keeps its id in the record",
        SE,
        '    if task and effects.TARGET_KEYS["task"].fullmatch(task):  # a task like `td-auto-1a2b` has no id in a record',
        '    if task and False:  # a task like `td-auto-1a2b` has no id in a record',
        T + "test_a_task_id_the_record_can_hold_is_still_recorded",
    ),
)
