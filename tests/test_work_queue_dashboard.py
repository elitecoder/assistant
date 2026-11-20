"""The dashboard's Work queue tab appears only in Pro mode."""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

from assistant import mode
from test_pro_mode import home, render_page, set_mode  # noqa: F401
from work_queue_fakes import old_python

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.real_mode
TAB = '<button class="tab" data-tab="work-queue"'


def test_default_mode_hides_the_work_queue_tab(home, monkeypatch):
    monkeypatch.delenv("WQ_DATA", raising=False)
    page = render_page(home)
    assert TAB not in page and 'data-panel="work-queue"' not in page and "127.0.0.1:8851" not in page


def test_pro_mode_links_the_work_queue_and_says_when_it_last_refreshed(home, monkeypatch):
    monkeypatch.delenv("WQ_DATA", raising=False)
    set_mode(home, "pro")
    page = render_page(home)
    panel = page[page.index('data-panel="work-queue"'):]
    assert TAB in page and 'href="http://127.0.0.1:8851/"' in panel and "Not refreshed yet." in panel
    (home / ".assistant/work-queue").mkdir(parents=True)
    (home / ".assistant/work-queue/sources.json").write_text(json.dumps({"port": 9123}))
    (home / ".claude/work-queue").mkdir(parents=True)
    (home / ".claude/work-queue/refresh-status.json").write_text(json.dumps(
        {"finished": "2026-11-18T09:00:00+00:00", "error": "Outlook: <b>expired</b>"}))
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert 'href="http://127.0.0.1:9123/"' in panel
    assert "Last refreshed 2026-11-18T09:00:00+00:00. Last refresh problems: Outlook: &lt;b&gt;expired&lt;/b&gt;" in panel
    (home / ".assistant/work-queue/sources.json").write_text("{torn")
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert "The Work queue can't read its settings:" in panel and "isn&#x27;t valid JSON" in panel


@pytest.mark.parametrize("port", ['"8851"', "[8851]", "0", "70000", "true", "null"])
def test_a_bad_port_in_the_settings_never_breaks_the_dashboard(home, monkeypatch, port):
    monkeypatch.delenv("WQ_DATA", raising=False)
    set_mode(home, "pro")
    (home / ".assistant/work-queue").mkdir(parents=True)
    (home / ".assistant/work-queue/sources.json").write_text(f'{{"port": {port}}}')
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert "The Work queue can't read its settings:" in panel and "must be a whole number from 1 to 65535" in panel


def test_an_unreadable_settings_file_never_breaks_the_dashboard(home, monkeypatch):
    monkeypatch.delenv("WQ_DATA", raising=False)
    set_mode(home, "pro")
    (home / ".assistant/work-queue/sources.json").mkdir(parents=True)
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert "The Work queue can't read its settings:" in panel and "Is a directory" in panel


def work_queue_plist(home: Path, python: str = sys.executable) -> Path:
    plist = home / "Library/LaunchAgents/com.assistant.work-queue.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": "com.assistant.work-queue",
                                      "ProgramArguments": [python, "-m", "work_queue.server"]}))
    return plist


def load_todo_server(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin/todo-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_launchctl(home: Path, monkeypatch, loaded: bool) -> Path:
    stub = home / "stubbin"
    stub.mkdir(exist_ok=True)
    (stub / "launchctl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
                                    f'[ "$1" = print ] && exit {0 if loaded else 113}\nexit 0\n')
    (stub / "launchctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return home / "launchctl.log"


@pytest.mark.parametrize("loaded", [False, True])
def test_the_dashboard_switch_loads_and_unloads_the_work_queue(home, monkeypatch, loaded):
    server = load_todo_server(f"todo_server_wq_{loaded}")
    monkeypatch.setattr(server, "rerender_dashboard", lambda: True)
    log = fake_launchctl(home, monkeypatch, loaded)
    plist = work_queue_plist(home)
    domain = f"gui/{os.getuid()}"
    assert server.set_mode("pro") == (True, "pro") and mode.is_pro()
    start = f"kickstart {domain}/com.assistant.work-queue" if loaded else f"bootstrap {domain} {plist}"
    assert log.read_text().splitlines() == [f"print {domain}/com.assistant.work-queue", start]
    log.write_text("")
    assert server.set_mode("default") == (True, "default") and not mode.is_pro()
    assert log.read_text().splitlines() == [f"bootout {domain}/com.assistant.work-queue"]


def test_the_dashboard_switch_leaves_launchd_alone_without_an_installed_work_queue(home, monkeypatch):
    server = load_todo_server("todo_server_wq_none")
    monkeypatch.setattr(server, "rerender_dashboard", lambda: True)
    log = fake_launchctl(home, monkeypatch, True)
    assert server.set_mode("pro") == (True, "pro")
    assert server.set_mode("default") == (True, "default")
    assert not log.exists()


def test_a_missing_launchctl_still_switches_the_mode(home, monkeypatch):
    server = load_todo_server("todo_server_wq_nolaunchctl")
    monkeypatch.setattr(server, "rerender_dashboard", lambda: True)
    (home / "empty").mkdir()
    monkeypatch.setenv("PATH", str(home / "empty"))
    work_queue_plist(home)
    assert server.set_mode("pro") == (True, "pro") and mode.is_pro()
    assert server.set_mode("default") == (True, "default") and not mode.is_pro()


@pytest.mark.parametrize("python", ["old", "unreadable", "missing"])
def test_the_dashboard_switch_never_starts_the_work_queue_on_python_3_10(home, monkeypatch, python):
    server = load_todo_server(f"todo_server_wq_old_{python}")
    monkeypatch.setattr(server, "rerender_dashboard", lambda: True)
    log = fake_launchctl(home, monkeypatch, False)
    if python == "old":
        old = old_python(home / "py")
        work_queue_plist(home, str(old))
        why = f"{old} is older"
    elif python == "missing":
        work_queue_plist(home, str(home / "no-such-python3"))
        why = f"{home / 'no-such-python3'} doesn't run"
    else:
        work_queue_plist(home).write_text("<plist/>")
        why = f"the python3 in {home / 'Library/LaunchAgents/com.assistant.work-queue.plist'} can't be read"
    assert server.set_mode("pro") == (True, f"Switched to pro. The Work queue needs Python 3.11 or newer, and {why}, "
                                            "so it wasn't started. Install Python 3.11+, then run "
                                            "./install.sh --apply --pro.")
    assert mode.is_pro() and not log.exists()
    assert server.set_mode("default") == (True, "default")
    assert log.read_text().splitlines() == [f"bootout gui/{os.getuid()}/com.assistant.work-queue"]


def test_the_dashboard_switch_says_why_even_when_the_page_cant_rebuild(home, monkeypatch):
    server = load_todo_server("todo_server_wq_old_norender")
    monkeypatch.setattr(server, "rerender_dashboard", lambda: False)
    fake_launchctl(home, monkeypatch, False)
    work_queue_plist(home, str(old_python(home / "py")))
    ok, message = server.set_mode("pro")
    assert ok and message.startswith("Switched to pro. The page updates within 5 minutes. The Work queue needs Python 3.11")
