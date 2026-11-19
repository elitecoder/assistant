"""Drive the Work queue page in Chromium against a real server on a spare port with temporary data.

Done marks live on the server, so they survive a fresh browser. Send to
Assistant writes a TODO into a temporary store. Approving Gmail and applying
moves it to Trash through a fake gog.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from work_queue import config, page, refresh  # noqa: E402
from work_queue_fakes import fake_tool, real_browsers, wq  # noqa: E402, F401

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.real_mode
PRS = [{"key": f"https://github.com/o/r/pull/{n}", "link": f"https://github.com/o/r/pull/{n}", "draft": n == 3,
        "title": f"o/r#{n}: Change {n}", "meta": "Review requested · ana · opened Nov 2"} for n in (1, 2, 3)]
MAIL = {"from": "Ana", "day": "Nov 2", "to": ["Me"], "cc": [], "body": "Body text", "unread": False}


def snapshot() -> dict:
    gmail_rows = [{**MAIL, "key": "gmail:1", "account": "me@example.com", "msg_ids": ["m1"], "group": "news",
                   "title": "Digest", "subject": "Digest", "reason": "A mailing list or newsletter.", "addr": "news@list.example",
                   "unsub_url": "", "link": "https://mail.google.com/mail/#inbox/t1"}]
    outlook_rows = [{**MAIL, "key": "mail:1", "group": "gh", "title": "PR merged", "subject": "PR merged", "reason": "GitHub.",
                     "addr": "notifications@github.com", "link": "https://outlook.office.com/m1"}]
    return {"built": "Nov 18, 9:00 AM", "github": PRS, "gmail": {"me@example.com": gmail_rows}, "outlook": outlook_rows,
            "status": [{"name": "GitHub", "state": "ok", "detail": "read 3 items"}]}


def spare_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(wq, monkeypatch):
    real_browsers(monkeypatch)
    (wq.home / ".assistant").mkdir()
    (wq.home / ".assistant/config.json").write_text('{"mode": "pro"}')
    doc, cands = page.render(snapshot())
    refresh.site_path().parent.mkdir(parents=True)
    refresh.site_path().write_text(doc)
    config.write_json(refresh.candidates_path(), cands)
    fake_tool(wq.bin, "gog", [("gmail trash m1", {"ok": True}), ("gmail get m1", {"message": {"labelIds": ["TRASH"]}})])
    port = spare_port()
    env = {**os.environ, "REVIEW_PORT": str(port), "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}"}
    proc = subprocess.Popen([sys.executable, "-m", "work_queue.server"], cwd=REPO, env=env, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().startswith("Serving the Work queue")
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()
    proc.wait(10)


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def test_marks_persist_send_to_assistant_queues_a_todo_and_apply_trashes_gmail(served, wq):
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
        third.get_by_role("button", name="Send to Assistant").click()
        fresh.wait_for_function("document.querySelector('.sf-row[data-key=\"https://github.com/o/r/pull/3\"] .q-status')"
                                ".textContent.startsWith('Queued as td-051')")
        todo = wq.items()[-1]
        assert (todo["id"], todo["title"], todo["autoDispatch"], todo["url"]) == \
            ("td-051", "o/r#3: Change 3", True, PRS[2]["link"])
        assert third.locator(".sf-send").inner_text() == "Sent td-051"

        fresh.locator('#tabs .tab[data-t="email"]').click()
        fresh.wait_for_selector("#open-list", state="hidden")
        assert fresh.locator("#email").is_visible()
        fresh.locator('#email details').first.evaluate("d => d.open = true")
        fresh.locator('.ap-row[data-key="mail:1"]').get_by_role("button", name="Approve").click()
        fresh.wait_for_function("document.getElementById('mq-q').textContent === 'subject:\"PR merged\"'")

        fresh.locator('#tabs .tab[data-t="gmail-1"]').click()
        fresh.locator('#gmail-1 details').first.evaluate("d => d.open = true")
        fresh.locator('.ap-row[data-key="gmail:1"]').get_by_role("button", name="Approve").click()
        fresh.wait_for_function("document.getElementById('ap-count').textContent === '1 approved'")
        fresh.locator("#ap-apply").click()
        fresh.wait_for_function("document.querySelector('.ap-row[data-key=\"gmail:1\"] .ap-status').textContent"
                                " === 'In Gmail Trash, verified'", timeout=15000)

        phone = browser.new_context(viewport={"width": 375, "height": 800}).new_page()
        phone.goto(served)
        widths = phone.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert widths[0] <= widths[1], widths
        browser.close()
