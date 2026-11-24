"""GET /api/why on the Work queue server: the steps one row went through, and nothing of what it said."""
from __future__ import annotations

import json
import sys
import time
import urllib.parse

import pytest

from assistant import effects, feedback, telemetry, todostore, why
from work_queue import config, refresh, server, traces
from work_queue_fakes import wq  # noqa: F401
from test_feedback_server import CAND, GMAIL_MARK, LINK, PR, SECRET, SENDER, TITLE, seed
from test_work_queue_actions import http, pro  # noqa: F401

FIRST = 1_795_000_000.0
READY = "https://work-queue.local/ready/td-041"
MARKERS = ["MARKERTITLE" + "9" * 4, "MARKERSENDER" + "8" * 4, "MARKERDETAIL" + "7" * 4, "MARKERWORDS" + "6" * 4,
           "MARKERWHY" + "5" * 4, "MARKERLINK" + "4" * 4]
TOKENS = ["-".join(["xoxb", "1234567890", "abcdefghijklmnop"]), "_".join(["ghp", "A" * 36]),
          "".join(["AK", "IA", "IOSFODNN7EXAMPLE"]), "person@example.com", "/Users/someone/private/notes.txt"]


def ask(call, key):
    code, body, _ = call("/api/why?key=" + urllib.parse.quote(key, safe=""))
    return code, json.loads(body), body.decode()


def trail(wq, monkeypatch, hostile=False):
    """A row with a full trail: first seen, sorted, sent, started, reported done, checked, archived, thumbed. With
    `hostile`, every place content could come from holds a marker."""
    monkeypatch.setattr(traces, "now", lambda: FIRST)
    ids = seed(wq, monkeypatch)
    row = traces.lookup(LINK)
    task = {"id": "td-041", "status": "done", "trace": row, "title": MARKERS[0] if hostile else "T",
            "detail": ("\n\nRESULT: " + MARKERS[2] + " " + MARKERS[4]) if hostile else "x", "url": LINK}
    wq.todo.write_text(json.dumps({"items": [task], "completed": [], "removed": []}))
    config.write_json(wq.data / "queued.json", {LINK: {"id": "td-041", "at": "2026-11-20T00:00:00Z", "mode": "assist", "trace": row}})
    if hostile:
        config.write_json(refresh.rowctx_path(), {LINK: {"source": "slack", "who": feedback.who_pointer(MARKERS[1] + SENDER)}})
    sub = "row:" + "1" * 16
    telemetry.emit("triage_verdict", row, sub, "work-queue", verdict="you", lane="do_first", model="claude-test-1",
                   prompt_version="rubric-1a2b3c4d", source="slack")
    telemetry.emit("task", row, "td-41", "work-queue", state="open", origin="work-queue", mode="assist")
    telemetry.emit("dispatch", row, "td-41", "pulse", outcome="failed", ok=False, reason="session_tool_down")
    telemetry.emit("dispatch", row, "td-41", "pulse", outcome="done", ok=True, workspace="workspace:1000010013", confirmed=True)
    telemetry.emit("task", row, "td-41", "pulse", state="in-progress", prev_state="open")
    telemetry.emit("task", row, "td-41", "cli", state="done", prev_state="in-progress")
    telemetry.emit("check", row, "td-41", "work-queue", name="pr-merged", outcome="done", ok=True)
    ef = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                       {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"}, trace=row, component="work-queue")
    effects.finish(ef, "done", True)
    telemetry.emit("effect", row, ef, "work-queue", action="gmail.archive", outcome="done", ok=True)
    monkeypatch.setattr(feedback, "_now", lambda: time.time() + 3)  # its times are whole seconds: keep what you said last
    feedback.record("thumbs_down", "row:" + "1" * 16, "row", "work-queue", trace=row)
    feedback.record("tell", "row:" + "1" * 16, "row", "work-queue", trace=row, words=(" ".join(MARKERS[3:4] + TOKENS) if hostile else "hi"))
    return row, ids


def test_a_row_with_a_full_trail_answers_every_step_in_order_in_plain_sentences(wq, http, monkeypatch):
    pro(wq)
    row, _ = trail(wq, monkeypatch)
    code, answer, raw = ask(http, LINK)
    assert code == 200 and answer["state"] == "ok" and answer["more"] == 0 and answer["note"] == ""
    assert [(s["kind"], s["text"]) for s in answer["steps"]] == [
        ("first_seen", "Assistant first saw this."),
        ("sorted", "The model sorted it into Do first and suggested you do it yourself. Model: claude-test-1, rules: rubric-1a2b3c4d."),
        ("task", "You sent it to an agent for a first pass. It became task td-41."),
        ("start_failed", "Assistant tried to start an agent and couldn't: the program that runs agent sessions didn't answer. It tries again on the next round."),
        ("started", "Assistant started an agent on it in workspace:1000010013."),
        ("task", "The agent reported it finished."),
        ("check", "Assistant checked the result: the pull request is merged."),
        ("effect", "Archived in Gmail, because you pressed the button."),
        ("feedback", "You said this is wrong."),
        ("feedback", "You told Assistant something about it."),
    ]
    assert answer["steps"][0]["at"] == FIRST
    assert [s["at"] for s in answer["steps"]] == sorted(s["at"] for s in answer["steps"])
    assert set(answer) == {"state", "steps", "more", "message", "left_out", "note", "footer"}
    print(json.dumps(answer, indent=1))


def test_a_ready_for_you_result_answers_with_the_steps_of_the_row_it_came_from(wq, http, monkeypatch):
    pro(wq)
    trail(wq, monkeypatch)
    _, row_answer, _ = ask(http, LINK)
    code, answer, _ = ask(http, READY)
    assert code == 200 and answer == row_answer
    assert ask(http, "https://work-queue.local/ready/td-999")[1] == why.empty("none")
    store = json.loads(wq.todo.read_text())
    del store["items"][0]["trace"]  # the task has an id of its own, or none: the row's id is the one that counts
    wq.todo.write_text(json.dumps(store))
    assert ask(http, READY)[1] == row_answer


def test_an_email_and_a_row_waiting_for_approval_answer_by_their_own_key(wq, http, monkeypatch):
    pro(wq)
    monkeypatch.setattr(traces, "now", lambda: FIRST)
    seed(wq, monkeypatch)
    for key in (GMAIL_MARK, CAND, PR):
        _, answer, _ = ask(http, key)
        assert (answer["state"], [s["text"] for s in answer["steps"]]) == ("ok", ["Assistant first saw this."]), key


def test_a_row_from_before_the_record_says_so_at_the_end_of_its_answer(wq, http, monkeypatch):
    pro(wq)
    monkeypatch.setattr(traces, "now", lambda: FIRST)
    row, _ = trail(wq, monkeypatch)
    registry = json.loads(traces.path().read_text())
    del registry[LINK]["first"]
    traces.path().write_text(json.dumps(registry))
    _, answer, _ = ask(http, LINK)
    assert answer["state"] == "ok" and answer["steps"][0]["kind"] == "sorted"
    assert answer["note"] == "This row was already here when Assistant started keeping this record, so earlier steps aren't shown."


@pytest.mark.parametrize("key", ["", "https://unknown.example/none", "x" * 5000, "a\nb", "\x00", "https://work-queue.local/ready/td-1",
                                 "row:" + "1" * 16, "../../etc/passwd", "'; DROP TABLE events; --"])
def test_a_key_with_no_trace_an_unknown_one_and_a_malformed_one_all_get_the_same_none_answer(wq, http, monkeypatch, key):
    pro(wq)
    seed(wq, monkeypatch)
    code, answer, _ = ask(http, key)
    assert (code, answer) == (200, why.empty("none"))
    assert answer["message"] == "Assistant has no record for this yet. It starts one at the next refresh."


def test_a_request_with_no_key_or_a_key_twice_never_fails(wq, http, monkeypatch):
    pro(wq)
    seed(wq, monkeypatch)
    assert json.loads(http("/api/why")[1]) == why.empty("none")
    both = http("/api/why?key=" + urllib.parse.quote(LINK) + "&key=other")
    assert json.loads(both[1])["state"] == "ok", "the first key is the one asked about"


def test_a_store_that_cant_be_read_is_unreadable_and_a_broken_task_file_does_not_make_an_error_page(wq, http, monkeypatch):
    pro(wq)
    trail(wq, monkeypatch)
    (wq.home / ".assistant/telemetry/telemetry.db").write_bytes(b"junk" * 200)
    code, answer, _ = ask(http, LINK)
    assert (code, answer["state"]) == (200, "unreadable")
    assert answer["message"] == "Assistant can't read its record right now. Nothing is lost; try again in a minute."
    assert [s["kind"] for s in answer["steps"]] == ["first_seen", "effect", "feedback", "feedback"]
    wq.todo.write_text("{not json")
    code, answer, _ = ask(http, READY)
    assert (code, answer["state"]) == (200, "unreadable")


def test_with_the_telemetry_module_unimportable_the_route_still_answers_unreadable(wq, http, monkeypatch):
    pro(wq)
    trail(wq, monkeypatch)
    import assistant
    for name in ("telemetry", "effects", "feedback"):
        monkeypatch.delattr(assistant, name, raising=False)
        monkeypatch.setitem(sys.modules, "assistant." + name, None)
    code, answer, _ = ask(http, LINK)
    assert (code, answer["state"], answer["message"]) == (200, "unreadable", why.UNREADABLE)


@pytest.mark.real_mode
def test_the_route_is_refused_like_every_other_when_pro_is_off(wq, http, monkeypatch):
    seed(wq, monkeypatch)
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/config.json").write_text('{"mode": "default"}')
    code, body, _ = http("/api/why?key=" + urllib.parse.quote(LINK))
    assert code == 403 and "Pro mode is off" in json.loads(body)["error"]


def test_the_route_refuses_a_request_for_another_host(wq, monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    pro(wq)
    seed(wq, monkeypatch)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=30)
        conn.request("GET", "/api/why?key=" + urllib.parse.quote(LINK), headers={"Host": "evil.example"})
        reply = conn.getresponse()
        assert reply.status == 403 and json.loads(reply.read()) == {"error": "unknown host"}
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_handler_never_takes_the_servers_shared_lock(wq, monkeypatch):
    seed(wq, monkeypatch)
    with server.lock:  # held for the whole call: a read that took it would hang here
        assert server.why_of("key=" + urllib.parse.quote(LINK))["state"] == "ok"


def test_the_answer_holds_no_content_whatever_the_records_around_it_hold(wq, http, monkeypatch):
    pro(wq)
    trail(wq, monkeypatch, hostile=True)
    code, answer, raw = ask(http, LINK)
    assert code == 200 and answer["state"] == "ok" and len(answer["steps"]) == 10
    for marker in [*MARKERS, *TOKENS, SECRET, TITLE, SENDER, LINK, "slack.com", "me@example.com", "t1"]:
        assert marker not in raw, marker
    code, answer, raw = ask(http, READY)
    assert all(marker not in raw for marker in [*MARKERS, *TOKENS, SECRET])


def test_a_record_that_holds_a_hostile_name_shows_none_of_it(wq, http, monkeypatch):
    pro(wq)
    row, _ = trail(wq, monkeypatch)
    for fake in ({"kind": "check", "ts": FIRST, "subject": "", "fields": {"name": "<script>alert(1)</script>"}},
                 {"kind": "triage_verdict", "ts": FIRST, "subject": "", "fields": {"verdict": "you", "model": "a b\n<b>"}}):
        steps, _ = why.steps_of([fake])
        assert all("<" not in s["text"] for s in steps)


def test_a_key_that_is_too_long_or_not_printable_is_refused_even_when_the_registry_holds_it(wq, http, monkeypatch):
    pro(wq)
    row, _ = trail(wq, monkeypatch)
    registry = json.loads(traces.path().read_text())
    for odd in ("x" * 3000, "a\nb", "tab\there"):
        registry[odd] = {"trace": row, "seen": "2026-11-20", "first": FIRST}
    traces.path().write_text(json.dumps(registry))
    for odd in ("x" * 3000, "a\nb", "tab\there"):
        assert ask(http, odd)[1] == why.empty("none"), odd[:10]
