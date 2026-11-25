"""Checks for: a typing is held and counted by what is typed (for the sources whose text a person or an outside
script chooses), a task id the record can't hold doesn't stop a typing, and "run now" types what a proposal says."""

SE = "src/assistant/session_effects.py"
E = "src/assistant/effects.py"
FB = "src/assistant/feedback.py"
SEND = "bin/cmux-send.py"
TS = "bin/todo-server.py"
H = "tests/test_typing_holds_by_text.py::"
HS = "tests/test_typing_holds_by_text_send.py::"
P = "tests/test_session_effects_pulse.py::"

CHECKS = (
    (
        "a task id of another shape is left out of the record, not refused",
        SE,
        '    if task and effects.TARGET_KEYS["task"].fullmatch(task):  # a task like `td-auto-1a2b` has no id in a record',
        '    if task:  # a task like `td-auto-1a2b` has no id in a record',
        H + "test_a_task_id_the_record_cant_hold_is_left_out_and_the_typing_goes_ahead",
    ),
    (
        "a pulse task with an auto id is typed into and not parked",
        SE,
        '    if task and effects.TARGET_KEYS["task"].fullmatch(task):  # a task like `td-auto-1a2b` has no id in a record',
        '    if task:  # a task like `td-auto-1a2b` has no id in a record',
        P + "test_a_pulse_task_with_an_auto_id_is_typed_into_recorded_and_not_parked",
    ),
    (
        "the text digest is part of the key for the sources whose text is chosen each time",
        SE,
        '    keyed = {**target, "msg_digest": digest} if digest else target  # what is counted and held',
        '    keyed = target  # what is counted and held',
        H + "test_four_different_texts_in_half_an_hour_are_all_typed",
    ),
    (
        "the same text is still held after an unconfirmed typing",
        SE,
        '_held_by_unknown(keyed, now):',
        '_held_by_unknown({"source": source, "session": session}, now):',
        H + "test_the_same_text_is_still_stopped_the_fourth_time",
    ),
    (
        "a different text is typed after an unconfirmed one",
        SE,
        '_held_by_unknown(keyed, now):',
        '_held_by_unknown(target, now):',
        H + "test_a_different_text_is_typed_after_one_nobody_could_tell_about_and_the_same_text_is_held",
    ),
    (
        "the digest is not part of the key for the other sources",
        SE,
        'digest = text_digest if source in DIGEST_SOURCES and',
        'digest = text_digest if True and',
        H + "test_every_other_source_is_still_limited_by_source_and_session_whatever_digest_it_is_given",
    ),
    (
        "the list of sources that carry a text digest is the three whose text is chosen each time",
        SE,
        'DIGEST_SOURCES = ("cli", "run-now", "merge")',
        'DIGEST_SOURCES = ("cli", "run-now")',
        HS + "test_four_different_texts_into_one_session_are_all_typed_by_the_script",
    ),
    (
        "a digest that is not 16 hex characters is not used and never refuses",
        SE,
        'effects.TARGET_KEYS["msg_digest"].fullmatch(text_digest) else ""',
        'True else ""',
        H + "test_a_digest_that_cant_be_used_goes_ahead_under_the_old_key_and_never_refuses",
    ),
    (
        "a digest that can't be made is empty, not an error",
        SE,
        '    except Exception:  # noqa: BLE001 - a digest that can\'t be made never stops a typing\n        return ""',
        '    except ZeroDivisionError:  # noqa: BLE001 - a digest that can\'t be made never stops a typing\n        return ""',
        H + "test_a_digest_that_cant_be_made_is_empty_not_an_error",
    ),
    (
        "the digest is keyed by this install's secret",
        FB,
        '    return hmac.new(key, f"{kind}\\0{text}".encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]',
        '    return hashlib.sha256(f"{kind}\\0{text}".encode("utf-8", "replace")).hexdigest()[:16]',
        H + "test_the_digest_is_a_keyed_pointer_and_never_the_text",
    ),
    (
        "the digest is not the sender's pointer for the same words",
        FB,
        '{kind}\\0{text}".encode',
        '{text}".encode',
        H + "test_the_feedback_pointer_for_a_text_is_the_same_each_time_and_not_the_senders_pointer",
    ),
    (
        "the ledger records the digest it is given, as the target's own key",
        E,
        '        target = {**target, "msg_digest": text_digest}',
        '        target = {**target}',
        H + "test_the_ledger_takes_a_text_digest_only_through_its_own_argument_and_only_as_a_digest",
    ),
    (
        "the ledger takes a text digest only as 16 hex characters",
        E,
        'TARGET_KEYS["msg_digest"].fullmatch(text_digest)):\n            raise ValueError("text_digest must be 16 hex characters")',
        'TARGET_KEYS["msg_digest"].search(text_digest)):\n            raise ValueError("text_digest must be 16 hex characters")',
        H + "test_the_ledger_takes_a_text_digest_only_through_its_own_argument_and_only_as_a_digest",
    ),
    (
        "a digest never replaces a conversation's own",
        E,
        'if "msg_digest" in target or not (isinstance',
        'if not (isinstance',
        H + "test_the_ledger_takes_a_text_digest_only_through_its_own_argument_and_only_as_a_digest",
    ),
    (
        "the send script gives the ledger the digest of the text for the listed sources",
        SEND,
        'digest = session_effects.typed_digest(args.text) if args.source in session_effects.DIGEST_SOURCES else ""',
        'digest = ""',
        HS + "test_four_different_texts_into_one_session_are_all_typed_by_the_script",
    ),
    (
        "the send script gives no digest to the other sources",
        SEND,
        'if args.source in session_effects.DIGEST_SOURCES else ""',
        'if True else ""',
        H + "test_every_other_source_is_still_limited_by_source_and_session_whatever_digest_it_is_given",
    ),
    (
        "run now types a proposal of any length",
        TS,
        'if not isinstance(text, str) or not text:\n        return "run-now has no text to type',
        'if not isinstance(text, str) or not text or len(text) > 500:\n        return "run-now has no text to type',
        HS + "test_run_now_types_a_long_or_several_line_proposal_as_it_says_it",
    ),
    (
        "run now types a proposal with several lines and tabs",
        TS,
        'c not in "\\n\\t" and unicodedata',
        'c not in "" and unicodedata',
        HS + "test_run_now_types_a_long_or_several_line_proposal_as_it_says_it",
    ),
    (
        "run now says which hidden characters it refused",
        TS,
        'f"U+{ord(c):04X}" for c in hidden[:5]',
        '"U+0000" for c in hidden[:5]',
        HS + "test_run_now_still_refuses_characters_nobody_could_see_and_says_which",
    ),
    (
        "run now names at most five hidden characters",
        TS,
        'for c in hidden[:5]',
        'for c in hidden',
        HS + "test_run_now_names_at_most_five_hidden_characters",
    ),
    (
        "run now types nothing when a proposal has no text",
        TS,
        'if not isinstance(text, str) or not text:',
        'if not isinstance(text, str):',
        HS + "test_run_now_with_no_text_types_nothing",
    ),
)
