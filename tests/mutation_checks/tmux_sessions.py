"""Checks that Pro mode can keep sessions in tmux (src/assistant/session_backend.py, bin/tmux_backend.py) and that the
default mode never does. Their tests run a real tmux server, on a Mac and on Linux."""

BACKEND = "tests/test_tmux_backend.py::"
FLEET = "tests/test_tmux_fleet.py::"
CHOICE = "tests/test_session_backend.py::"

CHECKS = (
    (
        "the default mode never keeps sessions in tmux, whatever the config says",
        "src/assistant/session_backend.py",
        "        if not mode.is_pro():\n            return CMUX\n",
        "        if False:\n            return CMUX\n",
        CHOICE + "test_the_default_mode_keeps_cmux_whatever_else_the_config_says",
    ),
    (
        "with no setting, Pro mode on a Mac stays on cmux",
        "src/assistant/session_backend.py",
        '    return CMUX if sys.platform == "darwin" or os.environ.get("CMUX_BIN") else TMUX\n',
        "    return TMUX\n",
        CHOICE + "test_with_no_setting_pro_mode_uses_tmux_only_where_there_can_be_no_cmux",
    ),
    (
        "a failure while asking which program holds the sessions is cmux",
        "src/assistant/session_backend.py",
        "    except Exception:  # noqa: BLE001 - see the module's docstring\n        return CMUX\n",
        "    except Exception:  # noqa: BLE001 - see the module's docstring\n        return TMUX\n",
        CHOICE + "test_a_failure_while_asking_is_cmux_and_never_stops_the_script",
    ),
    (
        "only the exact words cmux and tmux are a setting",
        "src/assistant/mode.py",
        "    return chosen if chosen in SESSION_BACKENDS else None\n",
        "    return chosen or None\n",
        CHOICE + "test_a_setting_that_is_not_exactly_cmux_or_tmux_is_no_setting",
    ),
    (
        "the comms daemon follows the choice of session program",
        "bin/comms_lib.py",
        '            cmux_bin=Path(cmux_sessions.session_cli(env.get("CMUX_BIN", '
        '"/Applications/cmux.app/Contents/Resources/bin/cmux"))),\n',
        '            cmux_bin=Path(env.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")),\n',
        CHOICE + "test_the_comms_daemon_and_the_session_reader_follow_the_same_choice",
    ),
    (
        "tmux workspaces are numbered from 1, as cmux's are",
        "bin/tmux_backend.py",
        '    return f"{kind}:{int(tmux_id[1:]) + 1}"\n',
        '    return f"{kind}:{int(tmux_id[1:])}"\n',
        BACKEND + "test_a_new_workspace_is_a_detached_session_whose_shell_runs_the_command",
    ),
    (
        "a workspace is never started in a folder that doesn't exist",
        "bin/tmux_backend.py",
        "    if not os.path.isdir(cwd):\n",
        "    if False:\n",
        BACKEND + "test_a_workspace_is_not_started_in_a_folder_that_does_not_exist",
    ),
    (
        "a key tmux doesn't know is refused, not typed as text",
        "bin/tmux_backend.py",
        '        if not held:\n            raise Failure("invalid_params", f"Unknown key {key!r}")\n'
        '        tmux_key = f"C-{held.group(2)}"\n',
        '        tmux_key = f"C-{held.group(2)}" if held else str(key)\n',
        BACKEND + "test_a_key_tmux_would_type_as_text_is_refused",
    ),
    (
        "a typing call that names no pane is refused, not sent to some pane",
        "bin/tmux_backend.py",
        "    if not target and not workspace:\n        raise Failure(*_NO_SURFACE)\n",
        "",
        BACKEND + "test_typing_or_reading_with_no_pane_named_is_refused_not_guessed",
    ),
    (
        "a task title is a window's name as written, never expanded by tmux",
        "bin/tmux_backend.py",
        '    return text.replace("#", "##")\n',
        "    return text\n",
        BACKEND + "test_a_title_or_folder_with_a_hash_is_taken_as_written_not_expanded_by_tmux",
    ),
    (
        "a session that can't be finished is ended, not left for the next try to duplicate",
        "bin/tmux_backend.py",
        '        _tmux("kill-window", "-t", window_id)\n        raise\n',
        "        raise\n",
        BACKEND + "test_a_session_whose_command_cant_be_typed_is_ended_not_left_behind",
    ),
    (
        "a Claude Code that doesn't answer is not read as no sessions",
        "bin/tmux_backend.py",
        "    if rows is None:\n        raise Failure(",
        "    if rows is None:\n        return []\n    if False:\n        raise Failure(",
        BACKEND + "test_a_claude_that_does_not_answer_is_an_error_not_an_empty_fleet",
    ),
    (
        "a socket that can't be opened is not read as no tmux server",
        "bin/tmux_backend.py",
        '        if "no server running" in err or ("error connecting" in err and (\n'
        '                "No such file or directory" in err or "Connection refused" in err)):\n',
        '        if "no server running" in err or "error connecting" in err:\n',
        BACKEND + "test_a_tmux_that_fails_for_another_reason_is_an_error_not_an_empty_fleet",
    ),
    (
        "text to type may start with dashes",
        "bin/tmux_backend.py",
        "        if arg in _PLAIN_FLAGS:\n            flags[arg] = True\n",
        '        if arg.startswith("--"):\n            flags[arg] = True\n',
        BACKEND + "test_text_that_starts_with_dashes_is_typed_not_read_as_a_flag",
    ),
    (
        "tmux commands go to the default server, not the tmux the caller runs in",
        "bin/tmux_backend.py",
        '    env = {name: value for name, value in os.environ.items() if name not in ("TMUX", "TMUX_PANE")}\n',
        "    env = dict(os.environ)\n",
        BACKEND + "test_commands_go_to_the_default_server_even_when_run_inside_another_tmux",
    ),
    (
        "a pane with two candidate sessions is given neither",
        "bin/tmux_backend.py",
        "        if len(candidates) != 1:\n            continue\n",
        "        if not candidates:\n            continue\n",
        BACKEND + "test_a_pane_with_two_candidate_sessions_gets_none",
    ),
    (
        "only the foreground job of a pane's terminal is its session",
        "bin/tmux_backend.py",
        "        if len(fields) == 4 and fields[1] == fields[2]:\n",
        "        if len(fields) == 4:\n",
        BACKEND + "test_a_claude_process_that_is_not_its_panes_foreground_job_is_not_a_session",
    ),
    (
        "a session is found by the terminal its process is on",
        "bin/tmux_backend.py",
        '"tty": row["pane_tty"].replace("/dev/", "", 1), "type": "terminal",\n',
        '"tty": row["pane_tty"], "type": "terminal",\n',
        BACKEND + "test_a_claude_session_is_placed_in_the_pane_its_process_runs_in",
    ),
    (
        "a turn shorter than the wait between looks is still reported as finished",
        "bin/tmux_backend.py",
        'name = "agent.hook.Stop" if state == "idle" and (was == "busy" or now[key][1] > size) else None\n',
        'name = "agent.hook.Stop" if state == "idle" and was == "busy" else None\n',
        BACKEND + "test_events_report_a_finished_turn_and_a_session_that_starts_waiting",
    ),
    (
        "a session that keeps waiting is reported once",
        "bin/tmux_backend.py",
        '                name = "agent.hook.Notification" if was != "waiting" else None\n',
        '                name = "agent.hook.Notification"\n',
        BACKEND + "test_events_report_a_finished_turn_and_a_session_that_starts_waiting",
    ),
    (
        "a task started in tmux has its launch command run, so the agent receives its instruction",
        "bin/tmux_backend.py",
        '            send_text({"tmux_pane": pane_id}, command)\n            _must("send-keys", "-t", pane_id, "Enter")\n',
        '            send_text({"tmux_pane": pane_id}, command)\n',
        FLEET + "test_the_pulse_starts_a_task_in_tmux_and_its_agent_receives_the_instruction",
    ),
    (
        "the world scanner's session in a tmux pane comes from the agent's own list",
        "bin/tmux_backend.py",
        '            and isinstance(row.get("sessionId"), str) and row["sessionId"]]\n',
        '            and isinstance(row.get("sessionId"), str) and not row["sessionId"]]\n',
        FLEET + "test_the_world_scanner_lists_tmux_workspaces_and_verifies_the_claude_session_in_one",
    ),
    (
        "a workspace that is gone is told from one that is live, in cmux's own words",
        "bin/tmux_backend.py",
        '_NO_WORKSPACE = ("invalid_params", "Missing or invalid workspace_id")\n',
        '_NO_WORKSPACE = ("not_found", "No such workspace")\n',
        BACKEND + "test_tree_for_one_workspace_and_cmuxs_error_for_one_that_is_gone",
    ),
    (
        "open workspace never raises the cmux app when sessions are kept in tmux",
        "work_queue/server.py",
        '    for step in steps if in_tmux else [*steps, ["open", "-b", "com.cmuxterm.app"]]:\n',
        '    for step in [*steps, ["open", "-b", "com.cmuxterm.app"]]:\n',
        FLEET + "test_open_workspace_selects_the_tasks_tmux_window_and_never_raises_the_cmux_app",
    ),
    (
        "the doctor checks tmux when sessions are kept in tmux",
        "bin/assistant-doctor.py",
        "    if cmux_sessions.in_tmux():\n        return check_tmux(cmux)\n",
        "    if False:\n        return check_tmux(cmux)\n",
        FLEET + "test_the_doctor_checks_tmux_in_cmuxs_place",
    ),
)
