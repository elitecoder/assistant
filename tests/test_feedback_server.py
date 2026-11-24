"""The Work queue records what each action means (work_queue/server.py: `noting`, `give_feedback`), and never lets
recording get in the way of the action."""
from __future__ import annotations

import json
import re
import time
import types

import pytest

from assistant import feedback, pointers, todostore
from work_queue import config, page, refresh, server, traces
from work_queue_fakes import fake_tool, wq  # noqa: F401
from test_work_queue_actions import http, pro  # noqa: F401

LINK = "https://acme.slack.com/archives/C1/p111"
PR = "https://github.com/o/r/pull/1"
GKEY = "gmail:abc123"
GMAIL_MARK = server.EMAIL_MARK + GKEY
OKEY = "mail:outlook-1"
OMARK = server.EMAIL_MARK + OKEY
CAND = "gmail:cand9"
ACCT = "me@example.com"
SENDER = "Ana Example <ana@example.com>"
WHO = feedback.who_pointer(SENDER)
TITLE = "Quarterly budget review with Ana"
SECRET = "-".join(["xoxb", "1234567890", "abcdefghijklmnop"])


def ptr(key):
    return pointers.row_pointer(key)


def stored(home) -> list[dict]:
    path = home / ".assistant/feedback/feedback.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def seed(wq, monkeypatch=None, labels=()) -> dict:
    """The files a refresh leaves: ids for the rows, what the refresh saved about each, candidates, a Gmail thread, and
    a task for a sent row."""
    ids = iter(f"tr-{n:016x}" for n in range(1, 99))
    if monkeypatch:
        monkeypatch.setattr(traces, "new_trace_id", lambda: next(ids))
    traces.stamp([[LINK], [PR], [GMAIL_MARK], [OMARK, "https://outlook.office.com/o1"], [CAND]])
    config.write_json(refresh.rowctx_path(), {
        LINK: {"source": "slack", "who": WHO}, PR: {"source": "github"}, GMAIL_MARK: {"source": "gmail", "who": WHO},
        CAND: {"source": "gmail", "who": WHO}, OMARK: {"source": "outlook"}, "https://outlook.office.com/o1": {"source": "outlook"}})
    config.write_json(refresh.candidates_path(), {CAND: {"kind": "gmail", "account": ACCT, "msg_ids": ["m1"], "link": LINK, "mk": GMAIL_MARK}})
    wq.sources({"gmail": {"accounts": [ACCT]}})
    config.write_json(refresh.emails_path(), {GKEY: {"account": ACCT, "tid": "t1"}})
    fake_tool(wq.bin, "gog", [("thread modify", {}), ("thread get", {"thread": {"messages": [{"id": "m0", "labelIds": list(labels)}]}})])
    return {"sent": traces.lookup(LINK)}


def run(path, body):
    return server.ROUTES[path](body)


def mark(key, state, until=0):
    return "/api/mark", {"key": key, "state": state, "until": until}


ACTIONS = {
    "done": (*mark(LINK, "done"), [("done", "row", LINK)]),
    "snooze": (*mark(LINK, "snoozed", 5), [("snooze", "row", LINK)]),
    "drop": (*mark(PR, "dropped"), [("drop", "row", PR)]),
    "undo": (*mark(LINK, "clear"), [("undo", "row", LINK)]),
    "ready done": (*mark("https://work-queue.local/ready/td-041", "done"), [("kept_result", "result", "td-041")]),
    "ready undo": (*mark("https://work-queue.local/ready/td-041", "clear"), [("undo", "result", "td-041")]),
    "email snooze": (*mark(OMARK, "snoozed", 5), [("snooze", "email", OMARK)]),
    "batch": ("/api/mark", {"marks": {LINK: {"state": "done"}, PR: {"state": "snoozed", "until": 5}}},
              [("done", "row", LINK), ("snooze", "row", PR)]),
    "email undo is one": ("/api/mark", {"marks": {"https://outlook.office.com/o1": {"state": "clear"}, OMARK: {"state": "clear"}}},
                          [("undo", "email", OMARK)]),
    "send": ("/api/todo", {"key": LINK, "title": TITLE, "url": LINK, "note": "my words"}, [("send", "row", LINK)]),
    "send again": ("/api/todo", {"key": LINK, "title": TITLE, "url": LINK, "mode": "assist"}, [("send", "row", LINK)]),
    "hands-off": ("/api/todo", {"key": PR, "title": TITLE, "url": PR, "mode": "handsoff"}, [("send_handsoff", "row", PR)]),
    "send email": ("/api/todo", {"key": OMARK, "title": TITLE, "url": "https://outlook.office.com/o1"}, [("send", "email", OMARK)]),
    "archive": ("/api/gmail", {"key": GKEY, "action": "archive"}, [("archive", "email", GMAIL_MARK)]),
    "mark read": ("/api/gmail", {"key": GKEY, "action": "read"}, [("mark_read", "email", GMAIL_MARK)]),
    "unarchive": ("/api/gmail", {"key": GKEY, "action": "unarchive"}, [("undo", "email", GMAIL_MARK)]),
    "approve": ("/api/decide", {"keys": [CAND], "decision": "approve"}, [("approve", "candidate", CAND)]),
    "keep": ("/api/decide", {"keys": [CAND], "decision": "keep"}, [("keep", "candidate", CAND)]),
    "decide undo": ("/api/decide", {"keys": [CAND], "decision": "undo"}, [("undo", "candidate", CAND)]),
    "decide clear": ("/api/decide", {"keys": [CAND, CAND], "decision": "clear"},
                     [("undo", "candidate", CAND), ("undo", "candidate", CAND)]),
}
HANDLERS = {"/api/mark": server.set_marks, "/api/todo": server.queue_todo, "/api/gmail": server.gmail_action,
            "/api/decide": server.decide}


def tree(wq) -> dict:
    files = {str(p.relative_to(wq.root)): p.read_bytes() for p in sorted(wq.root.rglob("*"))
             if p.is_file() and "fakebin" not in p.parts and ".assistant/feedback" not in str(p)
             and ".assistant/telemetry" not in str(p) and p.suffix != ".lock"}
    return files


@pytest.mark.parametrize("name", ACTIONS)
def test_each_action_records_one_line_per_row_and_changes_nothing_else(wq, monkeypatch, name):
    path, body, expected = ACTIONS[name]
    monkeypatch.setattr(server, "now", lambda: "2026-11-24T10:00:00+00:00")
    monkeypatch.setattr(server, "secrets", types.SimpleNamespace(token_hex=lambda n: "ab" * n))
    monkeypatch.setattr(traces, "today", lambda: "2026-11-24")
    config.write_json(wq.data / "marks.json", {LINK: {"state": "done", "until": 0}, OMARK: {"state": "done", "until": 0}})
    seed(wq, monkeypatch, ["INBOX"] if name == "unarchive" else [])
    before = tree(wq)
    plain = HANDLERS[path](json.loads(json.dumps(body)))
    plain_files = tree(wq)
    for p in wq.root.rglob("*"):  # put the world back
        if p.is_file() and "fakebin" not in p.parts:
            p.unlink()
    for gone in (wq.home / ".assistant").glob("*"):
        if gone.is_dir():
            import shutil
            shutil.rmtree(gone)
    for rel, data in before.items():
        (wq.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (wq.root / rel).write_bytes(data)
    code, out = run(path, json.loads(json.dumps(body)))
    assert (code, {k: v for k, v in out.items() if k != "feedback"}) == plain
    assert tree(wq) == plain_files, "the action writes exactly the files it always wrote"
    got = stored(wq.home)
    assert out["feedback"] == {"recorded": True, "ids": [r["id"] for r in got]}
    assert [(r["signal"], r["kind"]) for r in got] == [(s, k) for s, k, _ in expected]
    for r, (signal, kind, key) in zip(got, expected):
        if kind == "result":
            assert (r["subject"], r["trace"]) == (key, todostore.trace_of(wq.items()[0]))
            continue
        assert (r["surface"], r["subject"], r.get("trace", "")) == ("work-queue", ptr(key), traces.lookup(key))
        want = {LINK: {"source": "slack", "who": WHO}, GMAIL_MARK: {"source": "gmail", "who": WHO},
                CAND: {"source": "gmail", "who": WHO}, PR: {"source": "github"}, OMARK: {"source": "outlook"}}
        assert r["context"] == want[key]


@pytest.mark.parametrize("name", ACTIONS)
def test_an_unwritable_store_never_stops_an_action_or_changes_what_it_writes(wq, monkeypatch, name):
    path, body, _ = ACTIONS[name]
    monkeypatch.setattr(server, "now", lambda: "2026-11-24T10:00:00+00:00")
    monkeypatch.setattr(server, "secrets", types.SimpleNamespace(token_hex=lambda n: "ab" * n))
    monkeypatch.setattr(traces, "today", lambda: "2026-11-24")
    seed(wq, monkeypatch, ["INBOX"] if name == "unarchive" else [])
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/feedback").write_text("a file where the folder belongs")
    code, out = run(path, json.loads(json.dumps(body)))
    assert code == 200 and out["feedback"] == {"recorded": False}
    ref_code, ref = HANDLERS[path](json.loads(json.dumps(body)))
    assert ref_code == 200 and {k: v for k, v in out.items() if k != "feedback"}.keys() == ref.keys()


def test_an_action_that_fails_records_nothing_and_adds_nothing(wq):
    seed(wq)
    assert run("/api/mark", {"key": "not a link", "state": "done"}) == (400, {"error": "unknown row or state"})
    assert run("/api/decide", {"keys": ["unknown"], "decision": "approve"})[0] == 400
    assert run("/api/gmail", {"key": GKEY, "action": "explode"})[0] == 400
    assert stored(wq.home) == []


def test_a_note_that_cannot_be_listed_is_reported_as_not_recorded(wq, monkeypatch):
    seed(wq)
    monkeypatch.setattr(server, "mark_notes", lambda body, out: 1 / 0)
    server.ROUTES["/api/mark"] = server.noting(server.set_marks, server.mark_notes)
    code, out = server.ROUTES["/api/mark"]({"key": LINK, "state": "done"})
    assert code == 200 and out["feedback"] == {"recorded": False} and out["marks"][LINK]["state"] == "done"


def test_a_request_never_names_the_trace_the_source_or_the_sender(wq):
    seed(wq)
    liar = {"trace": "tr-" + "ff" * 8, "source": "calendar", "who": "who:" + "ee" * 8, "subject": "row:" + "dd" * 8}
    run("/api/mark", {"key": LINK, "state": "done", **liar})
    code, out = run("/api/feedback", {"key": LINK, "kind": "row", "signal": "thumbs_down", **liar})
    assert code == 200
    for r in stored(wq.home):
        assert (r["subject"], r["trace"], r["context"]) == (ptr(LINK), traces.lookup(LINK), {"source": "slack", "who": WHO})


def test_a_row_the_refresh_no_longer_knows_is_recorded_with_empty_context(wq):
    code, out = run("/api/feedback", {"key": "https://gone.example/x", "kind": "row", "signal": "thumbs_down"})
    assert code == 200
    assert stored(wq.home)[0].keys() == {"id", "ts", "signal", "kind", "surface", "subject"}


# ─── the new route ───────────────────────────────────────────────────────────

def give(**body):
    return run("/api/feedback", {"kind": "row", "key": LINK, **body})


def test_a_thumbs_down_then_a_reason_then_a_take_back(wq):
    seed(wq)
    code, first = give(signal="thumbs_down")
    assert code == 200 and first["view"] == {"kind": "row", "down": first["id"], "reason": "", "told": ""}
    code, second = give(signal="thumbs_down", reason="not_mine", amends=first["id"])
    assert second["view"]["reason"] == "not_mine" and second["view"]["down"] == first["id"]
    code, third = give(signal="retract", amends=first["id"])
    assert (code, third["view"]) == (200, {})
    assert [r["signal"] for r in stored(wq.home)] == ["thumbs_down", "thumbs_down", "retract"]
    assert server.state()["feedback"] == {}


def test_tell_keeps_the_words_for_an_item_and_for_the_page(wq):
    seed(wq)
    code, out = give(signal="tell", words="  This is   Ana's, not mine ")
    assert code == 200 and out["view"]["told"] == "This is Ana's, not mine"
    code, out = run("/api/feedback", {"kind": "page", "key": "ignored", "signal": "tell", "words": "Too many Slack rows"})
    assert code == 200 and out["view"]["told"] == "Too many Slack rows"
    assert stored(wq.home)[1].keys() == {"id", "ts", "signal", "kind", "surface", "words"}
    assert set(server.state()["feedback"]) == {ptr(LINK), "page"}


def test_a_used_draft_is_about_the_draft_and_a_result_is_about_its_task(wq):
    seed(wq)
    config.write_json(wq.data / "queued.json", {PR: {"id": "td-041", "at": "t"}})
    wq.todo.write_text(json.dumps({"items": [{"id": "td-041", "status": "done", "trace": "tr-" + "12" * 8}]}))
    assert give(kind="draft", signal="used_draft")[0] == 200
    key = "https://work-queue.local/ready/td-041"
    code, out = run("/api/feedback", {"kind": "result", "key": key, "signal": "thumbs_down"})
    assert code == 200
    draft, result = stored(wq.home)
    assert (draft["kind"], draft["subject"]) == ("draft", ptr(LINK))
    assert (result["kind"], result["subject"], result["trace"], result["context"]) == \
        ("result", "td-041", "tr-" + "12" * 8, {"source": "github"})
    code, out = run("/api/feedback", {"kind": "result", "key": "https://work-queue.local/ready/td-999", "signal": "thumbs_down"})
    assert code == 200 and "trace" not in stored(wq.home)[2]


@pytest.mark.parametrize("body", [
    {"signal": "done"}, {"signal": "send"}, {"signal": "approve"}, {"signal": "nope"}, {"signal": None},
    {"signal": "thumbs_down", "kind": "candidate"}, {"signal": "thumbs_down", "kind": "page"},
    {"signal": "retract", "kind": "page"}, {"signal": "tell", "kind": "row", "words": "  "},
    {"signal": "tell", "kind": "row", "words": 5}, {"signal": "tell", "kind": "page", "words": ""},
    {"signal": "thumbs_down", "reason": "bad_draft"}, {"signal": "thumbs_down", "kind": "draft", "reason": "not_mine"},
    {"signal": "thumbs_down", "kind": "result", "reason": "not_important"},
    {"signal": "thumbs_down", "reason": "not_mine"}, {"signal": "thumbs_down", "reason": "not_mine", "amends": "fb-" + "0" * 16},
    {"signal": "retract"}, {"signal": "retract", "amends": 4}, {"signal": "thumbs_down", "key": "javascript:alert(1)"},
    {"signal": "thumbs_down", "key": 5}, {"signal": "thumbs_down", "kind": "result", "key": LINK},
    {"signal": "thumbs_down", "words": "extra words"}, {"signal": "thumbs_down", "reason": 3},
])
def test_the_route_refuses_what_the_page_may_not_claim_and_writes_nothing(wq, body):
    code, out = give(**body)
    assert code == 400 and out["error"], out
    assert stored(wq.home) == []


def test_a_reason_may_not_amend_another_items_thumbs_down(wq):
    first = give(signal="thumbs_down")[1]["id"]
    code, out = give(signal="thumbs_down", reason="not_mine", amends=first, key=PR)
    assert code == 400 and "no earlier record" in out["error"]


def test_every_reason_the_page_offers_is_accepted_for_its_kind(wq):
    for kind, reasons in feedback.REASONS_FOR.items():
        key = "https://work-queue.local/ready/td-041" if kind == "result" else LINK
        first = run("/api/feedback", {"kind": kind, "key": key, "signal": "thumbs_down"})[1]["id"]
        for reason in reasons:
            assert run("/api/feedback", {"kind": kind, "key": key, "signal": "thumbs_down", "reason": reason,
                                         "amends": first})[0] == 200


def test_a_store_that_cannot_be_written_is_an_error_the_page_can_show(wq):
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/feedback").write_text("in the way")
    code, out = give(signal="thumbs_down")
    assert code == 500 and "couldn’t save that" in out["error"] and "isn’t affected" in out["error"]


def test_a_failed_flush_answers_with_an_error_and_a_slow_one_makes_the_answer_wait(wq, monkeypatch):
    real = feedback._fsync

    def slow(fd):
        time.sleep(0.3)
        real(fd)
    monkeypatch.setattr(feedback, "_fsync", slow)
    started = time.monotonic()
    assert give(signal="thumbs_down")[0] == 200
    assert time.monotonic() - started >= 0.3

    def broken(fd):
        raise OSError(5, "disk")
    monkeypatch.setattr(feedback, "_fsync", broken)
    assert give(signal="thumbs_down")[0] == 500


def test_the_route_passes_the_same_request_checks_as_every_other(http, wq):
    seed(wq)
    pro(wq)
    body = {"key": LINK, "kind": "row", "signal": "thumbs_down"}
    code, raw, _ = http("/api/feedback", body)
    assert code == 200 and json.loads(raw)["id"].startswith("fb-")
    code, raw, _ = http("/api/feedback", "not an object")
    assert code == 400


@pytest.mark.real_mode
def test_in_default_mode_nothing_is_written_and_the_route_refuses(http, wq):
    assert http("/api/feedback", {"key": LINK, "kind": "row", "signal": "thumbs_down"})[0] == 403
    assert stored(wq.home) == []


# ─── what is kept, and what never is ─────────────────────────────────────────

def test_the_store_holds_no_title_sender_link_or_token_from_any_path(wq, monkeypatch):
    """Every path, with a row whose title, sender and link carry markers and a token shape."""
    link = f"https://acme.slack.com/archives/C1/p222?MARKERLINK&t={SECRET}"
    mk = server.EMAIL_MARK + "MARKERKEY"
    sender = f"MARKERSENDER <marker.sender@{'example'}.com>"
    traces.stamp([[link], [mk], ["cand-MARKER"]])
    config.write_json(refresh.rowctx_path(), {link: {"source": "slack", "who": feedback.who_pointer(sender)}})
    config.write_json(refresh.candidates_path(), {"cand-MARKER": {"kind": "slack", "cid": "C1", "ts": "1.1", "link": link}})
    title = f"MARKERTITLE {SECRET}"
    run("/api/todo", {"key": link, "title": title, "detail": "MARKERDETAIL", "url": link, "note": "MARKERNOTE", "mode": "handsoff"})
    run("/api/mark", {"key": mk, "state": "snoozed", "until": 9})
    run("/api/mark", {"key": link, "state": "dropped"})
    run("/api/decide", {"keys": ["cand-MARKER"], "decision": "keep"})
    first = run("/api/feedback", {"key": link, "kind": "row", "signal": "thumbs_down", "title": title, "sender": sender})[1]["id"]
    run("/api/feedback", {"key": link, "kind": "draft", "signal": "used_draft", "words": "", "title": title})
    run("/api/feedback", {"key": link, "kind": "row", "signal": "thumbs_down", "reason": "not_mine", "amends": first})
    run("/api/feedback", {"key": link, "kind": "row", "signal": "tell", "words": "my own words about it"})
    raw = (wq.home / ".assistant/feedback/feedback.jsonl").read_text()
    for marker in ("MARKER", SECRET, "xoxb", "marker.sender", "acme.slack.com", "https://"):
        assert marker not in raw, marker
    assert "my own words about it" in raw
    telemetry_text = json.dumps(__import__("assistant.telemetry", fromlist=["x"]).events(kind="feedback"))
    assert "MARKER" not in telemetry_text and "my own words" not in telemetry_text


# ─── what the refresh saves ──────────────────────────────────────────────────

def test_the_refresh_saves_each_rows_source_and_a_hash_of_its_sender_under_every_key_the_page_uses(wq):
    snap = {"slack": [{"key": "s1", "link": LINK, "who": "Ana", "group": "open"},
                      {"key": "s2", "link": "https://acme.slack.com/archives/C1/p3", "group": "unread"}],
            "github": [{"key": PR, "link": PR}],
            "outlook": [{"key": "mail:1", "link": "https://outlook.office.com/m1", "from": "Bob", "addr": ""},
                        {"key": "mail:2", "link": "https://outlook.office.com/m2", "addr": "Bob@Example.com", "from": "x"}],
            "gmail": {ACCT: [{"key": GKEY, "link": "https://mail.google.com/m", "from": "", "addr": ""}]}}
    found = page.row_context(snap)
    assert found[LINK] == found["s1"] == {"source": "slack", "who": feedback.who_pointer("ana")}
    assert found["s2"] == {"source": "slack"} and found[PR] == {"source": "github"}
    assert found["mail:1"] == found[server.EMAIL_MARK + "mail:1"] == {"source": "outlook", "who": feedback.who_pointer("bob")}
    assert found["mail:2"]["who"] == feedback.who_pointer("bob@example.com")
    assert found[GMAIL_MARK] == {"source": "gmail"}
    assert "Ana" not in json.dumps(found) and "Bob" not in json.dumps(found)


def test_a_refresh_writes_the_context_file_beside_the_others(wq):
    refresh.write_page({"built": "now", "status": [], "github": [{"key": PR, "link": PR, "title": "t", "meta": "m", "draft": False}]})
    assert json.loads(refresh.rowctx_path().read_text()) == {PR: {"source": "github"}}


# ─── the page: controls only where triage runs (Pro mode) ────────────────────

def page_snap(triage) -> dict:
    snap = {"built": "now", "status": [], "github": [{"key": PR, "link": PR, "title": "t", "meta": "m", "draft": False}],
            "outlook": [{"key": "mail:1", "link": "https://outlook.office.com/m1", "title": "Hi", "subject": "Hi", "from": "Bob", "addr": "",
                         "day": "Nov 2", "to": [], "cc": [], "body": "Body", "unread": False, "group": "act", "reason": "Because"}]}
    verdict = {"why": "w", "disposition": "you", "effort": "Quick", "draft": "A draft", "lane": "do_first"}
    start = [{"key": PR, "title": "t", "source": "GitHub", "effort": "", "reason": "r", "blocked": False}]
    return {**snap, **({"triage": {"rows": {PR: verdict}, "start": start, "changes": None, "waiting": 0, "skipped": 0,
                                   "note": "", "failed": []}} if triage else {})}


def test_the_page_has_no_feedback_controls_without_triage_which_is_default_mode():
    doc = page.render(page_snap(False))[0].split("<script>")[0]
    assert 'class="fb"' not in doc and "data-fp" not in doc and "draft-fb" not in doc


def test_the_page_names_each_item_by_its_pointer_and_never_by_its_link_in_a_pointer():
    doc = page.render(page_snap(True))[0].split("<script>")[0]
    mk = server.EMAIL_MARK + "mail:1"
    spans = re.findall(r'<span class="fb" data-fk="([a-z_]+)" data-fp="([^"]*)" data-fkey="([^"]*)"></span>', doc)
    assert sorted((k, p, key) for k, p, key in spans) == sorted([
        ("row", ptr(PR), PR), ("start_here", ptr(PR), PR), ("draft", "draft:" + ptr(PR), PR), ("page", "page", ""),
        ("email", ptr(mk), mk), ("email", ptr(mk), mk)])
    assert all("https:" not in p for _, p, _ in spans)
