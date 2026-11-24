"""Checks that a silent connection can't hold the dashboard server (bin/todo-server.py)."""

LOGGED = "tests/test_todo_server.py::test_dropping_a_silent_connection_writes_no_log_line_but_a_cut_off_request_still_does"

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
        "the limit is long enough for a real request on a slow link",
        "bin/todo-server.py",
        "IDLE_TIMEOUT_SEC = 2\n",
        "IDLE_TIMEOUT_SEC = 0.05\n",
        "tests/test_todo_server.py::test_a_silent_connection_is_closed_after_about_two_seconds",
    ),
    (
        "dropping a silent connection is not logged as an error",
        "bin/todo-server.py",
        '        if fmt.startswith("Request timed out") and not getattr(self, "raw_requestline", b""):\n',
        "        if False:\n",
        LOGGED,
    ),
    (
        "a request that was cut off part-way is still logged",
        "bin/todo-server.py",
        '        if fmt.startswith("Request timed out") and not getattr(self, "raw_requestline", b""):\n',
        '        if fmt.startswith("Request timed out"):\n',
        LOGGED,
    ),
    (
        "errors other than a dropped silent connection are still logged",
        "bin/todo-server.py",
        "        super().log_error(fmt, *args)\n",
        "        pass\n",
        LOGGED,
    ),
    (
        "sending a large page to a slow reader is not cut off by the limit on waiting for a request",
        "bin/todo-server.py",
        "        self.connection.settimeout(SEND_TIMEOUT_SEC)  # the request has been read; all that is left is sending\n",
        "",
        "tests/test_todo_server.py::test_a_large_page_sent_to_a_slow_reader_arrives_whole",
    ),
)
