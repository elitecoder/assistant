"""Checks for typing into agent sessions through the effects ledger (src/assistant/session_effects.py and the paths
that use it)."""

SE = "src/assistant/session_effects.py"
E = "src/assistant/effects.py"
PULSE = "bin/pulse.py"
SEND = "bin/cmux-send.py"
MERGE = "bin/merge-pr-dispatch.py"
CS = "bin/comms_session.py"
CL = "bin/comms-listen.py"
TS = "bin/todo-server.py"
RS = "bin/cmux-restore-sessions.py"
T = "tests/test_session_effects.py::"
P = "tests/test_session_effects_pulse.py::"
S = "tests/test_session_effects_send.py::"
C = "tests/test_session_effects_comms.py::"
D = "tests/test_session_effects_todo_server.py::"
R = "tests/test_session_effects_restore.py::"

CHECKS = (
    (
        "the fourth identical typing in half an hour is refused: the loop limit counts the earlier ones",
        SE,
        "    if _loop_count(target, now) >= LOOP_MAX:",
        "    if _loop_count(target, now) > LOOP_MAX:",
        T + "test_the_fourth_identical_typing_in_half_an_hour_is_refused",
    ),
    (
        "a typing that may have landed is not repeated by something with no time of its own",
        SE,
        "    if anchor is None:\n        last = effects.last(CLASS, target)",
        "    if False:\n        last = effects.last(CLASS, target)",
        T + "test_a_typing_that_may_have_landed_is_not_repeated_by_something_with_no_time_of_its_own",
    ),
    (
        "nothing can be typed without an intent on disk that is still open",
        SE,
        "        if not _open(effect_id):",
        "        if False:",
        T + "test_nothing_can_be_typed_without_an_intent_on_disk_that_is_still_open",
    ),
    (
        "typing is refused when the intent can't be written",
        SE,
        "    except effects.Unrecorded:\n        raise Refused(\"unrecorded\", UNRECORDED) from None",
        "    except ZeroDivisionError:\n        raise Refused(\"unrecorded\", UNRECORDED) from None",
        T + "test_an_intent_that_cant_be_written_is_refused_with_a_sentence_a_person_can_read",
    ),
    (
        "a typing is allowed as of now, rounded up, so it never falls before the last result",
        SE,
        "_iso(math.ceil(now) if anchor is None else anchor)",
        "_iso(int(now) if anchor is None else anchor)",
        T + "test_a_typing_that_finished_a_moment_ago_does_not_block_the_next_one_in_the_same_second",
    ),
    (
        "a failed typing doesn't count toward the loop",
        SE,
        '        if found["outcome"] != "failed" and found["target"] == target:',
        '        if found["target"] == target:',
        T + "test_a_typing_that_failed_does_not_count_toward_the_loop",
    ),
    (
        "each Slack message or task is its own thing for the loop limit",
        SE,
        '        if found["outcome"] != "failed" and found["target"] == target:',
        '        if found["outcome"] != "failed" and found["target"].get("source") == target["source"]:',
        T + "test_each_task_and_each_slack_message_is_its_own_thing",
    ),
    (
        "the ledger accepts only a fixed name for where text came from",
        E,
        '"source": re.compile(r"[a-z][a-z-]{0,23}(?::[a-z][a-z-]{0,23})?"),',
        '"source": re.compile(r".{1,100}"),',
        T + "test_a_target_that_is_not_a_pointer_is_refused_and_nothing_is_written",
    ),
    (
        "dispatch types nothing into a workspace when the folder-trust answer can't be recorded",
        PULSE,
        "            if trust_ef is None:\n                return False",
        "            if trust_ef is None:\n                pass",
        P + "test_when_the_intent_cant_be_written_after_the_workspace_exists_nothing_is_typed",
    ),
    (
        "dispatch types nothing when the instruction can't be recorded",
        PULSE,
        "    if task_ef is None:\n        return False",
        "    if task_ef is None:\n        pass",
        P + "test_when_the_intent_cant_be_written_after_the_workspace_exists_nothing_is_typed",
    ),
    (
        "dispatch starts no workspace when the ledger can't be written at all",
        PULSE,
        "    if not session_effects.writable():\n        global _dispatch_unrecorded",
        "    if False:\n        global _dispatch_unrecorded",
        P + "test_with_the_ledger_unwritable_nothing_is_started_or_typed_and_the_task_stays_open",
    ),
    (
        "the dashboard says dispatch is paused when it can't record what it types",
        PULSE,
        "        if _dispatch_unrecorded:\n            held = ",
        "        if False:\n            held = ",
        P + "test_a_card_says_dispatch_is_paused_when_it_cant_be_recorded_and_it_goes_when_it_can",
    ),
    (
        "one refused dispatch stops the rest of the pulse's dispatches",
        PULSE,
        "            if _dispatch_unrecorded:\n                break",
        "            if False:\n                break",
        P + "test_a_card_says_dispatch_is_paused_when_it_cant_be_recorded_and_it_goes_when_it_can",
    ),
    (
        "dispatch records that the task's auto-start flag allowed the typing",
        PULSE,
        'session_effects.begin(ws_ref, source, kind="owner_flag", ref=todo_id,',
        'session_effects.begin(ws_ref, source, kind="standing", ref=todo_id,',
        P + "test_the_intent_is_on_disk_before_the_first_keystroke_and_names_the_task",
    ),
    (
        "dispatch records whether the transcript showed the instruction arrive",
        PULSE,
        "    typing.confirmed = submitted\n",
        "    typing.confirmed = True\n",
        P + "test_the_intent_is_on_disk_before_the_first_keystroke_and_names_the_task",
    ),
    (
        "the pulse says a nudge or /cleanup was allowed by the standing setting",
        PULSE,
        '        "--auth-kind", "standing",\n        "--auth-ref", source,',
        '        "--auth-kind", "tap",\n        "--auth-ref", source,',
        S + "test_the_pulse_says_it_is_the_standing_setting_and_which_fixed_nudge",
    ),
    (
        "the pulse records /cleanup and the stalled nudge under their own names",
        PULSE,
        '{"/cleanup": "cleanup", SAFE_STRANDED_NUDGE: "nudge:stranded"}',
        '{"/cleanup": "nudge:stranded", SAFE_STRANDED_NUDGE: "nudge:stranded"}',
        S + "test_the_pulse_says_it_is_the_standing_setting_and_which_fixed_nudge",
    ),
    (
        "the pulse runs merge dispatch as the standing setting",
        PULSE,
        '"--pr", str(pr), "--authorized-by", "standing"]',
        '"--pr", str(pr)]',
        S + "test_the_pulse_runs_merge_dispatch_as_standing_and_a_person_as_a_tap",
    ),
    (
        "merge dispatch passes what allowed the typing on to cmux-send",
        MERGE,
        '            "--auth-kind", authorized_by,',
        '            "--auth-kind", "tap",',
        S + "test_merge_dispatch_says_who_allowed_it_and_reports_a_refusal",
    ),
    (
        "cmux-send stops with exit 4 when the typing is refused",
        SEND,
        "    except session_effects.Refused as refusal:",
        "    except ZeroDivisionError as refusal:",
        S + "test_with_the_ledger_unwritable_nothing_is_typed_and_it_exits_4",
    ),
    (
        "cmux-send says a timeout may have landed",
        SEND,
        "        if rc == 124:\n            raise TimeoutError(method)",
        "        if False:\n            raise TimeoutError(method)",
        S + "test_the_result_says_what_happened",
    ),
    (
        "cmux-send records the intent for the workspace and source it was given",
        SEND,
        "            args.ws, args.source, kind=args.auth_kind, ref=args.auth_ref, task=args.task,",
        '            args.ws, "cli", kind=args.auth_kind, ref=args.auth_ref, task=args.task,',
        S + "test_the_intent_is_on_disk_before_the_first_keystroke_and_one_effect_is_made",
    ),
    (
        "a typing in the Slack session is closed with its result when the scope ends",
        CS,
        "        _scope.keys = outer\n        keys.finish()",
        "        _scope.keys = outer",
        C + "test_a_message_is_recorded_before_its_first_keystroke_with_the_message_time_as_its_allowance",
    ),
    (
        "a scope in the Slack session allows typing only into its own terminal",
        CS,
        "    if keys is None or keys.surface_id != surface_ref:",
        "    if keys is None:",
        C + "test_typing_outside_a_recorded_scope_is_refused_and_a_scope_is_per_thread",
    ),
    (
        "an error inside a typing scope leaves the typing unknown",
        CS,
        "        keys.raised = True  # an error nobody expected after it started: the text may be there",
        "        pass",
        C + "test_an_error_inside_a_scope_leaves_the_typing_unknown",
    ),
    (
        "a /clear that can't be recorded is skipped and the session kept",
        CS,
        "        log(f\"not clearing {sess['ws_ref']}: {refusal}\")\n        return sess\n",
        "        log(f\"not clearing {sess['ws_ref']}: {refusal}\")\n",
        C + "test_a_clear_that_cant_be_recorded_is_skipped_and_the_session_kept",
    ),
    (
        "the Slack session's boot prompt is typed only when it is recorded",
        CS,
        "    except session_effects.Refused as refusal:\n        log(f\"boot prompt not typed into",
        "    except ZeroDivisionError as refusal:\n        log(f\"boot prompt not typed into",
        C + "test_a_boot_prompt_that_cant_be_recorded_is_not_typed_and_the_spawn_is_abandoned",
    ),
    (
        "a Slack message is allowed by the owner's own message, as of its time",
        CL,
        'ref=f"slack:{ts}"[:100], anchor=float(ts) if real else None,',
        'ref=f"slack:{ts}"[:100], anchor=None,',
        C + "test_a_message_that_was_typed_is_never_typed_again_and_the_session_is_kept",
    ),
    (
        "only the owner's Slack messages are typed",
        CL,
        "        if author == owner:\n            return True",
        "        if True:\n            return True",
        C + "test_only_the_owners_message_is_typed",
    ),
    (
        "a channel with no known owner types nothing",
        CL,
        '    if _target(paths).startswith("D"):\n        return True',
        "    if True:\n        return True",
        C + "test_only_the_owners_message_is_typed",
    ),
    (
        "a message from someone else is never queued for typing",
        CL,
        "        if not typed_for(paths, rec, env):\n            continue",
        "        if False:\n            continue",
        C + "test_a_message_from_someone_else_is_recorded_as_received_but_never_queued",
    ),
    (
        "a Slack message that was typed leaves the queue and doesn't replace the session",
        CL,
        '_FINAL_REFUSALS = ("done", "unknown", "invalid", "unauthorized")',
        "_FINAL_REFUSALS = ()",
        C + "test_a_message_that_was_typed_is_never_typed_again_and_the_session_is_kept",
    ),
    (
        "a message that can't be recorded stays queued and is not typed",
        CL,
        "        except session_effects.Refused as refusal:\n            return _refused_delivery(paths, fresh, refusal)",
        "        except ZeroDivisionError as refusal:\n            return _refused_delivery(paths, fresh, refusal)",
        C + "test_with_the_ledger_unwritable_nothing_is_typed_and_the_message_stays_queued",
    ),
    (
        "run now refuses a session that isn't a workspace:N",
        TS,
        '                if not re.fullmatch(r"workspace:[0-9]{1,9}", str(ws_ref)):',
        "                if False:",
        D + "test_a_bad_session_or_text_is_refused_with_nothing_typed_and_the_proposal_untouched",
    ),
    (
        "run now refuses text longer than 500 characters",
        TS,
        "len(text) > RUN_NOW_MAX",
        "len(text) > 5 * RUN_NOW_MAX",
        D + "test_a_bad_session_or_text_is_refused_with_nothing_typed_and_the_proposal_untouched",
    ),
    (
        "run now refuses text of more than one line",
        TS,
        ' or "\\n" in text or "\\r" in text:',
        ":",
        D + "test_a_bad_session_or_text_is_refused_with_nothing_typed_and_the_proposal_untouched",
    ),
    (
        "run now doesn't mark a proposal as working when nothing was typed",
        TS,
        "                if sent.returncode != 0:\n                    return False,",
        "                if False:\n                    return False,",
        D + "test_when_the_script_refuses_the_proposal_is_not_marked_working_and_the_reason_is_shown",
    ),
    (
        "run now is recorded as a person's tap on its route",
        TS,
        '"--auth-kind", "tap",\n                         "--auth-ref", "/proposal/run-now"],',
        '"--auth-kind", "standing",\n                         "--auth-ref", "/proposal/run-now"],',
        D + "test_run_now_is_recorded_as_a_tap_on_the_route_and_types_through_the_one_script",
    ),
    (
        "restore types nothing when the intent can't be recorded",
        RS,
        '        print(f"  NOT TYPED: {refusal}")\n        return False',
        '        print(f"  NOT TYPED: {refusal}")',
        R + "test_with_the_ledger_unwritable_nothing_is_typed_and_the_reason_is_printed",
    ),
    (
        "restore is recorded as a person's tap",
        RS,
        'session_effects.begin("", "restore", kind="tap", ref="cli:restore"',
        'session_effects.begin("", "restore", kind="standing", ref="cli:restore"',
        R + "test_the_intent_is_on_disk_before_the_first_keystroke_and_is_a_persons_tap",
    ),
    (
        "restore's retry after a refused try is still one clean typing",
        RS,
        "keys.surface_id, keys.refused = panel_id, 0",
        "keys.surface_id = panel_id",
        R + "test_a_refused_first_try_is_retried_by_the_panels_id_and_is_still_one_clean_effect",
    ),
    (
        "the fence fails the build for any other file that types into a session",
        "tests/test_session_typing_fence.py",
        '            if node.value in METHODS or SHELL.search(node.value):',
        "            if False:",
        "tests/test_session_typing_fence.py::test_the_fence_sees_rpc_names_argument_lists_and_shell_strings_and_ignores_docs_and_other_calls",
    ),
)
