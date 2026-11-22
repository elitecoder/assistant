"""Drive the Work queue page in Chromium against a real server on a spare port with temporary data.

Done marks live on the server, so they survive a fresh browser. Send to
Assistant writes a TODO into a temporary store. Approving Gmail and applying
moves it to Trash through a fake gog.

With $WQ_BROWSER_COVERAGE set to a file path, every page here records the V8
coverage of the page's script (work_queue/page.py JS), and the module writes it
there for tests/browser_coverage.cjs and the change-coverage gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error, sync_playwright  # noqa: E402

from work_queue import config, page, refresh, server, triage  # noqa: E402
from work_queue_fakes import (VERDICT, Model, calls, chromium_missing, fake_tool, first_line, gh_pr, raises,  # noqa: E402, F401
                              require_chromium, spare_port, wq)


REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.real_mode
PRS = [{"key": f"https://github.com/o/r/pull/{n}", "link": f"https://github.com/o/r/pull/{n}", "draft": n == 3,
        "title": f"o/r#{n}: Change {n}", "meta": "Review requested · ana · opened Nov 2"} for n in (1, 2, 3)]
MAIL = {"from": "Ana", "day": "Nov 2", "to": ["Me"], "cc": [], "body": "Body text", "unread": False}
AGENT = {**VERDICT, "disposition": "agent"}
COVERAGE = os.environ.get("WQ_BROWSER_COVERAGE")
FOLLOW = ("Its status and result show on the Tasks tab of the Assistant dashboard, and its session on the Sessions tab "
          "once it starts.")
SCRIPTS: list[dict] = []


def snapshot() -> dict:
    gmail_rows = [{**MAIL, "key": "gmail:1", "account": "me@example.com", "msg_ids": ["m1"], "group": "news",
                   "title": "Digest", "subject": "Digest", "reason": "A mailing list or newsletter.", "addr": "news@list.example",
                   "unsub_url": "", "link": "https://mail.google.com/mail/#inbox/t1"}]
    outlook_rows = [{**MAIL, "key": "mail:1", "group": "gh", "title": "PR merged", "subject": "PR merged", "reason": "GitHub.",
                     "addr": "notifications@github.com", "link": "https://outlook.office.com/m1"}]
    return {"built": "Nov 18, 9:00 AM", "github": PRS, "gmail": {"me@example.com": gmail_rows}, "outlook": outlook_rows,
            "status": [{"name": "GitHub", "state": "ok", "detail": "read 3 items"}],
            "triage": {"rows": {PRS[2]["link"]: AGENT}, "start": [], "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []}}


@pytest.fixture(scope="module", autouse=True)
def coverage_file():
    yield
    if COVERAGE:
        assert SCRIPTS, "No Work queue page script was measured"
        Path(COVERAGE).parent.mkdir(parents=True, exist_ok=True)
        Path(COVERAGE).write_text(json.dumps(SCRIPTS))


class Coverage:
    """Measure the page's script on one tab when $WQ_BROWSER_COVERAGE is set; otherwise do nothing."""

    def __init__(self, tab):
        self.cdp, self.parsed = None, {}
        if not COVERAGE:
            return
        self.cdp = tab.context.new_cdp_session(tab)
        self.cdp.on("Debugger.scriptParsed", lambda event: self.parsed.__setitem__(event["scriptId"], event))
        self.cdp.on("Debugger.paused", lambda event: (self.snap(), self.cdp.send("Debugger.resume")))
        self.cdp.send("Profiler.enable")
        self.cdp.send("Debugger.enable")
        self.cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})

    def snap(self) -> None:
        """Keep the counts so far. Call it before the tab navigates away, which drops the old document's counts."""
        if self.cdp is None:
            return
        for entry in self.cdp.send("Profiler.takePreciseCoverage")["result"]:
            if self.source(entry["scriptId"]) == page.JS:
                SCRIPTS.append({**entry, "source": page.JS})

    def source(self, script: str) -> str:
        """A script's text; "" for one the page has already dropped, such as a finished evaluate."""
        try:
            return self.cdp.send("Debugger.getScriptSource", {"scriptId": script})["scriptSource"]
        except Error:
            return ""

    def snap_before(self, statement: str) -> None:
        """Pause the current page's script just before `statement`, which navigates, and keep the counts then."""
        if self.cdp is None:
            return
        script = [sid for sid in list(self.parsed) if self.source(sid) == page.JS][-1]
        lines = page.JS.split("\n")
        line = next(n for n, text in enumerate(lines) if statement in text)
        self.cdp.send("Debugger.setBreakpoint", {"location": {
            "scriptId": script, "lineNumber": self.parsed[script]["startLine"] + line,
            "columnNumber": lines[line].index(statement)}})


def write_page(snap_doc: dict) -> dict:
    doc, cands = page.render(snap_doc)
    refresh.site_path().parent.mkdir(parents=True, exist_ok=True)
    refresh.site_path().write_text(doc)
    config.write_json(refresh.candidates_path(), cands)
    config.write_json(refresh.emails_path(), page.emails(snap_doc))
    return cands


def pro_mode(wq) -> None:
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/config.json").write_text('{"mode": "pro"}')


@pytest.fixture
def served(wq, monkeypatch):
    require_chromium(monkeypatch)
    pro_mode(wq)
    write_page(snapshot())
    fake_tool(wq.bin, "gog", [("gmail trash m1", {"ok": True}), ("gmail get m1", {"message": {"labelIds": ["TRASH"]}})])
    port = spare_port()
    wq.sources({"port": port})
    env = {**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}"}
    proc = subprocess.Popen([sys.executable, "-m", "work_queue.server"], cwd=REPO, env=env, stdout=subprocess.PIPE, text=True)
    assert first_line(proc).startswith("Serving the Work queue")
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def site(wq, monkeypatch):
    """The server in this process, so a test can change its data between requests."""
    require_chromium(monkeypatch)
    pro_mode(wq)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()
    srv.server_close()


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def text(tab, selector: str) -> str:
    return tab.locator(selector).first.text_content()


def test_marks_persist_send_to_agent_queues_a_first_pass_and_apply_trashes_gmail(served, wq):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        first = browser.new_page()
        first.goto(served)
        row = first.locator('.sf-row[data-key="https://github.com/o/r/pull/1"]')
        row.get_by_role("button", name="Done").click()
        assert wait_for(lambda: json.loads((wq.data / "marks.json").read_text() if (wq.data / "marks.json").exists()
                                           else "{}").get(PRS[0]["key"], {}).get("state") == "done")
        first.locator('.sf-row[data-key="https://github.com/o/r/pull/2"]').get_by_role("button", name="Snooze").click()
        first.wait_for_selector('.sf-row[data-key="https://github.com/o/r/pull/2"].is-hidden', state="attached")
        assert first.locator("#showhidden").inner_text() == "Show snoozed and dropped (1)"

        fresh = browser.new_context().new_page()
        fresh.goto(served)
        fresh.wait_for_selector('.sf-row[data-key="https://github.com/o/r/pull/1"].is-done')
        done_row = fresh.locator('.sf-row[data-key="https://github.com/o/r/pull/1"]')
        assert done_row.locator(".sf-mark").inner_text() == "Undo"
        assert fresh.locator(".sf-lane h3 .count").first.inner_text() == "1"

        third = fresh.locator('.sf-row[data-key="https://github.com/o/r/pull/3"]')
        third.get_by_role("button", name="Send to Agent").click()
        third.locator(".cp-go").click()
        fresh.wait_for_selector('.sf-row[data-key="https://github.com/o/r/pull/3"]', state="detached")
        wait_text(fresh, ".toast", "Sent to an agent as td-051 for a first pass. Its status and result show on the Tasks tab of the Assistant dashboard, and its session on the Sessions tab once it starts.")
        todo = wq.items()[-1]
        assert (todo["id"], todo["title"], todo["autoDispatch"], todo["url"]) == \
            ("td-051", "o/r#3: Change 3", True, PRS[2]["link"])
        code = re.search(r"----- BEGIN UNTRUSTED ROW ([0-9a-f]{16}) -----", todo["detail"]).group(1)
        assert todo["detail"].endswith("Title: o/r#3: Change 3\nDetail: Review requested · ana · opened Nov 2 · draft\n"
                                       f"Source: https://github.com/o/r/pull/3\n----- END UNTRUSTED ROW {code} -----")
        assert todo["workQueueMode"] == "assist"
        assert "You were sent this for a first pass." in todo["detail"] and "Automated Message" not in todo["detail"]
        assert json.loads((wq.data / "marks.json").read_text())[PRS[2]["key"]]["state"] == "done"
        assert fresh.locator('.ag-row[data-key="https://github.com/o/r/pull/3"]').count() == 0

        fresh.locator('#tabs .tab[data-t="mailbox"]').click()
        fresh.wait_for_selector("#open-list", state="hidden")
        assert fresh.locator("#email").is_visible()
        fresh.locator("#email > summary").click()
        fresh.locator('#email details').first.evaluate("d => d.open = true")
        fresh.locator('.ap-row[data-key="mail:1"]').get_by_role("button", name="Approve").click()
        fresh.wait_for_function("document.getElementById('mq-q').textContent === 'subject:\"PR merged\"'")

        fresh.locator("#gmail-1 > summary").click()
        fresh.locator('#gmail-1 details').first.evaluate("d => d.open = true")
        fresh.locator('.ap-row[data-key="gmail:1"]').get_by_role("button", name="Approve").click()
        fresh.wait_for_function("document.getElementById('ap-count').textContent === '1 approved'")
        fresh.locator("#ap-apply").click()
        fresh.wait_for_function("document.querySelector('.ap-row[data-key=\"gmail:1\"] .ap-status').textContent"
                                " === 'In Gmail Trash, verified'", timeout=15000)

        phone = browser.new_context(viewport={"width": 375, "height": 800}).new_page()
        phone.goto(served + "#all")
        phone.wait_for_selector('.sf-row[data-key="https://github.com/o/r/pull/2"]', state="attached")
        phone.wait_for_selector('.sf-row[data-key="https://github.com/o/r/pull/3"]', state="detached")
        assert phone.locator('.sf-row[data-key="https://github.com/o/r/pull/3"]').count() == 0, "a sent row stays off"
        widths = phone.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert widths[0] <= widths[1], widths
        browser.close()


# ─── every state the page shows, on the in-process server ────────────────────

NOW_MS = int(time.time() * 1000)
DAY_MS = 86400000


def pr(n: int) -> dict:
    return {"key": f"https://github.com/o/r/pull/{n}", "link": f"https://github.com/o/r/pull/{n}", "draft": False,
            "title": f"o/r#{n}: Change {n}", "meta": "Review requested"}


def say(who: str, words: str) -> dict:
    return {"who": who, "ts": str(NOW_MS // 1000), "text": words}


def slack_row(key: str, group: str, n: int) -> dict:
    return {"key": key, "group": group, "title": f"Save {key}", "who": "Ana", "when": "Nov 2", "cid": "C1", "ts": f"{n}.1",
            "link": f"https://acme.slack.com/archives/C1/p{n}", "reason": "You had the last word.", "evidence": "Me: done",
            "where": "#general", "ctx": {"root": None, "msg": say("Ana", "Can you look?"), "after": [say("Me", "done")],
                                         "after_n": 1}}


def mail(key: str, group: str, addr: str, subject: str, **extra) -> dict:
    return {**MAIL, "key": key, "group": group, "title": subject or "(no subject)", "subject": subject, "addr": addr,
            "reason": "Automated.", "link": f"https://outlook.office.com/{key}", **extra}


def gmail_row(key: str, group: str, addr: str = "digest@list.example", unsub: str = "") -> dict:
    return {**MAIL, "key": key, "account": "me@example.com", "msg_ids": [f"id-{key}"], "group": group, "title": f"Mail {key}",
            "subject": f"Mail {key}", "reason": "A newsletter.", "addr": addr, "unsub_url": unsub,
            "link": f"https://mail.google.com/mail/#inbox/{key}"}


def rich_snapshot() -> dict:
    slack = [slack_row("s-open", "open", 1), slack_row("s-done-v", "last", 2), slack_row("s-done-nv", "last", 3),
             slack_row("s-undone", "moved", 4), slack_row("s-undo", "moved", 5), slack_row("s-gone", "last", 7),
             {**slack_row("s-unread", "unread", 6), "ctx": {"error": "gone"}}]
    outlook = [mail("o-act", "act", "ana@example.com", "Can you review?"), mail("o-read", "read", "", ""),
               mail("a1", "gh", "longsender@example.com", "Hi"), mail("b1", "bulk", "same@example.com", "Same"),
               mail("b2", "bulk", "same@example.com", "Same")]
    gmail = [gmail_row("g-act", "act", "friend@example.com"), gmail_row("g-look", "unsorted", "friend@example.com")]
    gmail += [gmail_row(k, "news") for k in ("g-plain", "g-approve", "g-done-v", "g-done-nv", "g-err", "g-undone",
                                             "g-keep", "g-undo-done", "g-undo")]
    gmail += [gmail_row("g-shop", "shop"), gmail_row("u-a", "note", "a@promo.example", "https://promo.example/a"),
              gmail_row("u-b", "note", "b@promo.example", "https://promo.example/b")]
    return {"built": "Nov 18, 9:00 AM", "github": [pr(n) for n in range(1, 18)], "slack": slack, "outlook": outlook,
            "triage": {"rows": {pr(n)["link"]: AGENT for n in range(1, 11)}, "start": [], "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []},
            "gmail": {"me@example.com": gmail}, "calendar": [{"when": "9:00 AM", "title": "Standup", "account": "me@example.com",
                                                             "declined": False, "link": "https://calendar.google.com/e1"}],
            "status": [{"name": "GitHub", "state": "ok", "detail": "read 17 items"},
                       {"name": "Outlook", "state": "error", "detail": "expired"}]}


def seed(wq) -> dict:
    """Write the page and every saved state its rows can be in."""
    cands = write_page(rich_snapshot())
    unsub = {c["url"]: k for k, c in cands.items() if c["kind"] == "unsub"}
    decisions = {"g-approve": "approve", "g-keep": "keep", "g-undo-done": "undo", "g-undo": "undo", "s-undo": "undo",
                 "b2": "keep", "g-shop": "keep", "s-gone": "approve"}
    config.write_json(wq.data / "decisions.json", {k: {"decision": d, "at": "t"} for k, d in decisions.items()})
    applied = {"g-done-v": {"status": "done", "verified": True}, "g-done-nv": {"status": "done", "verified": False},
               "g-err": {"status": "error", "error": "quota"}, "g-undone": {"status": "undone"},
               "g-undo-done": {"status": "done", "verified": True}, "s-done-v": {"status": "done", "verified": True},
               "s-done-nv": {"status": "done"}, "s-undone": {"status": "undone"},
               "s-gone": {"status": "gone", "error": "Slack no longer has this save", "sent": True},
               unsub["https://promo.example/a"]: {"status": "done", "verified": True},
               unsub["https://promo.example/b"]: {"status": "done", "verified": False}}
    config.write_json(wq.data / "applied.json", applied)
    config.write_json(wq.data / "apply-status.json", {"running": False, "finished": "t", "verified": 2, "gmail": 3,
                                                       "error": "Gmail: quota"})
    config.write_json(wq.data / "marks.json", {
        pr(11)["key"]: {"state": "done", "until": 0}, pr(12)["key"]: {"state": "dropped", "until": 0},
        pr(13)["key"]: {"state": "snoozed", "until": NOW_MS + DAY_MS}, pr(14)["key"]: {"state": "snoozed", "until": NOW_MS - 1000}})
    items = [{"id": "td-041", "status": "open"},
             {"id": "td-101", "status": "done", "detail": "x\n\nRESULT: Filed https://jira.example/T-1 for you"},
             {"id": "td-102", "status": "done"}, {"id": "td-103", "status": "blocked", "statusReason": "Need access"},
             {"id": "td-104", "status": "blocked"}, {"id": "td-105", "status": "in-progress"},
             {"id": "td-106", "status": "open", "dispatchedWs": "workspace:3"}, {"id": "td-107", "status": "open"}]
    wq.todo.write_text(json.dumps({"items": items}))
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": f"td-1{n:02d}" if n < 8 else "td-199", "at": "t"}
                                                 for n in range(1, 9)})
    return unsub


def status_of(tab, key: str) -> str:
    return tab.locator(f'.ap-row[data-key="{key}"] .ap-status').text_content()


def wait_text(tab, selector: str, expected: str) -> None:
    tab.wait_for_function("([s, t]) => { const el = document.querySelector(s); return !!el && el.textContent === t }",
                          arg=[selector, expected])


def test_the_page_shows_every_saved_state(site, wq, monkeypatch):
    unsub = seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        tab = context.new_page()
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#email")
        wait_text(tab, '.ap-row[data-key="g-done-v"] .ap-status', "In Gmail Trash, verified")
        assert status_of(tab, "g-done-nv") == "In Gmail Trash, not verified"
        assert status_of(tab, "g-err") == "Failed: quota"
        assert status_of(tab, "g-undone") == "Back in your inbox"
        assert status_of(tab, "g-keep") == "Kept" and status_of(tab, "g-undo") == "Will restore"
        assert status_of(tab, "g-undo-done") == "In Gmail Trash, verified"
        assert status_of(tab, "g-plain") == "" and status_of(tab, "g-approve") == ""
        assert status_of(tab, "s-done-v") == "Marked done in Slack, verified"
        assert status_of(tab, "s-done-nv") == "Marked done in Slack, not verified"
        assert status_of(tab, "s-undone") == "Reopened in Slack" and status_of(tab, "s-undo") == "Will reopen"
        assert status_of(tab, "s-gone") == "Slack no longer has this save"
        gone = tab.locator('.ap-row[data-key="s-gone"]').get_attribute("class")
        assert "applied" in gone and "approved" not in gone, "an approved save that's gone is settled"
        assert status_of(tab, unsub["https://promo.example/a"]) == "Unsubscribed"
        assert status_of(tab, unsub["https://promo.example/b"]) == "Sent, but the sender didn’t confirm"
        assert status_of(tab, "b2") == "Kept" and status_of(tab, "a1") == ""
        assert text(tab, "#ap-count") == "1 approved, 1 to reopen"
        assert not tab.locator("#ap-apply").is_disabled()
        assert text(tab, "#ap-msg") == "Last run: 2 of 3 verified · Gmail: quota"
        assert text(tab, '.ap-group[data-group="gmail-shop"] .ap-all') == "All decided"
        assert tab.locator('.ap-group[data-group="gmail-shop"] .ap-all').is_disabled()
        assert text(tab, '.ap-group[data-group="gmail-news"] .ap-all') == "Approve all 4"
        assert text(tab, "#mq-q") == "Approve emails below to build your search." and tab.locator("#mq-copy").is_disabled()
        assert tab.locator("#email").is_visible() and not tab.locator("#open-list").is_visible()
        tab.evaluate("location.hash = '#all'")
        tab.wait_for_selector("#open-list", state="visible")
        tab.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")

        for n in range(1, 8):
            assert tab.locator(f'.sf-row[data-key="{pr(n)["key"]}"]').count() == 0, f"sent row {n} leaves the page"
            assert tab.locator(f'.ag-row[data-key="{pr(n)["key"]}"]').count() == 0
        assert tab.locator(f'.sf-row[data-key="{pr(8)["key"]}"] .sf-send').is_enabled(), "its TODO is gone, so it's back"
        assert text(tab, '#tabs .tab[data-t="open-list"] span') == "10"
        assert "Queued as" not in tab.content() and "Working on it" not in tab.content()

        rows = {n: tab.locator(f'.sf-row[data-key="{pr(n)["key"]}"]') for n in range(11, 18)}
        assert "is-done" in rows[11].get_attribute("class") and text(tab, f'.sf-row[data-key="{pr(11)["key"]}"] .sf-mark') == "Undo"
        assert "is-hidden" in rows[12].get_attribute("class") and "is-hidden" in rows[13].get_attribute("class")
        assert "is-hidden" not in rows[14].get_attribute("class"), "a snooze whose time has passed shows the row again"
        assert text(tab, "#showhidden") == "Show snoozed and dropped (2)"

        # Mail approvals build the narrowest Outlook search, and say which mail it can't single out.
        tab.locator('.ap-row[data-key="a1"] .ap-ok').click()
        wait_text(tab, "#mq-q", 'subject:"Hi"')
        assert status_of(tab, "a1") == "Approved: in your Outlook search"
        assert text(tab, "#mq-note").startswith("Finds the 1 emails you approved")
        tab.locator('.ap-row[data-key="b1"] .ap-ok').click()
        wait_text(tab, '.ap-row[data-key="b1"] .ap-status', "Approved. No search can single this one out, so delete it by hand")
        assert text(tab, "#mq-note").endswith("1 approved emails share a sender and subject with ones you kept, so delete those by hand.")
        tab.locator("#mq-copy").click()
        wait_text(tab, "#mq-copy", "Copied")
        assert tab.evaluate("navigator.clipboard.readText()") == 'subject:"Hi"'
        tab.locator('.ap-row[data-key="b1"] .ap-clear').click()
        wait_text(tab, '.ap-row[data-key="b1"] .ap-status', "")
        tab.locator('.ap-group[data-group="gmail-news"] .ap-all').click()
        wait_text(tab, "#ap-count", "5 approved, 1 to reopen")
        assert json.loads((wq.data / "decisions.json").read_text())["g-plain"]["decision"] == "approve"
        tab.route("**/api/decide", lambda route: route.abort())
        tab.locator('.ap-row[data-key="g-keep"] .ap-ok').click()
        tab.wait_for_function("document.getElementById('ap-msg').textContent.startsWith('Not saved: ')")
        tab.unroute("**/api/decide")

        # Apply: the bar works, polls, and reports a run that's already going.
        monkeypatch.setattr(server, "run_apply", lambda: None)
        tab.locator("#ap-apply").click()
        wait_text(tab, "#ap-apply", "Working…")
        assert tab.locator("#ap-apply").is_disabled()
        assert text(tab, "#ap-msg").startswith("Working. Slack changes run in your Slack browser session")
        tab.clock.run_for(1500)
        wait_text(tab, "#ap-apply", "Working…")
        config.write_json(wq.data / "apply-status.json", {"running": False, "finished": "t2"})
        tab.clock.run_for(2500)
        wait_text(tab, "#ap-msg", "Last run: 0 of 0 verified")
        tab.route("**/api/apply", lambda route: route.fulfill(status=409, json={"error": "already running"}))
        tab.locator("#ap-apply").click()
        wait_text(tab, "#ap-msg", "Not started: already running")
        tab.unroute("**/api/apply")

        # Open list: Done toggles, a failed save says so, and Send to Agent reports errors.
        tab.evaluate("location.hash = '#open-list'")
        tab.wait_for_selector("#open-list", state="visible")
        row15 = f'.sf-row[data-key="{pr(15)["key"]}"]'
        tab.locator(f"{row15} .sf-mark").click()
        wait_text(tab, f"{row15} .sf-mark", "Undo")
        tab.locator(f"{row15} .sf-mark").click()
        wait_text(tab, f"{row15} .sf-mark", "Done")
        assert pr(15)["key"] not in json.loads((wq.data / "marks.json").read_text())
        tab.locator(f'.sf-row[data-key="{pr(16)["key"]}"]').get_by_role("button", name="Snooze").click()
        wait_text(tab, "#showhidden", "Show snoozed and dropped (3)")
        assert json.loads((wq.data / "marks.json").read_text())[pr(16)["key"]]["until"] > NOW_MS + 2 * DAY_MS
        tab.locator("#showhidden").click()
        wait_text(tab, "#showhidden", "Hide snoozed and dropped (3)")
        tab.locator("#showhidden").click()
        wait_text(tab, "#showhidden", "Show snoozed and dropped (3)")
        tab.route("**/api/mark", lambda route: route.fulfill(status=500, body="nope"))
        tab.locator(f'.sf-row[data-key="{pr(17)["key"]}"]').get_by_role("button", name="Drop").click()
        wait_text(tab, ".toast", "Not saved: 500")
        tab.unroute("**/api/mark")
        tab.clock.run_for(12000)
        tab.wait_for_selector(".toast", state="detached")
        tab.locator(f'.sf-row[data-key="{pr(9)["key"]}"] .sf-send').click()
        tab.locator(f'.sf-row[data-key="{pr(9)["key"]}"] .cp-go').click()
        tab.wait_for_selector(f'.sf-row[data-key="{pr(9)["key"]}"]', state="detached")
        wait_text(tab, ".toast", "Sent to an agent as td-108 for a first pass. Its status and result show on the Tasks tab of the Assistant dashboard, and its session on the Sessions tab once it starts.")
        assert tab.locator(f'.ag-row[data-key="{pr(9)["key"]}"]').count() == 0
        tab.clock.run_for(12000)
        tab.wait_for_selector(".toast", state="detached")
        tab.route("**/api/todo", lambda route: route.fulfill(status=500, json={"error": "store is busy"}))
        tab.locator(f'.sf-row[data-key="{pr(10)["key"]}"] .sf-send').click()
        tab.locator(f'.sf-row[data-key="{pr(10)["key"]}"] .cp-go').click()
        wait_text(tab, ".toast", "Not sent: store is busy")
        assert text(tab, f'.sf-row[data-key="{pr(10)["key"]}"] .sf-send') == "Send to Agent"
        assert tab.locator(f'.sf-row[data-key="{pr(10)["key"]}"] .sf-send').is_enabled()
        tab.unroute("**/api/todo")
        tab.clock.run_for(12000)
        tab.wait_for_selector(".toast", state="detached")

        # The page copes with a server that's gone or answers with missing fields.
        tab.route("**/api/state", lambda route: route.abort())
        tab.evaluate("refresh()")
        wait_text(tab, "#ap-count", "Approvals need the Work queue server")
        assert tab.locator("#ap-apply").is_disabled()
        tab.unroute("**/api/state")
        bare = {"decisions": {}, "applied": {}, "status": {"running": True}, "sent": None, "marks": None, "refresh": None}
        tab.route("**/api/state", lambda route: route.fulfill(json=bare))
        tab.evaluate("refresh()")
        wait_text(tab, "#ap-apply", "Working…")
        bare["status"] = {}
        tab.clock.run_for(2500)
        wait_text(tab, "#ap-apply", "Apply approvals")
        assert text(tab, "#ap-count") == "0 approved"
        assert text(tab, "#showhidden") == "Show snoozed and dropped (0)"
        assert tab.locator(f'.sf-row[data-key="{pr(8)["key"]}"]').count() == 1, "no sent list removes nothing more"
        tab.unroute("**/api/state")

        # Refresh: the button polls, shows a failed refresh, and reloads the page when it finishes.
        monkeypatch.setattr(server, "start_refresh", lambda: (config.write_json(refresh.status_path(), {"running": True}),
                                                             (202, {"started": True}))[1])
        tab.route("**/api/refresh", lambda route: route.fulfill(status=409, json={"error": "already refreshing"}))
        tab.locator("#refresh").click()
        wait_text(tab, "#refresh-msg", "Not started: already refreshing")
        tab.unroute("**/api/refresh")
        tab.locator("#refresh").click()
        wait_text(tab, "#refresh", "Refreshing…")
        assert text(tab, "#refresh-msg") == "Reading your sources. This page reloads when it finishes."
        tab.clock.run_for(2500)
        wait_text(tab, "#refresh", "Refreshing…")
        config.write_json(refresh.status_path(), {"running": False, "finished": "t", "error": "Slack: expired"})
        cov.snap_before("location.reload()")
        with tab.expect_event("load"):
            tab.clock.run_for(2500)
        wait_text(tab, "#refresh-msg", "Last refresh failed: Slack: expired")

        # Tabs: a hash picks one, an unknown one falls back to the Open list, and All shows every section.
        tab.evaluate("location.hash = '#nope'")
        tab.wait_for_selector("#open-list", state="visible")
        assert tab.locator("#email").is_hidden()
        tab.evaluate("location.hash = '#all'")
        tab.wait_for_selector("#email", state="visible")
        assert tab.locator("#open-list").is_visible() and "on" in tab.locator('#tabs .tab[data-t="all"]').get_attribute("class")
        cov.snap()
        browser.close()


def test_the_page_works_without_storage_and_remembers_the_last_tab(site, wq):
    seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        blocked = browser.new_context()
        blocked.add_init_script("Storage.prototype.getItem = () => { throw new Error('blocked') };"
                                "Storage.prototype.setItem = () => { throw new Error('blocked') };")
        tab = blocked.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector("#open-list", state="visible")
        tab.locator('#tabs .tab[data-t="calendar"]').click()
        tab.wait_for_selector("#calendar", state="visible")
        cov.snap()

        remembering = browser.new_context().new_page()
        cov = Coverage(remembering)
        remembering.goto(site + "#gmail-1")
        remembering.wait_for_selector("#gmail-1", state="visible")
        cov.snap()
        remembering.goto(site)
        remembering.wait_for_selector("#gmail-1", state="visible")
        assert remembering.locator("#open-list").is_hidden()
        cov.snap()
        browser.close()


def test_the_placeholder_page_runs_the_same_script(site, wq):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(site)
        wait_text(tab, "#refresh", "Refresh")
        assert "Nothing to show yet" in tab.content() and errors == []
        cov.snap()
        browser.close()


# ─── the page rules these tests pin down ─────────────────────────────────────

def test_a_snooze_hides_a_row_only_until_its_time_passes(site, wq):
    write_page({**rich_snapshot(), "slack": None, "outlook": None, "gmail": {}, "calendar": []})
    config.write_json(wq.data / "marks.json", {pr(1)["key"]: {"state": "snoozed", "until": NOW_MS + DAY_MS},
                                                pr(2)["key"]: {"state": "snoozed", "until": NOW_MS - 1000}})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector(f'.sf-row[data-key="{pr(1)["key"]}"].is-hidden', state="attached")
        assert "is-hidden" not in tab.locator(f'.sf-row[data-key="{pr(2)["key"]}"]').get_attribute("class")
        assert text(tab, "#showhidden") == "Show snoozed and dropped (1)"
        cov.snap()
        browser.close()


def test_done_can_be_undone(site, wq):
    write_page({**rich_snapshot(), "slack": None, "outlook": None, "gmail": {}, "calendar": []})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        mark = f'.sf-row[data-key="{pr(1)["key"]}"] .sf-mark'
        tab.locator(mark).click()
        wait_text(tab, mark, "Undo")
        assert json.loads((wq.data / "marks.json").read_text())[pr(1)["key"]]["state"] == "done"
        tab.locator(mark).click()
        wait_text(tab, mark, "Done")
        assert json.loads((wq.data / "marks.json").read_text()) == {}
        assert "is-done" not in tab.locator(f'.sf-row[data-key="{pr(1)["key"]}"]').get_attribute("class")
        cov.snap()
        browser.close()


def test_the_apply_bar_stays_disabled_until_something_can_be_applied(site, wq, monkeypatch):
    write_page({"built": "now", "gmail": {"me@example.com": [gmail_row("g1", "news")]},
                "status": [{"name": "Gmail", "state": "ok", "detail": "read 1 item"}]})
    monkeypatch.setattr(server, "run_apply", lambda: None)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site + "#gmail-1")
        wait_text(tab, "#ap-count", "0 approved")
        assert tab.locator("#ap-apply").is_disabled()
        tab.evaluate("document.querySelector('#gmail-1 details').open = true")
        tab.locator('.ap-row[data-key="g1"] .ap-ok').click()
        wait_text(tab, "#ap-count", "1 approved")
        assert tab.locator("#ap-apply").is_enabled()
        tab.locator("#ap-apply").click()
        wait_text(tab, "#ap-apply", "Working…")
        assert tab.locator("#ap-apply").is_disabled()
        cov.snap()
        browser.close()


# ─── triage: Start here, drafts, Send to Assistant, and the failure note ─────

def triaged_site(wq, model) -> None:
    """Refresh three pull requests through a fake gh and the fake model, as the real refresh does."""
    fake_tool(wq.bin, "gh", [("search prs", [gh_pr(n) for n in (1, 2, 3)])])
    wq.sources({"github": {}})
    refresh.run(model=model)


def rows_for(items):
    by_title = {"o/r#1: Change 1": {**VERDICT, "draft": "Looking at it today."}, "o/r#2: Change 2": AGENT}
    return {"rows": [{"id": i["id"], **by_title.get(i["title"], VERDICT)} for i in items]}


def li(n: int) -> str:
    return f'.start-list li[data-key="https://github.com/o/r/pull/{n}"]'


def no_sideways_scroll(tab) -> None:
    widths = tab.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1], widths


def test_start_here_done_hides_an_item_and_survives_a_fresh_browser(site, wq):
    triaged_site(wq, Model(rows=rows_for))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        tab = context.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector("#start", state="visible")
        assert tab.locator("#open-list").is_hidden()
        assert [tab.locator(li(n)).is_visible() for n in (1, 2, 3)] == [True, True, True]
        assert text(tab, f"{li(1)} .ap-why") == "Start with o/r#1: Change 1"
        badge = {t: f'#tabs .tab[data-t="{t}"] span' for t in ("start", "open-list")}
        assert (text(tab, badge["start"]), text(tab, badge["open-list"]), text(tab, "#open-total")) == ("3", "3", "3")
        tab.locator(f"{li(1)} .sf-mark").click()
        tab.wait_for_selector(li(1), state="hidden")
        assert (text(tab, badge["start"]), text(tab, badge["open-list"]), text(tab, "#open-total")) == ("2", "2", "2")
        assert wait_for(lambda: json.loads((wq.data / "marks.json").read_text())[pr(1)["key"]]["state"] == "done")

        tab.locator('#tabs .tab[data-t="open-list"]').click()
        row1, row2 = f'.sf-row[data-key="{pr(1)["key"]}"]', f'.sf-row[data-key="{pr(2)["key"]}"]'
        tab.wait_for_selector(f"{row1}.is-done")
        assert tab.locator(f"{row1} .sf-send").count() == 1 and tab.locator(f"{row2} .sf-send").count() == 1
        assert text(tab, f"{row1} .disp") == "Model suggests: You" and text(tab, f"{row2} .disp") == "Agent can finish"
        tab.locator(f"{row1} .draft button").click()
        wait_text(tab, f"{row1} .draft button", "Copied")
        assert tab.evaluate("navigator.clipboard.readText()") == "Looking at it today."
        tab.locator(f"{row2} .sf-send").click()
        tab.locator(f"{row2} .cp-go").click()
        tab.wait_for_selector(row2, state="detached")
        assert json.loads((wq.data / "marks.json").read_text())[pr(2)["key"]]["state"] == "done"
        assert (text(tab, badge["start"]), text(tab, badge["open-list"])) == ("1", "1")
        tab.locator('#tabs .tab[data-t="start"]').click()
        tab.wait_for_selector(li(3), state="visible")
        assert tab.locator(li(2)).count() == 0, "a sent row's Start here item leaves the page with it"
        for width in (390, 1280):
            tab.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(tab)
        cov.snap()

        fresh = browser.new_context().new_page()
        cov = Coverage(fresh)
        fresh.goto(site + "#start")
        fresh.wait_for_selector(li(3), state="visible")
        fresh.wait_for_selector(li(1), state="hidden")
        fresh.wait_for_selector(li(2), state="detached")
        assert fresh.locator(li(1)).get_attribute("hidden") is not None and fresh.locator(li(2)).count() == 0
        for width in (390, 1280):
            fresh.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(fresh)
        cov.snap()
        browser.close()


def stop(message: str):
    def call(items):
        raise triage.Stop(message)
    return call


TIMEOUT = "the model didn't answer within 240 seconds"
STOP = "the model took longer than 10 minutes in all"


@pytest.mark.parametrize("model, expected", [
    (lambda: Model(rows=raises(TIMEOUT), start=raises(TIMEOUT)),
     f"Part of triage didn't finish. Row triage: {TIMEOUT}. Start here: {TIMEOUT}. Each part is asked again tomorrow, "
     "or sooner when its rows change."),
    (lambda: Model(rows=stop(STOP)),
     f"Triage unavailable: {STOP}. Rows without a model verdict still show, grouped by the rules, and offer Send to "
     "Agent."),
])
def test_the_failure_note_shows_when_the_model_fails(site, wq, model, expected):
    triaged_site(wq, model())
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 800})
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector("#open-list .triage-note", state="visible")
        assert tab.locator("#start").is_hidden(), "an empty Start here list doesn't open first"
        assert text(tab, "#open-list .triage-note") == expected
        tab.locator('#tabs .tab[data-t="start"]').click()
        tab.wait_for_selector("#start .triage-note", state="visible")
        assert tab.locator(".sf-row").count() == 3 and tab.locator(".sf-row .sf-send").count() == 3
        assert tab.locator(".sf-row .disp").count() == 0
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


def test_a_sent_row_leaves_the_page_and_a_blocked_one_stays_on_start_here(site, wq):
    start = [{"key": pr(2)["key"], "title": "o/r#2: Change 2", "source": "GitHub", "effort": "",
              "reason": triage.BLOCKED_REASON, "blocked": True},
             {"key": pr(6)["key"], "title": "o/r#6: Change 6", "source": "GitHub", "effort": "", "reason": "Go",
              "blocked": False}]
    yours = {"rows": {pr(n)["key"]: VERDICT for n in range(1, 7)}, "start": start, "changes": None, "waiting": 0,
             "skipped": 0, "note": "", "failed": []}
    write_page({"built": "now", "github": [pr(n) for n in range(1, 7)], "status": [], "triage": yours})
    config.write_json(wq.data / "marks.json", {pr(n)["key"]: {"state": "done", "until": 0} for n in (1, 2, 4, 5)})
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": td, "at": "t"} for n, td in
                                                 ((1, "td-101"), (2, "td-102"), (4, "td-199"), (5, "td-105"))})
    wq.todo.write_text(json.dumps({"items": [
        {"id": "td-101", "status": "open"}, {"id": "td-102", "status": "blocked", "statusReason": "Need access"}],
        "completed": [{"id": "td-105", "status": "done", "detail": "x\n\nRESULT: Opened PR 9"}]}))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (390, 1280):
            tab = browser.new_page(viewport={"width": width, "height": 900})
            cov = Coverage(tab)
            tab.goto(site + "#all")
            row = {n: f'.sf-row[data-key="{pr(n)["key"]}"]' for n in range(1, 7)}
            tab.wait_for_selector(row[4], state="visible")
            for n in (1, 2, 5):
                assert tab.locator(row[n]).count() == 0 and tab.locator(f'.ag-row[data-key="{pr(n)["key"]}"]').count() == 0
            assert "is-done" in tab.locator(row[4]).get_attribute("class"), "its TODO is gone, so it shows as Done"
            assert text(tab, f"{row[4]} .sf-mark") == "Undo" and text(tab, ".sf-lane h3 .count") == "2"
            assert tab.locator(li(2)).is_visible(), "a blocked TODO keeps its row on Start here"
            assert text(tab, li(2) + " .ap-why") == triage.BLOCKED_REASON
            assert tab.locator(".q-status").count() == 0 and tab.locator(".is-blocked").count() == 0
            no_sideways_scroll(tab)
            cov.snap()
        browser.close()


def test_start_here_hides_marked_items_except_a_passed_snooze_or_a_blocked_todo(site, wq):
    start = [{"key": pr(n)["key"], "title": f"o/r#{n}: Change {n}", "source": "GitHub", "effort": "",
              "reason": triage.BLOCKED_REASON if n in (3, 4) else "Go", "blocked": n == 3} for n in range(1, 6)]
    write_page({"built": "now", "github": [pr(n) for n in range(1, 6)], "status": [],
                "triage": {"rows": {}, "start": start, "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []}})
    config.write_json(wq.data / "marks.json", {
        pr(1)["key"]: {"state": "snoozed", "until": NOW_MS + DAY_MS}, pr(2)["key"]: {"state": "snoozed", "until": NOW_MS - 1000},
        pr(3)["key"]: {"state": "done", "until": 0}, pr(4)["key"]: {"state": "done", "until": 0}})
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": f"td-10{n}", "at": "t"} for n in (3, 4)})
    wq.todo.write_text(json.dumps({"items": [{"id": "td-103", "status": "blocked"}, {"id": "td-104", "status": "open"}]}))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector(li(4), state="detached")
        tab.wait_for_selector(li(1), state="hidden")
        assert tab.locator(li(4)).count() == 0, "a sent item whose reason merely reads blocked leaves like any other"
        assert [tab.locator(li(n)).is_visible() for n in (2, 3, 5)] == [True, True, True]
        assert text(tab, '#tabs .tab[data-t="start"] span') == "3"
        cov.snap()
        browser.close()


@pytest.mark.parametrize("iso, shown", [("2026-11-19T17:05:00+00:00", "Nov 20, 2:05 AM"),
                                        ("not a time", "Nov 19, 9:05 AM"), ("", "Nov 19, 9:05 AM")])
def test_last_refreshed_shows_in_the_viewers_local_time(site, wq, iso, shown):
    write_page({"built": "Nov 19, 9:05 AM", "built_iso": iso, "github": [pr(1)], "status": []})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(timezone_id="Asia/Tokyo", locale="en-US").new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#built", shown)
        assert text(tab, ".sf-head .meta").startswith(f"Last refreshed {shown} from your linked sources.")
        cov.snap()
        browser.close()


def test_a_missing_chromium_skips_the_browser_tests_instead_of_failing_them(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    assert "Executable doesn't exist" in chromium_missing(str(tmp_path / "no-browsers"))
    with pytest.raises(pytest.skip.Exception, match="Playwright's Chromium isn't installed"):
        require_chromium(monkeypatch)


# ─── Send to agents ───────────────────────────────────────────────────────────

def agents_snapshot() -> dict:
    slack = [{**slack_row("s-open", "open", 1), "title": "#general: Can you look?"}]
    outlook = [mail("o-act", "act", "ana@example.com", "Can you review?")]
    gmail = [gmail_row("g-act", "act", "friend@example.com")]
    verdicts = {pr(3)["link"]: AGENT, gmail[0]["link"]: AGENT, pr(1)["link"]: VERDICT}
    return {"built": "now", "github": [pr(n) for n in range(1, 5)], "slack": slack, "outlook": outlook,
            "gmail": {"me@example.com": gmail}, "status": [],
            "triage": {"rows": verdicts, "start": [], "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []}}


def ag(key: str) -> str:
    return f'.ag-row[data-key="{key}"]'


def test_send_to_agents_lists_every_open_row_and_sends_the_picked_ones_one_at_a_time(site, wq, monkeypatch):
    write_page(agents_snapshot())
    config.write_json(wq.data / "marks.json", {pr(4)["key"]: {"state": "dropped", "until": 0}})
    spans, real = [], server.ROUTES["/api/todo"]
    inflight, go_on = threading.Event(), threading.Event()

    def timed(body):
        start = time.monotonic()
        inflight.set()
        go_on.wait(10)
        time.sleep(0.2)
        out = real(body) if body["key"] != pr(2)["key"] else (500, {"error": "store is busy"})
        spans.append((start, time.monotonic(), body["key"], body["mode"]))
        return out
    monkeypatch.setitem(server.ROUTES, "/api/todo", timed)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.goto(site + "#agents")
        tab.wait_for_selector("#agents", state="visible")
        titles = [t.strip() for t in tab.locator(".ag-row:not([hidden]) .ag-text .sf-title").all_inner_texts()]
        assert titles == ["Agent can finisho/r#3: Change 3", "Agent can finishMail g-act", "#general: Can you look?",
                          "o/r#1: Change 1", "o/r#2: Change 2", "Can you review?"], "the dropped row stays out"
        assert text(tab, '#tabs .tab[data-t="agents"] span') == "6"
        assert text(tab, "#ag-go") == "Send 0 to agents" and tab.locator("#ag-go").is_disabled()
        assert not tab.locator("#ag-hands").is_checked(), "Hands-off is off until you turn it on"
        tab.locator("#ag-all").check()
        wait_text(tab, "#ag-go", "Send 6 to agents")
        tab.locator("#ag-all").uncheck()
        wait_text(tab, "#ag-go", "Send 0 to agents")

        dialogs = []
        answer = {"accept": False}

        def on_dialog(d):
            dialogs.append(d.message)
            d.accept() if answer["accept"] else d.dismiss()
        tab.on("dialog", on_dialog)
        for n in (1, 2):
            tab.locator(f'{ag(pr(n)["key"])} .ag-pick').check()
        wait_text(tab, "#ag-go", "Send 2 to agents")
        tab.locator("#ag-go").click()
        assert wait_for(lambda: dialogs) and dialogs[-1] == ("Start 2 agents? Each one takes a first pass and leaves "
                                                             "drafts for you to send.")
        tab.wait_for_timeout(1500)
        assert spans == [] and wq.items()[-1]["id"] == "td-041", "a cancelled dialog sends nothing"
        assert text(tab, "#ag-msg") == "" and tab.locator(ag(pr(1)["key"])).count() == 1
        answer["accept"] = True
        tab.locator("#ag-go").click()
        assert inflight.wait(10)
        wait_text(tab, "#ag-msg", "Sending 1 of 2...")
        assert tab.locator("#ag-go").is_disabled(), "no second batch while one is going"
        go_on.set()
        wait_text(tab, "#ag-msg", "Sent 1 to agents for a first pass. Their status and results show on the Tasks tab of the Assistant dashboard, and each session on the Sessions tab once it starts. "
                                  "Not sent: o/r#2: Change 2: store is busy.")
        assert [(k, m) for _, _, k, m in spans] == [(pr(1)["key"], "assist"), (pr(2)["key"], "assist")]
        assert spans[0][1] <= spans[1][0], "one send at a time"
        assert tab.locator(ag(pr(1)["key"])).count() == 0 and tab.locator(f'.sf-row[data-key="{pr(1)["key"]}"]').count() == 0
        assert tab.locator(f'{ag(pr(2)["key"])} .ag-pick').is_checked(), "a row that wasn't sent stays picked"
        assert text(tab, "#ag-go") == "Send 1 to agents" and text(tab, '#tabs .tab[data-t="agents"] span') == "5"
        todo = wq.items()[-1]
        assert (todo["id"], todo["url"]) == ("td-051", pr(1)["link"]) and "for a first pass" in todo["detail"]

        tab.locator(f'{ag(pr(2)["key"])} .ag-pick').uncheck()
        tab.locator("#ag-hands").check()
        for key in (pr(3)["key"], "https://outlook.office.com/o-act"):
            tab.locator(f"{ag(key)} .ag-pick").check()
        tab.locator("#ag-go").click()
        wait_text(tab, "#ag-msg", "Sent 2 to agents, hands-off. Their status and results show on the Tasks tab of the Assistant dashboard, and each session on the Sessions tab once it starts.")
        assert dialogs[-1] == ("Start 2 agents? Hands-off: each one finishes the work and posts, files, and opens PRs "
                               "without asking you.")
        sent = wq.items()[-2:]
        assert [i["url"] for i in sent] == [pr(3)["link"], "https://outlook.office.com/o-act"]
        assert all("hands-off" in i["detail"] and 'start each with "Automated Message"' in i["detail"] for i in sent)
        assert {k: q["mode"] for k, q in json.loads((wq.data / "queued.json").read_text()).items()} == {
            pr(1)["key"]: "assist", pr(3)["key"]: "handsoff", "https://outlook.office.com/o-act": "handsoff"}

        tab.locator("#ag-hands").uncheck()
        tab.locator(f'{ag(pr(2)["key"])} .ag-pick').check()
        tab.locator("#ag-go").click()
        assert wait_for(lambda: dialogs[-1].startswith("Start 1 agent? "))
        wait_text(tab, "#ag-msg", "Not sent: o/r#2: Change 2: store is busy.")
        wait_text(tab, "#ag-go", "Send 1 to agents")
        assert tab.locator("#ag-go").is_enabled()
        tab.route("**/api/state", lambda route: route.abort())
        tab.evaluate("refresh()")
        tab.wait_for_function("document.getElementById('ag-go').disabled")
        assert text(tab, "#ag-go") == "Send 1 to agents", "a page that can't reach the server sends nothing"
        tab.unroute("**/api/state")
        for width in (390, 1280):
            tab.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(tab)
        cov.snap()

        phone = browser.new_page(viewport={"width": 375, "height": 800})
        cov = Coverage(phone)
        phone.goto(site + "#agents")
        phone.wait_for_selector("#agents", state="visible")
        assert phone.locator(".ag-row:not([hidden])").count() == 3, "rows sent on another page stay off after a reload"
        no_sideways_scroll(phone)
        cov.snap()
        browser.close()


def test_send_to_agents_says_when_nothing_is_open(site, wq):
    write_page({"built": "now", "github": [], "status": []})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 800})
        cov = Coverage(tab)
        tab.goto(site + "#agents")
        tab.wait_for_selector("#agents", state="visible")
        assert text(tab, "#agents .ag-bar + p") == "Nothing open right now." and tab.locator("#ag-go").is_disabled()
        dialogs = []
        tab.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        tab.evaluate("agSend()")
        tab.wait_for_timeout(300)
        assert text(tab, "#ag-msg") == "" and dialogs == [], "nothing picked asks nothing"
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()



def test_start_here_sends_an_item_for_a_first_pass_and_it_leaves_with_its_row(site, wq):
    start = [{"key": pr(n)["key"], "title": f"o/r#{n}: Change {n}", "source": "GitHub", "effort": "",
              "reason": triage.BLOCKED_REASON if n == 4 else "Go", "blocked": n == 4} for n in range(1, 5)]
    write_page({"built": "now", "github": [pr(n) for n in range(1, 5)], "status": [],
                "triage": {"rows": {}, "start": start, "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []}})
    config.write_json(wq.data / "marks.json", {pr(4)["key"]: {"state": "done", "until": 0}})
    config.write_json(wq.data / "queued.json", {pr(4)["key"]: {"id": "td-104", "at": "t", "mode": "assist"}})
    wq.todo.write_text(json.dumps({"items": [{"id": "td-104", "status": "blocked"}]}))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for n, width in ((1, 390), (2, 1280)):
            tab = browser.new_page(viewport={"width": width, "height": 900})
            cov = Coverage(tab)
            tab.goto(site + "#start")
            tab.wait_for_selector(li(3), state="visible")
            open_items = [k for k in range(1, 4) if k >= n]
            for k in open_items:
                assert text(tab, f"{li(k)} .sf-send") == "Send to Agent"
            assert tab.locator(f"{li(4)} .sf-send").count() == 0, "a blocked item was already sent"
            assert tab.locator(li(4)).is_visible(), "a blocked item stays even though its row was sent"
            for k in range(1, n):
                assert tab.locator(li(k)).count() == 0 and tab.locator(f'.sf-row[data-key="{pr(k)["key"]}"]').count() == 0
            tab.locator(f"{li(n)} .sf-send").click()
            tab.locator(f"{li(n)} .cp-go").click()
            tab.wait_for_selector(li(n), state="detached")
            tab.wait_for_selector(f'.sf-row[data-key="{pr(n)["key"]}"]', state="detached")
            wait_text(tab, ".toast", f"Sent to an agent as td-{104 + n} for a first pass. Its status and result show on "
                                     "the Tasks tab of the Assistant dashboard, and its session on the Sessions tab once "
                                     "it starts.")
            todo = wq.items()[-1]
            assert (todo["id"], todo["title"], todo["url"], todo["workQueueMode"]) == (
                f"td-{104 + n}", f"o/r#{n}: Change {n}", pr(n)["link"], "assist")
            assert "\nDetail: Review requested\n" in todo["detail"], "the same detail as the row's own Send to Agent"
            assert json.loads((wq.data / "marks.json").read_text())[pr(n)["key"]]["state"] == "done"
            assert tab.locator(f'.ag-row[data-key="{pr(n)["key"]}"]').count() == 0
            no_sideways_scroll(tab)
            cov.snap()
        assert [i["id"] for i in wq.items()] == ["td-104", "td-105", "td-106"], "one TODO a send"

        tab = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(tab)
        tab.goto(site + "#start")
        tab.wait_for_selector(li(3), state="visible")
        tab.route("**/api/todo", lambda route: route.fulfill(status=500, json={"error": "store is busy"}))
        tab.locator(f"{li(3)} .sf-send").click()
        tab.locator(f"{li(3)} .cp-go").click()
        wait_text(tab, ".toast", "Not sent: store is busy")
        assert text(tab, f"{li(3)} .sf-send") == "Send to Agent" and tab.locator(f"{li(3)} .sf-send").is_enabled()
        assert tab.locator(li(3)).is_visible() and len(wq.items()) == 3
        cov.snap()
        browser.close()


# ─── Ready for you, Open workspace, and live status ─────────────────────────

def ready_seed(wq) -> Path:
    """Start here with a blocked, sent item and an open one, and TODOs in every state Ready for you shows."""
    notes = wq.home / "dev/generated-docs/notes/e2e plan.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# The E2E plan\n")
    start = [{"key": pr(2)["key"], "title": "o/r#2: Change 2", "source": "GitHub", "effort": "",
              "reason": triage.BLOCKED_REASON, "blocked": True},
             {"key": pr(3)["key"], "title": "o/r#3: Change 3", "source": "GitHub", "effort": "Quick", "reason": "Go",
              "blocked": False}]
    write_page({"built": "now", "github": [pr(n) for n in range(1, 9)], "status": [],
                "triage": {"rows": {}, "start": start, "changes": None, "waiting": 0, "skipped": 0, "note": "", "failed": []}})
    items = [
        {"id": "td-101", "title": "Write the E2E notes", "status": "done", "resultAt": "2026-11-20T10:00:00+00:00",
         "dispatchedWs": "workspace:7", "detail": f"x\n\nRESULT: Wrote {notes.as_uri()}. Reply drafted at "
                                                   "https://github.com/o/r/pull/1#c, and javascript:alert(1) stays text"},
        {"id": "td-102", "title": "Fix access", "status": "blocked", "statusReason": "Need Jira access",
         "statusUpdatedAt": "2026-11-20T11:00:00+00:00"},
        {"id": "td-104", "title": "Quiet block", "status": "blocked", "statusUpdatedAt": "2026-11-20T08:00:00+00:00"},
        {"id": "td-105", "title": "No result", "status": "done", "resultAt": "2026-11-20T09:00:00+00:00"},
        {"id": "td-106", "title": "Still going", "status": "in-progress", "dispatchedWs": "workspace:8"},
        {"id": "td-107", "title": "Dismissed", "status": "done", "resultAt": "2026-11-20T12:00:00+00:00"}]
    wq.todo.write_text(json.dumps({"items": items}))
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": f"td-10{n}", "at": "t"} for n in (1, 2, 4, 5, 6, 7)})
    config.write_json(wq.data / "marks.json", {"https://work-queue.local/ready/td-107": {"state": "done", "until": 0},
                                                **{pr(n)["key"]: {"state": "done", "until": 0} for n in (1, 2, 4, 5, 6, 7)}})
    return notes


def ready_ids(tab) -> list[str]:
    return tab.eval_on_selector_all("#ready .ready-list li", "els => els.map(e => e.dataset.id)")


def test_ready_for_you_shows_finished_work_opens_its_workspace_and_updates_live(site, wq, monkeypatch):
    notes = ready_seed(wq)
    focus = [(200, {"workspace": "workspace:7", "window": "window:9"}),
             (409, {"error": "workspace:7 no longer belongs to td-101"})]
    asked = []
    monkeypatch.setitem(server.ROUTES, "/api/focus", lambda body: asked.append(body) or focus.pop(0))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        states = []
        tab.on("request", lambda r: states.append(r.url) if r.url.endswith("/api/state") else None)
        tab.clock.install()
        tab.goto(site + "#start")
        tab.wait_for_selector("#ready", state="visible")
        assert ready_ids(tab) == ["td-102", "td-101", "td-105", "td-104"], "newest first; open and dismissed TODOs stay out"
        item = {n: f'#ready li[data-id="td-{n}"]' for n in (101, 102, 104, 105, 106)}
        assert text(tab, f"{item[102]} .sf-title") == "Fix access (td-102)"
        assert text(tab, f"{item[102]} .ready-result") == "Blocked: Need Jira access"
        assert text(tab, f"{item[104]} .ready-result") == "Blocked: the agent didn’t say why."
        assert text(tab, f"{item[105]} .ready-result") == ("Done, but the agent left no written result. Open td-105 on "
                                                           "the Tasks tab of the Assistant dashboard to see what happened.")
        result = tab.locator(f"{item[101]} .ready-result")
        assert result.inner_text() == (f"Wrote {notes.as_uri()}. Reply drafted at https://github.com/o/r/pull/1#c, and "
                                       "javascript:alert(1) stays text")
        links = result.locator("a")
        assert [links.nth(n).get_attribute("href") for n in range(links.count())] == [
            "/files" + urllib.parse.quote(str(notes)), "https://github.com/o/r/pull/1#c"]
        assert [links.nth(n).get_attribute("target") for n in range(2)] == ["_blank", "_blank"]
        assert tab.evaluate("fetch(document.querySelector('#ready a').href).then(r => r.text())") == "# The E2E plan\n"
        assert [tab.locator(f"{item[n]}").get_by_role("button", name="Open workspace").count() for n in (101, 102)] == [1, 0]
        assert text(tab, f"{li(2)} .sf-sent") == "Sent td-102" and tab.locator(f"{li(2)} .sf-sent").is_disabled()
        assert tab.locator(f"{li(2)} .sf-send").count() == 0 and text(tab, f"{li(3)} .sf-send") == "Send to Agent"

        tab.locator(item[101]).get_by_role("button", name="Open workspace").click()
        wait_text(tab, ".toast", "Brought the workspace for td-101 to the front.")
        tab.clock.run_for(12000)
        tab.locator(item[101]).get_by_role("button", name="Open workspace").click()
        wait_text(tab, ".toast:last-of-type", "Couldn’t open the workspace: workspace:7 no longer belongs to td-101")
        assert asked == [{"id": "td-101"}, {"id": "td-101"}]
        assert tab.locator(item[101]).get_by_role("button", name="Open workspace").is_enabled()

        tab.route("**/api/mark", lambda route: route.fulfill(status=500, body="nope"))
        tab.locator(f"{item[104]} .sf-mark").click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Not saved: 500')")
        assert tab.locator(f"{item[104]} .sf-mark").is_enabled() and tab.locator(item[104]).count() == 1
        tab.unroute("**/api/mark")
        tab.locator(f"{item[104]} .sf-mark").click()
        tab.wait_for_selector(item[104], state="detached")
        assert json.loads((wq.data / "marks.json").read_text())["https://work-queue.local/ready/td-104"]["state"] == "done"

        # While a TODO is still going, the page asks again every 20 seconds and keeps a note you're typing.
        tab.locator(f"{li(3)} .sf-send").click()
        tab.locator(f"{li(3)} .compose textarea").fill("Keep this note")
        doc = json.loads(wq.todo.read_text())
        doc["items"][4].update(status="done", resultAt="2026-11-20T13:00:00+00:00", detail="x\n\nRESULT: Finished it")
        wq.todo.write_text(json.dumps(doc))
        before = len(states)
        tab.clock.run_for(20000)
        tab.wait_for_selector(item[106], state="attached")
        assert len(states) > before and ready_ids(tab)[0] == "td-106" and text(tab, f"{item[106]} .ready-result") == "Finished it"
        assert tab.locator(f"{li(3)} .compose textarea").input_value() == "Keep this note"
        for width in (390, 1280):
            tab.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(tab)

        # Once every sent TODO is done and dismissed, the page stops asking, and a new send starts it again.
        for n in ("td-106", "td-102", "td-101", "td-105"):
            tab.locator(f'#ready li[data-id="{n}"] .sf-mark').click()
            tab.wait_for_selector(f'#ready li[data-id="{n}"]', state="detached")
        assert tab.locator("#ready").is_hidden()
        before = len(states)
        tab.clock.run_for(45000)
        tab.wait_for_timeout(300)
        assert len(states) == before, "nothing left to watch"
        tab.locator(f"{li(3)} .cp-go").click()
        tab.wait_for_selector(li(3), state="detached")
        tab.clock.run_for(20000)
        assert wait_for(lambda: len(states) > before), "a send starts the polling again"
        assert errors == []
        cov.snap()

        fresh = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(fresh)
        fresh.goto(site + "#start")
        fresh.wait_for_selector(li(2), state="visible")
        assert fresh.locator("#ready").is_hidden(), "a dismissed item stays dismissed in a fresh browser"
        no_sideways_scroll(fresh)
        cov.snap()
        browser.close()


def test_ready_for_you_sits_on_the_open_list_when_there_is_no_start_here(site, wq):
    write_page({"built": "now", "github": [pr(1), pr(2)], "status": []})
    wq.todo.write_text(json.dumps({"items": [{"id": "td-101", "title": "A", "status": "done", "resultAt": "t",
                                              "detail": "x\n\nRESULT: Opened a draft PR"}]}))
    config.write_json(wq.data / "queued.json", {pr(1)["key"]: {"id": "td-101", "at": "t"}})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(tab)
        tab.goto(site)
        tab.wait_for_selector("#open-list #ready", state="visible")
        assert tab.locator("#start").count() == 0 and text(tab, "#ready .ready-result") == "Opened a draft PR"
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


# ─── the Email tab ───────────────────────────────────────────────────────────

def email(key: str, group: str, account: str = "", **extra) -> dict:
    """An Outlook row, or a Gmail row when account is set."""
    base = {**MAIL, "key": key, "group": group, "title": f"Mail {key}", "subject": f"Mail {key}", "reason": "A reason.",
            "addr": f"{key}@example.com", "link": f"https://outlook.office.com/{key}"}
    if account:
        base.update(key=row_key(key), account=account, msg_ids=[f"id-{key}"], tid=f"t-{key}", unsub_url="", archived=False,
                    link=f"https://mail.google.com/mail/#inbox/t-{key}")
    return {**base, **extra}


def mailbox_snapshot() -> dict:
    outlook = [email("o-act", "act"), email("o-act2", "act"), email("o-act3", "act"), email("o-read", "read"),
               email("o-del1", "gh"), email("o-del2", "gh")]
    me = [email("g-act", "act", ACCT_A, unread=True), email("g-look", "unsorted", ACCT_A, archived=True),
          email("g-news", "news", ACCT_A, unread=True), email("g-news2", "news", ACCT_A), email("g-bad", "act", ACCT_A)]
    other = [email("h-news", "news", ACCT_B, unsub_url="https://news.example/u", addr="news@news.example")]
    return {"built": "now", "outlook": outlook, "gmail": {ACCT_A: me, ACCT_B: other}, "status": []}


ACCT_A, ACCT_B = "me@example.com", "other@example.com"


def row_key(key: str) -> str:
    """A Gmail row's key starts with gmail:, as gmail.rows makes it; the tests name rows g-... and h-... for short."""
    return f"gmail:{key}" if key.startswith(("g-", "h-")) else key


def mk(key: str) -> str:
    return page.EMAIL_MARK + row_key(key)


def em(key: str) -> str:
    return f'.em-row[data-mk="{mk(key)}"]'


def test_the_email_tab_holds_outlook_each_gmail_account_and_unsubscribe_and_old_links_land_on_it(site, wq):
    snap = mailbox_snapshot()
    snap["outlook"] = [m for m in snap["outlook"] if m["group"] != "act"]
    write_page(snap)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.goto(site + "#email")
        tab.wait_for_selector("#mailbox", state="visible")
        tabs = tab.eval_on_selector_all("#tabs .tab", "els => els.map(e => e.dataset.t)")
        assert tabs == ["open-list", "agents", "mailbox", "all"], "one Email tab replaces Outlook, Gmail, and Unsubscribe"
        assert text(tab, '#tabs .tab[data-t="mailbox"]') == "Email 2", "it counts the email that needs you"
        is_open = "id => document.getElementById(id).open"
        assert tab.evaluate(is_open, "email"), "an old Outlook link opens the Outlook part"
        assert tab.evaluate(is_open, "gmail-1"), "a part with email that needs you starts open"
        assert not tab.evaluate(is_open, "gmail-2") and not tab.evaluate(is_open, "unsubscribe")
        assert "on" in tab.locator('#tabs .tab[data-t="mailbox"]').get_attribute("class")
        for old in ("gmail-2", "unsubscribe"):
            tab.evaluate(f"location.hash = '#{old}'")
            tab.wait_for_function(is_open, arg=old)
            assert tab.locator("#mailbox").is_visible() and tab.locator(f"#{old}").is_visible()
        tab.evaluate("document.getElementById('email').open = false; location.hash = '#mq-q'")
        tab.wait_for_function(is_open, arg="email")
        assert tab.locator("#mq-q").is_visible(), "a link to anything inside a part opens that part"
        for elsewhere in ("#tabs", "#nope"):
            tab.evaluate(f"location.hash = '{elsewhere}'")
            tab.wait_for_selector("#open-list", state="visible")
            assert tab.locator("#mailbox").is_hidden()
        tab.evaluate("location.hash = '#mailbox'")
        tab.wait_for_selector("#mailbox", state="visible")
        tab.evaluate("location.hash = ''")
        tab.wait_for_selector("#open-list", state="visible")
        no_sideways_scroll(tab)
        cov.snap()

        saved = browser.new_context(viewport={"width": 390, "height": 900})
        saved.add_init_script("try { localStorage.setItem('wq-tab', 'gmail-2') } catch (_) {}")
        old = saved.new_page()
        cov = Coverage(old)
        old.goto(site)
        old.wait_for_function(is_open, arg="gmail-2")
        assert old.locator("#mailbox").is_visible(), "a tab saved before the change lands on the Email tab"
        no_sideways_scroll(old)
        cov.snap()
        browser.close()


def test_email_rows_send_snooze_archive_mark_read_undo_and_stay_out_of_approvals(site, wq):
    now_ms = int(time.time() * 1000)
    snap = mailbox_snapshot()
    wq.sources({"gmail": {"accounts": [ACCT_A, ACCT_B]}})
    write_page(snap)
    config.write_json(wq.data / "marks.json", {
        mk("o-del1"): {"state": "snoozed", "until": now_ms + 86400000},
        "https://outlook.office.com/o-act3": {"state": "dropped", "until": 0},
        mk("o-act3"): {"state": "dropped", "until": 0},
        mk("g-news2"): {"state": "snoozed", "until": now_ms - 1000}})
    fake_tool(wq.bin, "gog", [
        ("thread modify t-g-bad", {"exit": 1, "stderr": "quota exceeded"}),
        ("thread modify", {}),
        ("thread get t-g-act", {"seq": [{"thread": {"messages": [{"id": "1", "labelIds": ["UNREAD"]}]}},
                                        {"thread": {"messages": [{"id": "1", "labelIds": ["INBOX", "UNREAD"]}]}}]}),
        ("thread get t-g-news", {"thread": {"messages": [{"id": "1", "labelIds": ["INBOX"]}, {"id": "2"}]}})])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(site + "#mailbox")
        tab.wait_for_selector(em("g-act"), state="visible")
        count = '#tabs .tab[data-t="mailbox"] span'
        assert text(tab, count) == "4" and text(tab, "#em-showhidden") == "Show hidden email (2)"
        assert tab.locator("#email .em-archive, #email .em-readbtn").count() == 0, "Outlook stays read-only"
        assert tab.locator(f"#email {em('o-read')} .sf-send").count() == 1 and \
            tab.locator(f"{em('o-del2')} .em-acts button", has_text="Snooze").count() == 1
        assert [tab.locator(f"{em(k)} .em-archive").count() for k in ("g-act", "g-look", "g-news")] == [1, 0, 1]
        assert [tab.locator(f"{em(k)} .em-readbtn").count() for k in ("g-act", "g-look", "g-news2")] == [1, 0, 0]
        assert tab.locator(em("o-act3")).is_hidden() and tab.locator(em("o-del1")).is_hidden()
        assert tab.locator(f'.sf-row[data-mk="{mk("o-act3")}"]').get_attribute("class").count("is-hidden") == 1

        # Approvals leave hidden email out.
        tab.locator('#email .ap-group[data-group="mail-gh"] > summary').click()
        assert text(tab, '.ap-group[data-group="mail-gh"] .ap-all') == "Approve all 1"
        tab.locator('.ap-group[data-group="mail-gh"] .ap-all').click()
        wait_text(tab, f"{em('o-del2')} .ap-status", "Approved: in your Outlook search")
        assert {k for k, d in json.loads((wq.data / "decisions.json").read_text()).items()} == {"o-del2"}
        assert text(tab, "#mq-q") == 'subject:"Mail o-del2"'
        tab.locator('#gmail-1 .ap-group[data-group="gmail-news"] > summary').click()
        tab.locator(f"{em('g-news2')} .ap-ok").click()
        wait_text(tab, "#ap-count", "1 approved")
        tab.locator(f"{em('g-news2')} .em-acts button", has_text="Snooze").click()
        wait_text(tab, "#ap-count", "0 approved")
        assert tab.locator("#ap-apply").is_disabled()
        tab.locator(f"{em('o-del2')} .em-acts button", has_text="Snooze").click()
        wait_text(tab, "#mq-q", "Approve emails below to build your search.")

        # Archive and Mark read change Gmail, and say so only once Gmail confirms.
        tab.locator(f"{em('g-act')} .em-archive").click()
        tab.wait_for_selector(em("g-act"), state="hidden")
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent.startsWith('Archived in Gmail.'))")
        assert text(tab, count) == "3"
        assert "is-hidden" in tab.locator(f'.sf-row[data-mk="{mk("g-act")}"]').get_attribute("class"), "its Open list row hides too"
        assert json.loads((wq.data / "marks.json").read_text())[mk("g-act")]["state"] == "done"
        tab.locator(f"{em('g-news')} .em-readbtn").click()
        tab.wait_for_selector(f"{em('g-news')}.em-read", state="attached")
        assert tab.locator(f"{em('g-news')} .em-readbtn").is_hidden() and tab.locator(f"{em('g-news')} .pr-pill.amber").is_hidden()
        tab.locator(f"{em('g-bad')} .em-archive").click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === \"Not done: Gmail didn't make "
                              "the change: gog gmail thread failed: quota exceeded. Nothing changed on the page.\")")
        assert tab.locator(em("g-bad")).is_visible() and tab.locator(f"{em('g-bad')} .em-archive").is_enabled()

        # Snooze, then Show hidden email lists every hidden email with why, and Undo brings it back.
        tab.locator(f"{em('o-act')} .em-acts button", has_text="Snooze").click()
        tab.wait_for_selector(em("o-act"), state="hidden")
        assert json.loads((wq.data / "marks.json").read_text())[mk("o-act")]["until"] > now_ms + 2 * 86400000
        tab.locator("#tabs .tab[data-t='open-list']").click()
        tab.locator('.sf-row[data-key="https://outlook.office.com/o-act2"] .sf-mark').click()
        tab.wait_for_selector('.sf-row[data-key="https://outlook.office.com/o-act2"].is-done', state="attached")
        tab.locator("#tabs .tab[data-t='mailbox']").click()
        tab.locator("#em-showhidden").click()
        wait_text(tab, "#em-showhidden", "Hide hidden email (7)")
        status = {k: text(tab, f"{em(k)} .em-status") for k in ("g-act", "o-act", "o-act2", "o-act3", "o-del1", "g-news2")}
        assert status["g-act"] == "Archived in Gmail" and status["o-act2"] == "Marked done" and status["o-act3"] == "Dropped"
        assert status["o-act"].startswith("Snoozed until ") and status["o-del1"].startswith("Snoozed until ")
        assert all(tab.locator(f"{em(k)} .em-undo").is_visible() for k in status)
        assert text(tab, f"{em('g-look')} .em-status") == "" and tab.locator(f"{em('g-look')} .em-undo").is_hidden()
        tab.locator(f"{em('o-act2')} .em-undo").click()
        tab.wait_for_function("t => !document.querySelector(t).classList.contains('em-gone')", arg=em("o-act2"))
        assert "https://outlook.office.com/o-act2" not in json.loads((wq.data / "marks.json").read_text())
        assert "is-done" not in tab.locator('.sf-row[data-key="https://outlook.office.com/o-act2"]').get_attribute("class")
        tab.locator(f"{em('g-act')} .em-undo").click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Moved back to your Gmail inbox.')")
        tab.wait_for_function("t => !document.querySelector(t).classList.contains('em-gone')", arg=em("g-act"))
        assert calls(wq.bin, "gog")[-2] == f"gmail thread modify t-g-act --add INBOX -a {ACCT_A} -j"
        tab.route("**/api/mark", lambda route: route.fulfill(status=500, body="nope"))
        tab.locator(f"{em('o-act')} .em-undo").click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Not saved: 500')")
        tab.unroute("**/api/mark")
        tab.locator(f"{em('o-act')} .em-undo").click()
        tab.wait_for_function("t => !document.querySelector(t).classList.contains('em-gone')", arg=em("o-act"))
        tab.locator("#em-showhidden").click()
        wait_text(tab, "#em-showhidden", "Show hidden email (4)")

        # Send to Agent from an email: the note goes in, the row hides as sent, and its Open list row leaves.
        tab.locator(f"{em('o-act')} .sf-send").click()
        box = tab.locator(f"{em('o-act')} .compose textarea")
        box.fill("Draft a short yes")
        box.press("Meta+Enter")
        tab.wait_for_selector(em("o-act"), state="hidden")
        todo = wq.items()[-1]
        assert (todo["title"], todo["url"]) == ("Email: Mail o-act", "https://outlook.office.com/o-act")
        assert "Note" not in todo["title"] and f"{server.NOTE_LABEL}\nDraft a short yes\n\n----- BEGIN UNTRUSTED ROW" in todo["detail"]
        assert 'Detail: Email from Ana on Nov 2, subject "Mail o-act". Why it\'s on the list: A reason. It says: Body text' in todo["detail"]
        assert json.loads((wq.data / "queued.json").read_text())[mk("o-act")]["id"] == todo["id"]
        assert tab.locator('.sf-row[data-key="https://outlook.office.com/o-act"]').count() == 0
        tab.locator("#em-showhidden").click()
        wait_text(tab, f"{em('o-act')} .em-status", f"Sent to an agent as {todo['id']}")
        assert tab.locator(f"{em('o-act')} .em-undo").is_hidden(), "a sent email can't be un-sent here"
        tab.locator("#em-showhidden").click()
        tab.route("**/api/todo", lambda route: route.fulfill(status=500, json={"error": "store is busy"}))
        tab.locator(f"{em('g-look')} .sf-send").click()
        tab.locator(f"{em('g-look')} .compose textarea").fill("Keep me")
        tab.locator(f"{em('g-look')} .cp-go").click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Not sent: store is busy')")
        assert tab.locator(f"{em('g-look')} .compose textarea").input_value() == "Keep me", "a failed send keeps the note"
        assert tab.locator(f"{em('g-look')} .sf-send").is_enabled() and tab.locator(f"{em('g-look')} .cp-go").is_enabled()
        tab.unroute("**/api/todo")
        tab.route("**/api/mark", lambda route: route.fulfill(status=500, body="nope"))
        tab.locator(f"{em('g-look')} .em-acts button", has_text="Snooze").click()
        assert wait_for(lambda: tab.evaluate("[...document.querySelectorAll('.toast')].filter(t => t.textContent === 'Not saved: 500').length") == 2)
        tab.unroute("**/api/mark")
        for width in (390, 1280):
            tab.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(tab)
        assert errors == []
        cov.snap()

        again = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(again)
        again.goto(site + "#gmail-1")
        again.wait_for_selector(f"{em('g-news')}.em-read", state="attached")
        assert again.locator(f"{em('g-news')} .em-readbtn").is_hidden(), "Mark read is remembered after a reload"
        assert again.locator(em("g-act")).is_visible() and again.locator(em("o-act")).is_hidden()
        no_sideways_scroll(again)
        cov.snap()
        browser.close()


# ─── the note box on Send to Agent ───────────────────────────────────────────

def test_the_note_box_spans_the_row_grows_then_scrolls_and_sends_or_cancels_from_the_keyboard(site, wq, monkeypatch):
    write_page({"built": "now", "github": [pr(n) for n in range(1, 6)], "status": []})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.goto(site)
        row = {n: f'.sf-row[data-key="{pr(n)["key"]}"]' for n in range(1, 6)}
        tab.locator(f"{row[1]} .sf-send").click()
        box = tab.locator(f"{row[1]} .compose textarea")
        assert box.evaluate("t => document.activeElement === t")
        shape = box.evaluate("""t => { const s = getComputedStyle(t), f = t.closest('.compose'), r = f.parentElement;
            return {resize: s.resize, basis: getComputedStyle(f).flexBasis, grow: getComputedStyle(f).flexGrow,
                    width: f.offsetWidth, room: r.clientWidth - parseFloat(getComputedStyle(r).paddingLeft)
                    - parseFloat(getComputedStyle(r).paddingRight)} }""")
        assert (shape["resize"], shape["basis"], shape["grow"]) == ("none", "100%", "1") and abs(shape["width"] - shape["room"]) <= 1
        first = box.evaluate("t => t.offsetHeight")
        box.fill("one\ntwo\nthree\nfour")
        grown = box.evaluate("t => t.offsetHeight")
        box.fill("\n".join(f"line {n}" for n in range(40)))
        tall = box.evaluate("t => [t.offsetHeight, t.scrollHeight, t.clientHeight]")
        assert first < grown < tall[0] <= 242 and tall[1] > tall[2], "it grows to about 240px, then scrolls"
        tab.locator(f"{row[1]} .sf-send").click()
        assert tab.locator(f"{row[1]} .compose").count() == 1 and box.evaluate("t => document.activeElement === t")
        box.press("Escape")
        tab.wait_for_selector(f"{row[1]} .compose", state="detached")
        assert len(wq.items()) == 1 and tab.locator(row[1]).is_visible(), "Esc cancels and sends nothing"

        tab.locator(f"{row[2]} .sf-send").click()
        box = tab.locator(f"{row[2]} .compose textarea")
        box.press("a")
        box.press("Enter")
        box.type("b")
        assert box.input_value() == "a\nb" and len(wq.items()) == 1, "Enter alone adds a line"
        box.press("Control+Enter")
        tab.wait_for_selector(row[2], state="detached")
        assert wq.items()[-1]["detail"].count(f"{server.NOTE_LABEL}\na\nb\n\n----- BEGIN UNTRUSTED ROW") == 1
        wait_text(tab, ".toast", f"Sent to an agent as td-051 for a first pass. {FOLLOW}")

        tab.locator(f"{row[3]} .sf-send").click()
        tab.locator(f"{row[3]} .cp-no").click()
        tab.wait_for_selector(f"{row[3]} .compose", state="detached")
        tab.locator(f"{row[3]} .sf-send").click()
        tab.locator(f"{row[3]} .cp-go").click()
        tab.wait_for_selector(row[3], state="detached")
        assert server.NOTE_LABEL not in wq.items()[-1]["detail"], "no note, no note block"

        # A row already sent from another browser says so, and says the note wasn't added.
        server.queue_todo({"key": pr(4)["key"], "title": "o/r#4: Change 4"})
        server.queue_todo({"key": pr(5)["key"], "title": "o/r#5: Change 5"})
        items = len(wq.items())
        tab.locator(f"{row[4]} .sf-send").click()
        tab.locator(f"{row[4]} .compose textarea").fill("Also check the tests")
        tab.locator(f"{row[4]} .cp-go").click()
        wait_text(tab, ".toast:last-of-type", "Already sent as td-053, so nothing new was sent. Your note wasn’t added to "
                                              f"it. Add it in that agent’s workspace once it starts. {FOLLOW}")
        tab.locator(f"{row[5]} .sf-send").click()
        tab.locator(f"{row[5]} .cp-go").click()
        wait_text(tab, ".toast:last-of-type", f"Already sent as td-054, so nothing new was sent. {FOLLOW}")
        assert len(wq.items()) == items
        cov.snap()
        browser.close()


def test_a_refresh_that_finishes_while_you_type_a_note_doesnt_reload_it_away(site, wq, monkeypatch):
    write_page({"built": "now", "github": [pr(1)], "status": []})
    monkeypatch.setattr(server, "start_refresh", lambda: (config.write_json(refresh.status_path(), {"running": True}),
                                                         (202, {"started": True}))[1])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site)
        row = f'.sf-row[data-key="{pr(1)["key"]}"]'
        tab.locator(f"{row} .sf-send").click()
        tab.locator(f"{row} .compose textarea").fill("Half a thought")
        tab.locator("#refresh").click()
        wait_text(tab, "#refresh", "Refreshing…")
        config.write_json(refresh.status_path(), {"running": False, "finished": "t"})
        tab.clock.run_for(2500)
        wait_text(tab, ".toast", "The refresh finished. Send or cancel your note, then reload the page to see the new rows.")
        assert tab.locator(f"{row} .compose textarea").input_value() == "Half a thought"
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()
