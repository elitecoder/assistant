"""Tests for the warm session's submission check and workspace liveness.

2026-09-27/28: warm sessions came up with the boot prompt typed but never
submitted, Slack messages sat in the prompt box for hours, and every refused
cmux connection was read as "workspace gone" and closed a healthy session.
These pin the pure pieces that now decide both: what's in the prompt box,
whether a transcript recorded a prompt, when to press Enter again, and whether
a cmux failure means the workspace is gone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import comms_session as cs

RULE = "─" * 60


def _screen(box_lines: list[str], history: list[str] | None = None) -> str:
    return "\n".join([*(history or []), RULE, *box_lines, RULE,
                      "  main │ ●1 │ context 11% │ $2.13 │ #c2f4fe01",
                      "  ⏵⏵ bypass permissions on (shift+tab to cycle)"])


# ─── input_box_text ─────────────────────────────────────────────────────────


def test_input_box_text_reads_a_wrapped_prompt():
    """The live stuck boot prompt from 2026-09-28 wrapped across two rows."""
    screen = _screen(["❯ Read /x/prompt.md in full and execute every instruction in it. [boot",
                      "  20260928T153605Z-7988]"])
    box = cs.input_box_text(screen)
    assert box == "Read /x/prompt.md in full and execute every instruction in it. [boot 20260928T153605Z-7988]"


def test_input_box_text_empty_box_is_empty_string_not_none():
    assert cs.input_box_text(_screen(["❯ "])) == ""


def test_input_box_text_ignores_earlier_prompts_in_the_scrollback():
    """A past prompt starts with ❯ too, but only the fenced region is the box."""
    screen = _screen(["❯ "], history=["❯ Okay, how many active workspaces do you see?",
                                      "⏺ Twelve."])
    assert cs.input_box_text(screen) == ""


def test_input_box_text_none_without_a_box():
    assert cs.input_box_text("just some output\nno rules here") is None
    assert cs.input_box_text(f"{RULE}\nDo you trust this folder?\n{RULE}") is None
    assert cs.input_box_text(f"{RULE}\n{RULE}") is None


# ─── box_holds ──────────────────────────────────────────────────────────────


def test_box_holds_matches_a_marker_split_by_a_line_wrap():
    box = "[slack channel=C1 msg_ts=17905639 89.005039 send_cli=x] hi"
    assert cs.box_holds(box, "msg_ts=1790563989.005039")


def test_box_holds_collapsed_paste():
    assert cs.box_holds("[Pasted text #1 +12 lines]", "msg_ts=1")


def test_box_holds_false_for_other_text_or_empty():
    assert not cs.box_holds("something the user is typing", "msg_ts=1790563989.005039")
    assert not cs.box_holds("", "msg_ts=1")
    assert not cs.box_holds(None, "msg_ts=1")


# ─── transcript_has_submission / find_submission ─────────────────────────────


def _jsonl(path: Path, recs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


MARK = "msg_ts=1790563989.005039"


def test_submission_counts_a_user_prompt(tmp_path):
    t = _jsonl(tmp_path / "t.jsonl", [
        {"type": "user", "entrypoint": "cli",
         "message": {"role": "user", "content": f"[slack channel=C1 {MARK}] Are you alive?"}}])
    assert cs.transcript_has_submission(t, MARK)


def test_submission_counts_text_blocks_and_queued_prompts(tmp_path):
    blocks = _jsonl(tmp_path / "a.jsonl", [
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": f"hi {MARK}"}]}}])
    queued = _jsonl(tmp_path / "b.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "content": f"hi {MARK}"}])
    assert cs.transcript_has_submission(blocks, MARK)
    assert cs.transcript_has_submission(queued, MARK)


def test_submission_ignores_tool_results_headless_runs_and_assistant_text(tmp_path):
    """The warm session greps logs that contain msg_ts markers, the proofgate
    Stop hook's headless run quotes its prompts, and the assistant may repeat
    one — none of those mean the prompt was submitted."""
    t = _jsonl(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": f"log line {MARK}"}]}},
        {"type": "user", "entrypoint": "sdk-cli",
         "message": {"role": "user", "content": f"verify this trace: {MARK}"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": MARK}]}},
        {"type": "queue-operation", "operation": "dequeue"},
        {"type": "user", "message": "not-a-dict " + MARK},
    ])
    assert not cs.transcript_has_submission(t, MARK)


def test_submission_skips_corrupt_lines_and_missing_files(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(f"{{broken {MARK}\n\"a string {MARK}\"\n")
    assert not cs.transcript_has_submission(t, MARK)
    assert not cs.transcript_has_submission(tmp_path / "missing.jsonl", MARK)


def test_submission_reads_only_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TRANSCRIPT_TAIL_BYTES", 200)
    rec = {"type": "user", "message": {"role": "user", "content": f"old {MARK}"}}
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(rec) + "\n" + ("x" * 400) + "\n")
    assert not cs.transcript_has_submission(t, MARK)


def test_submission_droid_schema(tmp_path):
    t = _jsonl(tmp_path / "t.jsonl", [
        {"type": "message", "message": {"role": "user", "content": f"go {MARK}"}}])
    assert cs.transcript_has_submission(t, MARK)


def _prompt(text: str) -> dict:
    return {"type": "user", "entrypoint": "cli", "message": {"role": "user", "content": text}}


def test_find_submission_returns_the_transcript_that_recorded_it(tmp_path):
    """Binding follows the marker, not the newest file: the newest file here is
    a different session's."""
    hit = _jsonl(tmp_path / "hit.jsonl", [_prompt(f"x {MARK}")])
    other = _jsonl(tmp_path / "other.jsonl", [_prompt("unrelated")])
    os.utime(hit, (1000, 1000))
    os.utime(other, (2000, 2000))
    assert cs.find_submission(tmp_path, MARK, since=0) == str(hit)


def test_find_submission_skips_old_files_subagents_and_missing_dirs(tmp_path):
    old = _jsonl(tmp_path / "old.jsonl", [_prompt(f"x {MARK}")])
    os.utime(old, (1000, 1000))
    _jsonl(tmp_path / "sess" / "subagents" / "a.jsonl", [_prompt(f"x {MARK}")])
    assert cs.find_submission(tmp_path, MARK, since=5000) is None
    assert cs.find_submission(tmp_path / "nope", MARK, since=0) is None


def test_find_submission_skips_files_it_cannot_stat(tmp_path):
    (tmp_path / "dangling.jsonl").symlink_to(tmp_path / "gone.jsonl")
    hit = _jsonl(tmp_path / "hit.jsonl", [_prompt(f"x {MARK}")])
    assert cs.find_submission(tmp_path, MARK, since=0) == str(hit)


def test_find_submission_finds_nested_session_transcripts(tmp_path):
    nested = _jsonl(tmp_path / "sess" / "main.jsonl", [_prompt(f"x {MARK}")])
    assert cs.find_submission(tmp_path, MARK, since=0) == str(nested)


def test_boot_instruction_is_unique_per_nonce():
    a = cs.boot_instruction(Path("/p.md"), "n1")
    assert a.startswith("Read /p.md in full and execute every instruction in it.")
    assert "[boot n1]" in a
    assert a != cs.boot_instruction(Path("/p.md"), "n2")


# ─── submit_until_confirmed ─────────────────────────────────────────────────


class FakeTerminal:
    """Records keystrokes; confirms after a given number of Enter presses."""

    def __init__(self, confirm_after_enters: int | None, box: str | None = "stuck msg_ts=1"):
        self.confirm_after = confirm_after_enters
        self.box = box
        self.typed = 0
        self.enters = 0
        self.now = 0.0

    def send_text(self):
        self.typed += 1

    def press_enter(self):
        self.enters += 1

    def read_box(self):
        return self.box

    def confirmed(self):
        return self.confirm_after is not None and self.enters >= self.confirm_after

    def sleep(self, sec):
        self.now += sec

    def clock(self):
        return self.now


def _submit(term: FakeTerminal, attempts=3, wait_sec=5):
    return cs.submit_until_confirmed(
        term.send_text, term.press_enter, term.read_box, term.confirmed, "msg_ts=1",
        attempts=attempts, wait_sec=wait_sec, sleep=term.sleep, clock=term.clock)


def test_submit_confirms_on_the_first_enter():
    term = FakeTerminal(confirm_after_enters=1)
    assert _submit(term)
    assert (term.typed, term.enters) == (1, 1)


def test_submit_presses_enter_again_when_the_box_still_holds_the_text():
    """The 2026-09-27 failure: Enter was lost and the text sat in the box."""
    term = FakeTerminal(confirm_after_enters=2)
    assert _submit(term)
    assert (term.typed, term.enters) == (1, 2), "text typed once, Enter pressed twice"


def test_submit_gives_up_after_the_attempt_budget():
    term = FakeTerminal(confirm_after_enters=None)
    assert not _submit(term, attempts=3)
    assert (term.typed, term.enters) == (1, 3)


def test_submit_never_presses_enter_on_a_box_without_the_marker():
    """If the box holds someone else's text (or nothing), a retry could submit
    the wrong thing — so no extra Enter, and no retyping."""
    for box in ("the user is typing here", "", None):
        term = FakeTerminal(confirm_after_enters=None, box=box)
        assert not _submit(term)
        assert (term.typed, term.enters) == (1, 1)


def test_submit_waits_the_full_window_before_retrying():
    term = FakeTerminal(confirm_after_enters=None)
    _submit(term, attempts=2, wait_sec=5)
    assert term.now >= 10


# ─── liveness ───────────────────────────────────────────────────────────────


def test_classify_tree_result():
    assert cs.classify_tree_result(0, "") == cs.ALIVE
    assert cs.classify_tree_result(1, "Error: invalid_params: Missing or invalid workspace_id") == cs.GONE
    # The 2026-09-27 stall signature: exit 1, but cmux never answered.
    refused = ("Error: Failed to connect to socket at /x/cmux.sock "
               "(Connection refused, errno 61)")
    assert cs.classify_tree_result(1, refused) == cs.UNKNOWN
    assert cs.classify_tree_result(-1, "timeout after 10s") == cs.UNKNOWN
    assert cs.classify_tree_result(1, None) == cs.UNKNOWN


def test_ref_listed_matches_whole_refs_only():
    listing = "  workspace:258  assistant-comms (warm) #ea290b [258]\n  workspace:1  Build [1]"
    assert cs.ref_listed(listing, "workspace:258")
    assert cs.ref_listed(listing, "workspace:1")
    assert not cs.ref_listed(listing, "workspace:25")
    assert not cs.ref_listed(listing, "workspace:2")


def test_resolve_workspace_state_retries_through_a_blip():
    answers = iter([cs.UNKNOWN, cs.ALIVE])
    slept: list = []
    assert cs.resolve_workspace_state(lambda: next(answers), attempts=3, retry_sec=2,
                                      sleep=slept.append) == cs.ALIVE
    assert slept == [2]


def test_resolve_workspace_state_returns_gone_at_once_and_unknown_when_silent():
    slept: list = []
    assert cs.resolve_workspace_state(lambda: cs.GONE, sleep=slept.append) == cs.GONE
    assert slept == []
    assert cs.resolve_workspace_state(lambda: cs.UNKNOWN, attempts=3, retry_sec=1,
                                      sleep=slept.append) == cs.UNKNOWN
    assert slept == [1, 1], "no sleep after the last try"
