"""Checks that a silent connection can't hold the dashboard server (bin/todo-server.py)."""

CHECKS = (
    (
        "a connection that sends nothing is dropped, so it can't hold the dashboard server",
        "bin/todo-server.py",
        "    timeout = IDLE_TIMEOUT_SEC\n",
        "    timeout = None\n",
        "tests/test_todo_server.py::test_the_server_drops_a_silent_connection_after_a_couple_of_seconds",
    ),
    (
        "a silent connection is held for seconds, not minutes",
        "bin/todo-server.py",
        "IDLE_TIMEOUT_SEC = 2\n",
        "IDLE_TIMEOUT_SEC = 600\n",
        "tests/test_todo_server.py::test_the_server_drops_a_silent_connection_after_a_couple_of_seconds",
    ),
    (
        "dropping a silent connection is not logged as an error",
        "bin/todo-server.py",
        '        if fmt.startswith("Request timed out"):\n            return',
        '        if False:\n            return',
        "tests/test_todo_server.py::test_dropping_a_silent_connection_writes_no_log_line_but_other_errors_still_do",
    ),
    (
        "errors other than a dropped silent connection are still logged",
        "bin/todo-server.py",
        '        super().log_error(fmt, *args)\n',
        '        pass\n',
        "tests/test_todo_server.py::test_dropping_a_silent_connection_writes_no_log_line_but_other_errors_still_do",
    ),
)
