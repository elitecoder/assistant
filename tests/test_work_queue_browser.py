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

from assistant import pointers  # noqa: E402
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
    config.write_json(refresh.rowctx_path(), page.row_context(snap_doc))
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


IN_TRASH = "In Gmail Trash, verified"
NOT_RECORDED = ("Assistant couldn't record this action, so it didn't run it. "
                "Check that your disk isn't full, then try again")


def held_page(wq, keys) -> None:
    """Gmail rows that are in Trash, each with an undo approved."""
    write_page({"built": "now", "gmail": {"me@example.com": [gmail_row(k, "news") for k in keys]},
                "status": [{"name": "Gmail", "state": "ok", "detail": f"read {len(keys)} items"}]})
    decided = "2026-11-02T10:00:00+00:00"
    config.write_json(wq.data / "decisions.json", {k: {"decision": "undo", "at": decided} for k in keys})
    config.write_json(wq.data / "applied.json", {k: {"status": "done", "verified": True, "at": decided} for k in keys})


def test_an_undo_the_record_refuses_leaves_the_row_in_trash_says_why_and_can_be_applied_again(site, wq):
    """The row doesn't flip to Failed, which would make Apply forget it is still in Trash."""
    held_page(wq, ["g1"])
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/effects").write_text("a file where the record's folder should be")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#ap-count", "0 approved, 1 to reopen")
        assert status_of(tab, "g1") == IN_TRASH
        tab.locator("#ap-apply").click()
        wait_text(tab, "#ap-msg", f"Last run: 0 of 1 verified · {NOT_RECORDED}")
        assert status_of(tab, "g1") == f"{IN_TRASH} · Undo not done: {NOT_RECORDED}"
        assert "failed" not in tab.locator('.ap-row[data-key="g1"]').get_attribute("class")
        assert text(tab, "#ap-count") == "0 approved, 1 to reopen" and tab.locator("#ap-apply").is_enabled()
        cov.snap()
        browser.close()


def test_two_rows_held_for_different_reasons_each_say_theirs_and_the_bar_gives_each_once(site, wq):
    held_page(wq, ["g1", "g2", "g3"])
    fake_tool(wq.bin, "gog", [("messages modify id-g2", {"exit": 1, "stderr": "quota exceeded"}),
                              ("messages modify id-g3", {"exit": 1, "stderr": "quota exceeded"}),
                              ("messages modify", {"ok": True})])
    server.effects.begin("gmail.untrash", {"account": "me@example.com", "msgs": ["id-g1"], "row": server.pointers.row_pointer("g1")},
                         {"kind": "decision", "ref": server.pointers.row_pointer("g1"), "at": "2026-11-02T10:00:00+00:00"})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#ap-count", "0 approved, 3 to reopen")
        tab.locator("#ap-apply").click()
        wait_text(tab, '.ap-row[data-key="g1"] .ap-status', f"{IN_TRASH} · Undo not done: {server.IN_PROGRESS}")
        assert status_of(tab, "g2") == status_of(tab, "g3")
        assert status_of(tab, "g2").startswith(f"{IN_TRASH} · Undo not done: ") and "quota exceeded" in status_of(tab, "g2")
        bar = text(tab, "#ap-msg")
        assert bar.startswith(f"Last run: 0 of 3 verified · {server.IN_PROGRESS}; ") and bar.count("quota exceeded") == 1
        assert text(tab, "#ap-count") == "0 approved, 3 to reopen" and tab.locator("#ap-apply").is_enabled()
        cov.snap()
        browser.close()


SAVE_KEY = "C0123456789:1700000000.000100"


def slack_page(wq) -> None:
    """One Slack save that is marked done, with a reopen approved."""
    write_page({"built": "now", "slack": [{**slack_row(SAVE_KEY, "last", 1), "cid": "C0123456789", "ts": "1700000000.000100"}],
                "status": [{"name": "Slack", "state": "ok", "detail": "read 1 item"}]})
    decided = "2026-11-02T10:00:00+00:00"
    config.write_json(wq.data / "decisions.json", {SAVE_KEY: {"decision": "undo", "at": decided}})
    config.write_json(wq.data / "applied.json", {SAVE_KEY: {"status": "done", "verified": False, "at": decided,
                                                           "error": "Slack accepted the change but the save still shows the old state"}})


def test_a_reopen_the_record_refuses_leaves_a_slack_save_done_and_says_why_on_its_row(site, wq):
    slack_page(wq)
    server.effects.begin("slack.save.uncomplete", {"channel": "C0123456789", "ts": "1700000000.000100",
                                                   "row": server.pointers.row_pointer(SAVE_KEY)},
                         {"kind": "decision", "ref": server.pointers.row_pointer(SAVE_KEY), "at": "2026-11-02T10:00:00+00:00"})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#ap-count", "0 approved, 1 to reopen")
        assert status_of(tab, SAVE_KEY) == "Marked done in Slack, not verified", "the mark's own note isn't a refused undo"
        tab.locator("#ap-apply").click()
        wait_text(tab, f'.ap-row[data-key="{SAVE_KEY}"] .ap-status',
                  f"Marked done in Slack, not verified · Undo not done: {server.IN_PROGRESS}")
        wait_text(tab, "#ap-msg", f"Last run: 0 of 1 verified · {server.IN_PROGRESS}")
        assert "failed" not in tab.locator(f'.ap-row[data-key="{SAVE_KEY}"]').get_attribute("class")
        cov.snap()
        browser.close()


def hold_saves_for_page(count: int, reason: str = "ratelimited") -> None:
    for n in range(count):
        for _ in range(4):
            server.held_saves.hold({f"C0123456789:170000000{n}.000100": (
                {"cid": "C0123456789", "ts": f"170000000{n}.000100", "at": f"2026-11-02T10:4{n}:00+00:00",
                 "link": f"https://acme.slack.com/archives/C0123456789/p170000000{n}000100"}, reason)}, 1.0)


def test_a_sent_save_that_still_couldnt_be_marked_says_so_in_one_line_with_links_that_survives_a_reload_and_clears(site, wq):
    write_page({"built": "now", "slack": [], "status": [{"name": "Slack", "state": "ok", "detail": "read 0 items"}]})
    line = ("5 saves you sent to an agent couldn't be marked done in Slack: Slack asked us to slow down. "
            "Mark them done in Slack yourself. Open in Slack (sent Nov 2, 7:40 PM) · Open in Slack (sent Nov 2, 7:41 PM) · "
            "Open in Slack (sent Nov 2, 7:42 PM) and 2 more.")
    hold_saves_for_page(5)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(timezone_id="Asia/Tokyo", locale="en-US", viewport={"width": 900, "height": 300}).new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#held-msg", line)
        assert tab.locator("#held-msg").is_visible()
        links = tab.locator("#held-msg a")
        assert [links.nth(i).get_attribute("href") for i in range(3)] == [
            f"https://acme.slack.com/archives/C0123456789/p170000000{n}000100" for n in range(3)]
        assert [links.nth(i).get_attribute("aria-label") for i in range(3)] == [
            f"Open save {n + 1} of 3 in Slack, sent Nov 2, 7:4{n} PM" for n in range(3)], "each link has its own name"
        assert links.nth(0).get_attribute("rel") == "noopener" and links.nth(0).get_attribute("target") == "_blank"
        if os.environ.get("WQ_SHOT"):
            tab.screenshot(path=os.environ["WQ_SHOT"])
        tab.reload()
        wait_text(tab, "#held-msg", line)
        tab.evaluate("refresh()")
        wait_text(tab, "#held-msg", line)
        server.record({f"C0123456789:170000000{n}.000100": {"status": "done"} for n in range(3)})
        tab.evaluate("refresh()")
        wait_text(tab, "#held-msg", "2 saves you sent to an agent couldn't be marked done in Slack: Slack asked us to "
                                    "slow down. Mark them done in Slack yourself. Open in Slack (sent Nov 2, 7:43 PM) · "
                                    "Open in Slack (sent Nov 2, 7:44 PM).")
        server.record({"C0123456789:1700000003.000100": {"status": "done"}, "C0123456789:1700000004.000100": {"status": "gone"}})
        tab.evaluate("refresh()")
        wait_text(tab, "#held-msg", "")
        cov.snap()
        browser.close()


def test_a_link_whose_send_time_cant_be_read_is_still_offered_without_it(site, wq):
    write_page({"built": "now", "slack": [], "status": [{"name": "Slack", "state": "ok", "detail": "read 0 items"}]})
    server.held_saves.hold({"C0123456789:1700000000.000100": (
        {"cid": "C0123456789", "ts": "1700000000.000100", "at": "yesterday",
         "link": "https://acme.slack.com/archives/C0123456789/p1700000000000100"}, "x")}, 1.0)
    for _ in range(3):
        server.held_saves.hold({"C0123456789:1700000000.000100": (
            {"cid": "C0123456789", "ts": "1700000000.000100", "at": "yesterday",
             "link": "https://acme.slack.com/archives/C0123456789/p1700000000000100"}, "x")}, 1.0)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#held-msg", "1 save you sent to an agent couldn't be marked done in Slack: Slack refused the "
                                    "change. Mark it done in Slack yourself. Open in Slack.")
        assert tab.locator("#held-msg a").get_attribute("aria-label") == "Open save 1 of 1 in Slack"
        cov.snap()
        browser.close()


def test_a_save_that_needs_a_sign_in_is_on_the_page_before_its_last_try(site, wq):
    write_page({"built": "now", "slack": [], "status": [{"name": "Slack", "state": "ok", "detail": "read 0 items"}]})
    server.held_saves.hold({"C0123456789:1700000000.000100": (
        {"cid": "C0123456789", "ts": "1700000000.000100", "at": "t", "link": ""}, "Slack sign-in needed: run it")}, 1.0)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#held-msg", "1 save you sent to an agent couldn't be marked done in Slack: Slack needs you to "
                                    "sign in again. Sign in to Slack again, then mark it done in Slack yourself. "
                                    "They are still open in your Slack Later list.")
        cov.snap()
        browser.close()


def test_an_undo_gmail_refuses_stays_in_trash_and_the_next_apply_puts_it_back(site, wq):
    held_page(wq, ["g1"])
    fake_tool(wq.bin, "gog", [("messages modify", {"exit": 1, "stderr": "quota exceeded"})])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site)
        wait_text(tab, "#ap-count", "0 approved, 1 to reopen")
        tab.locator("#ap-apply").click()
        tab.wait_for_function("document.querySelector('.ap-row[data-key=\"g1\"] .ap-status').textContent.includes('quota exceeded')")
        assert status_of(tab, "g1").startswith(f"{IN_TRASH} · Undo not done: ")
        assert text(tab, "#ap-count") == "0 approved, 1 to reopen" and tab.locator("#ap-apply").is_enabled()
        fake_tool(wq.bin, "gog", [("messages modify", {"ok": True}), ("gmail get", {"message": {"labelIds": ["INBOX"]}})])
        tab.locator("#ap-apply").click()
        wait_text(tab, '.ap-row[data-key="g1"] .ap-status', "Back in your inbox")
        wait_text(tab, "#ap-msg", "Last run: 1 of 1 verified")
        cov.snap()
        browser.close()


def test_an_unsubscribe_sent_with_no_answer_is_held_says_so_and_goes_again_only_when_approved_again(site, wq, monkeypatch):
    import http.client
    cands = write_page({"built": "now", "gmail": {"me@example.com": [gmail_row("u1", "note", "a@promo.example", "https://promo.example/a")]},
                        "status": [{"name": "Gmail", "state": "ok", "detail": "read 1 item"}]})
    key = next(k for k, c in cands.items() if c["kind"] == "unsub")
    monkeypatch.setattr(server, "unsafe_target", lambda url: "")
    answers = []

    class Opener:
        def open(self, req, timeout):
            if not answers:
                answers.append("cut off")
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            answers.append("sent")

            class Done:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False
            return Done()
    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *a: Opener())
    row = f'.ap-row[data-key="{key}"]'
    held = ("Assistant sent this but didn't hear back, so it may have gone through. "
            "Approve it again to send it once more")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        cov = Coverage(tab)
        tab.goto(site + "#unsubscribe")
        tab.evaluate("document.querySelector('#unsubscribe details').open = true")
        tab.locator(f"{row} .ap-ok").click()
        wait_text(tab, "#ap-count", "1 approved")
        tab.locator("#ap-apply").click()
        wait_text(tab, f"{row} .ap-status", f"Failed: {held}")
        wait_text(tab, "#ap-msg", "Last run: 0 of 1 verified")
        assert "Unsubscribed" not in status_of(tab, key) and answers == ["cut off"]
        assert text(tab, "#ap-count") == "1 approved", "the row is still waiting for an approval"
        tab.locator("#ap-apply").click()  # a second Apply with the old approval sends nothing
        tab.wait_for_function("document.getElementById('ap-apply').textContent === 'Apply approvals' && !document.getElementById('ap-apply').disabled")
        assert answers == ["cut off"] and status_of(tab, key) == f"Failed: {held}"
        time.sleep(1.2)  # an approval has to be later than the answer that never came
        tab.locator(f"{row} .ap-ok").click()
        tab.locator("#ap-apply").click()
        wait_text(tab, f"{row} .ap-status", "Unsubscribed")
        assert answers == ["cut off", "sent"]
        cov.snap()
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
        tab.keyboard.press("/")
        tab.evaluate("location.hash = '#elsewhere'")
        tab.wait_for_timeout(100)
        assert tab.locator("#tab-search").count() == 0 and errors == [], "no search box, and / or a new address does nothing"
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
        tab.wait_for_selector(row2, state="detached", timeout=5000)
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


def titled(items):
    plan = {"o/r#1: Change 1": {"title": "Review Ana's parser change before Monday", "lane": "do_first"},
            "o/r#2: Change 2": {"title": "Send Bo the release notes you promised", "lane": "promise"}}
    return {"rows": [{"id": i["id"], **VERDICT, **plan.get(i["title"], {})} for i in items]}


def test_rows_show_the_models_action_titles_grouped_by_lane_at_both_widths(site, wq):
    triaged_site(wq, Model(rows=titled))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page()
        errors: list[str] = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.wait_for_selector("#open-list .sf-lane", state="visible")
        heads = "#open-list .sf-lane h3, #open-list .sf-group h3"
        assert tab.locator(heads).all_inner_texts() == ["Do first 1", "Promise 1", "Not sorted yet", "Pull requests 1"]
        row1, row3 = f'.sf-row[data-key="{pr(1)["key"]}"]', f'.sf-row[data-key="{pr(3)["key"]}"]'
        assert text(tab, f'.sf-lane[data-lane="do_first"] {row1} .sf-title') == (
            "Model suggests: YouReview Ana's parser change before Monday")
        assert text(tab, f"{row1} .sf-orig") == "o/r#1: Change 1"
        assert text(tab, f"{row1} .sf-meta") == "GitHub Review requested · ana · opened Nov 2"
        assert text(tab, f"{row3} .sf-title") == "Model suggests: Youo/r#3: Change 3" and tab.locator(f"{row3} .sf-orig").count() == 0
        tab.locator('#tabs .tab[data-t="start"]').click()
        tab.wait_for_selector(f"{li(1)} .sf-orig", state="visible")
        assert text(tab, f"{li(1)} .sf-title") == "Review Ana's parser change before Monday"
        assert text(tab, f"{li(3)} .sf-title") == "o/r#3: Change 3"
        for width in (1280, 390):
            tab.set_viewport_size({"width": width, "height": 900})
            for tab_id in ("start", "open-list"):
                tab.locator(f'#tabs .tab[data-t="{tab_id}"]').click()
                tab.wait_for_selector(f"#{tab_id}", state="visible")
                no_sideways_scroll(tab)
            assert tab.locator(f"{row1} .sf-orig").is_visible() and tab.locator(f"{row1} .pr-pill.src").is_visible()
            rows = tab.evaluate("[...document.querySelectorAll('#open-list .sf-row')].map(r => r.scrollWidth <= r.clientWidth)")
            assert rows == [True, True, True], width
        tab.locator(f"{row1} .sf-mark").click()
        wait_text(tab, '.sf-lane[data-lane="do_first"] h3 .count', "0")
        assert (text(tab, "#open-total"), text(tab, '.sf-lane[data-lane="promise"] h3 .count')) == ("2", "1")
        tab.locator(f'.sf-row[data-key="{pr(2)["key"]}"] .sf-send').click()
        tab.locator(f'.sf-row[data-key="{pr(2)["key"]}"] .cp-go').click()
        wait_text(tab, '.sf-lane[data-lane="promise"] h3 .count', "0")
        todo = json.loads(wq.todo.read_text())["items"][-1]
        assert todo["title"].endswith("o/r#2: Change 2"), "Send to Agent still names the row's own text"
        assert errors == []
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


def working_steps(tab) -> list[list]:
    """Each With agents row: its title, its current step, how many steps it passed, its PR link, and each step's tooltip."""
    return tab.eval_on_selector_all("#working .working-list > li", """ls => ls.map(l => [
        l.querySelector('.sf-title').textContent, l.querySelector('[aria-current=step]').textContent,
        l.querySelectorAll('[aria-current=step]').length, l.querySelectorAll('.task-step.reached').length,
        l.querySelector('.task-step a') ? [l.querySelector('.task-step a').href, l.querySelector('.task-step a').target] : null,
        [...l.querySelectorAll('.task-step')].map(s => s.getAttribute('title'))])""")


def test_with_agents_shows_each_sent_task_still_going_with_the_steps_assistant_recorded(site, wq, monkeypatch):
    write_page({"built": "now", "github": [pr(n) for n in range(1, 9)], "status": []})
    items = [
        {"id": "td-301", "title": "Not started", "status": "open"},
        {"id": "td-302", "title": "Started here", "status": "in-progress", "dispatchedAt": "2026-11-20T12:00:00Z",
         "dispatchedWs": "workspace:7"},
        {"id": "td-303", "title": "Has a PR", "status": "in-progress", "dispatchedAt": "2026-11-20T12:00:00Z",
         "prUrl": "https://github.com/o/r/pull/30"},
        {"id": "td-304", "title": "Workspace only", "status": "open", "dispatchedWs": "workspace:9",
         "prUrl": "javascript:alert(1)"},
        {"id": "td-305", "title": "Unreadable start", "status": "in-progress", "dispatchedAt": "soon"},
        {"id": "td-306", "title": "Finished", "status": "done", "resultAt": "2026-11-20T10:00:00+00:00",
         "detail": "x\n\nRESULT: Done it"},
        {"id": "td-307", "title": "Stuck", "status": "blocked", "statusReason": "Need access"},
        {"id": "td-308", "title": "Put off", "status": "deferred"}]
    wq.todo.write_text(json.dumps({"items": items}))
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": f"td-30{n}", "at": "t"} for n in range(1, 9)})
    asked = []
    monkeypatch.setitem(server.ROUTES, "/api/focus", lambda body: asked.append(body) or (200, {"workspace": "workspace:7"}))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(site)
        tab.wait_for_selector("#working", state="visible")
        going = "That doesn’t show the session is still working."
        queued, done = "Assistant starts it at its next check.", "The agent marks it done when it records its result."
        rows = working_steps(tab)
        assert [r[:4] for r in rows] == [
            ["Unreadable start (td-305)", "Started", 1, 1], ["Workspace only (td-304)", "Started", 1, 1],
            ["Has a PR (td-303)", "PR open", 1, 2], ["Started here (td-302)", "Started Nov 20", 1, 1],
            ["Not started (td-301)", "Queued", 1, 0]], "newest first; done, blocked, and deferred work stays out"
        assert [r[4] for r in rows] == [None, None, ["https://github.com/o/r/pull/30", "_blank"], None, None], \
            "only a web address becomes a link"
        tips = [r[5] for r in rows]
        assert tips[0] == [queued, f"Assistant started it. {going}", None, done]
        assert tips[1] == [queued, f"Assistant started it in workspace:9. {going}", None, done]
        assert tips[2][1].startswith("Assistant started it Nov 20") and tips[2][1].endswith(f"M. {going}")
        assert tips[2][2] == "https://github.com/o/r/pull/30"
        assert tips[3][1].startswith("Assistant started it Nov 20") and tips[3][1].endswith(f" in workspace:7. {going}")
        assert tips[4] == [queued, None, None, done]
        assert tab.locator("#ready .ready-list > li").count() == 2 and tab.locator('#working li[data-id="td-306"]').count() == 0
        assert [tab.locator(f'#working li[data-id="td-30{n}"]').get_by_role("button", name="Open workspace").count()
                for n in (1, 2, 3, 4, 5)] == [0, 1, 0, 1, 0]
        tab.locator('#working li[data-id="td-302"]').get_by_role("button", name="Open workspace").click()
        wait_text(tab, ".toast", "Brought the workspace for td-302 to the front.")
        assert asked == [{"id": "td-302"}]

        doc = json.loads(wq.todo.read_text())
        doc["items"][0].update(status="done", resultAt="2026-11-20T13:00:00+00:00", detail="x\n\nRESULT: Finished it")
        wq.todo.write_text(json.dumps(doc))
        tab.evaluate("refresh()")
        tab.wait_for_selector('#ready li[data-id="td-301"]')
        assert tab.locator('#working li[data-id="td-301"]').count() == 0, "finished work moves to Ready for you"
        for item in doc["items"]:
            item["status"] = "done"
        wq.todo.write_text(json.dumps(doc))
        tab.evaluate("refresh()")
        tab.wait_for_selector("#working", state="hidden")
        tab.set_viewport_size({"width": 390, "height": 900})
        no_sideways_scroll(tab)
        assert errors == []
        cov.snap()
        browser.close()


def shown(tab, selector: str) -> list[str]:
    return tab.eval_on_selector_all(selector, "els => els.filter(e => e.offsetParent).map(e => e.querySelector('.sf-title').textContent)")


def press_slash(tab, **mods) -> None:
    tab.evaluate("m => document.body.dispatchEvent(new KeyboardEvent('keydown', {key: '/', bubbles: true, ...m}))", mods)


def test_slash_searches_the_tab_you_are_on_by_row_text_and_clears_on_escape_or_a_tab_switch(site, wq):
    seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(site + "#open-list")
        tab.wait_for_selector("#tab-search", state="visible")
        focused = "document.activeElement.id"
        for mod in ("metaKey", "ctrlKey", "altKey"):
            press_slash(tab, **{mod: True})
            assert tab.evaluate(focused) != "tab-search", mod
        tab.keyboard.press("/")
        assert tab.evaluate(focused) == "tab-search"
        rows = "#open-list .sf-row"
        everything = shown(tab, rows)
        tab.keyboard.type("change 15")
        assert shown(tab, rows) == ["o/r#15: Change 15"]
        assert tab.locator("#open-list .sf-lane:visible").count() == 1, "a lane with no match hides"
        assert tab.locator("#open-list #ready").is_hidden() and tab.locator("#tab-search-empty").is_hidden()
        tab.fill("#tab-search", "td-101")
        assert shown(tab, "#ready .ready-list > li") == [" (td-101)"]
        tab.evaluate("refresh()")
        tab.wait_for_timeout(300)
        assert shown(tab, "#ready .ready-list > li") == [" (td-101)"], "a refresh redraws Ready for you and keeps the search"

        tab.fill("#tab-search", "change 13")
        assert tab.locator("#open-list .sf-lane:visible").count() == 0, "a lane whose only match is snoozed hides"
        assert tab.locator("#tab-search-empty").is_visible()
        tab.fill("#tab-search", "send to agent")
        assert shown(tab, rows) == [] and tab.locator("#tab-search-empty").is_visible(), "button labels don't match"
        assert text(tab, "#tab-search-empty") == "Nothing on this tab matches. Press Escape in the box to clear the search."
        tab.press("#tab-search", "Escape")
        assert tab.input_value("#tab-search") == "" and tab.evaluate(focused) != "tab-search"
        assert shown(tab, rows) == everything and tab.locator("#tab-search-empty").is_hidden()
        tab.focus("#tab-search")
        tab.keyboard.type("a/b")
        assert tab.input_value("#tab-search") == "a/b", "a slash typed in the box is a slash"

        tab.locator('#tabs .tab[data-t="agents"]').click()
        tab.wait_for_selector('#tabs .tab.on[data-t="agents"]')
        tab.locator(f'.ag-row[data-key="{pr(16)["key"]}"] .ag-pick').check()
        assert text(tab, "#ag-go") == "Send 1 to agents"
        tab.locator("#tab-search").fill("change 15")
        assert text(tab, "#ag-go") == "Send 0 to agents", "a picked row the search hides isn't sent"
        tab.locator("#ag-all").check()
        assert text(tab, "#ag-go") == "Send 1 to agents", "Select all picks only the rows the search shows"
        assert tab.evaluate("agPicks().map(r => r.dataset.key)") == [pr(15)["key"]]
        tab.locator("#tab-search").fill("")
        assert text(tab, "#ag-go") == "Send 1 to agents", "rows the search hid aren't left picked when it clears"
        assert not tab.is_checked("#ag-all"), "a new search unticks Select all"

        tab.locator('#tabs .tab[data-t="mailbox"]').click()
        tab.wait_for_selector('#tabs .tab.on[data-t="mailbox"]')
        assert tab.input_value("#tab-search") == "", "switching tabs clears the search"
        told = tab.evaluate("""() => { const r = [...document.querySelectorAll('#mailbox .pr-row, #mailbox .ap-row')]
            .find(r => r.querySelector('.fb-extra')); const s = r.querySelector('.fb');
            ST.feedback[s.dataset.fp] = {told: 'zebrafish notes', told_id: 'f1'}; fbPaint();
            return r.querySelector('.fb-extra').textContent }""")
        assert "zebrafish notes" in told
        tab.locator("#tab-search").fill("zebrafish")
        assert shown(tab, "#mailbox .pr-row, #mailbox .ap-row") == [], "what you told Assistant isn't searched"
        tab.locator("#tab-search").fill("")
        news = '#mailbox details[data-group="gmail-news"] .ap-all'
        many = text(tab, news)
        tab.evaluate("$('ap-msg').textContent = 'Not saved: 500'")
        tab.locator("#tab-search").fill("g-plain")
        assert text(tab, "#ap-msg") == "Not saved: 500", "a search keeps the Apply bar's message"
        assert many != "Approve all 1" and text(tab, news) == "Approve all 1", "Approve all counts only the rows shown"
        tab.locator(news).click()
        assert wait_for(lambda: json.loads((wq.data / "decisions.json").read_text()).get("g-plain", {}).get("decision") == "approve")
        decided = json.loads((wq.data / "decisions.json").read_text())
        assert all(k not in decided for k in ("g-err", "g-undone")), "rows the search hid stay undecided"
        tab.locator("#tab-search").fill("")
        tab.dispatch_event("#tab-search", "input")
        section = '#mailbox details[data-group="gmail-shop"]'
        assert not tab.evaluate(f"document.querySelector('{section}').open"), "the seed's shopping group starts closed"
        tab.locator("#tab-search").press_sequentially("g-shop")
        assert tab.evaluate(f"(d => d.open && d.dataset.qWas === '')(document.querySelector('{section}'))"), \
            "a closed section with a match opens"
        assert shown(tab, "#mailbox .ap-row") == ["Mail g-shop"]
        tab.press("#tab-search", "Escape")
        assert tab.evaluate(f"(d => !d.open && !('qWas' in d.dataset))(document.querySelector('{section}'))"), \
            "Escape puts it back the way it was"
        tab.set_viewport_size({"width": 390, "height": 900})
        no_sideways_scroll(tab)
        assert errors == []
        cov.snap()
        browser.close()


def cleared_seed(wq, ready_marks: dict) -> None:
    """Ready for you on the Open list: a done item still waiting, a blocked one with a note, and marked ones."""
    write_page({"built": "now", "github": [pr(n) for n in range(1, 7)], "status": []})
    items = [{"id": "td-201", "title": "Open PR", "status": "done", "resultAt": "2026-11-20T12:00:00+00:00",
              "detail": "x\n\nRESULT: Opened https://github.com/o/r/pull/3"},
             {"id": "td-202", "title": "Locked repo", "status": "blocked", "statusReason": "Need access to o/x",
              "statusUpdatedAt": "2026-11-20T11:00:00+00:00"},
             {"id": "td-203", "title": "Merged PR", "status": "done", "resultAt": "2026-11-20T10:00:00+00:00",
              "detail": "x\n\nRESULT: Merged https://github.com/o/r/pull/2"},
             {"id": "td-204", "title": "Long ago", "status": "done", "resultAt": "2026-11-01T10:00:00+00:00"},
             {"id": "td-205", "title": "You marked it", "status": "done", "resultAt": "2026-11-20T09:00:00+00:00"}]
    wq.todo.write_text(json.dumps({"items": items}))
    config.write_json(wq.data / "queued.json", {pr(n)["key"]: {"id": f"td-20{n}", "at": "t"} for n in range(1, 6)})
    config.write_json(wq.data / "ready-checks.json", {
        "td-201": {"pending": "Clears when o/r#3 merges or closes, or once you comment on or review it.", "at": "t"},
        "td-202": {"note": "The blocker looks gone: GitHub can read o/x now, so you can resume td-202.", "at": "t"},
        "td-203": {"cleared": "pr-merged", "why": "o/r#2 was merged.", "at": "t"}})
    config.write_json(wq.data / "marks.json", ready_marks)


def auto_mark(days_ago: float, why: str) -> dict:
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))
    return {"state": "done", "until": 0, "at": at, "auto": "pr-merged", "why": why}


def test_ready_for_you_hides_work_that_cleared_on_its_own_says_why_and_undoes_it(site, wq):
    ready_key = "https://work-queue.local/ready/"
    cleared_seed(wq, {ready_key + "td-203": auto_mark(1, "o/r#2 was merged."),
                      ready_key + "td-204": auto_mark(8, "o/r#1 was merged."),
                      ready_key + "td-205": {"state": "done", "until": 0, "at": "2026-11-20T09:00:00Z"}})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(site)
        tab.wait_for_selector("#open-list #ready", state="visible")
        assert ready_ids(tab) == ["td-201", "td-202"], "cleared and marked items leave the list"
        assert text(tab, '#ready li[data-id="td-201"] .ready-check') == "Clears when o/r#3 merges or closes, or once you comment on or review it."
        note = tab.locator('#ready li[data-id="td-202"] .ready-check')
        assert note.text_content() == "The blocker looks gone: GitHub can read o/x now, so you can resume td-202."
        assert "note" in note.get_attribute("class")
        assert text(tab, '#ready li[data-id="td-202"] .ready-result') == "Blocked: Need access to o/x"
        chip = tab.locator("#ready .ready-show")
        assert chip.is_visible() and chip.text_content() == "Show cleared (1)", "only the last 7 days' cleared items"
        assert tab.locator("#ready .ready-cleared").is_hidden()

        chip.click()
        tab.wait_for_selector('#ready .ready-cleared li[data-id="td-203"]')
        assert chip.text_content() == "Hide cleared (1)"
        assert text(tab, '.ready-cleared li[data-id="td-203"] .sf-title') == "Merged PR (td-203)"
        assert text(tab, '.ready-cleared li[data-id="td-203"] .ready-check') == "Cleared on its own: o/r#2 was merged."
        for width in (390, 1280):
            tab.set_viewport_size({"width": width, "height": 900})
            no_sideways_scroll(tab)
            assert tab.evaluate("(b => b.scrollWidth <= b.clientWidth)(document.querySelector('#ready'))")

        tab.route("**/api/mark", lambda route: route.fulfill(status=500, body="nope"))
        tab.locator('.ready-cleared li[data-id="td-203"] .sf-mark').click()
        tab.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Not saved: 500')")
        assert tab.locator('.ready-cleared li[data-id="td-203"] .sf-mark').is_enabled()
        tab.unroute("**/api/mark")
        tab.locator('.ready-cleared li[data-id="td-203"] .sf-mark').click()
        wait_text(tab, ".toast:last-of-type", "It’s back on Ready for you, and it won’t clear on its own again.")
        tab.wait_for_selector('#ready .ready-list li[data-id="td-203"]')
        assert ready_ids(tab) == ["td-201", "td-202", "td-203"] and chip.is_hidden()
        assert tab.locator("#ready .ready-cleared li").count() == 0
        assert ready_key + "td-203" not in json.loads((wq.data / "marks.json").read_text())
        assert errors == []
        cov.snap()

        # With nothing left but a cleared item, the box stays so you can still find and undo it.
        done_mark = {"state": "done", "until": 0}
        cleared_seed(wq, {ready_key + "td-201": done_mark, ready_key + "td-202": done_mark,
                          ready_key + "td-203": auto_mark(0, "o/r#2 was merged."), ready_key + "td-204": done_mark,
                          ready_key + "td-205": done_mark})
        small = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(small)
        small.goto(site)
        small.wait_for_selector("#ready .ready-show", state="visible")
        assert ready_ids(small) == [] and text(small, "#ready .ready-show") == "Show cleared (1)"
        no_sideways_scroll(small)
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
        assert wait_for(lambda: tab.evaluate("[...document.querySelectorAll('.toast')].filter(t => t.textContent === 'Not saved: 500').length") == 1)
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


# ─── feedback: the thumbs-down and Tell Assistant ────────────────────────────

def fb_seed(wq) -> dict:
    """The Ready for you seed, plus an open row with a draft, a plain row, an email, a row waiting for approval, and the
    Start here item for the row with the draft."""
    ready_seed(wq)
    row = pr(3)["key"]
    verdict = {**VERDICT, "disposition": "you", "lane": "do_first", "draft": "Hi Ana, I will take this one.", "title": "Review change 3"}
    start = [{"key": pr(2)["key"], "title": "o/r#2: Change 2", "source": "GitHub", "effort": "", "reason": triage.BLOCKED_REASON,
              "blocked": True},
             {"key": row, "title": "o/r#3: Change 3", "source": "GitHub", "effort": "Quick", "reason": "Go", "blocked": False}]
    write_page({"built": "now", "github": [pr(n) for n in range(1, 9)], "status": [],
                "outlook": [mail("o-act", "act", "ana@example.com", "Can you review?")],
                "gmail": {"me@example.com": [gmail_row("g-news", "news")]},
                "triage": {"rows": {row: verdict}, "start": start, "changes": None, "waiting": 0, "skipped": 0, "note": "",
                           "failed": []}})
    config.write_json(wq.data / "traces.json", {row: {"trace": "tr-" + "ab" * 8, "seen": "2026-11-20"}})
    return {"row": row, "plain": pr(8)["key"], "mail": server.EMAIL_MARK + "o-act", "cand": "g-news"}


def fb_lines(wq) -> list[dict]:
    path = wq.home / ".assistant/feedback/feedback.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class Sel:
    """The selectors for one Open list row: its sf-main bar, the extra block after it, and its popup."""

    def __init__(self, key):
        self.row = f'.sf-row[data-key="{key}"]'
        self.bar = f"{self.row} > .sf-main"
        self.thumb = f"{self.bar} .fb-down"
        self.open = f"{self.bar} .fb-open"
        self.pop = f"{self.bar} .fb-pop"
        self.note = f"{self.bar} .fb-note"
        self.x = f"{self.row} > .sf-main + .fb-extra"


def pressed(tab, thumb: str, on: bool = True) -> None:
    tab.wait_for_selector(f'{thumb}[aria-pressed="{str(on).lower()}"]')


def labels(tab, selector: str) -> list[str]:
    return tab.locator(selector).evaluate_all("els => els.map(e => e.textContent)")


def heights(tab, *keys) -> list[float]:
    return [tab.locator(f'.sf-row[data-key="{k}"]').bounding_box()["height"] for k in keys]


PROMISE = "Assistant keeps what you tell it. It doesn't use it yet; that's planned for the next update."


def many_rows(wq, n: int = 12) -> list[str]:
    """n plain Open list rows (the first with the model's verdict and a draft), in Pro mode."""
    verdict = {**VERDICT, "disposition": "you", "lane": "do_first", "draft": "Hi Ana, I will take this one.", "title": "Review change 1"}
    write_page({"built": "now", "github": [pr(k) for k in range(1, n + 1)], "status": [],
                "triage": {"rows": {pr(1)["key"]: verdict}, "start": [], "changes": None, "waiting": 0, "skipped": 0,
                           "note": "", "failed": []}})
    return [pr(k)["key"] for k in range(1, n + 1)]


ROW_THUMBS = ".sf-row > .sf-main .fb-down"
OVERLAPS = """(ownNote) => {
  const panel = document.querySelector('.fb-pop:not([hidden])').getBoundingClientRect();
  const hit = (a, b) => !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
  const own = document.querySelector(ownNote).closest('.sf-row, li, .ap-row'); const covered = [...document.querySelectorAll('.fb-down, .fb-open')].filter(e => { const r = e.getBoundingClientRect(); return r.width && hit(panel, r) && !(own.contains(e) && e.classList.contains('fb-open')); })
    .map(e => e.className + ' ' + (e.closest('[data-key]') || {dataset: {}}).dataset.key);
  const note = document.querySelector(ownNote).getBoundingClientRect();
  if (hit(panel, note)) covered.push('its own note');
  const card = document.querySelector('.fb-pop:not([hidden])').closest('body').querySelector('.section').getBoundingClientRect();
  return {covered, inside: panel.left >= 8 && panel.right <= innerWidth - 8 && panel.top >= 8 && panel.bottom <= innerHeight - 8,
          inCard: panel.left >= Math.min(card.left, 8) - 0.5 && panel.right <= card.right + 0.5};
}"""


def test_a_thumbs_down_is_one_tap_that_only_says_wrong_and_a_reason_is_asked_for_from_the_note(site, wq):
    keys = fb_seed(wq)
    a, plain = Sel(keys["row"]), Sel(keys["plain"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        errors = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.clock.install()
        tab.goto(site + "#open-list")
        assert tab.locator(a.thumb).get_attribute("aria-pressed") == "false"
        assert tab.locator(a.thumb).get_attribute("aria-label") == "This is wrong"
        assert tab.locator(a.open).inner_text() == "Tell Assistant"
        assert labels(tab, f"{a.bar} > button") == ["Done", "Snooze", "Drop", "Send to Agent"], "the old buttons keep their places"
        assert tab.locator(f"{a.bar} > :last-child").get_attribute("class") == "fb"
        assert tab.locator(a.pop).is_hidden() and tab.locator(a.pop).get_attribute("aria-label") == "Why is this wrong?"
        before = heights(tab, keys["row"], keys["plain"])
        tops = [tab.locator(a.row).bounding_box()["y"], tab.locator(plain.row).bounding_box()["y"]]

        tab.locator(a.thumb).click()  # one tap: it says "wrong", and nothing opens
        pressed(tab, a.thumb)
        (first,) = fb_lines(wq)
        assert (first["signal"], first["kind"], first["surface"], first["subject"], first["trace"]) == \
            ("thumbs_down", "row", "work-queue", pointers.row_pointer(keys["row"]), "tr-" + "ab" * 8)
        assert tab.locator(a.pop).is_hidden()
        assert tab.locator(a.note).inner_text() == "You said: this is wrong. Add a reason · Undo"
        assert tab.locator(a.note).get_attribute("title") == PROMISE
        assert tab.locator(a.thumb).get_attribute("aria-label") == "This is wrong. Press to take it back"
        assert heights(tab, keys["row"], keys["plain"]) == before, "the tap moved no row"
        tab.locator(plain.thumb).click()
        pressed(tab, plain.thumb)
        assert heights(tab, keys["row"], keys["plain"]) == before, "nor did a note on a plain row"
        assert [tab.locator(a.row).bounding_box()["y"], tab.locator(plain.row).bounding_box()["y"]] == tops
        tab.locator(plain.thumb).click()  # a second tap takes it back
        pressed(tab, plain.thumb, False)
        assert fb_lines(wq)[-1]["signal"] == "retract" and tab.locator(plain.note).count() == 0

        tab.locator(f"{a.note} button", has_text="Add a reason").click()
        assert tab.locator(a.pop).is_visible() and tab.evaluate("document.activeElement.dataset.fb") == "r:not_important"
        assert heights(tab, keys["row"], keys["plain"]) == before, "the panel lies over the page and moves no row"
        assert labels(tab, f"{a.pop} button") == ["Not important", "Wrong order", "Not mine", "Already done", "Skip"]
        assert tab.locator(f"{a.pop} .fb-why").inner_text() == "Why is this wrong? Optional"
        tab.locator(a.pop).get_by_role("button", name="Not mine").click()
        tab.wait_for_selector(a.pop, state="hidden")
        second = fb_lines(wq)[-1]
        assert (second["signal"], second["reason"], second["amends"], second["subject"]) == \
            ("thumbs_down", "not_mine", first["id"], first["subject"]) and "words" not in second
        assert tab.locator(a.note).inner_text() == "You said: not mine. Change · Undo"
        assert heights(tab, keys["row"], keys["plain"]) == before

        tab.reload()
        pressed(tab, a.thumb)
        assert tab.locator(a.note).inner_text() == "You said: not mine. Change · Undo" and tab.locator(a.pop).is_hidden()
        start = f'.start-list li[data-key="{keys["row"]}"]'
        assert tab.locator(f"{start} .fb-down").get_attribute("aria-pressed") == "true", "the same row says the same"
        assert tab.locator(f"{start} .fb-line .fb-note").first.inner_text().startswith("You said: not mine.")

        n = len(fb_lines(wq))
        tab.locator(f"{a.note} button", has_text="Change").click()
        assert tab.locator(a.pop).is_visible() and tab.locator(f'{a.pop} [data-fb="r:not_mine"]').get_attribute("aria-pressed") == "true"
        tab.locator(a.pop).get_by_role("button", name="Not mine").click()  # the same reason again changes nothing
        tab.wait_for_selector(a.pop, state="hidden")
        assert len(fb_lines(wq)) == n
        tab.locator(f"{a.note} button", has_text="Change").click()
        tab.locator(a.pop).get_by_role("button", name="Wrong order").click()
        tab.wait_for_function("document.querySelector('.sf-row .fb-note').textContent.startsWith('You said: wrong order.')")
        assert fb_lines(wq)[-1]["amends"] == first["id"], "a new reason is a new record about the first thumbs-down"

        tab.locator(f"{a.note} button", has_text="Change").click()
        tab.keyboard.press("Escape")
        tab.wait_for_selector(a.pop, state="hidden")
        assert tab.evaluate("document.activeElement.dataset.fb") == "down", "Esc closes it and gives focus back"
        tab.locator(f"{a.note} button", has_text="Change").click()
        tab.locator("h2").first.click()  # a click outside
        tab.wait_for_selector(a.pop, state="hidden")
        tab.locator(f"{a.note} button", has_text="Change").click()
        tab.locator(a.pop).get_by_role("button", name="Skip").click()
        tab.wait_for_selector(a.pop, state="hidden")
        assert len(fb_lines(wq)) == n + 1
        tab.locator(f"{a.note} button", has_text="Undo").click()  # the same as a second tap
        pressed(tab, a.thumb, False)
        assert fb_lines(wq)[-1]["signal"] == "retract" and fb_lines(wq)[-1]["amends"] == first["id"]
        assert tab.locator(a.note).count() == 0 and heights(tab, keys["row"], keys["plain"]) == before
        assert not errors
        cov.snap()
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_ten_thumbs_in_a_row_with_a_real_mouse_make_ten_records_and_no_reason_nobody_chose(site, wq, width):
    many_rows(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(site + "#open-list")
        thumbs = tab.locator(ROW_THUMBS)
        thumbs.first.wait_for()
        clicks = 0
        for i in range(10):
            thumbs.nth(i).scroll_into_view_if_needed()
            thumbs.nth(i).click()  # a real click at the thumb's place: Playwright refuses one that something else covers
            clicks += 1
        tab.wait_for_function("document.querySelectorAll('.sf-row > .sf-main .fb-down[aria-pressed=true]').length === 10")
        got = fb_lines(wq)
        assert [r["signal"] for r in got] == ["thumbs_down"] * 10 and all("reason" not in r for r in got) and clicks == 10
        assert len({r["subject"] for r in got}) == 10
        assert tab.locator(".fb-pop:not([hidden])").count() == 0
        for i in range(10):  # and one click on each takes it back
            thumbs.nth(i).click()
        tab.wait_for_function("document.querySelectorAll('.sf-row > .sf-main .fb-down[aria-pressed=true]').length === 0")
        assert [r["signal"] for r in fb_lines(wq)[10:]] == ["retract"] * 10
        browser.close()


def test_with_the_reasons_open_a_click_on_another_rows_thumb_records_that_row_and_nothing_on_the_first(site, wq):
    keys = many_rows(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 800})
        tab.goto(site + "#open-list")
        thumbs = tab.locator(ROW_THUMBS)
        thumbs.first.wait_for()
        thumbs.nth(3).click()
        tab.locator(".sf-row .fb-note button", has_text="Add a reason").click()
        assert tab.locator(".fb-pop:not([hidden])").count() == 1
        thumbs.nth(4).click()  # aimed at the next row
        tab.wait_for_function("document.querySelectorAll('.sf-row > .sf-main .fb-down[aria-pressed=true]').length === 2")
        assert tab.locator(".fb-pop:not([hidden])").count() == 0, "a click outside closes it"
        got = fb_lines(wq)
        assert [(r["signal"], r["subject"]) for r in got] == [("thumbs_down", pointers.row_pointer(keys[3])),
                                                              ("thumbs_down", pointers.row_pointer(keys[4]))]
        assert all("reason" not in r and "amends" not in r for r in got), "nothing was added to the first row"
        browser.close()


@pytest.mark.parametrize("width", [1280, 900, 390])
def test_the_reasons_never_cover_a_thumb_a_tell_assistant_link_their_own_note_or_the_edge_of_the_screen(site, wq, width):
    many_rows(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(site + "#open-list")
        thumbs = tab.locator(ROW_THUMBS)
        thumbs.first.wait_for()
        for i in range(thumbs.count()):  # every row, the middle ones and the last
            thumb = thumbs.nth(i)
            thumb.scroll_into_view_if_needed()
            thumb.click()
            row = tab.locator(".sf-row").nth(i)
            row.locator(":scope > .sf-main .fb-note button", has_text="Add a reason").click()
            tab.wait_for_selector(".fb-pop:not([hidden])")
            tab.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")  # a scroll has been seen to
            tab.evaluate("(i) => { document.querySelectorAll('.sf-row')[i].querySelector(':scope > .sf-main .fb-note').dataset.x = i }", i)
            found = tab.evaluate(OVERLAPS, f'.fb-note[data-x="{i}"]')
            assert found == {"covered": [], "inside": True, "inCard": True}, (width, i, found)
            tab.keyboard.press("Escape")
            thumb.click()  # take it back, so the next row starts clean
            tab.wait_for_selector(".fb-note", state="detached")
        browser.close()


TAP_AREAS = """(thumb) => { const num = v => parseFloat(v) || 0;
  const area = el => { const r = el.getBoundingClientRect(), a = getComputedStyle(el, '::after');
    return a.content !== 'none' && a.position === 'absolute' ? {left: r.left + num(a.left), top: r.top + num(a.top), right: r.right - num(a.right), bottom: r.bottom - num(a.bottom)}
      : {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; };
  const t = thumb.getBoundingClientRect(), cx = t.left + t.width / 2, cy = t.top + t.height / 2;
  // The strict reading: walk out from each edge along the thumb's own middle lines, a whole pixel at a time, for as long as the thumb is what is there.
  const mine = (x, y) => { const e = document.elementFromPoint(x, y); return !!e && e.closest('.fb-down') === thumb; };
  let l = 0, r = 0, u = 0, d = 0;
  while (mine(cx - t.width / 2 - l - 1, cy)) l++;
  while (mine(cx + t.width / 2 + r + 1, cy)) r++;
  while (mine(cx, cy - t.height / 2 - u - 1)) u++;
  while (mine(cx, cy + t.height / 2 + d + 1)) d++;
  // The generous reading: the extent of every point within 30 px that is the thumb, a quarter pixel at a time.
  const xs = [], ys = [];
  for (let x = cx - 30; x <= cx + 30; x += 0.25) for (let y = cy - 30; y <= cy + 30; y += 0.25) if (mine(x, y)) { xs.push(x); ys.push(y); }
  const own = area(thumb), over = [];
  for (const el of document.querySelectorAll('button, a[href], textarea, input, summary')) {
    if (el === thumb || el.closest('.fb-pop') || el.contains(thumb)) continue;
    const b = el.getBoundingClientRect(); if (!b.width || Math.abs(b.top - t.top) > 90) continue;
    const o = area(el), w = Math.min(own.right, o.right) - Math.max(own.left, o.left), h = Math.min(own.bottom, o.bottom) - Math.max(own.top, o.top);
    if (w > 0 && h > 0) over.push((el.className || el.tagName) + ' ' + el.textContent.slice(0, 14) + ' ' + w.toFixed(1) + 'x' + h.toFixed(1)); }
  return {strict: [Math.round(t.width + l + r), Math.round(t.height + u + d)], generous: [Math.max(...xs) - Math.min(...xs) + 0.25, Math.max(...ys) - Math.min(...ys) + 0.25], over}; }"""


def test_the_thumb_on_every_kind_of_row_has_a_44_pixel_tap_area_on_a_phone_that_no_other_tap_area_overlaps(site, wq):
    keys = fb_seed(wq)
    kinds = {
        "a row in a lane": f'.sf-lane .sf-row[data-key="{keys["row"]}"] > .sf-main .fb-down',
        "a plain row": f'.sf-row[data-key="{keys["plain"]}"] > .sf-main .fb-down',
        "a draft": ".sf-row .draft-fb .fb-down",
        "a Start here item": f'.start-list li[data-key="{keys["row"]}"] .fb-down',
        "a result": '#ready li[data-id="td-101"] .fb-down',
        "an email row": f'.em-row[data-mk="{keys["mail"]}"] .fb-down',
        "an approval row": f'.ap-row[data-key="{keys["cand"]}"] .fb-down',
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 844})
        tab.goto(site + "#all")
        tab.locator("#gmail-1 > summary").click()
        tab.locator("#gmail-1 details").first.evaluate("d => d.open = true")
        measured = {}
        for kind, selector in kinds.items():
            for pressed_too in (False, True):
                thumb = tab.locator(selector).first
                thumb.evaluate("e => e.scrollIntoView({block: 'center'})")
                if pressed_too:
                    if thumb.get_attribute("aria-pressed") != "true":  # the same row in two places says the same
                        thumb.click()
                    pressed(tab, selector)
                    thumb.evaluate("e => e.scrollIntoView({block: 'center'})")
                found = tab.evaluate(f"(sel) => ({TAP_AREAS})(document.querySelector(sel))", selector)
                measured[kind + (", pressed" if pressed_too else "")] = (found["strict"], found["generous"], found["over"])
        print("TAP AREAS (strict, generous, overlaps)", measured)
        bad = {k: v for k, v in measured.items() if min(v[0]) < 44 or min(v[1]) < 43.75 or v[2]}  # the generous scan steps by a quarter: 44 reads as 43.75
        assert not bad, bad
        browser.close()


OPEN_AT = {"top": "e => { e.scrollIntoView({block: 'start'}); window.scrollBy(0, -90); }", "middle": "e => e.scrollIntoView({block: 'center'})",
           "bottom": "e => e.scrollIntoView({block: 'end'})"}
SPOTS = """(own) => {
  const pop = document.querySelector('.fb-pop:not([hidden])'), panel = pop.getBoundingClientRect(), w = panel.width, t = panel.height;
  const hit = (a, b, m = 0) => !(a.right + m <= b.left || a.left >= b.right + m || a.bottom + m <= b.top || a.top >= b.bottom + m);
  const row = document.querySelector(own), thumb = row.querySelector('.fb-down').getBoundingClientRect(), rowHeight = row.getBoundingClientRect().height;
  // how far the middle of the list is, up or down, from the middle of the thumb (the reviewer's measure, and what a person sees first)
  const centre = Math.abs(panel.top + t / 2 - thumb.top - thumb.height / 2);
  const centreAt = (x, y) => Math.abs(y + t / 2 - thumb.top - thumb.height / 2);
  const gapOf = (x, y) => Math.hypot(Math.max(thumb.left - x - w, x - thumb.right, 0), Math.max(thumb.top - y - t, y - thumb.bottom, 0));
  const things = [...document.querySelectorAll('.fb-down, .fb-open, .fb-note')].map(e => ({e, r: e.getBoundingClientRect()})).filter(o => o.r.width && o.r.bottom > 0 && o.r.top < innerHeight);
  const name = o => o.e.className.split(' ').pop() + ' of ' + ((o.e.closest('.sf-row') || {dataset: {}}).dataset.key || '').split('/').pop();
  const covered = things.filter(o => hit(panel, o.r)).map(name);
  // Is there anywhere within one row's height of the thumb that keeps clear of each group of things? Then the panel must be on such a place.
  const card = row.closest('.section').getBoundingClientRect(), lo = Math.max(8, card.left), hi = Math.min(innerWidth - 8, card.right);
  const ownTell = row.querySelector('.fb-open'), isOwn = o => o.e === ownTell, isNote = o => o.e.classList.contains('fb-note');
  const must = o => !isOwn(o) && !isNote(o) || (isNote(o) && o.e.closest('.sf-row') === row);
  const clearOf = (x, y, keep) => !things.some(o => keep(o) && hit({left: x, right: x + w, top: y, bottom: y + t}, o.r, 4));
  const exists = keep => { for (let x = lo; x <= hi - w; x += 6) for (let y = 8; y <= innerHeight - 8 - t; y += 6) if (gapOf(x, y) <= rowHeight && (innerWidth > 620 || centreAt(x, y) <= rowHeight) && clearOf(x, y, keep)) return true; return false; };
  const free = exists(() => true);
  const reachable = exists(must);  // somewhere near enough that covers no thumb, no other Tell Assistant link and not its own note
  const freeOfOwnLink = exists(o => must(o) || isOwn(o));
  const coveredObjs = things.filter(o => hit(panel, o.r));
  const ownLinkCovered = coveredObjs.some(isOwn), otherNoteCovered = coveredObjs.some(o => isNote(o) && !must(o));
  return {gap: gapOf(panel.left, panel.top), centre, rowHeight, reachable, size: [Math.round(w), Math.round(t)], covered, free, freeOfOwnLink, ownLinkCovered, otherNoteCovered,
          inScreen: panel.left >= 8 && panel.right <= innerWidth - 8 && panel.top >= 8 && panel.bottom <= innerHeight - 8};
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_the_reasons_open_next_to_their_thumb_at_the_top_the_middle_and_the_bottom_of_the_window_and_clear_of_the_links_and_notes_round_it(site, wq, width):
    keys = many_rows(wq, 24)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(site + "#open-list")
        thumbs = tab.locator(ROW_THUMBS)
        thumbs.first.wait_for()
        for i in (0, 8, 17, 19, 20, 22, 23):
            for where, scroll in OPEN_AT.items():
                around = [j for j in (i - 1, i + 1) if 0 <= j < 24]
                for j in around:  # the rows beside it have notes of their own
                    thumbs.nth(j).evaluate(OPEN_AT["middle"])
                    thumbs.nth(j).click()
                tab.locator(".sf-row").nth(i).evaluate(scroll)
                thumbs.nth(i).click()
                tab.locator(".sf-row").nth(i).locator(":scope > .sf-main .fb-note button", has_text="Add a reason").click()
                tab.wait_for_selector(".fb-pop:not([hidden])")
                tab.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
                found = tab.evaluate(SPOTS, f'.sf-row[data-key="{keys[i]}"]')
                assert found["inScreen"] and (not found["reachable"] or found["gap"] <= found["rowHeight"]), (width, i, where, found)
                if width == 390:
                    assert not found["reachable"] or found["centre"] <= found["rowHeight"], (width, i, where, found)
                assert not [c for c in found["covered"] if c.startswith("fb-down") or c.startswith("fb-open")] or all(
                    c.endswith(f" of {keys[i].split('/')[-1]}") and c.startswith("fb-open") for c in found["covered"] if c.startswith(("fb-down", "fb-open"))), (width, i, where, found)
                assert not found["free"] or found["covered"] == [], ("a place that covers nothing existed", width, i, where, found)
                assert not found["freeOfOwnLink"] or not found["ownLinkCovered"], ("a place off its own Tell Assistant link existed", width, i, where, found)
                tab.keyboard.press("Escape")
                for j in [i, *around]:
                    thumbs.nth(j).evaluate(OPEN_AT["middle"])
                    thumbs.nth(j).click()
                tab.wait_for_selector(".fb-note", state="detached")
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_with_every_row_below_already_thumbed_the_reasons_still_open_next_to_their_thumb_at_the_top_of_the_window(site, wq, width):
    keys = many_rows(wq, 24)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 844})
        tab.goto(site + "#open-list")
        thumbs = tab.locator(ROW_THUMBS)
        thumbs.first.wait_for()
        for j in range(12, 24):  # a long run of rows with notes of their own
            thumbs.nth(j).evaluate(OPEN_AT["middle"])
            thumbs.nth(j).click()
        # (row, where it is put, whether that is the row's top or its thumb's, and whether the list must be near whatever else is there)
        # The last three are where a person looks: the thumb 20 px from the top of the window (under the tab bar), in the middle, and 80 px from the bottom.
        places = [(12, 0, "row", False), (12, 58, "row", False), (13, 90, "row", False), (15, 140, "row", False), (17, 120, "row", False),
                  (23, 0, "row", False), (23, 90, "row", False), (13, 20, "thumb", True), (17, 422, "thumb", True), (22, 844 - 80, "thumb", True)]
        for i, offset, what, must_be_near in places:
            tab.locator(".sf-row").nth(i).evaluate("""(e, [y, what]) => { if (what === 'row') { e.scrollIntoView({block: 'start'}); window.scrollBy(0, -y); }
                else window.scrollBy(0, e.querySelector('.fb-down').getBoundingClientRect().top - y); }""", [offset, what])
            tab.locator(".sf-row").nth(i).locator(":scope > .sf-main .fb-note button", has_text="Add a reason").evaluate("e => e.click()")  # the tab bar may cover it
            tab.wait_for_selector(".fb-pop:not([hidden])")
            tab.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
            found = tab.evaluate(SPOTS, f'.sf-row[data-key="{keys[i]}"]')
            assert found["inScreen"], (width, i, offset, found)
            if found["reachable"] or (must_be_near and width == 390):  # where something near enough keeps clear of what must stay uncovered, the list is on it
                assert found["gap"] <= found["rowHeight"] and (width > 390 or found["centre"] <= found["rowHeight"]), (width, i, offset, what, found)
            assert not [c for c in found["covered"] if c.startswith(("fb-down", "fb-open")) and not c.endswith(f" of {keys[i].split('/')[-1]}")], (width, i, offset, found)
            assert not found["free"] or found["covered"] == [], ("a place that covers nothing existed", width, i, offset, found)
            assert not found["freeOfOwnLink"] or not found["ownLinkCovered"], (width, i, offset, found)
            tab.keyboard.press("Escape")
        browser.close()


def test_choosing_a_reason_with_the_keyboard_puts_focus_on_the_notes_change_link_and_skip_or_esc_put_it_on_the_thumb(site, wq):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    focused = "document.activeElement.dataset.fb"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        tab.goto(site + "#open-list")
        tab.locator(a.thumb).focus()
        tab.keyboard.press("Enter")
        pressed(tab, a.thumb)
        for _ in range(3):  # Tell Assistant, Why?, then Add a reason
            tab.keyboard.press("Tab")
        assert tab.evaluate(focused) == "add"
        tab.keyboard.press("Enter")
        for _ in range(2):  # Not important, Wrong order, Not mine
            tab.keyboard.press("Tab")
        assert tab.evaluate(focused) == "r:not_mine"
        tab.keyboard.press("Enter")
        tab.wait_for_selector(a.pop, state="hidden")
        assert tab.evaluate(focused) == "change", "not the top of the page"
        tab.keyboard.press("Enter")  # Change
        for _ in range(4):  # on to Skip
            tab.keyboard.press("Tab")
        assert tab.evaluate(focused) == "skip"
        tab.keyboard.press("Enter")
        tab.wait_for_selector(a.pop, state="hidden")
        assert tab.evaluate(focused) == "down"
        tab.locator(f"{a.note} [data-fb='change']").focus()
        tab.keyboard.press("Enter")
        tab.keyboard.press("Escape")
        tab.wait_for_selector(a.pop, state="hidden")
        assert tab.evaluate(focused) == "down"
        tab.locator(f"{a.note} [data-fb='change']").focus()  # the same reason again changes nothing, and focus still lands
        tab.keyboard.press("Enter")
        tab.keyboard.press("Tab")
        tab.keyboard.press("Tab")
        assert tab.evaluate(focused) == "r:not_mine"
        tab.keyboard.press("Enter")
        tab.wait_for_selector(a.pop, state="hidden")
        assert tab.evaluate(focused) == "change"
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_the_thumb_has_a_44_pixel_touch_target_without_looking_bigger_and_fits_the_row_of_buttons_on_a_phone(site, wq, width):
    many_rows(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(site + "#open-list")
        thumb = tab.locator(ROW_THUMBS).nth(4)
        thumb.evaluate("e => e.scrollIntoView({block: 'center'})")
        box = thumb.bounding_box()
        assert box["width"] < 40 and box["height"] < 34, "it looks as small as before"
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        hit = tab.evaluate("""([cx, cy]) => { const xs = [], ys = [];
            for (let x = Math.round(cx) - 30; x <= cx + 30; x++) for (let y = Math.round(cy) - 30; y <= cy + 30; y++) {
              const e = document.elementFromPoint(x, y); if (e && e.closest('.fb-down')) { xs.push(x); ys.push(y); } }
            return [Math.max(...xs) - Math.min(...xs) + 1, Math.max(...ys) - Math.min(...ys) + 1]; }""", list(centre))
        assert hit[0] >= 44 and hit[1] >= 44, f"what a finger can hit (whatever sits beside it): {hit}"
        done = tab.locator(".sf-row").nth(4).locator(":scope > .sf-main .sf-mark").bounding_box()
        assert abs(done["y"] - box["y"]) < 12, "in the row of buttons, not on a line of its own"
        rows = "() => { let sum = 0, n = 0; document.querySelectorAll('.sf-lane .sf-row, .sf-list > .sf-row').forEach(r => { if (n < 12 && r.offsetHeight) { sum += r.offsetHeight; n++; } }); return sum; }"
        with_controls, rows_with = tab.evaluate("document.documentElement.scrollHeight"), tab.evaluate(rows)
        tab.evaluate("document.querySelectorAll('.fb').forEach(e => e.style.display = 'none')")
        without, rows_without = tab.evaluate("document.documentElement.scrollHeight"), tab.evaluate(rows)
        print(f"page height at {width} wide, 12 rows: {without} without the controls, {with_controls} with them; the rows alone: {rows_without} and {rows_with}")
        assert (with_controls - without) / 12 < (30 if width < 620 else 6)
        browser.close()


def test_every_kind_of_item_offers_its_own_choices_and_says_what_it_is(site, wq):
    keys = fb_seed(wq)
    start = '.start-list li[data-key="%s"]' % keys["row"]
    mail = f'.em-row[data-mk="{keys["mail"]}"]'
    result = '#ready li[data-id="td-101"]'
    rows = ["Not important", "Wrong order", "Not mine", "Already done"]
    items = {  # kind: (the item's own controls, where its note is, what it offers, what a plain thumbs-down says)
        "start_here": (start, f"{start} .fb-line .fb-note", rows, "this is wrong"),
        "email": (mail, f"{mail} .fb-line .fb-note", rows, "this is wrong"),
        "draft": (".draft-fb", ".draft-fb + .fb-extra .fb-note", ["Wrong tone", "Wrong facts", "Not how I’d say it"], "this draft is wrong"),
        "result": (result, f"{result} .fb-line .fb-note", ["Not what I asked", "Didn’t finish", "Should not have been started"], "this result is wrong"),
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        tab = context.new_page()
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#all")
        for kind, (host, note, choices, plain) in items.items():
            fb = f"{host} .fb"
            tab.locator(f"{fb} .fb-down").first.click()
            tab.wait_for_selector(f'{fb} .fb-down[aria-pressed="true"]')
            assert tab.locator(f"{fb} .fb-pop").is_hidden(), "one tap opens nothing"
            tab.wait_for_function("([n, t]) => (document.querySelector(n) || {}).textContent?.startsWith('You said: ' + t + '. Add a reason')", arg=[note, plain])
            tab.locator(f"{note} button", has_text="Add a reason").click()
            assert labels(tab, f"{fb} .fb-pop button")[:-1] == choices and labels(tab, f"{fb} .fb-pop button")[-1] == "Skip", kind
            assert tab.locator(".fb-pop:visible").count() == 1, f"{kind}: the choices open in one place only"
            tab.locator(f"{fb} .fb-pop button").nth(len(choices) - 1).click()
            tab.wait_for_selector(f"{fb} .fb-pop", state="hidden")
        got = {r["kind"]: r for r in fb_lines(wq) if r["signal"] == "thumbs_down" and r.get("amends")}
        assert {k: r["reason"] for k, r in got.items()} == {"start_here": "already_done", "email": "already_done",
                                                           "draft": "not_how_id_say", "result": "should_not_have_started"}
        assert got["result"]["subject"] == "td-101" and got["email"]["subject"] == pointers.row_pointer(keys["mail"])
        assert got["draft"]["subject"] == pointers.row_pointer(keys["row"])
        assert text(tab, f"{result} .fb-line .fb-note").startswith("You said: should not have been started. Change")
        assert text(tab, ".draft-fb + .fb-extra .fb-note").startswith("You said: not how I’d say it.")
        assert tab.locator(f'.sf-row[data-key="{keys["row"]}"] > .sf-main .fb-note').inner_text().startswith("You said: already done."), "a draft is its own item"

        draft = ".draft-fb"
        assert tab.locator(f"{draft} .fb-what").inner_text() == "Draft"
        assert tab.locator(f"{draft} .fb-open").inner_text() == "Tell Assistant about this draft"
        assert tab.locator(f"{draft} .fb-down").get_attribute("aria-label") == "This draft is wrong. Press to take it back"
        assert tab.locator(f"{result} .fb-down").get_attribute("aria-label") == "This result is wrong. Press to take it back"
        tab.locator(f"{draft} .fb-down").click()  # a second tap takes it back
        tab.wait_for_selector(".draft-fb + .fb-extra .fb-note", state="detached")
        assert tab.locator(f"{draft} .fb-down").get_attribute("aria-label") == "This draft is wrong"
        tab.locator(f"{draft} .fb-down").click()
        tab.locator(".draft-fb + .fb-extra .fb-note button", has_text="Add a reason").click()
        tab.locator(f"{draft} .fb-pop").get_by_role("button", name="Wrong facts").click()
        tab.wait_for_function("document.querySelector('.draft-fb + .fb-extra .fb-note').textContent.startsWith('You said: wrong facts.')")
        tab.locator(".draft-fb + .fb-extra .fb-note button", has_text="Change").click()
        tab.locator(f"{draft} .fb-pop").get_by_role("button", name="Skip").click()
        tab.locator(f"{result} .fb-down").click()
        tab.wait_for_selector(f"{result} .fb-note", state="detached")
        tab.locator(f"{result} .fb-down").click()
        tab.wait_for_function("(document.querySelector('#ready li[data-id=\"td-101\"] .fb-note') || {}).textContent?.startsWith('You said: this result is wrong.')")

        tab.get_by_role("button", name="Copy draft").click()
        tab.wait_for_function("document.querySelector('.draft .btn').textContent === 'Copied'")
        assert wait_for(lambda: any(r["signal"] == "used_draft" for r in fb_lines(wq)))
        used = next(r for r in fb_lines(wq) if r["signal"] == "used_draft")
        assert (used["kind"], used["subject"]) == ("draft", pointers.row_pointer(keys["row"]))

        # A row waiting for approval: a thumbs-down and Tell Assistant, no reasons.
        tab.locator("#gmail-1 > summary").click()
        tab.locator("#gmail-1 details").first.evaluate("d => d.open = true")
        cand = f'.ap-row[data-key="{keys["cand"]}"]'
        assert labels(tab, f"{cand} .ap-btns > button") == ["Approve", "Keep", "Undo"]
        assert tab.locator(f"{cand} .ap-btns > :last-child").get_attribute("class") == "fb"
        tab.locator(f"{cand} .fb-down").click()
        pressed(tab, f"{cand} .fb-down")
        assert tab.locator(f"{cand} .fb-pop").is_hidden(), "nothing to choose from"
        note = tab.locator(f"{cand} .fb-line .fb-note")
        assert note.inner_text() == "You said: this is wrong. Undo" and tab.locator(f"{cand} .fb-line .fb-note button").count() == 1
        assert fb_lines(wq)[-1]["kind"] == "candidate" and fb_lines(wq)[-1]["subject"] == pointers.row_pointer(keys["cand"])
        tab.locator(f"{cand} .fb-down").click()
        pressed(tab, f"{cand} .fb-down", False)
        assert fb_lines(wq)[-1]["signal"] == "retract"
        tab.locator(f"{cand} .fb-open").click()
        tab.locator(f"{cand} .fb-tell textarea").fill("Keep this one")
        tab.locator(f"{cand} .fb-go").click()
        tab.wait_for_selector(f"{cand} .fb-tell", state="detached")
        assert fb_lines(wq)[-1]["words"] == "Keep this one"
        cov.snap()
        browser.close()


def test_tell_assistant_records_your_words_for_an_item_and_for_the_page(site, wq):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#all")
        tab.locator(a.open).click()
        box = tab.locator(f"{a.x} .fb-tell textarea")
        assert tab.evaluate("document.activeElement.dataset.fb") == "text", "the box takes focus"
        tab.locator(f"{a.x} .fb-go").click()
        wait_text(tab, ".toast", "Write something to tell Assistant first.")
        assert fb_lines(wq) == []
        box.fill("never mind")
        tab.keyboard.press("Escape")
        tab.wait_for_selector(f"{a.x} .fb-tell", state="detached")
        tab.locator(a.open).click()
        assert tab.locator(f"{a.x} .fb-tell textarea").input_value() == ""
        tab.locator(f"{a.x} .fb-no").click()
        tab.wait_for_selector(f"{a.x} .fb-tell", state="detached")
        tab.locator(a.open).click()
        tab.locator(f"{a.x} .fb-tell textarea").fill("This is Ana's, not mine. " + "Please ask her. " * 10)
        tab.keyboard.press("Shift+Enter")
        tab.keyboard.press("Meta+Enter")
        tab.wait_for_selector(f"{a.x} .fb-tell", state="detached")
        (told,) = fb_lines(wq)
        assert told["signal"] == "tell" and told["words"].startswith("This is Ana's, not mine. Please ask her.")
        assert told["subject"] == pointers.row_pointer(keys["row"]) and told["kind"] == "row"
        note = tab.locator(a.note).inner_text()
        assert note.startswith("You told Assistant: “This is Ana's, not mine. Please ask her.") and note.endswith("…” Undo")
        assert tab.locator(a.thumb).get_attribute("aria-pressed") == "false", "words are not a thumbs-down"
        tab.locator(a.thumb).click()
        pressed(tab, a.thumb)
        assert tab.locator(a.note).inner_text().startswith("You said: this is wrong. Add a reason · Undo")
        tab.locator(f'{a.note} [data-fb="undo-told"]').click()
        tab.wait_for_function("!document.querySelector('.sf-row .fb-note').textContent.includes('You told')")
        assert fb_lines(wq)[-1]["signal"] == "retract" and fb_lines(wq)[-1]["amends"] == told["id"]
        assert tab.locator(a.thumb).get_attribute("aria-pressed") == "true"

        page_box = '.sf-head .fb[data-fk="page"]'
        assert tab.locator(f"{page_box} .fb-down").count() == 0 and tab.locator(f"{page_box} .fb-open").inner_text() == \
            "Tell Assistant about this page"
        assert tab.locator(".sf-head .fb-promise").inner_text() == PROMISE
        assert "Nothing has changed yet" not in tab.locator("body").inner_text()
        tab.locator(f"{page_box} .fb-open").click()
        tab.locator(".fb-extra[data-fp=page] textarea").fill("Too many Slack rows")
        tab.locator(".fb-extra[data-fp=page] .fb-go").click()
        tab.wait_for_selector(".fb-extra[data-fp=page] .fb-tell", state="detached")
        page_note = fb_lines(wq)[-1]
        assert (page_note["kind"], page_note["words"], "subject" in page_note) == ("page", "Too many Slack rows", False)
        assert tab.locator(".fb-extra[data-fp=page] .fb-note").inner_text() == "You told Assistant: “Too many Slack rows”. Undo"
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


def test_a_refresh_that_finishes_while_you_choose_a_reason_or_type_keeps_your_place(site, wq, monkeypatch):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    monkeypatch.setattr(server, "start_refresh", lambda: (config.write_json(refresh.status_path(), {"running": True}),
                                                         (202, {"started": True}))[1])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#all")

        def finish_a_refresh(n: int) -> None:
            tab.locator("#refresh").click()
            wait_text(tab, "#refresh", "Refreshing…")
            config.write_json(refresh.status_path(), {"running": False, "finished": f"t{n}"})
            tab.clock.run_for(2500)

        tab.evaluate("window.stillHere = 1")
        tab.locator("#refresh").click()
        wait_text(tab, "#refresh", "Refreshing…")
        tab.locator(a.thumb).click()
        pressed(tab, a.thumb)
        tab.locator(f"{a.note} button", has_text="Add a reason").click()
        config.write_json(refresh.status_path(), {"running": False, "finished": "t1"})
        tab.clock.run_for(2500)
        wait_text(tab, ".toast", "The refresh finished. Pick a reason or Skip, then reload to see the new rows.")
        assert tab.locator(a.pop).is_visible(), "the choices are still open"
        assert tab.evaluate("window.stillHere") == 1
        tab.locator(a.pop).get_by_role("button", name="Wrong order").click()
        tab.wait_for_selector(a.pop, state="hidden")

        tab.locator(a.open).click()
        tab.locator(f"{a.x} .fb-tell textarea").fill("Half a thought")
        finish_a_refresh(2)
        wait_text(tab, ".toast", "The refresh finished. Send or cancel what you are telling Assistant, then reload to see the new rows.")
        assert tab.locator(f"{a.x} .fb-tell textarea").input_value() == "Half a thought"
        assert tab.evaluate("window.stillHere") == 1
        tab.locator(f"{a.x} .fb-no").click()

        tab.locator(a.open).click()  # an empty box has nothing to lose
        tab.locator(f"{a.x} .fb-no").click()
        tab.locator(a.open).click()
        finish_a_refresh(3)
        tab.wait_for_function("window.stillHere === undefined")
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


def test_a_ready_result_keeps_its_open_box_its_text_and_its_focus_when_the_list_is_redrawn(site, wq):
    fb_seed(wq)
    item = '#ready li[data-id="td-101"]'
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#start")
        tab.wait_for_selector(item)
        tab.locator(f"{item} .fb-open").click()
        box = tab.locator(f"{item} .fb-tell textarea")
        box.fill("Looks wrong to me")
        box.evaluate("t => t.setSelectionRange(3, 8)")
        tab.evaluate("paint()")
        assert tab.locator(f"{item} .fb-tell textarea").input_value() == "Looks wrong to me"
        assert tab.evaluate("[document.activeElement.dataset.fb, document.activeElement.selectionStart, "
                            "document.activeElement.selectionEnd, !!document.activeElement.closest('#ready li[data-id=\"td-101\"]')]") == \
            ["text", 3, 8, True]
        tab.locator(f"{item} .fb-down").click()
        pressed(tab, f"{item} .fb-down")
        tab.locator(f"{item} .fb-line .fb-note button", has_text="Add a reason").click()
        assert tab.evaluate("document.activeElement.dataset.fb") == "r:not_what_i_asked"
        tab.evaluate("paint()")
        assert tab.evaluate("document.activeElement.dataset.fb") == "r:not_what_i_asked", "a button in the choices keeps focus"
        assert tab.locator(f"{item} .fb-pop").is_visible()
        tab.locator(f"{item} .fb-tell textarea").focus()
        tab.evaluate("ST.queued = Object.fromEntries(Object.entries(ST.queued).filter(([k, q]) => q.id !== 'td-101')); paint()")
        tab.wait_for_selector(item, state="detached")
        tab.evaluate("document.activeElement.blur()")
        cov.snap()
        browser.close()


def test_the_blocked_start_item_keeps_its_sent_button_and_the_new_controls_come_last(site, wq):
    fb_seed(wq)
    blocked = f'.start-list li[data-key="{pr(2)["key"]}"]'
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#start")
        tab.wait_for_selector(f"{blocked} .sf-sent")
        names = tab.locator(f"{blocked} > div > *").evaluate_all("els => els.map(e => e.className.split(' ')[0])")
        assert names[-1] == "fb" and names[-2] == "btn" and names.count("fb") == 1
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


def test_when_a_note_cant_be_saved_the_page_says_so_once_and_the_work_still_happens(site, wq):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    (wq.home / ".assistant").mkdir(exist_ok=True)
    import shutil
    shutil.rmtree(wq.home / ".assistant/feedback")
    (wq.home / ".assistant/feedback").write_text("in the way")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        tab = context.new_page()
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#all")
        tab.locator(a.thumb).click()
        wait_text(tab, ".toast", "Assistant couldn’t save that. Try again.")
        assert tab.locator(".toast").get_attribute("class") == "toast err"
        assert tab.locator(a.thumb).get_attribute("aria-pressed") == "false"
        tab.locator(a.open).click()
        tab.locator(f"{a.x} .fb-tell textarea").fill("My words")
        tab.locator(f"{a.x} .fb-go").click()
        tab.wait_for_function("document.querySelectorAll('.toast').length === 1 && document.querySelector('.toast').textContent === 'Assistant couldn’t save that. Try again.'")
        assert tab.locator(f"{a.x} .fb-tell textarea").input_value() == "My words", "your words are kept"
        tab.locator(f"{a.x} .fb-go").click()
        tab.get_by_role("button", name="Copy draft").click()
        wait_text(tab, ".toast", "Copied. Assistant couldn’t record that you used the draft.")
        assert tab.locator(".toast").count() == 1

        tab.locator(f"{a.bar} .sf-mark").click()
        tab.wait_for_selector(f"{a.row}.is-done")
        wait_text(tab, ".toast", "Done. Assistant couldn’t record that for later.")
        assert json.loads((wq.data / "marks.json").read_text())[keys["row"]]["state"] == "done", "Done still happened"
        tab.locator(f"{Sel(keys['plain']).bar} button", has_text="Snooze").click()  # not the Done button: it says so in other words
        wait_text(tab, ".toast", "That worked. Assistant couldn’t record it for later.")
        assert json.loads((wq.data / "marks.json").read_text())[keys["plain"]]["state"] == "snoozed"
        cov.snap()
        browser.close()


def test_a_new_message_replaces_the_one_before_it_at_once(site, wq):
    """Two messages with nothing between them: only the newer one is on the page. They are shown in one step, so
    the older one cannot have gone away by its own timer, however slow the machine is."""
    fb_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        tab.clock.install()
        tab.goto(site + "#all")
        shown = tab.evaluate(
            "() => { toast('first'); toast('second'); return [...document.querySelectorAll('.toast')].map(t => t.textContent) }")
        assert shown == ["second"]
        browser.close()


LOST_IT = "Assistant couldn’t record it for later."


def block_the_feedback_file(wq) -> None:
    import shutil
    (wq.home / ".assistant").mkdir(exist_ok=True)
    shutil.rmtree(wq.home / ".assistant/feedback", ignore_errors=True)
    (wq.home / ".assistant/feedback").write_text("in the way")


def toast_is(tab, expected: str) -> None:
    tab.wait_for_function("(want) => document.querySelectorAll('.toast').length === 1 && document.querySelector('.toast').textContent === want", arg=expected)
    tab.clock.run_for(13000)  # it goes away, so the next one can't be this one still showing
    tab.wait_for_selector(".toast", state="detached")


def test_a_row_action_whose_note_cant_be_saved_says_so_as_well_as_what_it_says_itself(site, wq):
    keys = fb_seed(wq)
    block_the_feedback_file(wq)
    a, plain = Sel(keys["row"]), Sel(keys["plain"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        tab.clock.install()
        tab.goto(site + "#all")
        tab.locator(f"{plain.bar} .sf-send").click()  # Send to Agent: it has a message of its own
        tab.locator(f"{plain.row} .cp-go").click()
        tab.wait_for_function("document.querySelector('.toast')?.textContent.startsWith('Sent to an agent as td-')")
        said = text(tab, ".toast")
        assert said.endswith(" " + LOST_IT) and said.count("Assistant couldn’t record") == 1 and tab.locator(".toast").count() == 1, said
        tab.locator(f"{a.bar} .sf-mark").click()
        tab.wait_for_selector(f"{a.row}.is-done")
        toast_is(tab, "Done. Assistant couldn’t record that for later.")
        tab.locator(f"{a.bar} .sf-mark").click()  # Undo, with no message of its own: the notice stands alone
        tab.wait_for_selector(f"{a.row}.is-done", state="detached")
        toast_is(tab, "That worked. " + LOST_IT)
        tab.locator(f"{a.bar} button", has_text="Snooze").click()
        toast_is(tab, "That worked. " + LOST_IT)
        tab.locator("#gmail-1 > summary").click()
        tab.locator("#gmail-1 details").first.evaluate("d => d.open = true")
        tab.locator(f'.ap-row[data-key="{keys["cand"]}"] .ap-ok').click()  # Approve
        toast_is(tab, "That worked. " + LOST_IT)
        tab.locator("#ready li[data-id='td-101'] .sf-mark").click()  # Done on a Ready for you result
        toast_is(tab, "Done. Assistant couldn’t record that for later.")
        browser.close()


def test_an_email_action_whose_note_cant_be_saved_says_so_as_well_as_what_it_says_itself(site, wq):
    write_page(mailbox_snapshot())
    wq.sources({"gmail": {"accounts": [ACCT_A, ACCT_B]}})
    fake_tool(wq.bin, "gog", [
        ("thread modify", {}),
        ("thread get t-g-act", {"seq": [{"thread": {"messages": [{"id": "1", "labelIds": ["UNREAD"]}]}},
                                        {"thread": {"messages": [{"id": "1", "labelIds": ["INBOX", "UNREAD"]}]}}]}),
        ("thread get t-g-news", {"thread": {"messages": [{"id": "1", "labelIds": ["INBOX"]}, {"id": "2"}]}})])
    block_the_feedback_file(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        tab.clock.install()
        tab.goto(site + "#mailbox")
        tab.wait_for_selector(em("g-act"), state="visible")
        tab.locator('#gmail-1 .ap-group[data-group="gmail-news"] > summary').click()
        tab.locator(f"{em('o-read')} .sf-send").click()
        tab.locator(f"{em('o-read')} .cp-go").click()
        tab.wait_for_function("document.querySelector('.toast')?.textContent.startsWith('Sent to an agent as td-')")
        assert text(tab, ".toast").endswith(" " + LOST_IT) and tab.locator(".toast").count() == 1
        tab.locator(f"{em('g-news2')} .em-acts button", has_text="Snooze").click()  # a message for the mark of several keys
        toast_is(tab, "Snoozed for 3 days. Show hidden email lists it, with Undo. " + LOST_IT)
        tab.locator(f"{em('g-act')} .em-archive").click()
        toast_is(tab, "Archived in Gmail. Gmail confirmed that no message in the conversation is in your inbox. " + LOST_IT)
        tab.locator(f"{em('g-news')} .em-readbtn").click()
        toast_is(tab, "Marked read in Gmail. Gmail confirmed that every message in the conversation is read. " + LOST_IT)
        tab.locator("#em-showhidden").click()
        tab.locator(f"{em('g-news2')} .em-undo").click()  # undo of a snooze
        toast_is(tab, "It’s back on the list. " + LOST_IT)
        tab.locator(f"{em('g-act')} .em-undo").click()  # undo of an archive: moved back to the inbox
        toast_is(tab, "Moved back to your Gmail inbox. " + LOST_IT)
        browser.close()


def test_a_reason_or_a_take_back_that_the_server_refuses_leaves_the_page_as_it_was(site, wq):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#all")
        tab.locator(a.thumb).click()
        pressed(tab, a.thumb)
        tab.locator(f"{a.note} button", has_text="Add a reason").click()
        tab.route("**/api/feedback", lambda route: route.fulfill(status=400, json={"error": "that reason doesn't fit"}))
        tab.locator(a.pop).get_by_role("button", name="Not mine").click()
        wait_text(tab, ".toast", "Assistant couldn’t save that. Try again.")
        assert tab.locator(a.pop).is_visible() and tab.locator(a.note).inner_text() == "You said: this is wrong. Add a reason · Undo"
        tab.locator(a.thumb).click()  # taking it back is refused too
        wait_text(tab, ".toast", "Assistant couldn’t save that. Try again.")
        assert tab.locator(a.thumb).get_attribute("aria-pressed") == "true" and tab.locator(".toast").count() == 1
        cov.snap()
        browser.close()


def test_the_choices_are_real_buttons_reached_and_closed_with_the_keyboard_and_stay_on_screen(site, wq):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 420})
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#open-list")
        tab.locator(a.thumb).focus()
        tab.keyboard.press("Enter")
        pressed(tab, a.thumb)
        assert tab.locator(a.pop).is_hidden()
        tab.keyboard.press("Tab")  # Tell Assistant
        tab.keyboard.press("Tab")  # Why is this here?
        tab.keyboard.press("Tab")  # Add a reason
        assert tab.evaluate("document.activeElement.dataset.fb") == "add"
        tab.keyboard.press("Enter")
        assert tab.evaluate("document.activeElement.dataset.fb") == "r:not_important", "focus moves into the choices"
        box = tab.locator(a.pop).bounding_box()
        assert box["x"] >= 8 and box["x"] + box["width"] <= 382, "kept inside a narrow screen"
        tab.keyboard.press("Tab")
        tab.keyboard.press("Tab")
        assert tab.evaluate("document.activeElement.dataset.fb") == "r:not_mine"
        tab.keyboard.press("Enter")
        tab.wait_for_selector(a.pop, state="hidden")
        assert fb_lines(wq)[-1]["reason"] == "not_mine"
        assert tab.locator(f"{a.bar} .fb > button").evaluate_all("els => els.map(e => e.type)") == ["button", "button", "button"]
        assert labels(tab, f"{a.bar} .fb > button")[1:] == ["Tell Assistant", "Why?"]
        # Scrolled near the bottom of a short screen, the choices open above the thumbs-down; scrolling moves them with it.
        last = Sel(pr(8)["key"])
        tab.locator(last.row).scroll_into_view_if_needed()
        tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        tab.locator(last.thumb).click()
        pressed(tab, last.thumb)
        tab.locator(f"{last.note} button", has_text="Add a reason").click()
        thumb, pop = tab.locator(last.thumb).bounding_box(), tab.locator(last.pop).bounding_box()
        assert (pop["y"] + pop["height"] <= thumb["y"] or pop["y"] >= thumb["y"] + thumb["height"]
                or pop["x"] + pop["width"] <= thumb["x"] or pop["x"] >= thumb["x"] + thumb["width"]), "it doesn't sit on the thumb"
        before = tab.locator(last.pop).bounding_box()["y"]
        tab.evaluate("window.scrollBy(0, -40)")
        tab.wait_for_function("(b) => document.querySelector('.fb-pop:not([hidden])').getBoundingClientRect().y !== b", arg=before)
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


CONTRAST = """(selector) => {
  const parse = c => (c.match(/[\\d.]+/g) || [0, 0, 0, 0]).map(Number);
  const over = (top, under) => { const a = top.length > 3 ? top[3] : 1; return [0, 1, 2].map(i => top[i] * a + under[i] * (1 - a)); };
  const backdrop = el => { const chain = []; for (let e = el; e; e = e.parentElement) chain.unshift(parse(getComputedStyle(e).backgroundColor)); let rgb = [255, 255, 255];
    for (const c of chain) if (c.length < 4 || c[3] > 0) rgb = over(c, rgb); return rgb; };
  const lum = rgb => { const [r, g, b] = rgb.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const el = document.querySelector(selector); const back = backdrop(el);
  let fade = 1; for (let e = el; e; e = e.parentElement) fade *= Number(getComputedStyle(e).opacity);  // a faded control is as faint as it looks
  const own = parse(getComputedStyle(el).color), fore = over([own[0], own[1], own[2], (own.length > 3 ? own[3] : 1) * fade], back);
  const [hi, lo] = [lum(fore), lum(back)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05);
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_thumb_and_the_small_labels_reach_4_5_to_1_on_the_pages_own_backgrounds(site, wq, scheme):
    keys = fb_seed(wq)
    a = Sel(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme).new_page()
        cov = Coverage(tab)
        tab.clock.install()
        tab.goto(site + "#open-list")
        draft = ".sf-row .draft-fb"
        tab.locator(f"{draft} .fb-down").click()  # a thumbs-down on the draft too, so its note is on the page
        tab.locator(a.thumb).click()
        pressed(tab, a.thumb)
        tab.locator(f"{a.note} button", has_text="Add a reason").click()
        seen = {name: tab.evaluate(CONTRAST, selector) for name, selector in {
            "the thumb": f"{a.row} > .sf-main .fb-down[aria-pressed='true']", "the thumb, not pressed": f"{a.bar} .fb-down:not([aria-pressed='true'])",
            "Tell Assistant": a.open, "the note": a.note, "a note's link": f"{a.note} .fb-link", "a reason": f"{a.pop} .fb-r",
            "the reasons' question": f"{a.pop} .fb-why", "Skip": f"{a.pop} [data-fb='skip']", "the Draft label": f"{draft} .fb-what",
            "the draft's link": f"{draft} .fb-open", "the page's promise": ".fb-promise"}.items() if tab.locator(selector).count()}
        low = {name: round(ratio, 2) for name, ratio in seen.items() if ratio < 4.5}
        assert not low and len(seen) >= 9, (scheme, low, sorted(seen))
        cov.snap()
        browser.close()


# ─── "Why is this here?" ─────────────────────────────────────────────────────

WHY_TRACE = "tr-" + "ab" * 8
WHY_FIRST = 1_795_000_000.0
WHY_LINES = ["Assistant first saw this, in GitHub.",
             "The model sorted it into Do first and suggested you do it yourself. Model: claude-test-1. Sorting rules: version 1a2b3c4d.",
             "You sent it to an agent for a first pass. It became task td-41.",
             "Assistant started an agent on it.", "The agent reported it finished.",
             "Archived in Gmail, because you pressed the button."]


def why_seed(wq, monkeypatch=None) -> dict:
    """fb_seed, plus a trail for its row (and the same trail for the task td-101 that came from another row)."""
    from assistant import effects as ledger, telemetry
    keys = fb_seed(wq)
    config.write_json(wq.data / "traces.json", {keys["row"]: {"trace": WHY_TRACE, "seen": "2026-11-20", "first": WHY_FIRST},
                                                pr(1)["key"]: {"trace": "tr-" + "cd" * 8, "seen": "2026-11-20"}})
    sub = "row:" + "1" * 16
    for kind, subject, fields in [
            ("triage_verdict", sub, {"verdict": "you", "lane": "do_first", "model": "claude-test-1", "prompt_version": "rubric-1a2b3c4d",
                                     "source": "github"}),
            ("task", "td-41", {"state": "open", "origin": "work-queue", "mode": "assist"}),
            ("dispatch", "td-41", {"outcome": "done", "ok": True, "workspace": "workspace:1000010013", "confirmed": True}),
            ("task", "td-41", {"state": "done", "prev_state": "in-progress"})]:
        telemetry.emit(kind, WHY_TRACE, subject, "work-queue", **fields)
    ef = ledger.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                      {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"}, trace=WHY_TRACE)
    ledger.finish(ef, "done", True)
    return keys


def why_link(key):
    return f'.sf-row[data-key="{key}"] > .sf-main .why-link'


def why_box(key):
    return f'.sf-row[data-key="{key}"] > .why-box'


def test_the_why_link_opens_only_on_a_click_and_shows_the_steps_in_order_with_their_times(site, wq):
    keys = why_seed(wq)
    link, box = why_link(keys["row"]), why_box(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US", timezone_id="UTC").new_page()
        cov = Coverage(tab)
        requests = []
        tab.on("request", lambda r: requests.append(r.url) if "/api/why" in r.url else None)
        tab.clock.install(time=WHY_FIRST + 3600 * 5)  # the same day, five hours later
        tab.goto(site + "#open-list")
        tab.wait_for_selector(link)
        assert tab.locator(link).inner_text() == "Why?"
        assert tab.locator(link).get_attribute("aria-label") == "Why is this here?"
        assert tab.locator(link).get_attribute("title") == "Why is this here?"
        assert tab.locator(box).count() == 0 and requests == [], "nothing opens, and nothing is asked, by itself"
        assert tab.locator(link).get_attribute("aria-expanded") == "false"
        tab.locator(link).click()
        tab.wait_for_selector(f"{box} .why-steps li")
        assert tab.locator(link).get_attribute("aria-expanded") == "true"
        assert len(requests) == 1 and "key=" + urllib.parse.quote(keys["row"], safe="") in requests[0]
        assert tab.locator(f"{box} .why-head").inner_text() == "Why is this here?", "the full question is on screen"
        lines = tab.locator(f"{box} .why-steps li").evaluate_all("els => els.map(e => e.lastChild.textContent)")
        assert lines == WHY_LINES
        times = tab.locator(f"{box} .why-steps time").evaluate_all("els => els.map(e => e.textContent)")
        assert times[0] == "Today 11:06 am", times  # the reader's own clock, UTC here
        assert all(re.fullmatch(r"(Today|[A-Z][a-z]{2} \d{1,2},) \d{1,2}:\d{2} [ap]m", t) for t in times), times
        assert any(t.startswith("Nov ") for t in times), "a step from another day names the day"
        assert tab.locator(f"{box} .why-foot").inner_text() == (
            "This is everything Assistant has recorded about this item. The record stays on this Mac.")
        assert tab.locator(f"{box} p", has_text="Loading").count() == 0
        # directly under the row's own line of buttons, above the draft
        order = tab.evaluate("""(sel) => { const box = document.querySelector(sel), row = box.parentElement;
            return [...row.children].map(c => c.className.split(' ')[0]).filter(c => ['sf-main', 'fb-extra', 'why-box', 'draft'].includes(c)); }""", box)
        assert order[:4] == ["sf-main", "fb-extra", "why-box", "draft"], order
        no_sideways_scroll(tab)
        cov.snap()
        browser.close()


def test_escape_in_the_search_clears_it_and_leaves_an_open_why_block_open(site, wq):
    keys = why_seed(wq)
    link, box = why_link(keys["row"]), why_box(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.locator(link).click()
        tab.wait_for_selector(f"{box} .why-steps li")
        tab.locator("#tab-search").fill("change 3")
        tab.press("#tab-search", "Escape")
        assert tab.input_value("#tab-search") == ""
        assert tab.locator(box).count() == 1, "the Why block stays open"
        tab.focus("#tab-search")
        tab.keyboard.press("Escape")
        tab.wait_for_selector(box, state="detached")  # with nothing to clear, Escape closes the Why block
        cov.snap()
        browser.close()


def test_the_why_block_takes_focus_reaches_close_with_one_tab_and_closes_every_way_with_focus_back_on_the_link(site, wq):
    keys = why_seed(wq)
    link, box = why_link(keys["row"]), why_box(keys["row"])
    focus_on = "document.activeElement.className"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")

        def opened():
            tab.locator(link).click()
            tab.wait_for_selector(f"{box} .why-steps li")

        def closed():
            tab.wait_for_selector(box, state="detached")
            assert tab.locator(link).get_attribute("aria-expanded") == "false"
            assert "why-link" in tab.evaluate(focus_on), "focus returns to the link"

        opened()
        assert "why-head" in tab.evaluate(focus_on), "on open, focus is in the block"
        tab.keyboard.press("Tab")
        assert "why-close" in tab.evaluate(focus_on), "one Tab reaches Close, before the draft"
        tab.keyboard.press("Enter")
        closed()
        opened()
        tab.locator(link).click()  # a second click
        closed()
        opened()
        tab.locator(f"{box} .why-steps li").first.click()  # Esc works after a click on the block's own text
        tab.keyboard.press("Escape")
        closed()
        opened()
        tab.locator("h2").first.click()  # and after a click on the page
        tab.keyboard.press("Escape")
        closed()
        tab.keyboard.press("Escape")  # with nothing open, nothing happens
        tab.locator(link).focus()
        tab.keyboard.press("Enter")  # opens from the keyboard
        tab.wait_for_selector(f"{box} .why-steps li")
        cov.snap()
        browser.close()


def test_escape_closes_the_note_box_the_reasons_or_the_newest_block_in_that_order(site, wq):
    keys = why_seed(wq)
    a, b = Sel(keys["row"]), Sel(keys["plain"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])} .why-steps li")
        tab.locator(why_link(keys["plain"])).click()
        tab.wait_for_selector(f"{why_box(keys['plain'])} .why-foot")
        tab.locator(a.open).click()  # a Tell Assistant box on the first row
        tab.wait_for_selector(f"{a.x} .fb-tell textarea")
        tab.keyboard.press("Escape")  # the note box closes first, and only it
        tab.wait_for_selector(f"{a.x} .fb-tell", state="detached")
        assert tab.locator(why_box(keys["row"])).count() == 1 and tab.locator(why_box(keys["plain"])).count() == 1
        tab.locator(b.thumb).click()
        pressed(tab, b.thumb)
        tab.locator(f"{b.note} button", has_text="Add a reason").click()
        tab.wait_for_selector(".fb-pop:not([hidden])")
        tab.keyboard.press("Escape")  # then the reasons
        tab.wait_for_selector(".fb-pop", state="hidden")
        assert tab.locator(".why-box").count() == 2
        tab.locator("h2").first.click()
        tab.keyboard.press("Escape")  # then the block opened last
        tab.wait_for_selector(why_box(keys["plain"]), state="detached")
        assert tab.locator(why_box(keys["row"])).count() == 1
        tab.keyboard.press("Escape")
        tab.wait_for_selector(why_box(keys["row"]), state="detached")
        cov.snap()
        browser.close()


def layout_seed(wq) -> dict:
    """fb_seed with ten pull requests (a long title among them, and one the model says an agent can finish)."""
    keys = fb_seed(wq)
    row = keys["row"]
    verdict = {**VERDICT, "disposition": "you", "lane": "do_first", "draft": "Hi Ana, I will take this one.", "title": "Review change 3"}
    start = [{"key": pr(2)["key"], "title": "o/r#2: Change 2", "source": "GitHub", "effort": "", "reason": triage.BLOCKED_REASON,
              "blocked": True},
             {"key": row, "title": "o/r#3: Change 3", "source": "GitHub", "effort": "Quick", "reason": "Go", "blocked": False}]
    github = [pr(n) for n in range(1, 11)]
    github[7] = {**pr(8), "title": "o/r#8: " + "A long title that goes on and on about the change " * 2}
    write_page({"built": "now", "github": github, "status": [],
                "outlook": [mail("o-act", "act", "ana@example.com", "Can you review?")],
                "gmail": {"me@example.com": [gmail_row("g-news", "news")]},
                "triage": {"rows": {row: verdict, pr(9)["key"]: {**AGENT, "lane": "week"}}, "start": start, "changes": None,
                           "waiting": 0, "skipped": 0, "note": "", "failed": []}})
    return keys


KINDS = {"a plain Open list row": '.sf-row[data-key="https://github.com/o/r/pull/8"]',
         "a row with the Small pill and a draft": '.sf-row[data-key="https://github.com/o/r/pull/3"]',
         "an agent row": '.sf-row[data-key="https://github.com/o/r/pull/9"]', "an email row": ".em-row",
         "a row waiting for approval": ".ap-row", "a Start here item": ".start-list li"}
HEIGHTS = "(s) => [...document.querySelectorAll(s)].filter(e => e.getBoundingClientRect().height).map(e => e.getBoundingClientRect().height)"


@pytest.mark.parametrize("width", [390, 1280])
def test_no_kind_of_row_gains_a_line_from_the_closed_why_link(site, wq, width):
    layout_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector(".fb .why-link")
        with_link = {name: tab.evaluate(HEIGHTS, sel) for name, sel in KINDS.items()}
        total = tab.evaluate("document.documentElement.scrollHeight")
        tab.evaluate("document.querySelectorAll('.why-link').forEach(e => e.style.display = 'none')")
        without = {name: tab.evaluate(HEIGHTS, sel) for name, sel in KINDS.items()}
        print(f"row heights at {width} wide, with the link / without it:",
              {n: (round(sum(with_link[n])), round(sum(without[n]))) for n in KINDS},
              f"page: {total} / {tab.evaluate('document.documentElement.scrollHeight')}")
        for name in KINDS:
            assert with_link[name] and len(with_link[name]) == len(without[name]), (width, name)
            assert with_link[name] == without[name], (width, name, with_link[name], without[name])
        no_sideways_scroll(tab)
        browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_a_ready_result_with_an_open_workspace_button_a_blocked_one_and_one_without_gain_no_line(site, wq, width):
    fb_seed(wq)
    many_rows(wq, 12)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector("#ready li .why-link")
        probe = """() => Object.fromEntries([...document.querySelectorAll('#ready li[data-id]')].map(e => [e.dataset.id,
            [e.getBoundingClientRect().height, !!e.querySelector('button:not(.sf-mark):not(.fb-down):not(.fb-open):not(.why-link)'),
             e.querySelector('.ready-result').classList.contains('blocked')]]))"""
        with_link = tab.evaluate(probe)
        tab.evaluate("document.querySelectorAll('.why-link').forEach(e => e.style.display = 'none')")
        without = tab.evaluate(probe)
        print(f"ready results at {width} wide, with the link / without it:", {k: (v[0], without[k][0]) for k, v in with_link.items()})
        assert with_link["td-101"][1] and with_link["td-102"][2] and not with_link["td-105"][1], "the three kinds are there"
        assert {k: v[0] for k, v in with_link.items()} == {k: v[0] for k, v in without.items()}
        browser.close()


def test_with_the_link_hidden_a_ready_result_with_an_open_workspace_button_inside_start_here_is_one_line_shorter_at_390(site, wq):
    """Compares the same page with the link shown and with it hidden; it is not a comparison with the page before this
    change, which lays this result out just as tall (259 px). Start here holds the Ready for you box, which leaves a
    result 274 px wide at 390, and its Open workspace button, thumb, Tell Assistant and Why? need about 300, so the
    controls take a line the hidden-link page doesn't use. Every other result is the same either way."""
    layout_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 390, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector("#ready li .why-link")
        probe = "() => Object.fromEntries([...document.querySelectorAll('#ready li[data-id]')].map(e => [e.dataset.id, e.getBoundingClientRect().height]))"
        with_link = tab.evaluate(probe)
        tab.evaluate("document.querySelectorAll('.why-link').forEach(e => e.style.display = 'none')")
        without = tab.evaluate(probe)
        grown = {k: round(with_link[k] - without[k], 1) for k in with_link if abs(with_link[k] - without[k]) > 0.5}
        print("results inside Start here at 390: height gained with the link:", grown, "of", {k: round(v) for k, v in without.items()})
        assert set(grown) <= {"td-101"} and all(0 < g <= 40 for g in grown.values()), grown
        browser.close()


@pytest.mark.parametrize("width", [640, 700, 760, 800, 1024, 1280])
def test_a_title_keeps_at_least_200_pixels_and_rows_stack_before_it_is_squeezed(site, wq, width):
    layout_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector(".fb .why-link")
        wide = tab.evaluate("""() => [...document.querySelectorAll('.sf-row')].filter(r => r.getBoundingClientRect().height)
            .map(r => r.querySelector('.sf-link').getBoundingClientRect().width)""")
        assert wide and min(wide) >= 200, (width, wide)
        no_sideways_scroll(tab)
        browser.close()


def test_at_1280_the_link_moves_the_buttons_and_narrows_the_title_by_at_most_60_pixels(site, wq):
    layout_seed(wq)
    probe = """() => [...document.querySelectorAll('.sf-row')].filter(r => r.getBoundingClientRect().height).map(r => {
        const x = s => r.querySelector(s).getBoundingClientRect(); return {title: x('.sf-link').width, done: x('.sf-mark').left, send: x('.sf-send').left}; })"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": 1280, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector(".fb .why-link")
        with_link = tab.evaluate(probe)
        tab.evaluate("document.querySelectorAll('.why-link').forEach(e => e.style.display = 'none')")
        without = tab.evaluate(probe)
        moved = [(round(b["title"] - a["title"], 1), round(b["done"] - a["done"], 1), round(b["send"] - a["send"], 1))
                 for a, b in zip(with_link, without)]
        print("at 1280 the link narrows the title, and moves Done and Send to Agent, by (title, Done, Send):", moved)
        assert moved and all(0 <= t <= 60 and 0 <= d <= 60 and 0 <= s <= 60 for t, d, s in moved), moved
        browser.close()


@pytest.mark.parametrize("width, least", [(1280, 20), (390, 12)])
def test_why_is_a_link_of_its_own_with_clear_space_between_the_end_of_tell_assistant_and_it(site, wq, width, least):
    layout_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 900})
        tab.goto(site + "#all")
        tab.wait_for_selector("#ready li .why-link")
        gaps = tab.evaluate("""() => [...document.querySelectorAll('.fb')].filter(f => f.querySelector('.why-link')).map(f => {
            const tell = f.querySelector('.fb-open'), r = document.createRange(); r.selectNodeContents(tell);
            const why = f.querySelector('.why-link').getBoundingClientRect(), end = r.getBoundingClientRect();
            return [f.dataset.fk, Math.abs(why.top - end.top) < 14 ? why.left - end.right : 99]; })""")
        print(f"space between the end of Tell Assistant and Why? at {width} wide:", gaps)
        assert gaps and all(g >= least for _, g in gaps), gaps
        browser.close()


def test_the_why_link_is_on_every_kind_of_item_that_has_tell_assistant_and_nowhere_else(site, wq):
    keys = why_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#all")
        tab.wait_for_selector(".fb .why-link")
        every = tab.evaluate("""() => [...document.querySelectorAll('.fb')].map(f => ({kind: f.dataset.fk, tell: !!f.querySelector('.fb-open'),
            why: !!f.querySelector('.why-link'), name: (f.querySelector('.fb-open') || {}).textContent}))""")
        assert {e["kind"] for e in every} >= {"row", "start_here", "email", "candidate", "result", "draft", "page"}
        for e in every:
            assert e["why"] == (e["kind"] not in ("draft", "page")), e
            assert e["tell"], e
        assert tab.locator('.fb[data-fk="page"] .why-link, .draft-fb .why-link').count() == 0
        assert tab.locator("#ready li .why-link").count() >= 1 and tab.locator(".start-list li .why-link").count() >= 1
        cov.snap()
        browser.close()


def test_a_ready_result_shows_the_steps_of_the_row_it_came_from(site, wq):
    keys = why_seed(wq)
    config.write_json(wq.data / "traces.json", {**json.loads((wq.data / "traces.json").read_text()),
                                                pr(1)["key"]: {"trace": WHY_TRACE, "seen": "2026-11-20", "first": WHY_FIRST}})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#all")
        item = '#ready li[data-id="td-101"]'
        tab.locator(f"{item} .why-link").click()
        tab.wait_for_selector(f"{item} > .why-box .why-steps li")
        assert tab.locator(f"{item} > .why-box .why-steps li").count() == 6
        cov.snap()
        browser.close()


def test_the_why_block_says_each_empty_state_and_a_failed_request_in_the_block(site, wq):
    keys = why_seed(wq)
    plain = why_link(keys["plain"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.locator(plain).click()
        tab.wait_for_selector(f"{why_box(keys['plain'])} .why-foot")
        body = tab.locator(why_box(keys["plain"])).inner_text()
        assert "Assistant has no record for this yet. It starts one at the next refresh." in body
        assert "This is everything Assistant has recorded about this item." in body and "Close" in body
        tab.locator(plain).click()
        (wq.home / ".assistant/telemetry/telemetry.db").write_bytes(b"junk" * 200)
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])} .why-foot")
        body = tab.locator(why_box(keys["row"])).inner_text()
        assert "Assistant can't read its record right now. Nothing is lost; try again in a minute." in body
        assert "Assistant first saw this, in GitHub." not in body and "Assistant first saw this." in body, "what could still be read is shown"
        assert "Some of it couldn't be read, so this may be missing steps. The record stays on this Mac." in body
        assert "This is everything" not in body, "a partial read never says it is everything"
        tab.locator(why_link(keys["row"])).click()
        tab.route("**/api/why*", lambda route: route.fulfill(status=500, body="{}", content_type="application/json"))
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])}")
        tab.wait_for_function("(s) => document.querySelector(s).textContent.includes('couldn’t load')", arg=why_box(keys["row"]))
        assert "Loading" not in tab.locator(why_box(keys["row"])).inner_text()
        assert tab.locator(f"{why_box(keys['row'])} .why-close").count() == 1
        cov.snap()
        browser.close()


def test_the_why_block_says_loading_while_it_waits_and_shows_everything_the_server_sends_as_text(site, wq):
    keys = why_seed(wq)
    markup = "<img src=x onerror=window.pwned=1><script>window.pwned=1</script><b>x</b>"
    hostile = {"state": "ok", "more": 3, "left_out": "3 earlier steps aren't shown here. " + markup, "note": "<b>note</b>" + markup,
               "message": "<u>message</u>" + markup, "footer": "<i>footer</i>" + markup,
               "steps": [{"at": WHY_FIRST, "kind": "sorted", "text": "<img src=x onerror=window.pwned=1> Model: <script>window.pwned=1</script>"}]}
    release = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.route("**/api/why*", lambda route: release.append(route))
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])}")
        assert "Loading" in tab.locator(why_box(keys["row"])).inner_text()
        tab.wait_for_function("() => true")
        assert len(release) == 1
        release[0].fulfill(status=200, body=json.dumps(hostile), content_type="application/json")
        tab.wait_for_selector(f"{why_box(keys['row'])} .why-steps li")
        box = why_box(keys["row"])
        assert tab.evaluate("window.pwned") is None and tab.locator(f"{box} img, {box} script, {box} b, {box} i, {box} u").count() == 0
        body = tab.locator(box).inner_text()
        for shown in ("<img src=x", "<script>", "<b>note</b>", "<i>footer</i>", "<u>message</u>", "3 earlier steps aren't shown here."):
            assert shown in body, shown
        cov.snap()
        browser.close()


def test_a_step_with_a_time_that_cannot_be_formatted_still_draws_the_block_with_its_close_button(site, wq):
    keys = why_seed(wq)
    odd = {"state": "ok", "more": 0, "left_out": "", "note": "", "message": "", "footer": "Footer.",
           "steps": [{"at": 1e300, "kind": "task", "text": "A step with a time nobody can format."},
                     {"at": WHY_FIRST, "kind": "task", "count": 3, "last": 1e300, "text": "Repeated step."}]}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.route("**/api/why*", lambda route: route.fulfill(status=200, body=json.dumps(odd), content_type="application/json"))
        tab.locator(why_link(keys["row"])).click()
        box = why_box(keys["row"])
        tab.wait_for_selector(f"{box} .why-foot")
        assert tab.locator(f"{box} .why-steps li").count() == 2 and tab.locator(f"{box} .why-close").count() == 1
        assert tab.locator(f"{box} .why-steps li").first.inner_text().strip() == "A step with a time nobody can format."
        assert "Last time" not in tab.locator(box).inner_text(), "a last time that can't be formatted is left out"
        cov.snap()
        browser.close()


def test_a_step_that_happened_several_times_shows_its_first_time_and_says_when_the_last_was(site, wq):
    keys = why_seed(wq)
    answer = {"state": "ok", "more": 0, "left_out": "", "note": "", "message": "", "footer": "Footer.",
              "steps": [{"at": WHY_FIRST, "kind": "start_failed", "count": 14, "last": WHY_FIRST + 3600,
                         "text": "Assistant tried to start an agent 14 times and couldn't: cmux didn't answer. It will try again by itself."}]}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US", timezone_id="UTC").new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.route("**/api/why*", lambda route: route.fulfill(status=200, body=json.dumps(answer), content_type="application/json"))
        tab.locator(why_link(keys["row"])).click()
        box = why_box(keys["row"])
        tab.wait_for_selector(f"{box} .why-steps li")
        assert tab.locator(f"{box} .why-steps time").inner_text() == "Nov 18, 11:06 am"
        assert tab.locator(f"{box} .why-steps li").inner_text().endswith("Last time: Nov 18, 12:06 pm.")
        cov.snap()
        browser.close()


def test_an_open_why_block_holds_off_the_automatic_reload_and_a_closed_one_does_not(site, wq):
    keys = why_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.evaluate("window.stayed = true")
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])} .why-steps li")
        tab.evaluate("reloadIfIdle()")
        tab.wait_for_selector(".toast")
        assert "Close the explanation under “Why is this here?”" in tab.locator(".toast").inner_text()
        assert tab.evaluate("window.stayed") is True and tab.locator(why_box(keys["row"])).count() == 1
        tab.locator(f"{why_box(keys['row'])} .why-close").click()
        cov.snap_before("location.reload()")
        with tab.expect_navigation():
            tab.evaluate("reloadIfIdle()")
        cov.snap()
        browser.close()


def test_changing_tabs_closes_open_blocks_so_a_hidden_one_never_holds_the_reload(site, wq):
    keys = why_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#start")
        item = ".start-list li"
        tab.locator(f"{item} .why-link").last.click()
        tab.wait_for_selector(f"{item} .why-box")
        tab.locator('#tabs .tab[data-t="open-list"]').click()
        tab.wait_for_selector(".why-box", state="detached")
        tab.locator('#tabs .tab[data-t="start"]').click()
        assert tab.locator(".why-box").count() == 0 and tab.locator(f"{item} .why-link").last.get_attribute("aria-expanded") == "false"
        cov.snap_before("location.reload()")
        with tab.expect_navigation():
            tab.evaluate("reloadIfIdle()")
        cov.snap()
        browser.close()


def test_an_open_why_block_survives_the_page_repainting_its_lists(site, wq):
    keys = why_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#all")
        item = '#ready li[data-id="td-101"]'
        tab.locator(f"{item} .why-link").click()
        tab.wait_for_selector(f"{item} > .why-box")
        tab.evaluate("paint()")
        tab.wait_for_selector(f"{item} > .why-box")
        assert tab.locator(f"{item} .why-link").get_attribute("aria-expanded") == "true"
        assert tab.locator(".why-box").count() == 1
        cov.snap()
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_the_open_why_block_fits_the_screen_and_the_row_without_sideways_scroll(site, wq, width):
    keys = why_seed(wq)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(site + "#all")
        tab.locator(why_link(keys["row"])).click()
        tab.wait_for_selector(f"{why_box(keys['row'])} .why-steps li")
        no_sideways_scroll(tab)
        box = tab.locator(why_box(keys["row"])).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        for sel in ("li", "time"):
            for el in tab.locator(f"{why_box(keys['row'])} .why-steps {sel}").all():
                b = el.bounding_box()
                assert b["x"] + b["width"] <= width + 0.5
        browser.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_why_link_and_block_reach_4_5_to_1_on_the_pages_own_backgrounds(site, wq, scheme):
    keys = why_seed(wq)
    link, box = why_link(keys["row"]), why_box(keys["row"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme).new_page()
        cov = Coverage(tab)
        tab.goto(site + "#open-list")
        tab.locator(link).click()
        tab.wait_for_selector(f"{box} .why-steps li")
        seen = {"the link": tab.evaluate(CONTRAST, link), "the heading": tab.evaluate(CONTRAST, f"{box} .why-head"),
                "a step": tab.evaluate(CONTRAST, f"{box} .why-steps li"),
                "a time": tab.evaluate(CONTRAST, f"{box} time"), "the footer": tab.evaluate(CONTRAST, f"{box} .why-foot"),
                "Close": tab.evaluate(CONTRAST, f"{box} .why-close")}
        low = {name: round(ratio, 2) for name, ratio in seen.items() if ratio < 4.5}
        print(scheme, {name: round(ratio, 2) for name, ratio in seen.items()})
        assert not low, (scheme, low)
        cov.snap()
        browser.close()
