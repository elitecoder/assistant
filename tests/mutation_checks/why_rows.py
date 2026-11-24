"""Checks for "Why is this here?": the sentences (src/assistant/why.py), the route (work_queue/server.py), the time a row
was first seen (work_queue/traces.py), the telemetry read that tells "nothing" from "can't read", and the page's link and
block (work_queue/page.py).

Every mutant runs for real, and none can reach outside a test's temporary folder: the tests they name keep HOME in a
folder under `tmp_path` (the `home` and `wq` fixtures) and serve the page from stand-ins."""

W = "src/assistant/why.py"
S = "work_queue/server.py"
TR = "work_queue/traces.py"
TE = "src/assistant/telemetry.py"
P = "work_queue/page.py"
T = "tests/test_why.py::"
TS = "tests/test_why_server.py::"
TB = "tests/test_work_queue_browser.py::"

CHECKS = (
    (
        "an unsorted row says its reason",
        W,
        '"spend_limit": "today\'s spend limit for sorting was reached"',
        '"spend_limit": "the model call failed"',
        T + "test_an_unsorted_row_says_why_in_one_clause_for_each_reason",
    ),
    (
        "only fifty steps are shown",
        W,
        "SHOWN = 50 ",
        "SHOWN = 500 ",
        T + "test_only_the_newest_fifty_steps_are_shown_and_the_rest_are_counted",
    ),
    (
        "the newest steps are the ones kept",
        W,
        "    shown = steps[-SHOWN:]",
        "    shown = steps[:SHOWN]",
        T + "test_only_the_newest_fifty_steps_are_shown_and_the_rest_are_counted",
    ),
    (
        "the telemetry copy of a change isn't shown beside the ledger's",
        W,
        'COPIES = ("effect", "feedback")',
        'COPIES = ("feedback",)',
        T + "test_a_full_read_gathers_the_three_records_and_leaves_the_telemetry_copies_out",
    ),
    (
        "a store that can't be read is not called empty",
        W,
        '        unreadable = state not in ("ok", "missing")',
        "        unreadable = False",
        T + "test_a_store_that_is_there_but_cant_be_read_is_unreadable_and_never_nothing_recorded",
    ),
    (
        "a record that can't be loaded makes the answer unreadable",
        W,
        "        effects = list(ledger.history(trace=trace, limit=EFFECTS_READ))\n    except Exception:  # noqa: BLE001\n        unreadable = True",
        "        effects = list(ledger.history(trace=trace, limit=EFFECTS_READ))\n    except Exception:  # noqa: BLE001\n        unreadable = False",
        T + "test_a_record_that_cannot_be_loaded_makes_the_answer_unreadable_and_not_an_error",
    ),
    (
        "a name from a record is shown only when it is a name",
        W,
        'NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}")',
        'NAME = re.compile(r".*")',
        T + "test_a_sorted_row_names_the_model_and_the_rules_only_when_they_are_names",
    ),
    (
        "a send is said once",
        W,
        'if (sent and signal in ("send", "send_handsoff"))',
        'if (False and signal in ("send", "send_handsoff"))',
        T + "test_a_send_is_said_once_by_the_task_and_a_change_is_said_once_by_the_ledger",
    ),
    (
        "a change that undoes another says which",
        W,
        '        undone = EFFECTS.get(classes.get(e.get("undoes")), ("", "", "", ""))[3]',
        '        undone = ""',
        T + "test_a_change_that_undoes_another_says_which_and_a_class_nobody_knows_gets_a_general_sentence",
    ),
    (
        "a change with no answer for ten minutes is no longer in progress",
        W,
        'now - float(e.get("t") or 0) >= IN_FLIGHT_SEC',
        'now - float(e.get("t") or 0) >= IN_FLIGHT_SEC * 100',
        T + "test_a_change_with_no_answer_may_or_may_not_have_happened_and_one_still_going_is_in_progress",
    ),
    (
        "an error text nobody listed is never repeated",
        W,
        '        said = ERRORS.get(error) or (f"error {m.group(1)}" if (m := HTTP.fullmatch(error)) else "")',
        '        said = ERRORS.get(error) or error',
        T + "test_a_failed_change_says_how_and_never_repeats_an_error_text_it_doesnt_know",
    ),
    (
        "a row from before the record says so",
        W,
        '"note": "" if first_known else BEFORE_RECORDS',
        '"note": ""',
        T + "test_a_row_with_an_id_and_nothing_recorded_says_so_in_its_own_words_and_a_row_from_before_says_that",
    ),
    (
        "the telemetry read says a newer store is newer",
        TE,
        '    except _NewerSchema:\n        return "newer"',
        '    except _NewerSchema:\n        return "ok"',
        T + "test_the_store_state_tells_each_case_apart_and_events_still_gives_an_empty_list_for_all_of_them",
    ),
    (
        "the telemetry read says a missing store is missing",
        TE,
        '        if not os.path.isfile(_path(_DB)):\n            return "missing"\n        conn = _connect(create=False)\n        try:\n            conn.execute("PRAGMA query_only=ON")\n            conn.execute("SELECT 1',
        '        if not os.path.isfile(_path(_DB)):\n            return "ok"\n        conn = _connect(create=False)\n        try:\n            conn.execute("PRAGMA query_only=ON")\n            conn.execute("SELECT 1',
        T + "test_the_store_state_tells_each_case_apart_and_events_still_gives_an_empty_list_for_all_of_them",
    ),
    (
        "a busy store is unreadable, not empty",
        TE,
        '        return "missing" if "no such table" in str(exc) else "unreadable"',
        '        return "missing" if "no such table" in str(exc) else "ok"',
        T + "test_a_store_that_stays_busy_is_unreadable_not_empty",
    ),
    (
        "only an id made now starts the first-seen time",
        TR,
        "now() if (not found or chosen == made) else None",
        "now()",
        T + "test_a_row_given_the_id_a_sent_entry_holds_has_no_first_time",
    ),
    (
        "a first-seen time must be a number",
        TR,
        "type(found) in (int, float) and 0 < found < 1e12",
        "found is not None and 0 < found < 1e12",
        T + "test_a_row_made_now_has_a_first_time_and_an_older_file_loads_without_one",
    ),
    (
        "a row keeps its first-seen time when it is stamped again",
        TR,
        "        if when is not None:\n            entry[\"first\"] = when",
        "        if when is not None and own is None:\n            entry[\"first\"] = when",
        T + "test_a_first_time_survives_every_later_write_and_pruning_and_a_new_key_in_the_group_takes_it",
    ),
    (
        "the route answers",
        S,
        '        if route == "/api/why":',
        '        if route == "/api/whyx":',
        TS + "test_a_row_with_a_full_trail_answers_every_step_in_order_in_plain_sentences",
    ),
    (
        "the answer starts with when the row was first seen",
        S,
        "        return why.read(trace, traces.first_seen(row))",
        "        return why.read(trace, None)",
        TS + "test_a_row_with_a_full_trail_answers_every_step_in_order_in_plain_sentences",
    ),
    (
        "a result answers with the row's trail",
        S,
        '            trace = (traces.lookup(row) if row else "") or (todostore.trace_of(task) if task else "")',
        '            trace = (todostore.trace_of(task) if task else "")',
        TS + "test_a_ready_for_you_result_answers_with_the_steps_of_the_row_it_came_from",
    ),
    (
        "a broken record answers in words",
        S,
        '        traceback.print_exc()\n        return why.empty("unreadable")',
        '        traceback.print_exc()\n        raise',
        TS + "test_a_store_that_cant_be_read_is_unreadable_and_a_broken_task_file_does_not_make_an_error_page",
    ),
    (
        "a key that is too long or not printable gets the none answer",
        S,
        '    if not key or len(key) > KEY_MAX or not key.isprintable():\n        return why.empty("none")',
        '    if not key:\n        return why.empty("none")',
        TS + "test_a_key_that_is_too_long_or_not_printable_is_refused_even_when_the_registry_holds_it",
    ),
    (
        "the block shows text, never markup",
        P,
        "const e=document.createElement(tag);if(cls)e.className=cls;e.textContent=text;",
        "const e=document.createElement(tag);if(cls)e.className=cls;e.innerHTML=text;",
        TB + "test_the_why_block_says_loading_while_it_waits_and_shows_everything_as_text",
    ),
    (
        "the link isn't on a draft",
        P,
        "if(k!=='page'&&k!=='draft'){h.why=",
        "if(k!=='page'){h.why=",
        TB + "test_the_why_link_is_on_every_kind_of_item_that_has_tell_assistant_and_nowhere_else",
    ),
    (
        "the link says whether the block is open",
        P,
        "h.why.setAttribute('aria-expanded',open)",
        "h.why.setAttribute('aria-expanded',false)",
        TB + "test_the_why_link_opens_only_on_a_click_and_shows_the_steps_in_order_with_their_times",
    ),
    (
        "Escape closes the block",
        P,
        "box.onkeydown=ev=>{if(ev.key==='Escape')whyClose(s)}",
        "box.onkeydown=ev=>{if(ev.key==='Never')whyClose(s)}",
        TB + "test_the_why_block_closes_by_a_second_click_by_escape_and_by_close_and_focus_goes_back_to_the_link",
    ),
    (
        "focus goes back to the link",
        P,
        "function whyClose(s){delete WHY[s.dataset.fp];whyPaint();FBX.get(s).why.focus()}",
        "function whyClose(s){delete WHY[s.dataset.fp];whyPaint()}",
        TB + "test_the_why_block_closes_by_a_second_click_by_escape_and_by_close_and_focus_goes_back_to_the_link",
    ),
    (
        "a second click closes the block",
        P,
        "if(u&&u.where===fbWhere(s))return whyClose(s);",
        "if(false)return whyClose(s);",
        TB + "test_the_why_block_closes_by_a_second_click_by_escape_and_by_close_and_focus_goes_back_to_the_link",
    ),
    (
        "an open block holds off the reload",
        P,
        "function reloadIfIdle(){if(Object.keys(WHY).length)return toast(",
        "function reloadIfIdle(){if(false)return toast(",
        TB + "test_an_open_why_block_holds_off_the_automatic_reload_and_a_closed_one_does_not",
    ),
    (
        "the block shows what the route answered",
        P,
        "const sig=JSON.stringify([u.status,u.data]);if(box.dataset.sig!==sig)",
        "const sig=JSON.stringify([u.status,u.data]);if(false)",
        TB + "test_the_why_block_says_loading_while_it_waits_and_shows_everything_as_text",
    ),
    (
        "a request that fails says so in the block",
        P,
        "()=>{if(WHY[p]===mine)mine.status='failed'}",
        "()=>{if(WHY[p]===mine)mine.status='loading'}",
        TB + "test_the_why_block_says_each_empty_state_and_a_failed_request_in_the_block",
    ),
    (
        "the Close button is as readable as the rest",
        P,
        ".btn.why-close{margin-top:4px;color:var(--text-2)}",
        ".btn.why-close{margin-top:4px;color:var(--muted);opacity:.6}",
        TB + "test_the_why_link_and_block_reach_4_5_to_1_on_the_pages_own_backgrounds",
    ),
    (
        "on a phone the link keeps the Tell Assistant line",
        P,
        ".sf-main .fb-open,.sf-main .why-link{position:relative;padding:0;margin-top:-4px}",
        ".sf-main .fb-open{position:relative;padding:0;margin-top:-4px}",
        TB + "test_the_closed_why_link_sits_on_the_tell_assistant_line_and_adds_no_height_unless_that_line_is_full",
    ),
)
