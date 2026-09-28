"""Tests for comms_lib.py — the shared Slack comms helpers."""
from __future__ import annotations

import json
from pathlib import Path

import comms_lib as cl
import pytest
from assistant import slack


@pytest.fixture
def paths(tmp_path: Path, monkeypatch) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    return cl.Paths.from_env({
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_BIN_DIR": str(tmp_path / "bin"),
    })


def _write_config(paths: cl.Paths, **slack) -> None:
    paths.assistant_dir.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps({"slack": slack, "stale_heartbeat_sec": 1200}))


# ─── paths ────────────────────────────────────────────────────────────────

def test_paths_default_config_is_assistant_dir(paths: cl.Paths):
    assert paths.config == paths.assistant_dir / "config.json"
    assert paths.conversation == paths.comms_dir / "conversation.jsonl"
    assert paths.slack_cursor == paths.comms_dir / "slack.cursor"


def test_config_env_override_config_path(tmp_path: Path):
    custom = tmp_path / "elsewhere.json"
    p = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_CONFIG": str(custom)})
    assert p.config == custom


# ─── config + send-gate ─────────────────────────────────────────────────────

def test_config_load_reads_target_and_allowlist(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    cfg = cl.Config.load(paths.config, env={})
    assert cfg.target == "U123"
    assert cfg.allowed_targets == ("U123",)
    assert cfg.is_allowed("U123")
    assert not cfg.is_allowed("U999")


def test_config_ping_target_env_overrides_file(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    cfg = cl.Config.load(paths.config, env={"SLACK_PING_TARGET": "Cabc"})
    assert cfg.target == "Cabc"


def test_config_missing_file_raises(paths: cl.Paths):
    with pytest.raises(SystemExit):
        cl.Config.load(paths.config, env={})


def test_bot_token_from_env():
    assert cl.bot_token({"SLACK_BOT_TOKEN": "xoxb-x"}) == "xoxb-x"
    assert cl.bot_token({}) == ""


# ─── formatting ─────────────────────────────────────────────────────────────

def test_fmt_action_line_flags_screen_read():
    entry = {"kind": "ready_for_merge", "key": "workspace:5-ready_for_merge", "ws_ref": "ws:5",
             "outcome": "verified", "verified_via": "screen_read", "pulse_idx": 3,
             "td": "td-12", "evidence": "sent '/merge-when-ready' & <ok>"}
    # A verified step's evidence is machine detail: it leads the footer.
    assert cl.fmt_action_line(entry) == (
        "I asked a workspace to merge its PR.\n"
        "Heads up: I only confirmed this by reading the screen, which isn't reliable proof.\n"
        "_sent '/merge-when-ready' &amp; &lt;ok&gt; · ws:5 · ready_for_merge · "
        "workspace:5-ready_for_merge · td-12 · pulse 3_")


def test_fmt_action_line_observer_proof_has_no_warning():
    line = cl.fmt_action_line({"kind": "goal-edit", "outcome": "verified",
                               "verified_via": "observer"})
    assert line == "I updated your goals.\n_goal-edit_"


def test_fmt_action_line_failure_quotes_the_evidence():
    entry = {"kind": "self-update", "key": "self-update-fail-p3326", "ws_ref": "(launchd)",
             "outcome": "failed",
             "evidence": "fetch failed:\n\n  fatal: couldn't find remote ref " + "x" * 300}
    lines = cl.fmt_action_line(entry).splitlines()
    assert lines[0] == "I tried to update Assistant to the latest code, but it didn't work."
    assert lines[1] == "> fetch failed:"
    assert lines[2].startswith(">   fatal: couldn't find remote ref x") and lines[2].endswith("x…")
    assert lines[3] == "_(launchd) · self-update · self-update-fail-p3326_"


def test_fmt_action_line_maps_outcomes():
    upd = {"kind": "self-update"}
    assert cl.fmt_action_line({**upd, "outcome": "verified"}).startswith(
        "I updated Assistant to the latest code.")
    assert cl.fmt_action_line({**upd, "outcome": "rejected", "evidence": "no"}) == (
        "I tried to update Assistant to the latest code, but it was turned down.\n"
        "> no\n_self-update_")
    assert cl.fmt_action_line({**upd, "outcome": "skipped"}).startswith(
        "I didn't update Assistant to the latest code this time.")
    assert cl.fmt_action_line({**upd, "outcome": "odd<x>"}).startswith(
        "I tried to update Assistant to the latest code (result: odd&lt;x&gt;).")


def test_fmt_action_line_names_strategist_research_plainly():
    line = cl.fmt_action_line({
        "kind": "strategist-context-wrote", "key": "strategist:context-wrote:dec-ca7d",
        "ws_ref": "(strategist)", "outcome": "verified",
        "evidence": "pre-researched decision dec-ca7d context (draft-only, surfaced in brief)"})
    assert line.splitlines()[0] == (
        "I added background to your brief for a decision that's waiting on you.")
    assert "dec-ca7d" not in line.splitlines()[0], "the decision id is a ref: footer only"


def test_fmt_action_line_unknown_kind_and_bare_entry():
    assert cl.fmt_action_line({"kind": "brand-new-kind", "outcome": "verified"}) == (
        "I took an automatic step.\n_brand-new-kind_")
    # No evidence, key, ws, td, or pulse → no quote line and no empty refs.
    assert cl.fmt_action_line({}) == "I tried to take an automatic step (result: ?)."


def test_fmt_heartbeat_alert_stopped():
    body = cl.fmt_heartbeat_alert({"ws_ref": "(launchd)", "status": "running",
                                   "last_pulse_iso": "2026-09-28T10:02:00Z"}, 1500)
    assert body == ("*Assistant's main loop has stopped* — no run for 25m "
                    "(last run 2026-09-28T10:02:00Z). I'll post again when it's back.")


@pytest.mark.parametrize("status,words", [
    ("frozen", "it reports that it's frozen"),
    ("stale_world", "it's working from an out-of-date view of your workspaces"),
    ("respawn-requested", "it asked to be restarted"),
])
def test_fmt_heartbeat_alert_bad_status_in_words(status, words):
    body = cl.fmt_heartbeat_alert({"status": status,
                                   "last_pulse_iso": "2026-07-05T00:00:00Z"}, 720)
    assert body == (f"*Assistant's main loop needs a look* — {words}. Last run 12m ago "
                    f"(2026-07-05T00:00:00Z). I'll post again when it's back.")


def test_fmt_heartbeat_alert_missing_last_run():
    assert "(last run unknown)" in cl.fmt_heartbeat_alert({}, 60)


def test_fmt_heartbeat_recovered():
    assert cl.fmt_heartbeat_recovered({"last_pulse_iso": "2026-09-28T13:05:00Z"}, 3 * 3600) == (
        "*Assistant's main loop is running again* after 3h0m (latest run 2026-09-28T13:05:00Z).")
    assert cl.fmt_heartbeat_recovered({}, 1500) == (
        "*Assistant's main loop is running again* after 25m.")


def test_fmt_workspace_signal_handles_both_key_names():
    # cmux-watcher writes "signal"/"signal_type" — accept either.
    body = cl.fmt_workspace_signal({"ws_ref": "ws:2", "signal": "needs_input",
                                    "screen_snippet": "waiting for input"})
    assert body == "A workspace needs your input.\n> waiting for input\n_ws:2 · needs_input_"


def test_fmt_workspace_signal_question_leads_with_title_and_question():
    body = cl.fmt_workspace_signal({
        "ws_ref": "workspace:244", "signal_type": "needs_input",
        "pattern_matched": "AskUserQuestion", "ws_title": "Fix archself deferral door",
        "last_message": "Should I rebase or merge main?", "screen_snippet": "1. Rebase"})
    assert body == ("*Fix archself deferral door* is asking you: Should I rebase or merge main?\n"
                    "_workspace:244 · AskUserQuestion_")


def test_fmt_workspace_signal_question_without_text_falls_back_to_snippet():
    body = cl.fmt_workspace_signal({
        "ws_ref": "workspace:3", "signal_type": "needs_input",
        "pattern_matched": "AskUserQuestion", "screen_snippet": "1. Rebase\n2. Merge"})
    assert body == ("A workspace has a question for you.\n> 1. Rebase\n> 2. Merge\n"
                    "_workspace:3 · AskUserQuestion_")


def test_fmt_workspace_signal_prefers_last_message_over_snippet():
    body = cl.fmt_workspace_signal({
        "ws_ref": "workspace:244", "signal_type": "needs_input",
        "pattern_matched": "Notification", "ws_title": "Green E2E Suite",
        "last_message": "Can I run the full suite? It takes 40 min.",
        "screen_snippet": "✽ Boogieing…"})
    assert body == ("*Green E2E Suite* needs your input.\n"
                    "> Can I run the full suite? It takes 40 min.\n"
                    "_workspace:244 · Notification_")


def test_fmt_workspace_signal_headlines_by_signal():
    def first_line(signal_type):
        return cl.fmt_workspace_signal({"ws_title": "T", "signal_type": signal_type}).split("\n")[0]
    assert first_line("work_complete") == "*T* looks done."
    assert first_line("pattern_match") == "*T* showed something I watch for."
    assert first_line("mystery") == "*T* sent an update."


def test_fmt_workspace_signal_escapes_and_caps_dynamic_text():
    body = cl.fmt_workspace_signal({
        "ws_title": "a<b>", "signal_type": "work_complete", "last_message": "x&" + "y" * 600})
    assert body.startswith("*a&lt;b&gt;* looks done.\n> x&amp;")
    assert body.count("y") == 398 and body.endswith("y…")
    asked = cl.fmt_workspace_signal({"pattern_matched": "AskUserQuestion",
                                     "last_message": "<q>" + "z" * 600})
    assert asked.startswith("A workspace is asking you: &lt;q&gt;") and asked.count("z") == 397


def test_fmt_workspace_signal_title_star_cannot_break_bold():
    body = cl.fmt_workspace_signal({"ws_title": "Fix *all* flakes", "signal_type": "work_complete"})
    assert body == "*Fix all flakes* looks done."


def test_fmt_workspace_signal_bare_item_has_no_footer():
    assert cl.fmt_workspace_signal({}) == "A workspace sent an update."


_PARITY_ENTRIES = [
    {"kind": "ready_for_merge", "key": "k", "ws_ref": "ws:5", "outcome": "verified",
     "verified_via": "screen_read", "pulse_idx": 3, "td": "td-1", "evidence": "a & <b>"},
    {"kind": "strategist-context", "key": "strategist:context:d", "outcome": "verified"},
    {"kind": "self-update", "outcome": "failed", "evidence": "fetch failed\n\n" + "x" * 300},
    {"kind": "goal-edit", "outcome": "rejected"},
    {"kind": "policy-bootstrap-upgrade", "outcome": "skipped"},
    {"kind": "new-kind", "outcome": "weird"},
    {},
]
_PARITY_HEARTBEATS = [
    ({"status": "running", "last_pulse_iso": "2026-09-28T10:02:00Z"}, 1500),
    ({"status": "frozen"}, 60),
    ({"status": "respawn-requested", "last_pulse_iso": "x<y"}, 90000),
]


def test_slack_formatters_match_comms_lib():
    # The daemon package can't import bin/, so slack.py carries its own copy;
    # both Slack paths must still say the same thing.
    for entry in _PARITY_ENTRIES:
        assert slack.fmt_action_line(entry) == cl.fmt_action_line(entry)
    for hb, age in _PARITY_HEARTBEATS:
        assert slack.fmt_heartbeat_alert(hb, age) == cl.fmt_heartbeat_alert(hb, age)
        assert slack.fmt_heartbeat_recovered(hb, age) == cl.fmt_heartbeat_recovered(hb, age)


def test_strip_html():
    assert cl.strip_html("<b>hi</b> &amp; bye") == "hi & bye"


def test_fmt_age():
    assert cl.fmt_age(-5) == "0s"
    assert cl.fmt_age(45) == "45s"
    assert cl.fmt_age(120) == "2m"
    assert cl.fmt_age(3720) == "1h2m"
    assert cl.fmt_age(90000) == "1d"


def test_parse_duration():
    assert cl.parse_duration("30m") == 1800
    assert cl.parse_duration("2h") == 7200
    assert cl.parse_duration("10s") == 10
    assert cl.parse_duration("nope") is None
    assert cl.parse_duration("-5m") is None


# ─── ledger cursor ──────────────────────────────────────────────────────────

def test_ledger_cursor_initialize_skips_backlog(paths: cl.Paths):
    paths.ledger.write_text("line1\nline2\n")
    cl.initialize_cursor_if_missing(paths)
    assert cl.read_ledger_cursor(paths) == paths.ledger.stat().st_size
    assert cl.read_new_ledger_lines(paths) == []


def test_read_new_ledger_lines_reads_appends(paths: cl.Paths):
    paths.ledger.write_text("")
    cl.initialize_cursor_if_missing(paths)
    with open(paths.ledger, "a") as f:
        f.write(json.dumps({"key": "a", "kind": "cleanup"}) + "\n")
        f.write("garbage-not-json\n")
        f.write(json.dumps({"key": "b"}) + "\n")
    entries = cl.read_new_ledger_lines(paths)
    assert [e["key"] for e in entries] == ["a", "b"]
    assert cl.read_new_ledger_lines(paths) == []  # cursor advanced


def test_read_new_ledger_lines_handles_rotation(paths: cl.Paths):
    # Rotation is detected only when the file shrinks below the cursor (the
    # byte-cursor tail's inherent limit — matches the original comms_lib). Start
    # with a large file, then truncate to a smaller one.
    paths.ledger.write_text((json.dumps({"key": "old-and-long-entry-number-one"}) + "\n") * 5)
    cl.initialize_cursor_if_missing(paths)
    paths.ledger.write_text(json.dumps({"key": "fresh"}) + "\n")  # shrank below cursor
    entries = cl.read_new_ledger_lines(paths)
    assert [e["key"] for e in entries] == ["fresh"]


# ─── slack cursor ───────────────────────────────────────────────────────────

def test_slack_cursor_roundtrip(paths: cl.Paths):
    assert cl.read_slack_cursor(paths) == "0"
    cl.write_slack_cursor(paths, "1700000000.000200")
    assert cl.read_slack_cursor(paths) == "1700000000.000200"


# ─── proposals queue (delivery high-water mark) ─────────────────────────────

def _write_proposals(paths: cl.Paths, *entries: dict) -> None:
    paths.assistant_dir.mkdir(parents=True, exist_ok=True)
    paths.proposals.write_text(
        "".join(json.dumps(e) + "\n" for e in entries))


def _lesson(pid: str, status: str = "pending", **extra) -> dict:
    return {"id": pid, "ts": pid, "type": "lesson", "status": status,
            "trigger": f"trigger-{pid}", "rule": f"rule-{pid}",
            "target": "assistant", "scope": "general", **extra}


def test_proposals_cursor_initialize_skips_backlog(paths: cl.Paths):
    # A month-old backlog must NOT deliver — the cursor jumps to the newest id.
    _write_proposals(paths,
                     _lesson("2026-06-08T05:14:26.000000Z"),
                     _lesson("2026-06-10T18:00:29.000000Z"))
    cl.initialize_proposals_cursor_if_missing(paths)
    assert cl.read_proposals_cursor(paths) == "2026-06-10T18:00:29.000000Z"
    assert cl.read_new_proposals(paths) == [], "backlog skipped on first run"


def test_proposals_cursor_initialize_empty_when_no_file(paths: cl.Paths):
    cl.initialize_proposals_cursor_if_missing(paths)
    assert cl.read_proposals_cursor(paths) == ""
    # A later proposal is fresh (cursor is "").
    _write_proposals(paths, _lesson("2026-07-08T10:00:00.000000Z"))
    fresh = cl.read_new_proposals(paths)
    assert [e["id"] for e in fresh] == ["2026-07-08T10:00:00.000000Z"]


def test_read_new_proposals_only_pending_lessons(paths: cl.Paths):
    _write_proposals(paths,
                     _lesson("2026-07-08T10:00:00.000000Z"),
                     _lesson("2026-07-08T10:00:01.000000Z", status="confirmed"),
                     {"id": "2026-07-08T10:00:02.000000Z", "type": "pattern",
                      "status": "pending"},
                     {"id": "2026-07-08T10:00:03.000000Z", "type": "lesson_audit",
                      "status": "pending"},
                     _lesson("2026-07-08T10:00:04.000000Z"))
    cl.write_proposals_cursor(paths, "")
    fresh = cl.read_new_proposals(paths)
    ids = [e["id"] for e in fresh]
    assert ids == ["2026-07-08T10:00:00.000000Z", "2026-07-08T10:00:04.000000Z"], \
        "only type=lesson & status=pending deliver; pattern/audit/confirmed skipped"


def test_read_new_proposals_respects_cursor_and_order(paths: cl.Paths):
    # File order is deliberately shuffled; delivery must be oldest-id first.
    _write_proposals(paths,
                     _lesson("2026-07-08T10:00:02.000000Z"),
                     _lesson("2026-07-08T10:00:00.000000Z"),
                     _lesson("2026-07-08T10:00:01.000000Z"))
    cl.write_proposals_cursor(paths, "2026-07-08T10:00:00.000000Z")
    fresh = cl.read_new_proposals(paths)
    assert [e["id"] for e in fresh] == \
        ["2026-07-08T10:00:01.000000Z", "2026-07-08T10:00:02.000000Z"]


def test_read_new_proposals_limit(paths: cl.Paths):
    _write_proposals(paths, *[_lesson(f"2026-07-08T10:00:0{i}.000000Z")
                              for i in range(5)])
    cl.write_proposals_cursor(paths, "")
    assert len(cl.read_new_proposals(paths, limit=2)) == 2


def test_read_new_proposals_does_not_advance_cursor(paths: cl.Paths):
    _write_proposals(paths, _lesson("2026-07-08T10:00:00.000000Z"))
    cl.write_proposals_cursor(paths, "")
    cl.read_new_proposals(paths)
    # Reading is side-effect-free — the caller advances only after a good send.
    assert cl.read_proposals_cursor(paths) == ""
    assert len(cl.read_new_proposals(paths)) == 1


def test_fmt_lesson_proposal_carries_id_and_confirm_hint(paths: cl.Paths):
    body = cl.fmt_lesson_proposal(
        _lesson("2026-07-08T10:00:00.000000Z", pattern_count=4))
    assert "2026-07-08T10:00:00.000000Z" in body, "id must travel in the message"
    assert "y" in body and "n" in body
    assert "seen 4" in body


# ─── threads.jsonl ──────────────────────────────────────────────────────────

def test_threads_append_and_lookup(paths: cl.Paths):
    cl.append_thread(paths, "assistant:close:ws:5", "1700.0001", "D42", "action",
                     clock=lambda: 1700)
    cl.append_thread(paths, "assistant:close:ws:5", "1700.0002", "D43", "action",
                     clock=lambda: 1701)
    by_ts = cl.lookup_thread_by_msg_ts(paths, "1700.0001")
    assert by_ts and by_ts["channel"] == "D42"
    by_key = cl.lookup_thread_by_ledger_key(paths, "assistant:close:ws:5")
    assert len(by_key) == 2
    assert cl.lookup_thread_by_msg_ts(paths, "nope") is None


# ─── conversation.jsonl ─────────────────────────────────────────────────────

def test_conversation_append_and_window(paths: cl.Paths):
    cl.append_conversation_turn(paths, "D42", "1.1", "in", "hi", clock=lambda: 1000)
    cl.append_conversation_turn(paths, "D42", "1.2", "out", "hey", kind="reply",
                                reply_to="1.1", clock=lambda: 1001)
    cl.append_conversation_turn(paths, "D99", "9.1", "in", "other channel", clock=lambda: 1002)
    rows = cl.read_conversation_window(paths, "D42", now=lambda: 1002)
    assert [r["text"] for r in rows] == ["hi", "hey"]
    assert rows[1]["reply_to"] == "1.1"


def test_conversation_window_age_bound(paths: cl.Paths):
    cl.append_conversation_turn(paths, "D42", "1.1", "in", "old", clock=lambda: 0)
    cl.append_conversation_turn(paths, "D42", "1.2", "in", "new", clock=lambda: 10000)
    rows = cl.read_conversation_window(paths, "D42", max_age_sec=100, now=lambda: 10000)
    assert [r["text"] for r in rows] == ["new"]


def test_conversation_window_turn_bound(paths: cl.Paths):
    for i in range(30):
        cl.append_conversation_turn(paths, "D42", f"1.{i}", "in", f"m{i}", clock=lambda: 1000)
    rows = cl.read_conversation_window(paths, "D42", max_turns=5, now=lambda: 1000)
    assert [r["text"] for r in rows] == [f"m{i}" for i in range(25, 30)]


def test_conversation_bad_direction_raises(paths: cl.Paths):
    with pytest.raises(ValueError):
        cl.append_conversation_turn(paths, "D42", "1.1", "sideways", "x")


# ─── context measurement ────────────────────────────────────────────────────

def test_read_context_tokens_sums_last_usage(tmp_path: Path):
    t = tmp_path / "transcript.jsonl"
    t.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}}) + "\n"
        + json.dumps({"message": {"usage": {"input_tokens": 100,
                                            "cache_creation_input_tokens": 50,
                                            "cache_read_input_tokens": 350}}}) + "\n")
    assert cl.read_context_tokens(t) == 500


def test_context_fraction():
    assert cl.context_fraction(500_000) == 0.5
    assert cl.context_fraction(None) == 0.0


def test_should_clear_threshold_via_fraction(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 600_000}}}) + "\n")
    assert cl.context_fraction(cl.read_context_tokens(t)) >= 0.5


# ─── send_notification ──────────────────────────────────────────────────────

def test_send_notification_calls_slack_send_with_target(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    captured = {}

    def runner(argv):
        captured["argv"] = argv
        class R:
            returncode = 0
        return R()

    ok = cl.send_notification("hello", paths.config, Path("/repo/bin"),
                              kind="action", runner=runner)
    assert ok
    argv = captured["argv"]
    assert "slack-send.py" in argv[1]
    assert "--channel" in argv and "U123" in argv
    assert "--kind" in argv and "action" in argv


def test_send_notification_no_target_returns_false(paths: cl.Paths):
    _write_config(paths, allowed_targets=[])  # no target
    assert cl.send_notification("x", paths.config, Path("/repo/bin")) is False
