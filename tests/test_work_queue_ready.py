"""Each refresh checks Ready for you: work that landed or that you followed up on clears, with its reason, and a
blocked item only gets a note when its blocker looks gone.

gh and Slack are fakes here: gh is a fake CLI in the temporary PATH, and Slack is FakeSlack as the transport."""
from __future__ import annotations

import json
import threading

import pytest

from test_work_queue_actions import KEY, MARK, gmail, thread
from test_work_queue_inputs import FakeSlack, msg
from work_queue import config, ready, refresh, server, tools
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

FINISHED = "2026-11-20T10:00:00+00:00"
BEFORE, AFTER = "2026-11-20T09:00:00Z", "2026-11-20T11:00:00Z"
THREAD = "1795165200.000100"
SLACK_URL = f"https://acme.slack.com/archives/C12345678/p1795165300000200?thread_ts={THREAD}&cid=C12345678"


def pr_url(n: int, host: str = "github.com") -> str:
    return f"https://{host}/o/r/pull/{n}"


def done(td: str, result: str = "", **extra) -> dict:
    detail = "rules\n\nSource: https://github.com/o/r/pull/99" + (f"\n\nRESULT: {result}" if result else "")
    return {"id": td, "title": f"Work {td}", "status": "done", "resultAt": FINISHED, "statusUpdatedAt": FINISHED,
            "detail": detail, **extra}


def seed(wq, items: list[dict], marks: dict | None = None) -> None:
    wq.todo.write_text(json.dumps({"items": items}))
    config.write_json(wq.data / "queued.json", {f"https://row/{i['id']}": {"id": i["id"], "at": "t"} for i in items})
    config.write_json(wq.data / "marks.json", marks or {})
    wq.sources({})


def pr(state: str, comments=(), reviews=()) -> dict:
    return {"state": state, "comments": list(comments), "reviews": list(reviews)}


def marks(wq) -> dict:
    return json.loads((wq.data / "marks.json").read_text())


def ready_mark(td: str) -> str:
    return ready.READY_MARK + td


def mark_of(wq, td: str) -> dict:
    """The mark Ready for you's check wrote for td; the test fails when there's none."""
    found = marks(wq)
    assert ready_mark(td) in found, f"{td} didn't clear: {sorted(found)}"
    return found[ready_mark(td)]


def checks() -> dict:
    """What /api/state says the last refresh found about each item; the test fails when it says nothing."""
    state = server.state()
    assert "checks" in state, sorted(state)
    return state["checks"]


def gh(wq, *answers) -> None:
    fake_tool(wq.bin, "gh", [*answers, ("api user", {"login": "me"})])


# ─── the work landed ─────────────────────────────────────────────────────────

def test_a_done_item_whose_pull_request_merged_clears_with_its_reason_and_never_comes_back(wq):
    seed(wq, [done("td-258", f"Merged {pr_url(2)} myself.")])
    gh(wq, (f"pr view {pr_url(2)}", pr("MERGED")))
    status = refresh.run(now=0)
    assert all(s["state"] == "off" for s in status)
    mark = mark_of(wq, "td-258")
    assert (mark.get("state"), mark.get("until"), mark.get("auto"), mark.get("why")) == ("done", 0, "pr-merged", "o/r#2 was merged.")
    assert checks().get("td-258", {}).get("cleared") == "pr-merged"
    assert calls(wq.bin, "gh") == [f"pr view {pr_url(2)} --json state,comments,reviews"]

    refresh.run(now=0)
    assert len(calls(wq.bin, "gh")) == 1, "a cleared item isn't checked again"
    assert server.set_marks({"key": ready_mark("td-258"), "state": "clear"})[0] == 200
    refresh.run(now=0)
    assert ready_mark("td-258") not in marks(wq), "an item you brought back never clears on its own again"
    assert len(calls(wq.bin, "gh")) == 1


def test_every_named_pull_request_must_be_merged_or_closed_and_each_is_read_once(wq):
    seed(wq, [done("td-301", f"Opened {pr_url(3)} and {pr_url(4)}."), done("td-302", f"See {pr_url(4)}."),
              done("td-303", f"{pr_url(5)} merged, {pr_url(6)} closed.")])
    gh(wq, (f"pr view {pr_url(3)}", pr("OPEN")), (f"pr view {pr_url(4)}", pr("MERGED")),
       (f"pr view {pr_url(5)}", pr("MERGED")), (f"pr view {pr_url(6)}", pr("CLOSED")))
    refresh.run(now=0)
    found = marks(wq)
    assert ready_mark("td-301") not in found, "one open pull request keeps the item"
    assert mark_of(wq, "td-302").get("auto") == "pr-merged"
    assert (mark_of(wq, "td-303").get("auto"), mark_of(wq, "td-303").get("why")) == (
        "pr-closed", "o/r#5, o/r#6 were merged or closed.")
    seen = checks()
    assert seen.get("td-301", {}).get("pending") == \
        "Clears when o/r#3 merges or closes, or once you comment on or review it."
    views = [c for c in calls(wq.bin, "gh") if c.startswith("pr view")]
    assert sorted(views) == sorted(set(views)) and len(views) == 4, "one gh call per pull request per refresh"


def test_the_results_pull_requests_count_before_the_rows_own_link(wq):
    seed(wq, [done("td-310", f"Merged {pr_url(7)}."), done("td-311", f"Drafted a reply; {pr_url(8)} needs your push.")])
    gh(wq, (f"pr view {pr_url(7)}", pr("MERGED")), (f"pr view {pr_url(8)}", pr("OPEN")),
       (f"pr view {pr_url(99)}", pr("OPEN")))
    refresh.run(now=0)
    assert mark_of(wq, "td-310").get("why") == "o/r#7 was merged.", "the row's own open pull request doesn't count"
    assert ready_mark("td-311") not in marks(wq)
    assert checks().get("td-311", {}).get("pending") == \
        "Clears when o/r#8 merges or closes, or once you comment on or review it."
    assert not any("pull/99" in c for c in calls(wq.bin, "gh"))


def test_a_result_without_a_pull_request_falls_back_to_the_rows_link(wq):
    item = {**done("td-320", "Wrote the notes."), "detail": "rules\n\nRESULT: Wrote the notes.", "url": pr_url(9)}
    seed(wq, [item])
    gh(wq, (f"pr view {pr_url(9)}", pr("MERGED")))
    refresh.run(now=0)
    assert mark_of(wq, "td-320").get("why") == "o/r#9 was merged."


# ─── you followed up ─────────────────────────────────────────────────────────

def test_your_comment_or_review_after_it_finished_clears_it_and_earlier_or_other_peoples_dont(wq):
    seed(wq, [done("td-401", f"Opened {pr_url(11)}"), done("td-402", f"Opened {pr_url(12)}"),
              done("td-403", f"Opened {pr_url(13)}")])
    me, ana = {"login": "me"}, {"login": "ana"}
    gh(wq, (f"pr view {pr_url(11)}", pr("OPEN", comments=[{"author": me, "createdAt": BEFORE}])),
       (f"pr view {pr_url(12)}", pr("OPEN", comments=[{"author": ana, "createdAt": AFTER}],
                                    reviews=[{"author": me, "submittedAt": AFTER}])),
       (f"pr view {pr_url(13)}", pr("OPEN", comments=[{"author": ana, "createdAt": AFTER}])))
    refresh.run(now=0)
    found = marks(wq)
    assert ready_mark("td-401") not in found and ready_mark("td-403") not in found
    assert (mark_of(wq, "td-402").get("auto"), mark_of(wq, "td-402").get("why")) == (
        "you-commented", "You commented on or reviewed o/r#12 after it finished.")
    assert calls(wq.bin, "gh").count("api user") == 1, "your login is read once per host"


def test_a_login_gh_cant_read_never_counts_as_your_comment(wq):
    seed(wq, [done("td-410", f"Opened {pr_url(14)}")])
    fake_tool(wq.bin, "gh", [(f"pr view {pr_url(14)}", pr("OPEN", comments=[{"author": {"login": "me"}, "createdAt": AFTER}])),
                             ("api user", {"exit": 1, "stderr": "gh auth login"})])
    refresh.run(now=0)
    assert ready_mark("td-410") not in marks(wq)


def slack_with(replies: list[dict]) -> FakeSlack:
    fs = FakeSlack()
    fs.threads[("C12345678", THREAD)] = [msg(THREAD, "U2", "Can you look?", thread_ts=THREAD), *replies]
    return fs


def test_your_reply_in_the_slack_thread_after_it_finished_clears_it(wq, monkeypatch):
    seed(wq, [done("td-501", "Drafted a reply.", url=SLACK_URL), done("td-503", "Drafted another.", url=SLACK_URL)])
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    fs = slack_with([msg("1795172400.000000", "U1", "Sent it", thread_ts=THREAD)])
    asked = []
    refresh.run(transport=lambda m, p, t: asked.append((m, p)) or fs(m, p, t), now=0)
    assert mark_of(wq, "td-501") | {"at": ""} == {
        "state": "done", "until": 0, "at": "", "auto": "you-replied",
        "why": "You replied in its Slack thread after it finished."}
    assert ("conversations.replies", {"channel": "C12345678", "ts": THREAD, "oldest": "1795168800.000000",
                                      "limit": 200}) in asked
    assert mark_of(wq, "td-503").get("auto") == "you-replied"
    assert [m for m, _ in asked].count("auth.test") == 1, "one Slack sign-in check per refresh"


@pytest.mark.parametrize("replies", [[msg("1795166000.000000", "U1", "Before", thread_ts=THREAD)],
                                     [msg("1795172400.000000", "U2", "Someone else", thread_ts=THREAD)]])
def test_a_reply_before_it_finished_or_from_someone_else_keeps_it(wq, monkeypatch, replies):
    seed(wq, [done("td-502", "Drafted a reply.", url=SLACK_URL)])
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    refresh.run(transport=slack_with(replies), now=0)
    assert ready_mark("td-502") not in marks(wq)


def test_a_slack_link_on_a_save_without_a_thread_reads_the_message_itself(wq):
    assert ready.slack_thread("https://acme.slack.com/archives/C12345678/p1795165300000200") == (
        "C12345678", "1795165300.000200")
    assert ready.slack_thread("https://evil.example/archives/C12345678/p1795165300000200") is None
    assert ready.slack_thread("") is None


# ─── a failed check leaves the item ──────────────────────────────────────────

def test_a_failed_check_leaves_every_item_showing_and_the_refresh_finishes(wq, monkeypatch):
    seed(wq, [done("td-601", f"Opened {pr_url(21)}", url=SLACK_URL), done("td-602", f"Opened {pr_url(22)}"),
              {**done("td-603", f"Opened {pr_url(23)}"), "resultAt": "garbled", "statusUpdatedAt": None},
              done("td-604", f"Merged {pr_url(24)}")])
    fake_tool(wq.bin, "gh", [(f"pr view {pr_url(21)}", {"exit": 1, "stderr": "HTTP 403: Resource not accessible"}),
                             (f"pr view {pr_url(22)}", "not json"),
                             (f"pr view {pr_url(23)}", pr("OPEN", comments=[{"author": {"login": "me"}, "createdAt": AFTER}])),
                             (f"pr view {pr_url(24)}", pr("MERGED")), ("api user", {"login": "me"})])
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")

    def refused(method, params, token):
        return {"ok": False, "error": "invalid_auth"}
    refresh.run(transport=refused, now=0)
    assert [k for k in marks(wq) if k.startswith(ready.READY_MARK)] == [ready_mark("td-604")], \
        "the failed checks leave their items, and the next item is still checked"
    assert json.loads(refresh.status_path().read_text())["error"] == "" and refresh.site_path().exists()
    assert "api user" not in calls(wq.bin, "gh"), "a TODO without a readable finish time isn't checked for comments"

    def broken(*args):
        raise RuntimeError("a bug in a check")
    monkeypatch.setattr(ready, "check", broken)
    assert ready.run({}, refused, "") == {}
    refresh.run(transport=refused, now=0)
    assert json.loads(refresh.status_path().read_text())["running"] is False


def test_a_slack_that_cant_be_reached_leaves_the_item(wq, monkeypatch):
    seed(wq, [done("td-610", "Drafted a reply.", url=SLACK_URL)])
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")

    def offline(method, params, token):
        if method == "conversations.replies":
            raise OSError("network is down")
        return slack_with([])(method, params, token)
    refresh.run(transport=offline, now=0)
    assert ready_mark("td-610") not in marks(wq)
    monkeypatch.delenv("SLACK_USER_TOKEN")
    refresh.run(transport=lambda *a: pytest.fail("no token, so Slack isn't asked"), now=0)
    assert ready_mark("td-610") not in marks(wq)


def test_gh_calls_have_a_time_limit_and_use_the_linked_host(wq, monkeypatch):
    host = "git.corp.example.com"
    seed(wq, [done("td-701", f"Opened {pr_url(31, host)} and {pr_url(32, 'gitlab.example.com')}")])
    wq.sources({"github": {"host": host}})
    fake_tool(wq.bin, "gh", [("search prs", []), (f"pr view {pr_url(31, host)}", pr("MERGED"))])
    limits = []
    run_json = tools.run_json
    monkeypatch.setattr(tools, "run_json", lambda cmd, **kw: limits.append(kw.get("timeout")) or run_json(cmd, **kw))
    refresh.run(now=0)
    assert mark_of(wq, "td-701").get("why") == "o/r#31 was merged.", "a host gh doesn't know is left out"
    views = [n for n, c in enumerate(calls(wq.bin, "gh")) if c.startswith("pr view")]
    assert len(views) == 1 and (wq.bin / "gh.env").read_text().splitlines()[views[0]] == host
    assert ready.GH_TIMEOUT in limits and ready.GH_TIMEOUT <= 60


# ─── blocked items ───────────────────────────────────────────────────────────

def blocked(td: str, reason: str) -> dict:
    return {"id": td, "title": f"Work {td}", "status": "blocked", "statusReason": reason,
            "statusUpdatedAt": FINISHED, "detail": "rules"}


def test_a_blocked_item_never_clears_and_says_when_its_repo_access_works_now(wq):
    items = [blocked("td-801", "Need access to OneAdobe/architect-ffp-telemetry: gh returned 404."),
             blocked("td-802", "No permission on https://github.com/o/private, and Jira too."),
             blocked("td-803", "Need access to o/still-locked."),
             blocked("td-804", "Waiting on Ana to answer in the repo o/r about the plan."),
             blocked("td-805", f"Can't read {pr_url(40)}: 403.")]
    seed(wq, items)
    before = json.loads(wq.todo.read_text())
    fake_tool(wq.bin, "gh", [("api repos/OneAdobe/architect-ffp-telemetry", {"full_name": "x"}),
                             ("api repos/o/private", {"full_name": "y"}),
                             ("api repos/o/still-locked", {"exit": 1, "stderr": "HTTP 404: Not Found"}),
                             ("api repos/o/r", {"full_name": "z"})])
    refresh.run(now=0)
    seen = checks()
    assert seen.get("td-801", {}).get("note") == (
        "The blocker looks gone: GitHub can read OneAdobe/architect-ffp-telemetry now, so you can resume td-801.")
    assert seen.get("td-802", {}).get("note") == \
        "The blocker looks gone: GitHub can read o/private now, so you can resume td-802."
    assert "td-803" not in seen, "a repo gh still can't read leaves the item as it is"
    assert "td-804" not in seen, "a reason that isn't about access isn't checked"
    assert (seen.get("td-805", {}).get("note") or "").startswith("The blocker looks gone: GitHub can read o/r now")
    after = json.loads(wq.todo.read_text())  # the refresh may add each sent TODO's id, and changes nothing else
    assert [{k: v for k, v in i.items() if k != "trace"} for i in after["items"]] == before["items"], \
        "a blocked TODO is never restarted"
    assert {k: v for k, v in after.items() if k != "items"} == {k: v for k, v in before.items() if k != "items"}
    assert not any(k.startswith(ready.READY_MARK) for k in marks(wq))
    assert calls(wq.bin, "gh").count("api repos/o/r") == 1, "only the reason about access asks gh"


def test_only_unmarked_done_or_blocked_items_sent_from_the_page_are_checked(wq):
    items = [done("td-901", f"Opened {pr_url(51)}"), done("td-902", f"Opened {pr_url(52)}"),
             {**done("td-903", f"Opened {pr_url(53)}"), "status": "in-progress"}]
    seed(wq, items, {ready_mark("td-901"): {"state": "done", "until": 0}})
    doc = json.loads(wq.todo.read_text())
    doc["items"].append(done("td-904", f"Opened {pr_url(54)}"))
    wq.todo.write_text(json.dumps(doc))
    gh(wq, (f"pr view {pr_url(52)}", pr("MERGED")))
    refresh.run(now=0)
    assert calls(wq.bin, "gh") == [f"pr view {pr_url(52)} --json state,comments,reviews"]
    assert mark_of(wq, "td-901") == {"state": "done", "until": 0}, "your own Done mark stays as you left it"


def test_unreadable_saved_files_check_nothing_and_break_nothing(wq):
    seed(wq, [done("td-950", f"Opened {pr_url(60)}")])
    (wq.data / "queued.json").write_text("[1]")
    (wq.data / "ready-checks.json").write_text("[1]")
    refresh.run(now=0)
    assert calls(wq.bin, "gh") == [] and checks() == {}


def test_open_pull_requests_say_what_would_clear_the_item_without_naming_who_it_waits_on(wq):
    seed(wq, [done("td-257", "Drafted review comments on https://github.com/OneAdobe/architect-ffp/pull/420 and "
                             "https://github.com/OneAdobe/architect-ffp/pull/556 for you to post."),
              done("td-258", f"Opened {pr_url(61)}, {pr_url(62)}, and https://github.com/x/y/pull/63.")])
    gh(wq, ("pr view https://github.com/OneAdobe/architect-ffp/pull/420", pr("OPEN")),
       ("pr view https://github.com/OneAdobe/architect-ffp/pull/556", pr("OPEN")),
       (f"pr view {pr_url(61)}", pr("OPEN")), (f"pr view {pr_url(62)}", pr("OPEN")),
       ("pr view https://github.com/x/y/pull/63", pr("OPEN")))
    refresh.run(now=0)
    seen = checks()
    assert seen.get("td-257", {}).get("pending") == (
        "Clears when OneAdobe/architect-ffp#420 and #556 merge or close, or once you comment on or review them.")
    assert seen.get("td-258", {}).get("pending") == (
        "Clears when o/r#61, #62, and x/y#63 merge or close, or once you comment on or review them.")


# ─── the server and the refresh never lose each other's marks ───────────────

def held_marks_survive(wq, write, key: str) -> None:
    """Hold the marks lock the way the other process does: read marks.json, give `write` time to run in another
    thread, then write back what was read plus a mark of its own. Both marks must survive, so `write` must wait for
    the lock and read the file again inside it."""
    writer = threading.Thread(target=write)
    with config.marks_lock():
        before = config.read_json(wq.data / "marks.json", {})
        writer.start()
        writer.join(2)
        config.write_json(wq.data / "marks.json", {**before, "https://held/mark": {"state": "done", "until": 0}})
    writer.join(30)
    assert not writer.is_alive()
    found = marks(wq)
    assert "https://held/mark" in found and key in found, sorted(found)


def set_mark(wq):
    return lambda: server.set_marks({"key": "https://row/a", "state": "done"}), "https://row/a"


def send_row(wq):
    return lambda: server.queue_todo({"key": "https://row/b", "title": "B"}), "https://row/b"


def archive_email(wq):
    gmail(wq, thread([], []))
    return lambda: server.gmail_action({"key": KEY, "action": "archive"}), MARK


def auto_clear(wq):
    seed(wq, [done("td-990", f"Merged {pr_url(70)}")])
    gh(wq, (f"pr view {pr_url(70)}", pr("MERGED")))
    return lambda: ready.check({}, None, ""), ready_mark("td-990")


@pytest.mark.parametrize("writer", [set_mark, send_row, archive_email, auto_clear])
def test_every_change_to_marks_waits_for_the_lock_and_keeps_the_other_processs_mark(wq, writer):
    config.write_json(wq.data / "marks.json", {})
    write, key = writer(wq)
    held_marks_survive(wq, write, key)
