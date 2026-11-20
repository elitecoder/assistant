"""sources.json drives the refresh, and every linked source lands on one generated page."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from work_queue import config, page, refresh
from work_queue.inputs import outlook
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401
from test_work_queue_inputs import ACCT, ENTRY, ME, NOW, TODAY, fake_slack, gmail_fakes, outlook_fakes  # noqa: F401

PR = {"number": 7, "title": "Fix it", "url": "https://github.com/o/r/pull/7", "repository": {"nameWithOwner": "o/r"},
      "author": {"login": "ana"}, "createdAt": "2026-11-02T10:00:00Z", "isDraft": False}


def every_tool(wq):
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    fake_tool(wq.bin, "npx", [("", {"exit": 1, "stderr": "should not run"})])
    fake_tool(wq.bin, "uv", [("", {"exit": 1, "stderr": "should not run"})])


def test_only_linked_sources_run(wq):
    every_tool(wq)
    gmail_fakes(wq)
    wq.sources({"github": {"skip_authors": []}, "gmail": {"accounts": [ACCT]}})
    status = {s["name"]: s for s in refresh.run(now=NOW)}
    assert {n: s["state"] for n, s in status.items()} == {
        "Slack": "off", "GitHub": "ok", "Gmail": "ok", "Calendar": "off", "Outlook": "off"}
    assert status["Slack"]["detail"] == "not linked. Run bin/work-queue-setup.py link slack"
    assert status["Gmail"]["detail"] == "read 8 items" and status["GitHub"]["detail"] == "read 1 item"
    assert calls(wq.bin, "npx") == [] and calls(wq.bin, "uv") == []
    assert len(calls(wq.bin, "gh")) == 1 and not any("calendar" in c for c in calls(wq.bin, "gog"))
    html = refresh.site_path().read_text()
    assert 'data-key="https://github.com/o/r/pull/7"' in html and 'data-q-title="o/r#7: Fix it"' in html
    assert 'id="slack-saves"' not in html and 'id="email"' not in html and 'id="gmail-1"' in html
    assert 'id="ap-bar"' in html
    cands = json.loads(refresh.candidates_path().read_text())
    assert {c["kind"] for c in cands.values()} == {"gmail", "unsub"}
    assert json.loads(refresh.status_path().read_text())["error"] == ""

    wq.sources({"github": {"skip_authors": ["ana"]}})
    refresh.run(now=NOW)
    html = refresh.site_path().read_text()
    assert "o/r#7" not in html and 'id="gmail-1"' not in html and 'id="ap-bar"' not in html
    assert json.loads(refresh.candidates_path().read_text()) == {}


def test_every_linked_source_renders_into_one_page(wq, fake_slack, monkeypatch):
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    gmail_fakes(wq)
    outlook_fakes(wq)
    fake_tool(wq.bin, "gog", json.loads((wq.bin / "gog.json").read_text()) + [("calendar events", {"events": [
        {"summary": "Standup", "start": {"date": "2026-11-18"}, "htmlLink": "https://calendar.google.com/e1"}]})])
    read_outlook = outlook.rows
    monkeypatch.setattr(outlook, "rows", lambda entry: read_outlook(entry, TODAY, lambda s: None))
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    wq.sources({"slack": ENTRY, "github": {}, "gmail": {"accounts": [ACCT]}, "calendar": {"accounts": [ACCT]},
                "outlook": {"address": ME}})
    status = refresh.run(transport=fake_slack._transport, now=NOW)
    assert all(s["state"] == "ok" for s in status), status
    html = refresh.site_path().read_text()
    for tab in ("open-list", "calendar", "email", "gmail-1", "unsubscribe", "slack-saves", "all"):
        assert f'data-t="{tab}"' in html
    for lane in ("<h3>Slack ", "<h3>Pull requests ", "<h3>Outlook ", f"<h3>Gmail: {ACCT} "):
        assert lane in html
    assert "All day · Standup" in html and "Can you review?" in html and "Weekly digest" in html
    assert 'data-group="last"' in html and "Evidence: Me," in html and "Saves that can't be read" in html
    cands = json.loads(refresh.candidates_path().read_text())
    assert {c["kind"] for c in cands.values()} == {"slack", "gmail", "unsub", "mail"}
    assert [c["url"] for c in cands.values() if c["kind"] == "unsub"] == ["https://list.example/u?id=1"]
    slack_cands = [c for c in cands.values() if c["kind"] == "slack"]
    assert len(slack_cands) == 9 and all(c["link"].startswith("https://acme.slack.com/archives/") for c in slack_cands)


def test_a_failing_source_shows_its_error_and_the_rest_still_render(wq, monkeypatch):
    every_tool(wq)
    wq.sources({"github": {}, "outlook": {"address": ME}, "slack": ENTRY})
    status = {s["name"]: s for s in refresh.run(now=NOW)}
    assert status["Outlook"]["state"] == "error" and "should not run" in status["Outlook"]["detail"]
    assert status["Slack"]["detail"] == "Set SLACK_USER_TOKEN to a Slack user token (xoxp-)."
    assert status["GitHub"]["state"] == "ok"
    html = refresh.site_path().read_text()
    assert 'class="src-error"><b>Outlook</b>' in html and "o/r#7" in html
    error = json.loads(refresh.status_path().read_text())["error"]
    assert error.startswith("Slack: Set SLACK_USER_TOKEN") and "; Outlook: " in error


def test_an_unreadable_settings_file_stops_the_refresh_and_says_why(wq, capsys):
    wq.sources({})
    (wq.home / ".assistant/work-queue/sources.json").write_text("[1]")
    with pytest.raises(ValueError, match="isn't a JSON object"):
        refresh.run()
    assert json.loads(refresh.status_path().read_text())["error"].endswith("isn't a JSON object")
    assert refresh.main() == 1
    assert "Couldn't refresh:" in capsys.readouterr().err


def test_the_refresh_command_reports_each_source(wq, capsys):
    every_tool(wq)
    wq.sources({"github": {}})
    assert refresh.main() == 0
    out = capsys.readouterr().out
    assert "GitHub: ok — read 1 item" in out and "Gmail: off — not linked" in out
    wq.sources({"outlook": {"address": ME}})
    assert refresh.main() == 1


@pytest.mark.real_mode
def test_default_mode_never_refreshes(wq, capsys):
    assert refresh.main() == 1
    assert "Pro mode is off" in capsys.readouterr().out
    assert not refresh.site_path().exists()


def test_page_pieces_render_from_rows():
    gone = page.ctx_html({"error": "x"})
    assert "can't show this message anymore" in gone
    msgs = [{"who": "Ana", "ts": str(NOW - n * 60), "text": f"reply {n}"} for n in range(4)]
    many = page.ctx_html({"root": None, "msg": {"who": "Bo", "ts": str(NOW), "text": ""}, "after": msgs, "after_n": 6})
    assert "2 more replies in between" in many and "<i>(no text)</i>" in many and "cx-toggle" in many
    one = page.ctx_html({"root": None, "msg": {"who": "Bo", "ts": str(NOW), "text": "x"}, "after": msgs[:1], "after_n": 2})
    assert "1 more reply in between" in one and "cx-toggle" not in one
    mail = page.mail_ctx({"from": "Ana", "day": "Nov 2", "to": [f"p{n}" for n in range(6)], "cc": ["c"], "body": "b" * 300})
    assert "to p0, p1, p2, p3 and 2 more; cc c" in mail and "cx-toggle" in mail
    assert "· to" not in page.mail_ctx({"from": "Ana", "day": "Nov 2", "to": [], "cc": [], "body": "short"})
    assert "No sources are linked yet." in page.render({"status": [], "built": "now"})[0]
    assert "Nothing yet" in page.placeholder("Nothing yet") and "<script>" in page.placeholder("x")
    assert config.read_json(config.data_dir() / "missing.json", {"a": 1}) == {"a": 1}


def test_a_bug_in_a_source_reader_isnt_reported_as_that_source_failing(wq):
    wq.sources({"gmail": {}})
    with pytest.raises(KeyError, match="accounts"):
        refresh.run(now=NOW)


def test_one_item_reads_as_one_item(wq):
    every_tool(wq)
    wq.sources({"github": {}})
    assert {s["name"]: s["detail"] for s in refresh.run(now=NOW)}["GitHub"] == "read 1 item"


def test_the_page_carries_the_refresh_time_for_the_viewers_clock(wq):
    every_tool(wq)
    wq.sources({"github": {}})
    refresh.run(now=NOW)
    stamp = re.search(r'<span id="built" data-iso="([^"]+)">([^<]+)</span>', refresh.site_path().read_text())
    assert datetime.fromisoformat(stamp.group(1)).tzinfo is not None and stamp.group(2)
    assert abs(datetime.now(timezone.utc) - datetime.fromisoformat(stamp.group(1))) < timedelta(minutes=5)


def unsub_mail(key: str, addr: str, group: str, url: str = "") -> dict:
    return {"key": key, "account": "me@example.com", "msg_ids": [key], "group": group, "title": key, "subject": key,
            "reason": "r", "addr": addr, "from": addr.split("@")[0], "unsub_url": url, "day": "Nov 2", "to": [], "cc": [],
            "body": "b", "unread": False, "link": f"https://mail.google.com/mail/#inbox/{key}"}


def test_unsubscribe_offers_only_senders_whose_mail_can_all_go(wq):
    rows = [unsub_mail("x-kept", "x@shop.example", "act"), unsub_mail("x-old", "x@shop.example", "news", "https://shop.example/u"),
            unsub_mail("y-1", "y@news.example", "news", "mailto:u@news.example"),
            unsub_mail("y-2", "y@news.example", "note", "https://news.example/u"),
            unsub_mail("me", "me@example.com", "self", "https://example.com/u"),
            unsub_mail("z", "z@list.example", "shop")]
    html, cands = page.render({"built": "now", "status": [], "gmail": {"me@example.com": rows}})
    unsub = [c for c in cands.values() if c["kind"] == "unsub"]
    assert unsub == [{"kind": "unsub", "url": "https://news.example/u", "link": "https://mail.google.com/mail/#inbox/y-1"}]
    section = html[html.index('id="unsubscribe"'):]
    assert "y@news.example" in section and "2 of their emails can go" in section
    assert "x@shop.example" not in section and "me@example.com · me@example.com" not in section
    assert "1 senders support one-click unsubscribe" in section
