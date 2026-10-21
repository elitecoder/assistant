"""Tests for the dashboard's Slack tab (render_slack_focus_tab in
bin/render-assistant-page.py). HOME is a tmp dir set before the renderer
loads, so the real ~/.assistant is never read."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from assistant.slack_focus import store
from slack_focus_fixtures import CONFIG, write_config

SCRIPT = Path(__file__).resolve().parent.parent / "bin/render-assistant-page.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mod(home):
    spec = importlib.util.spec_from_file_location("renderer_slack_tab", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def item(text, lane="Do first", link="https://acme.slack.com/archives/C1/p1", thread_id=1, **extra):
    return {"lane": lane, "text": text, "link": link, "thread_id": thread_id, "channel": "#team",
            "who": "Tim Beauchamp", "last": "2026-10-18T09:30", **extra}


def seed(home, focus=None, threads=None, state=None, done=None):
    root = store.base_dir(home)
    if focus is not None:
        store.write_json(root / "focus.json", focus)
    store.write_json(root / "threads.json", {"threads": threads or [
        {"id": 1, "first": {"text": "a", "at": "1"}, "latest": []},
        {"id": 2, "first": {"text": "b", "at": "1"}, "latest": [{"text": "c", "at": "2"}]}]})
    if state is not None:
        store.write_json(root / "state.json", state)
    if done is not None:
        store.write_json(root / "done.json", done)


def board_focus(**extra):
    return {
        "generated": "2026-10-20T14:03:00+00:00", "window_days": 7, "judge": {"ran": True},
        "active": [item("Answer <Tim>", mention=True, why="he asked", effort="Quick", sig="a|1"),
                   item("Ship the plan", lane="Promise", thread_id=2, effort="Medium"),
                   item("Odd effort", lane="This week", effort="Huge", link="javascript:alert(1)")],
        "read": [item("FYI build green", lane="Read")],
        "judge_dropped": [{"text": "thanks", "channel": "#team", "who": "Aalia", "link": "https://x/2",
                           "why": "not something you need to act on"}],
        "handled": [{"text": "PR 7 merged", "channel": "#team", "link": "https://x/3",
                     "why": "linked PR merged or closed"}],
        **extra,
    }


def test_setup_steps_before_configuration(mod):
    html, count = mod.render_slack_focus_tab()
    assert count is None
    assert "Set up the Slack list" in html and "slack_focus" in html and "SLACK_USER_TOKEN" in html
    assert 'id="sf-refresh"' in html and 'data-running="false"' in html


def test_not_checked_yet_once_configured(mod, home):
    write_config(home, CONFIG | {"title": "Mukul's Slack"})
    html, count = mod.render_slack_focus_tab()
    assert count is None and "Mukul&#x27;s Slack" in html
    assert "Not checked yet" in html and "Nothing pulled from Slack yet" in html


def test_board_renders_lanes_escaped_and_safe(mod, home):
    write_config(home, CONFIG)
    seed(home, board_focus())
    html, count = mod.render_slack_focus_tab()
    assert count == 1
    assert "1 to do first" in html
    assert "Answer &lt;Tim&gt;" in html and "<Tim>" not in html
    assert '<span class="sf-tag">@you</span>' in html
    assert "he asked · #team · Tim Beauchamp · 10-18 09:30" in html
    assert 'class="sf-effort sf-quick">Quick' in html and 'class="sf-effort sf-medium">Medium' in html
    assert "Huge" not in html
    assert 'href="#"' in html and "javascript:" not in html
    assert "Last pulled Oct 20, 14:03 UTC from the last 7 days. A model wrote the titles" in html
    for label in ("Do first", "Promise", "This week", "Optional", "Done by you", "Just read", "Set aside",
                  "Auto-resolved"):
        assert label in html, label
    assert html.count('onclick="slackFocusMark(this)"') == 3
    assert html.count("Nothing here.") == 2  # Optional and Done by you


def test_checked_off_rows_move_to_done_by_you(mod, home):
    write_config(home, CONFIG)
    focus = board_focus()
    fp = store.fingerprint(focus["active"][0])
    seed(home, focus, done={fp: "2026-10-20T15:00:00+00:00"})
    html, count = mod.render_slack_focus_tab()
    assert count == 0
    done = html[html.index('data-context-key="sf-done"'):]
    assert "Answer &lt;Tim&gt;" in done[:done.index("</details>")]
    assert "checked off 10-20 15:00" in html and 'data-done="false"' in html and ">Undo<" in html


def test_running_and_failed_states(mod, home):
    write_config(home, CONFIG)
    started = store.now_iso()
    seed(home, board_focus(judge={"ran": False}),
         state={"running": True, "started": started, "phase": "Pulling Slack (11 of 40 threads)"})
    html, _ = mod.render_slack_focus_tab()
    assert 'data-running="true"' in html and " disabled>Refreshing…" in html
    assert "Refreshing: Pulling Slack (11 of 40 threads)." in html
    seed(home, board_focus(judge={"ran": False}), state={"running": False, "ok": False, "error": "invalid <auth>"})
    html, _ = mod.render_slack_focus_tab()
    assert "The last refresh didn't finish: invalid &lt;auth&gt;" in html
    assert "These are the raw rows" in html
    seed(home, board_focus(generated="garbage"), state={"running": False, "ok": False})
    html, _ = mod.render_slack_focus_tab()
    assert "at an unknown time" in html and "unknown error" in html
    seed(home, board_focus(), state={"running": True, "started": started})
    assert "Refreshing: Starting." in mod.render_slack_focus_tab()[0]


def test_tab_failure_degrades_to_one_message(mod, monkeypatch):
    def broken(home):
        raise ValueError("bad <data>")

    from assistant.slack_focus import board

    monkeypatch.setattr(board, "load", broken)
    html, count = mod.render_slack_focus_tab()
    assert count is None and "The Slack list is unavailable: bad &lt;data&gt;." in html


def test_slack_href_only_allows_plain_https(mod):
    assert mod._slack_href("https://acme.slack.com/a?b=1&c=2") == "https://acme.slack.com/a?b=1&amp;c=2"
    for bad in ("http://acme.slack.com", "javascript:x", "https://", "https://a b", None, "https://[::1"):
        assert mod._slack_href(bad) == "#", bad


def test_full_render_adds_the_slack_tab(mod, home):
    (home / ".claude/cache").mkdir(parents=True)
    (home / ".claude/cache/world.json").write_text(json.dumps({"_meta": {}, "counts": {}}))
    write_config(home, CONFIG)
    seed(home, board_focus())
    mod.render()
    html = (home / ".claude/assistant-dashboard.html").read_text()
    assert 'data-tab="slack"' in html and 'data-panel="slack"' in html
    assert 'Slack <span class="tab-count">1</span>' in html
    assert "'slack', 'todos'" in html and "function slackFocusRefresh()" in html
