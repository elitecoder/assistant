"""The Work queue records every Gmail action and unsubscribe in the effects ledger before it acts."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
from datetime import datetime, timezone
import urllib.error
from pathlib import Path

import pytest

from assistant import effects, pointers
from test_work_queue_actions import ACCT, KEY, gmail, gog_calls, thread
from test_work_queue_server import AT, closed_port, unsub_site  # noqa: F401
from work_queue import config, refresh, server
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

LATER = "2026-11-02T11:00:00+00:00"
NOON = datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc).timestamp()  # after AT and LATER
# Made-up secrets are built from parts so GitHub's push protection doesn't take them for real ones.
TOKEN = "-".join(["xoxb", "111111111", "222222222", "AbCdEfGhIjKlMnOpQrStUv0123456789"])
SUBJECT, SENDER = "ZEBRA-QUARTERLY-INVOICE", "Ana Lee <ana.lee@sender.example>"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """The clock stands still at half past eleven, so a decision's time is before or after a result whatever the date
    is. A test moves it with `clock.t`."""
    clock = type("Clock", (), {"t": NOON})()
    monkeypatch.setattr(effects, "_now", lambda: clock.t)
    monkeypatch.setattr(server, "now", lambda: datetime.fromtimestamp(clock.t, timezone.utc).replace(microsecond=0).isoformat())
    return clock


def ledger_path(wq) -> Path:
    return wq.home / ".assistant/effects/effects.jsonl"


def recorded(wq) -> list[dict]:
    """Every effect in the ledger, oldest first."""
    return list(reversed(effects.history(limit=1000)))


def gog_trash(wq, ids=("m1",), labels=("TRASH",)):
    fake_tool(wq.bin, "gog", [("gmail trash", {"ok": True}), ("messages modify", {"ok": True}),
                              ("gmail get", {"message": {"labelIds": list(labels)}})])


def gmail_job(key="g1", ids=("m1",), at=AT, undo=False):
    return [(key, {"kind": "gmail", "account": ACCT, "msg_ids": list(ids), "decided_at": at,
                   "title": SUBJECT, "from": SENDER}, undo)]


# --- Gmail Archive, Mark read, Move back ------------------------------------------------------------------------

def test_a_confirmed_archive_records_its_intent_then_a_verified_result(wq):
    gmail(wq, thread([], ["STARRED"]))
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 200
    [e] = recorded(wq)
    assert (e["class"], e["target"], e["outcome"], e["verified"], e["error"]) == (
        "gmail.archive", {"account": ACCT, "thread": "t1"}, "done", True, "")
    assert (e["authorized_by"]["kind"], e["authorized_by"]["ref"], e["component"], e["trace"]) == ("tap", "/api/gmail", "work-queue", "")
    rows = [json.loads(line)["kind"] for line in ledger_path(wq).read_text().splitlines()]
    assert rows == ["intent", "result"]


def test_the_intent_is_on_disk_before_gog_changes_anything(wq, monkeypatch):
    gmail(wq, thread([]))
    seen = []
    real = server.gog

    def watching(*args):
        if "modify" in args:
            seen.append([e["outcome"] for e in effects.history()])
        return real(*args)
    monkeypatch.setattr(server, "gog", watching)
    server.gmail_action({"key": KEY, "action": "read"})
    assert seen == [[None]] and recorded(wq)[0]["class"] == "gmail.mark_read"


@pytest.mark.parametrize("modify, read_back, outcome, verified, error", [
    ({"exit": 1, "stderr": "quota exceeded for ana@example.com"}, thread([]), "failed", None, "tool failed"),
    ({}, "not json", "done", None, ""),
    ({}, thread(["INBOX"]), "done", False, ""),
])
def test_a_failed_or_unconfirmed_gmail_action_still_gets_its_result(wq, modify, read_back, outcome, verified, error):
    gmail(wq, read_back, modify=modify)
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 502
    [e] = recorded(wq)
    assert (e["outcome"], e["verified"], e["error"]) == (outcome, verified, error)
    assert "ana@example.com" not in ledger_path(wq).read_text()


def test_moving_back_to_the_inbox_is_linked_to_the_archive_it_reverses(wq):
    gmail(wq, thread([]))
    server.gmail_action({"key": KEY, "action": "archive"})
    gmail(wq, thread(["INBOX"]))
    assert server.gmail_action({"key": KEY, "action": "unarchive"})[0] == 200
    gmail(wq, thread([]))
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 200
    gmail(wq, thread(["INBOX"]))
    assert server.gmail_action({"key": KEY, "action": "unarchive"})[0] == 200
    first, back, again, back2 = recorded(wq)
    assert (first["undoes"], back["undoes"], again["undoes"], back2["undoes"]) == ("", first["id"], "", again["id"])


def test_moving_back_with_no_archive_in_the_ledger_still_runs_unlinked(wq):
    gmail(wq, thread(["INBOX"]))
    assert server.gmail_action({"key": KEY, "action": "unarchive"})[0] == 200
    assert recorded(wq)[0]["undoes"] == ""


def test_a_second_click_while_the_first_is_still_working_makes_no_second_call(wq, monkeypatch):
    gmail(wq, thread([]))
    started, release, modifies = threading.Event(), threading.Event(), []
    real = server.gog

    def slow(*args):
        if "modify" in args:
            modifies.append(args)
            started.set()
            release.wait(20)
        return real(*args)
    monkeypatch.setattr(server, "gog", slow)
    first = []
    worker = threading.Thread(target=lambda: first.append(server.gmail_action({"key": KEY, "action": "archive"})))
    worker.start()
    assert started.wait(20)
    assert server.gmail_action({"key": KEY, "action": "archive"}) == (409, {"error": server.IN_PROGRESS})
    release.set()
    worker.join(20)
    assert first[0][0] == 200 and len(modifies) == 1


def test_an_unfinished_intent_older_than_ten_minutes_no_longer_blocks_a_click(wq, fixed_clock):
    gmail(wq, thread([]))
    effects.begin("gmail.archive", {"account": ACCT, "thread": "t1"}, {"kind": "tap", "ref": "/api/gmail", "at": AT})
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 409
    fixed_clock.t += 601
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 200


def break_the_ledger(wq, monkeypatch, how):
    if how == "folder is a file":
        (wq.home / ".assistant").mkdir(exist_ok=True)
        (wq.home / ".assistant/effects").write_text("not a folder")
        return
    def boom(*a):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(effects, "_write" if how == "append fails" else "_fsync", boom)


HOWS = ["folder is a file", "append fails", "flush fails"]


@pytest.mark.parametrize("how", HOWS)
def test_a_gmail_action_that_cant_be_recorded_does_not_run_and_changes_no_file(wq, monkeypatch, how):
    gmail(wq, thread([]))
    config.write_json(wq.data / "mail-actions.json", {KEY: {"read": "t"}})
    config.write_json(wq.data / "marks.json", {"https://keep/me": {"state": "done", "until": 0}})
    before = {p.name: p.read_bytes() for p in wq.data.iterdir()}
    break_the_ledger(wq, monkeypatch, how)
    assert server.gmail_action({"key": KEY, "action": "archive"}) == (503, {"error": server.UNRECORDED})
    assert "couldn't record" in server.UNRECORDED and "didn't run it" in server.UNRECORDED
    assert gog_calls(wq) == []
    assert {p.name: p.read_bytes() for p in wq.data.iterdir()} == before


def test_a_target_the_record_cant_describe_is_a_fault_in_assistant_and_does_not_run(wq, capsys):
    wq.sources({"gmail": {"accounts": ["not an address"]}})
    config.write_json(refresh.emails_path(), {KEY: {"account": "not an address", "tid": "t1"}})
    fake_tool(wq.bin, "gog", [("thread", {})])
    assert server.gmail_action({"key": KEY, "action": "archive"}) == (500, {"error": server.FAULT})
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "gmail.archive" in err and "not an address" not in err
    assert gog_calls(wq) == []


# --- Trash and back ---------------------------------------------------------------------------------------------

def test_a_trash_records_one_effect_per_conversation_authorized_by_its_decision(wq):
    gog_trash(wq, labels=("TRASH",))
    out = server.apply_gmail(gmail_job("g1", ["m2", "m1"]) + gmail_job("g2", ["m3"]))
    assert [out[k]["status"] for k in ("g1", "g2")] == ["done", "done"]
    first, second = recorded(wq)
    assert first["class"] == "gmail.trash" and first["outcome"] == "done" and first["verified"] is True
    assert first["target"] == {"account": ACCT, "msgs": ["m1", "m2"], "row": pointers.row_pointer("g1")}
    assert first["authorized_by"] == {"kind": "decision", "ref": pointers.row_pointer("g1"), "at": "2026-11-02T10:00:00Z"}
    assert second["target"]["row"] == pointers.row_pointer("g2")


def test_a_trash_that_gmail_refuses_or_cant_confirm_is_recorded_as_such(wq):
    fake_tool(wq.bin, "gog", [("gmail trash bad", {"exit": 1, "stderr": "quota exceeded"}), ("gmail trash", {"ok": True}),
                              ("gmail get", {"message": {"labelIds": ["INBOX"]}})])
    out = server.apply_gmail(gmail_job("g1", ["bad"]) + gmail_job("g2", ["m2"]))
    assert out["g1"]["status"] == "error" and out["g2"] == {"status": "done", "verified": False, "at": out["g2"]["at"]}
    failed, unverified = recorded(wq)
    assert (failed["outcome"], failed["error"]) == ("failed", "tool failed")
    assert (unverified["outcome"], unverified["verified"]) == ("done", False)
    assert "quota" not in ledger_path(wq).read_text()


def test_putting_a_trashed_conversation_back_is_linked_to_the_trash_it_reverses(wq):
    gog_trash(wq)
    server.apply_gmail(gmail_job())
    gog_trash(wq, labels=("INBOX",))
    out = server.apply_gmail(gmail_job(undo=True, at=LATER))
    assert out["g1"]["status"] == "undone"
    trash, untrash = recorded(wq)
    assert (untrash["class"], untrash["undoes"], untrash["verified"]) == ("gmail.untrash", trash["id"], True)


def test_putting_back_a_conversation_the_ledger_never_trashed_still_runs_unlinked(wq):
    gog_trash(wq, labels=("INBOX",))
    assert server.apply_gmail(gmail_job(undo=True))["g1"]["status"] == "undone"
    assert recorded(wq)[0]["undoes"] == ""


@pytest.mark.parametrize("how", HOWS)
def test_a_trash_that_cant_be_recorded_makes_no_gog_call(wq, monkeypatch, how):
    gog_trash(wq)
    break_the_ledger(wq, monkeypatch, how)
    out = server.apply_gmail(gmail_job())
    assert out["g1"] == {"status": "error", "error": server.UNRECORDED, "at": out["g1"]["at"]}
    assert gog_calls(wq) == []


def test_a_trash_with_no_decision_time_does_not_run(wq):
    gog_trash(wq)
    out = server.apply_gmail(gmail_job(at=None))
    assert out["g1"]["error"] == server.UNAPPROVED and gog_calls(wq) == [] and recorded(wq) == []


def test_a_conversation_already_being_trashed_is_not_trashed_twice(wq):
    gog_trash(wq)
    effects.begin("gmail.trash", {"account": ACCT, "msgs": ["m1"], "row": pointers.row_pointer("g1")},
                  {"kind": "decision", "ref": pointers.row_pointer("g1"), "at": AT})
    out = server.apply_gmail(gmail_job())
    assert out["g1"]["error"] == server.IN_PROGRESS and gog_calls(wq) == []


# --- Unsubscribe ------------------------------------------------------------------------------------------------

def unsub_job(url, key="u1", at=AT, undo=False):
    return [(key, {"kind": "unsub", "url": url, "decided_at": at}, undo)]


@pytest.fixture
def open_net(monkeypatch):
    monkeypatch.setattr(server, "unsafe_target", lambda url: "")


def test_an_approved_unsubscribe_records_its_intent_then_its_result_and_never_the_link(wq, monkeypatch, open_net):
    sent = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, req, timeout):
            sent.append(req.full_url)
            return Answer()
    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *a: Opener())
    link = "https://news.example.com/unsub/ok?u=" + TOKEN
    assert server.apply_unsub(unsub_job(link))["u1"]["verified"] is True and sent == [link]
    [e] = recorded(wq)
    assert (e["class"], e["outcome"], e["verified"], e["error"]) == ("mail.unsubscribe", "done", True, "")
    assert e["target"] == {"row": pointers.row_pointer("u1"), "host": "news.example.com"}
    assert e["authorized_by"]["kind"] == "decision" and e["authorized_by"]["at"] == "2026-11-02T10:00:00Z"
    text = ledger_path(wq).read_text()
    assert "AbCdEf" not in text and "unsub" not in text.replace("mail.unsubscribe", "") and "?u=" not in text


def test_the_intent_is_recorded_before_the_request_is_sent(wq, monkeypatch, open_net):
    seen = []

    class Opener:
        def open(self, req, timeout):
            seen.append([e["outcome"] for e in effects.history()])
            raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)
    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *a: Opener())
    server.apply_unsub(unsub_job("https://news.example.com/u"))
    assert seen == [[None]]


def opener_raising(monkeypatch, raised):
    class Opener:
        def open(self, req, timeout):
            raise raised
    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *a: Opener())


@pytest.mark.parametrize("raised", [
    urllib.error.URLError(ConnectionRefusedError("refused")),
    urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
    ConnectionRefusedError("refused"),
])
def test_an_unsubscribe_that_never_left_is_recorded_failed_and_may_be_sent_again(wq, monkeypatch, open_net, raised):
    opener_raising(monkeypatch, raised)
    out = server.apply_unsub(unsub_job("https://news.example.com/u"))
    assert out["u1"]["status"] == "error" and out["u1"]["error"] != server.SENT_UNHEARD
    [e] = recorded(wq)
    assert (e["outcome"], e["error"], e["verified"]) == ("failed", "network", None)
    sent = Sent()
    monkeypatch.setattr(server.urllib.request, "build_opener", sent.build)
    assert server.apply_unsub(unsub_job("https://news.example.com/u"))["u1"]["status"] == "done"


@pytest.mark.parametrize("raised, error", [
    (urllib.error.URLError(TimeoutError("slow")), "timeout"),
    (TimeoutError("slow"), "timeout"),
    (http.client.RemoteDisconnected("Remote end closed connection without response"), "network"),
    (ConnectionResetError("reset"), "network"),
    (urllib.error.URLError("odd"), "network"),
    (http.client.BadStatusLine("junk"), "network"),
])
def test_an_unsubscribe_sent_with_no_answer_is_recorded_unknown_and_not_sent_again_without_a_new_approval(
        wq, monkeypatch, open_net, fixed_clock, raised, error):
    opener_raising(monkeypatch, raised)
    out = server.apply_unsub(unsub_job("https://news.example.com/u"))
    assert out["u1"] == {"status": "error", "error": server.SENT_UNHEARD, "at": out["u1"]["at"]}
    [e] = recorded(wq)
    assert (e["outcome"], e["error"], e["verified"]) == ("unknown", error, None)
    assert [u["id"] for u in effects.uncertain()] == [e["id"]]
    sent = Sent()
    monkeypatch.setattr(server.urllib.request, "build_opener", sent.build)
    for _ in range(3):  # three Apply runs, and nothing goes out
        assert server.apply_unsub(unsub_job("https://news.example.com/u"))["u1"]["error"] == server.SENT_UNHEARD
    assert sent.urls == []
    fixed_clock.t += 600  # approving it again, later than the result, sends it once more
    assert server.apply_unsub(unsub_job("https://news.example.com/u", at="2026-11-02T11:35:00+00:00"))["u1"]["status"] == "done"
    assert len(sent.urls) == 1


def test_a_sender_that_answers_an_error_is_recorded_failed_with_its_status(wq, unsub_site, open_net):
    base, _ = unsub_site
    out = server.apply_unsub(unsub_job(base + "/no"))
    assert out["u1"]["error"] == "The sender answered 500"
    [e] = recorded(wq)
    assert (e["outcome"], e["error"]) == ("failed", "http 500")


def test_a_sender_unsubscribed_once_is_not_sent_the_request_again_but_a_failure_may_be_retried(wq, unsub_site, open_net,
                                                                                                fixed_clock):
    base, hits = unsub_site
    server.apply_unsub(unsub_job(base + "/no"))            # fails
    server.apply_unsub(unsub_job(base + "/no"))            # a failure doesn't block a retry
    assert len(hits) == 2 and [e["outcome"] for e in recorded(wq)] == ["failed", "failed"]
    server.apply_unsub(unsub_job(base + "/ok"))
    assert len(hits) == 3
    again = server.apply_unsub(unsub_job(base + "/ok"))
    assert again["u1"]["status"] == "done" and again["u1"]["verified"] is True and "error" not in again["u1"]
    assert len(hits) == 3
    # Approving it again afterwards is a new authorization.
    fixed_clock.t += 1200
    assert server.apply_unsub(unsub_job(base + "/ok", at="2026-11-02T11:45:00+00:00"))["u1"]["status"] == "done"
    assert len(hits) == 4


@pytest.mark.parametrize("at", [None, "", "yesterday", 5])
def test_an_unsubscribe_without_a_usable_decision_is_refused_and_sends_nothing(wq, unsub_site, open_net, at):
    base, hits = unsub_site
    out = server.apply_unsub(unsub_job(base + "/ok", at=at))
    assert out["u1"] == {"status": "error", "error": server.UNAPPROVED, "at": out["u1"]["at"]} and hits == []
    assert recorded(wq) == []


def test_a_tap_or_a_standing_rule_is_not_an_approval_for_an_unsubscribe(wq):
    for kind in ("tap", "standing", "owner_flag"):
        found = server.begin_effect("mail.unsubscribe", {"row": pointers.row_pointer("u1"), "host": "x.example.com"},
                                    kind, "/api/unsub", AT)
        assert found == ("", (403, server.UNAPPROVED))
    assert recorded(wq) == []


def test_an_unsubscribe_refused_by_the_existing_checks_writes_no_effect(wq, unsub_site):
    base, hits = unsub_site
    assert "private address" in server.apply_unsub(unsub_job("https://127.0.0.1/ok"))["u1"]["error"]
    assert server.apply_unsub(unsub_job(base + "/ok", undo=True))["u1"]["error"] == "Unsubscribing can't be undone from here"
    assert recorded(wq) == [] and hits == []


@pytest.mark.parametrize("how", HOWS)
def test_an_unsubscribe_that_cant_be_recorded_sends_nothing(wq, unsub_site, open_net, monkeypatch, how):
    base, hits = unsub_site
    break_the_ledger(wq, monkeypatch, how)
    out = server.apply_unsub(unsub_job(base + "/ok"))
    assert out["u1"] == {"status": "error", "error": server.UNRECORDED, "at": out["u1"]["at"]} and hits == []


# --- apply carries the decision's time to the effect ------------------------------------------------------------

def test_apply_records_each_effect_at_the_time_the_person_decided(wq, unsub_site, open_net):
    base, hits = unsub_site
    gog_trash(wq)
    config.write_json(refresh.candidates_path(), {
        "g1": {"kind": "gmail", "account": ACCT, "msg_ids": ["m1"]}, "u1": {"kind": "unsub", "url": base + "/ok"}})
    config.write_json(wq.data / "decisions.json", {"g1": {"decision": "approve", "at": AT},
                                                   "u1": {"decision": "approve", "at": LATER}})
    server.run_apply()
    assert {e["class"]: e["authorized_by"]["at"] for e in recorded(wq)} == {
        "gmail.trash": "2026-11-02T10:00:00Z", "mail.unsubscribe": "2026-11-02T11:00:00Z"}


def test_apply_reports_an_approval_it_cant_date_as_an_error_and_sends_nothing(wq, unsub_site, open_net):
    base, hits = unsub_site
    config.write_json(refresh.candidates_path(), {"u1": {"kind": "unsub", "url": base + "/ok"}})
    config.write_json(wq.data / "decisions.json", {"u1": {"decision": "approve"}})
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["u1"]["error"] == server.UNAPPROVED and hits == []


# --- no content, no secret --------------------------------------------------------------------------------------

def test_no_wired_path_writes_a_subject_a_sender_a_link_or_a_token(wq, unsub_site, open_net, monkeypatch):
    base, _ = unsub_site
    config.write_json(refresh.emails_path(), {KEY: {"account": ACCT, "tid": "t1", "subject": SUBJECT, "from": SENDER}})
    gmail(wq, thread([]))
    config.write_json(refresh.emails_path(), {KEY: {"account": ACCT, "tid": "t1", "subject": SUBJECT, "from": SENDER}})
    server.gmail_action({"key": KEY, "action": "archive"})
    gog_trash(wq)
    server.apply_gmail(gmail_job())
    server.apply_unsub([("u1", {"kind": "unsub", "url": f"{base}/ok?token={TOKEN}&who=ana.lee", "decided_at": AT,
                                "title": SUBJECT, "from": SENDER}, False)])
    text = ledger_path(wq).read_text()
    for marker in (SUBJECT, "Ana", "ana.lee", "sender.example", TOKEN, "AbCdEf", "/ok", "token=", "who="):
        assert marker not in text, marker
    assert len(recorded(wq)) == 3


def test_the_actions_ledger_is_never_written(wq, unsub_site, open_net):
    base, _ = unsub_site
    gmail(wq, thread([]))
    server.gmail_action({"key": KEY, "action": "archive"})
    server.apply_unsub(unsub_job(base + "/ok"))
    assert not (wq.home / ".assistant/actions-ledger.jsonl").exists()
    assert sorted(p.name for p in (wq.home / ".assistant/effects").iterdir()) == ["effects.jsonl", "effects.lock"]


# --- what worked before still works --------------------------------------------------------------------------

class Sent:
    """A stand-in for the unsubscribe request that answers 200 and keeps each address it was sent to."""
    status = 200

    def __init__(self):
        self.urls = []

    def build(self, *args):
        outer = self

        class Opener:
            def open(self, req, timeout):
                outer.urls.append(req.full_url)
                return outer

        return Opener()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch, open_net):
    found = Sent()
    monkeypatch.setattr(server.urllib.request, "build_opener", found.build)
    return found


@pytest.mark.parametrize("url, host", [
    ("https://News_Mailer.Example.com./u", "news_mailer.example.com"),
    ("https://münchen.example/u", "xn--mnchen-3ya.example"),
    ("https://[2001:db8::1]/u", "2001:db8::1"),
    ("https://EXAMPLE.com:8443/u", "example.com"),
])
def test_an_unsubscribe_to_any_kind_of_host_still_goes_out_and_is_recorded(wq, sent, url, host):
    assert server.apply_unsub(unsub_job(url))["u1"]["status"] == "done"
    assert sent.urls == [url] and recorded(wq)[0]["target"]["host"] == host


def test_example_com_with_and_without_a_dot_is_one_sender(wq, sent):
    server.apply_unsub(unsub_job("https://example.com/u"))
    again = server.apply_unsub(unsub_job("https://example.com./u"))
    assert again["u1"]["status"] == "done" and len(sent.urls) == 1


def test_a_conversation_of_hundreds_of_messages_is_trashed_and_recorded_as_a_count(wq, monkeypatch):
    ids = [f"m{n}" for n in range(300)]
    seen = []

    def gog(*args):
        seen.append(args)
        return {"ok": True} if args[1] == "trash" else {"message": {"labelIds": ["TRASH"]}}
    monkeypatch.setattr(server, "gog", gog)
    assert server.apply_gmail(gmail_job(ids=ids))["g1"]["verified"] is True and len(seen) == 600
    [e] = recorded(wq)
    assert e["target"]["msg_count"] == "300" and e["outcome"] == "done"
    assert "m17" not in ledger_path(wq).read_text()


def test_a_conversation_with_no_messages_is_left_as_it_was_and_records_nothing(wq, monkeypatch):
    monkeypatch.setattr(server, "gog", lambda *a: pytest.fail("nothing to change"))
    out = server.apply_gmail(gmail_job(ids=[]) + gmail_job("g2", ids=[], undo=True))
    assert (out["g1"]["status"], out["g1"]["verified"], out["g2"]["status"]) == ("done", True, "undone")
    assert recorded(wq) == [] and not ledger_path(wq).exists()


def test_a_gmail_address_with_an_apostrophe_is_archived(wq):
    account = "o'brien@example.com"
    gmail(wq, thread([]))
    wq.sources({"gmail": {"accounts": [account]}})
    config.write_json(refresh.emails_path(), {KEY: {"account": account, "tid": "t1"}})
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 200
    assert recorded(wq)[0]["target"]["account"] == account


# --- an error nobody expected leaves no open intent ---------------------------------------------------------

def test_an_error_nobody_expected_after_the_request_went_out_is_recorded_unknown_and_goes_on_up(wq, monkeypatch, open_net):
    opener_raising(monkeypatch, RuntimeError("something nobody planned for"))
    with pytest.raises(RuntimeError):
        server.apply_unsub(unsub_job("https://news.example.com/u"))
    [e] = recorded(wq)
    assert (e["outcome"], e["error"]) == ("unknown", "other") and effects.unfinished(0) == []
    sent = Sent()
    monkeypatch.setattr(server.urllib.request, "build_opener", sent.build)
    assert server.apply_unsub(unsub_job("https://news.example.com/u"))["u1"]["error"] == server.SENT_UNHEARD
    assert sent.urls == []


def test_a_gmail_action_or_trash_that_dies_unexpectedly_is_recorded_unknown_and_the_error_goes_on_up(wq, monkeypatch):
    gmail(wq, thread([]))

    def boom(*args):
        raise RuntimeError("gog crashed")
    monkeypatch.setattr(server, "gog", boom)
    with pytest.raises(RuntimeError):
        server.gmail_action({"key": KEY, "action": "archive"})
    with pytest.raises(RuntimeError):
        server.apply_gmail(gmail_job())
    assert [(e["outcome"], e["error"]) for e in recorded(wq)] == [("unknown", "other")] * 2 and effects.unfinished(0) == []


def test_a_read_back_that_dies_after_gmail_archived_is_unknown_not_failed(wq, monkeypatch):
    gmail(wq, thread([]))

    def boom(*args):
        raise RuntimeError("read-back crashed")
    monkeypatch.setattr(server, "thread_labels", boom)
    with pytest.raises(RuntimeError):
        server.gmail_action({"key": KEY, "action": "archive"})
    assert recorded(wq)[0]["outcome"] == "unknown"
    assert effects.latest_undoable("gmail.unarchive", {"account": ACCT, "thread": "t1"}) == ""  # nothing to link an undo to


# --- an undo the record refuses leaves the row as it was ----------------------------------------------------

def test_an_undo_the_record_refuses_keeps_the_row_done_with_the_reason_and_the_next_apply_tries_again(wq, monkeypatch):
    gog_trash(wq, labels=("INBOX",))
    config.write_json(refresh.candidates_path(), {"g1": {"kind": "gmail", "account": ACCT, "msg_ids": ["m1"]}})
    config.write_json(wq.data / "decisions.json", {"g1": {"decision": "undo", "at": AT}})
    before = {"status": "done", "verified": True, "at": "2026-11-02T09:00:00+00:00"}
    config.write_json(wq.data / "applied.json", {"g1": before})
    break_the_ledger(wq, monkeypatch, "folder is a file")
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["g1"] == {**before, "error": server.UNRECORDED}
    status = json.loads((wq.data / "apply-status.json").read_text())
    assert (status["running"], status["error"], status["ok"], status["verified"]) == (False, server.UNRECORDED, 0, 0)
    assert gog_calls(wq) == []
    (wq.home / ".assistant/effects").unlink()  # the disk is fine again
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["g1"]["status"] == "undone"
    assert "error" not in json.loads((wq.data / "apply-status.json").read_text())


def test_a_trash_the_record_refuses_is_an_error_the_next_apply_tries_again(wq, monkeypatch):
    gog_trash(wq)
    config.write_json(refresh.candidates_path(), {"g1": {"kind": "gmail", "account": ACCT, "msg_ids": ["m1"]}})
    config.write_json(wq.data / "decisions.json", {"g1": {"decision": "approve", "at": AT}})
    break_the_ledger(wq, monkeypatch, "folder is a file")
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["g1"]["status"] == "error"
    (wq.home / ".assistant/effects").unlink()
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["g1"]["status"] == "done"


def held_setup(wq, keys):
    """Rows that are in Trash with an undo approved, each with its own message."""
    config.write_json(refresh.candidates_path(), {k: {"kind": "gmail", "account": ACCT, "msg_ids": [f"m{n}"]}
                                                  for n, k in enumerate(keys, 1)})
    config.write_json(wq.data / "decisions.json", {k: {"decision": "undo", "at": AT} for k in keys})
    config.write_json(wq.data / "applied.json", {k: {"status": "done", "verified": True, "at": "before"} for k in keys})


def status_of(wq) -> dict:
    return json.loads((wq.data / "apply-status.json").read_text())


def test_each_row_an_undo_didnt_happen_for_keeps_its_own_reason_and_the_bar_gives_each_reason_once(wq):
    held_setup(wq, ["g1", "g2", "g3"])
    fake_tool(wq.bin, "gog", [("messages modify m2", {"exit": 1, "stderr": "quota exceeded"}),
                              ("messages modify m3", {"exit": 1, "stderr": "quota exceeded"}),
                              ("messages modify", {"ok": True}), ("gmail get", {"message": {"labelIds": ["INBOX"]}})])
    effects.begin("gmail.untrash", {"account": ACCT, "msgs": ["m1"], "row": pointers.row_pointer("g1")},
                  {"kind": "decision", "ref": pointers.row_pointer("g1"), "at": AT})  # g1 is already being put back
    server.run_apply()
    applied_now = json.loads((wq.data / "applied.json").read_text())
    assert applied_now["g1"] == {"status": "done", "verified": True, "at": "before", "error": server.IN_PROGRESS}
    assert applied_now["g2"]["status"] == applied_now["g3"]["status"] == "done"
    assert "quota exceeded" in applied_now["g2"]["error"] and applied_now["g2"]["error"] == applied_now["g3"]["error"]
    status = status_of(wq)
    assert status["error"] == f"{server.IN_PROGRESS}; {applied_now['g2']['error']}", "each distinct reason once"
    assert (status["gmail"], status["ok"], status["verified"]) == (3, 0, 0)


def test_an_undo_gmail_itself_refuses_keeps_the_row_in_trash_and_the_next_apply_puts_it_back(wq):
    held_setup(wq, ["g1"])
    fake_tool(wq.bin, "gog", [("messages modify", {"exit": 1, "stderr": "quota exceeded"})])
    server.run_apply()
    row = json.loads((wq.data / "applied.json").read_text())["g1"]
    assert row["status"] == "done" and row["verified"] is True and "quota exceeded" in row["error"]
    assert status_of(wq)["error"] == row["error"]
    fake_tool(wq.bin, "gog", [("messages modify", {"ok": True}), ("gmail get", {"message": {"labelIds": ["INBOX"]}})])
    server.run_apply()
    row = json.loads((wq.data / "applied.json").read_text())["g1"]
    assert (row["status"], row["verified"], "error" in row) == ("undone", True, False)
    assert "error" not in status_of(wq)


# --- an unsubscribe that went through stays done ------------------------------------------------------------

def test_an_unsubscribe_that_went_through_shows_as_done_when_applied_json_lost_its_entry(wq, sent):
    config.write_json(refresh.candidates_path(), {"u1": {"kind": "unsub", "url": "https://news.example.com/u"}})
    config.write_json(wq.data / "decisions.json", {"u1": {"decision": "approve", "at": AT}})
    server.run_apply()
    assert len(sent.urls) == 1
    (wq.data / "applied.json").unlink()
    server.run_apply()
    now_done = json.loads((wq.data / "applied.json").read_text())["u1"]
    assert (now_done["status"], now_done["verified"], "error" in now_done) == ("done", True, False)
    assert len(sent.urls) == 1
    status = json.loads((wq.data / "apply-status.json").read_text())
    assert (status["ok"], status["verified"]) == (1, 1)


def test_an_approval_dated_in_the_future_is_not_an_approval(wq, sent):
    out = server.apply_unsub(unsub_job("https://news.example.com/u", at="2099-01-01T00:00:00+00:00"))
    assert out["u1"]["error"] == server.UNAPPROVED and sent.urls == []


# --- the sentences ------------------------------------------------------------------------------------------

def test_the_sentences_a_person_can_see_read_after_not_done_and_failed_with_no_full_stop():
    assert server.UNRECORDED == ("Assistant couldn't record this action, so it didn't run it. "
                                 "Check that your disk isn't full, then try again")
    assert server.IN_PROGRESS == ("This is already in progress, or Assistant stopped partway through it. "
                                  "Try again in ten minutes")
    assert server.UNAPPROVED == ("Assistant couldn't confirm your approval for this, so it didn't run it. "
                                 "Approve it again, then select Apply approvals")
    assert server.ALREADY_DONE == "This was already done and can't be undone, so it wasn't run again"
    assert server.FAULT == ("Assistant couldn't record this action, so it didn't run it. "
                            "This is a fault in Assistant, not something you did")
    for sentence in (server.UNRECORDED, server.IN_PROGRESS, server.UNAPPROVED, server.ALREADY_DONE, server.FAULT):
        assert not sentence.endswith(".")
