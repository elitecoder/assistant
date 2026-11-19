"""bin/work-queue-setup.py links, checks, and unlinks your own sources, against fake tools."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("work_queue_setup", REPO / "bin/work-queue-setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

GOG_ACCOUNTS = {"accounts": [{"email": "a@example.com", "services": ["gmail", "calendar"]},
                             {"email": "b@example.com", "services": ["calendar"]}]}


def run(capsys, *argv) -> tuple[int, list[str]]:
    code = setup.main(list(argv))
    return code, capsys.readouterr().out.splitlines()


def test_status_says_what_each_source_needs_without_signing_in(wq, capsys):
    fake_tool(wq.bin, "gog", [])
    wq.sources({"gmail": {"accounts": ["a@example.com"]}, "github": {"host": "ghe.example.com"}})
    code, lines = run(capsys, "status")
    assert code == 0 and lines == [
        "Slack: not linked. Run bin/work-queue-setup.py link slack",
        "Gmail: linked (a@example.com)",
        "Calendar: not linked. Run bin/work-queue-setup.py link calendar",
        "Outlook: not linked. Run bin/work-queue-setup.py link outlook",
        "GitHub: tool missing. Install the GitHub CLI (https://cli.github.com/)"]
    assert calls(wq.bin, "gog") == []


def test_link_gmail_and_calendar_find_signed_in_google_accounts(wq, capsys):
    fake_tool(wq.bin, "gog", [("auth list", GOG_ACCOUNTS), ("gmail messages search", {"messages": []}),
                              ("calendar events", {"events": []})])
    assert run(capsys, "link", "gmail") == (0, ["Gmail: linked (a@example.com)"])
    assert run(capsys, "link", "calendar") == (0, ["Calendar: linked (a@example.com, b@example.com)"])
    assert wq.saved_sources() == {"gmail": {"accounts": ["a@example.com"]},
                                  "calendar": {"accounts": ["a@example.com", "b@example.com"]}}
    assert "gmail messages search in:inbox --max 1 -a a@example.com -j" in calls(wq.bin, "gog")
    assert run(capsys, "link", "gmail", "--account", "c@example.com", "--account", "d@example.com")[0] == 0
    assert wq.saved_sources()["gmail"] == {"accounts": ["c@example.com", "d@example.com"]}


def test_a_failed_link_prints_the_fix_and_saves_nothing(wq, capsys):
    code, lines = run(capsys, "link", "gmail")
    assert code == 1 and lines == ["Gmail: tool missing. Install the gog CLI, then run: gog auth add you@example.com"]
    fake_tool(wq.bin, "gog", [("auth list", {"accounts": [{"email": "b@example.com", "services": ["calendar"]}]})])
    code, lines = run(capsys, "link", "gmail")
    assert code == 1 and lines == ["Gmail: needs sign-in (No Google account in gog has gmail access). "
                                   "Run gog auth add you@example.com, then bin/work-queue-setup.py link gmail"]
    fake_tool(wq.bin, "gh", [("api user", {"exit": 1, "stderr": "not logged in"})])
    code, lines = run(capsys, "link", "github")
    assert code == 1 and lines[0].startswith("GitHub: needs sign-in (gh api user failed: not logged in). Run gh auth login")
    assert not (wq.home / ".assistant/work-queue/sources.json").exists()


def test_link_github_and_outlook_probe_read_only(wq, capsys):
    fake_tool(wq.bin, "gh", [("api user", "octo\n")])
    fake_tool(wq.bin, "npx", [("mail,userPrincipalName", {"mail": None, "userPrincipalName": "me@corp.example"}),
                              ("$select=mail", {"mail": "me@corp.example"})])
    assert run(capsys, "link", "github", "--host", "ghe.example.com") == (0, ["GitHub: linked (ghe.example.com)"])
    assert (wq.bin / "gh.env").read_text() == "ghe.example.com\n"
    assert run(capsys, "link", "github") == (0, ["GitHub: linked (github.com)"])
    assert run(capsys, "link", "outlook") == (0, ["Outlook: linked (me@corp.example)"])
    assert wq.saved_sources() == {"github": {"skip_authors": []}, "outlook": {"address": "me@corp.example"}}
    assert calls(wq.bin, "npx")[0] == "-y @microsoft/workiq fetch -u /me?$select=mail,userPrincipalName"


def test_link_slack_reads_the_workspace_from_your_token_and_signs_in_the_browser(wq, capsys, monkeypatch):
    code, lines = run(capsys, "link", "slack")
    assert code == 1 and lines[0].startswith("Slack: needs sign-in (Slack didn't accept your user token "
                                             "(SLACK_USER_TOKEN isn't set)). Set SLACK_USER_TOKEN in ~/.zprofile")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-secret-value")
    answers = iter([{"ok": False, "error": "invalid_auth"},
                    {"ok": True, "url": "https://acme.slack.com/", "team_id": "T1", "enterprise_id": "E9"},
                    {"ok": True, "url": "https://acme.slack.com/", "team_id": "T1"}])
    monkeypatch.setattr(setup, "TRANSPORT", lambda method, params, token: next(answers))
    assert run(capsys, "link", "slack")[1][0].startswith("Slack: needs sign-in (Slack didn't accept your user token (invalid_auth))")
    fake_tool(wq.bin, "uv", [("signin", {"seq": [{"ok": False, "error": "Slack sign-in needed"}, {"ok": True, "items": []}]})])
    code, lines = run(capsys, "link", "slack")
    assert code == 1 and "no Slack session in the Work queue's browser window" in lines[0]
    assert run(capsys, "link", "slack") == (0, ["Slack: linked (https://acme.slack.com)"])
    assert wq.saved_sources()["slack"] == {"team_id": "T1", "workspace_url": "https://acme.slack.com", "close_after_days": 14}
    signin = calls(wq.bin, "uv")[-1]
    assert "https://app.slack.com/client/T1/later" in signin and "--wait 300" in signin and "--headless" not in signin
    assert "xoxp-secret-value" not in json.dumps(wq.saved_sources()) + capsys.readouterr().out + signin


def test_check_probes_each_linked_source_and_unlink_removes_it(wq, capsys):
    fake_tool(wq.bin, "gh", [("api user", "octo\n")])
    fake_tool(wq.bin, "gog", [("gmail messages", {"messages": []}), ("calendar", {"exit": 1, "stderr": "token expired"})])
    fake_tool(wq.bin, "npx", [("", {"mail": "me@corp.example"})])
    fake_tool(wq.bin, "uv", [("list", {"ok": True, "items": []})])
    wq.sources({"slack": {"team_id": "T1", "workspace_url": "https://acme.slack.com"}, "gmail": {"accounts": ["a@x.com"]},
                "calendar": {"accounts": ["a@x.com"]}, "outlook": {"address": "me@corp.example"}, "github": {"host": "h"}})
    code, lines = run(capsys, "check")
    assert code == 1 and lines[0] == "Slack: linked (https://acme.slack.com)" and lines[1] == "Gmail: linked (a@x.com)"
    assert lines[2].startswith("Calendar: needs sign-in (gog calendar events failed: token expired)")
    assert lines[3:] == ["Outlook: linked (me@corp.example)", "GitHub: linked (h)"]
    assert "--headless" in calls(wq.bin, "uv")[0]
    assert run(capsys, "unlink", "calendar") == (0, ["Calendar: unlinked"])
    assert run(capsys, "unlink", "calendar") == (0, ["Calendar: not linked"])
    assert run(capsys, "unlinked") == (0, ["calendar"])
    assert run(capsys, "check")[0] == 0
    assert "Calendar: not linked. Run bin/work-queue-setup.py link calendar" in run(capsys, "check")[1]


def test_bad_input_and_an_unreadable_settings_file(wq, capsys):
    with pytest.raises(SystemExit) as ctx:
        setup.main(["link"])
    assert ctx.value.code == 2 and "link needs a source" in capsys.readouterr().err
    wq.sources({})
    (wq.home / ".assistant/work-queue/sources.json").write_text("{torn")
    assert setup.main(["status"]) == 2
    assert "Can't read the Work queue settings" in capsys.readouterr().err


def test_the_refresh_command_and_the_script_entry_point(wq, capsys):
    fake_tool(wq.bin, "gh", [("search prs", [])])
    wq.sources({"github": {}})
    assert setup.main(["refresh"]) == 0
    assert "GitHub: ok — read 0 items" in capsys.readouterr().out
    proc = subprocess.run([sys.executable, str(REPO / "bin/work-queue-setup.py"), "unlinked"], capture_output=True,
                          text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert proc.returncode == 0 and proc.stdout.split() == ["slack", "gmail", "calendar", "outlook"]
