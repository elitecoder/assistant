"""Default mode hides and stops the Pro features; Pro mode brings them back.

Every test here reads the real mode setting (the `real_mode` marker turns off
conftest's forced Pro mode).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from installer_sandbox import installer_env
from test_pulse import fixture_home, load_pulse as load_pulse_at

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import mode  # noqa: E402

pytestmark = pytest.mark.real_mode

PRO_TABS = tuple(f'<button class="tab" data-tab="{n}"' for n in ("brief", "decisions", "fleet", "connections"))
DEFAULT_TABS = tuple(f'<button class="tab" data-tab="{n}"' for n in ("overview", "workspaces", "slack", "todos"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    return tmp_path


def config(home: Path) -> Path:
    return home / ".assistant/config.json"


def set_mode(home: Path, value) -> None:
    config(home).write_text(json.dumps({"slack": {"target": "C1"}, "mode": value}))


# ─── the setting ─────────────────────────────────────────────────────────────

def test_fresh_install_is_default_and_existing_pro_data_counts_as_pro(home):
    assert mode.current() == "default"
    (home / ".claude").mkdir()
    (home / ".claude/assistant-goals.json").write_text('{"goals": []}')
    assert mode.current() == "pro"


@pytest.mark.parametrize("make", [
    lambda h: (h / ".assistant/decisions").mkdir() or (h / ".assistant/decisions/decisions.jsonl").write_text("{}\n"),
    lambda h: (h / ".assistant/brief").mkdir() or (h / ".assistant/brief/brief-2026-07-02.json").write_text("{}"),
    lambda h: (h / ".claude/skills").mkdir(parents=True) or (h / ".claude/skills/lesson").symlink_to(REPO / "skills/lesson"),
])
def test_each_kind_of_pro_data_keeps_an_install_pro(home, make):
    make(home)
    assert mode.current() == "pro"


def test_a_saved_mode_beats_inference(home):
    (home / ".claude").mkdir()
    (home / ".claude/assistant-goals.json").write_text('{"goals": []}')
    set_mode(home, "default")
    assert not mode.is_pro()


def test_save_keeps_other_settings_and_refuses_an_unreadable_file(home):
    config(home).write_text(json.dumps({"slack": {"target": "C1"}}))
    mode.save("pro")
    assert json.loads(config(home).read_text()) == {"slack": {"target": "C1"}, "mode": "pro"}
    broken = '{"slack": {"target": "C1",},}'
    config(home).write_text(broken)
    with pytest.raises(ValueError):
        mode.save("default")
    assert config(home).read_text() == broken


def test_switch_matches_links_through_a_symlinked_repo_path(home, tmp_path):
    real = tmp_path / "real-repo"
    for name in mode.PRO_SKILLS:
        (real / "skills" / name).mkdir(parents=True)
    alias = tmp_path / "alias-repo"
    alias.symlink_to(real)
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    for name in mode.PRO_SKILLS:
        (skills / name).symlink_to(alias / "skills" / name)
    mode.switch("default", repo=real)
    assert not any((skills / n).is_symlink() for n in mode.PRO_SKILLS)


def test_stale_links_from_a_moved_checkout_are_replaced_and_foreign_links_ignored(home, tmp_path):
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "goal").symlink_to(tmp_path / "old-checkout/skills/goal")
    (skills / "lesson").symlink_to(tmp_path)
    assert mode.current() == "default"
    mode.switch("pro")
    assert (skills / "goal").resolve() == REPO / "skills/goal"
    assert (skills / "lesson").resolve() == tmp_path
    mode.switch("default")
    assert not (skills / "goal").is_symlink() and (skills / "lesson").is_symlink()


def test_switch_links_and_unlinks_only_our_pro_skills(home):
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "attend").mkdir()
    mode.switch("pro")
    assert (skills / "goal").resolve() == REPO / "skills/goal"
    assert (skills / "attend").is_dir() and not (skills / "attend").is_symlink()
    mode.switch("default")
    assert not (skills / "goal").exists() and (skills / "attend").is_dir()
    assert json.loads(config(home).read_text())["mode"] == "default"


def run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "bin/assistant-mode.py"), *args],
                          capture_output=True, text=True, env={**os.environ, "HOME": str(home)})


def test_cli_ensure_saves_the_inferred_mode_and_keeps_a_broken_file(home):
    (home / ".claude").mkdir()
    (home / ".claude/assistant-goals.json").write_text('{"goals": []}')
    assert run_cli(home, "--ensure").stdout.strip() == "pro"
    assert json.loads(config(home).read_text())["mode"] == "pro"
    config(home).write_text("{torn")
    proc = run_cli(home, "default")
    assert proc.returncode == 1 and config(home).read_text() == "{torn"


# ─── the pulse ───────────────────────────────────────────────────────────────

def load_pulse():
    spec = importlib.util.spec_from_file_location("pulse_pro_mode", REPO / "bin/pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_mode_skips_triage_brief_and_planner(home, monkeypatch):
    pulse = load_pulse()
    from assistant import brief, goals, triage
    monkeypatch.setenv("ASSISTANT_STRATEGIST", "1")
    with mock.patch.object(triage, "triage_new_events", return_value={"cards": ["x"]}) as tri, \
            mock.patch.object(brief, "pulse_step", return_value={}) as built, \
            mock.patch.object(goals, "pulse_step", return_value={}) as planned:
        assert pulse.run_triage_step(1) == {"cards": []}
        pulse.run_brief_step(1)
        pulse.run_planner_step(1)
        assert not (tri.called or built.called or planned.called)
        assert not pulse.job_enabled("strategist")
        set_mode(home, "pro")
        assert pulse.run_triage_step(1) == {"cards": ["x"]}
        pulse.run_brief_step(1)
        pulse.run_planner_step(1)
        assert tri.called and built.called and planned.called
        assert pulse.job_enabled("strategist")


def test_default_mode_skips_the_alert_backlog_and_expires_old_requests(home):
    pulse = load_pulse()
    from assistant import decisions, triage
    events = triage.events_path()
    events.write_text("".join(json.dumps({"id": f"e{i}"}) + "\n" for i in range(5)))
    with mock.patch.object(decisions, "expire_open", return_value=[]) as expire:
        pulse.run_triage_step(1)
    assert json.loads(triage.cursor_path().read_text())["offset"] == events.stat().st_size
    expire.assert_called_once()


def test_the_pulse_reads_the_mode_once(home):
    pulse = load_pulse()
    set_mode(home, "pro")
    pulse.PULSE_PRO = False
    assert not pulse.job_enabled("triage")
    pulse.PULSE_PRO = None
    assert pulse.job_enabled("triage")


def test_default_mode_still_auto_starts_tasks(tmp_path):
    pulse = load_pulse_at(fixture_home(tmp_path))
    todo = {"id": "td-1", "priority": "P2"}
    with mock.patch.object(pulse, "pick_ws_batch", return_value={
            "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0}), \
            mock.patch.object(pulse, "purge_stale_awaiting"), \
            mock.patch.object(pulse, "call_observer_batch", return_value=({}, {})), \
            mock.patch.object(pulse, "pick_open_todos", return_value={"bucket_b": [todo]}), \
            mock.patch.object(pulse, "dispatch_todo", return_value=True) as dispatch, \
            mock.patch.object(pulse, "run", return_value=(0, "", "")), \
            mock.patch.object(sys, "argv", ["pulse.py"]):
        assert pulse.main() == 0
    assert pulse.PULSE_PRO is False
    dispatch.assert_called_once_with("td-1")


# ─── the dashboard ───────────────────────────────────────────────────────────

def render_page(home: Path, state: dict | None = None) -> str:
    (home / ".claude/cache").mkdir(parents=True, exist_ok=True)
    (home / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    if state is not None:
        (home / ".claude/cache/assistant-state.json").write_text(json.dumps(state))
    spec = importlib.util.spec_from_file_location("render_pro_mode", REPO / "bin/render-assistant-page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.render()
    return (home / ".claude/assistant-dashboard.html").read_text()


def test_default_dashboard_shows_only_default_tabs_and_the_switch(home):
    page = render_page(home)
    assert all(tab in page for tab in DEFAULT_TABS)
    assert not any(tab in page for tab in PRO_TABS)
    assert 'data-panel="brief"' not in page
    assert "Pro mode is off." in page and "setMode('pro')" in page


def test_pro_dashboard_shows_every_tab_and_the_switch(home):
    set_mode(home, "pro")
    page = render_page(home)
    assert all(tab in page for tab in DEFAULT_TABS + PRO_TABS)
    order = [page.index(tab) for tab in (DEFAULT_TABS[0], PRO_TABS[0], DEFAULT_TABS[1], DEFAULT_TABS[3])]
    assert order == sorted(order)
    assert "Pro mode is on." in page and "setMode('default')" in page


def test_tasks_tab_shows_the_held_back_warning(home):
    page = render_page(home, {"awaiting_input": [{
        "key": "dispatch-cap-hit", "title": "Dispatch capped — 2 TODO(s) waiting",
        "detail": "active=4/4, total=9/12. Holding: td-1, td-2."}]})
    todos = page[page.index('data-panel="todos"'):]
    assert "Dispatch capped — 2 TODO(s) waiting" in todos and "Holding: td-1, td-2." in todos


def test_unpause_hint_points_to_a_command_that_exists_in_each_mode(home):
    spec = importlib.util.spec_from_file_location("render_hint", REPO / "bin/render-assistant-page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "back-off.py remove workspace:3" in module._unpause_hint("workspace:3")
    set_mode(home, "pro")
    assert "/attend" in module._unpause_hint("workspace:3")


# ─── the server switch ───────────────────────────────────────────────────────

def test_server_switch_changes_mode_and_skills(home, monkeypatch):
    spec = importlib.util.spec_from_file_location("todo_server_mode", REPO / "bin/todo-server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    renders = []
    monkeypatch.setattr(server, "rerender_dashboard", lambda: renders.append(1) or True)
    assert server.set_mode("pro") == (True, "pro")
    assert (home / ".claude/skills/goal").is_symlink() and mode.is_pro()
    assert server.set_mode("default") == (True, "default")
    assert not (home / ".claude/skills/goal").exists() and not mode.is_pro()
    assert server.set_mode("turbo")[0] is False
    assert renders == [1, 1]
    monkeypatch.setattr(server, "rerender_dashboard", lambda: False)
    assert server.set_mode("pro") == (True, "Switched to pro. The page updates within 5 minutes.")


# ─── connectors ──────────────────────────────────────────────────────────────

def test_connectors_pause_while_pro_mode_is_off(home):
    from assistant import connector

    class Fake(connector.Connector):
        polls = 0

        def poll_once(self, now=None):
            Fake.polls += 1
            set_mode(home, "default")
            return {"status": "ok"}

    fake = Fake("fake", "fake", config={"cadence_sec": 1})
    fake.run_forever(max_iterations=2, sleep=lambda s: None)
    assert Fake.polls == 0
    set_mode(home, "pro")
    fake.run_forever(max_iterations=3, sleep=lambda s: None)
    assert Fake.polls == 1


# ─── the installer ───────────────────────────────────────────────────────────

def run_installer(env: dict, *flags: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--apply", *flags],
                          capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
    return proc


def test_installer_modes_and_skills(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    skills = home / ".claude/skills"
    env = installer_env(home)
    run_installer(env)
    assert json.loads(config(home).read_text())["mode"] == "default"
    assert (skills / "todo").is_symlink() and (skills / "cleanup").is_symlink()
    assert not any((skills / n).exists() for n in mode.PRO_SKILLS)

    run_installer(env, "--pro")
    assert json.loads(config(home).read_text())["mode"] == "pro"
    assert all((skills / n).is_symlink() for n in mode.PRO_SKILLS)

    run_installer(env, "--no-pro")
    assert json.loads(config(home).read_text())["mode"] == "default"
    assert not (skills / "goal").exists() and (skills / "todo").is_symlink()


def test_self_update_keeps_an_existing_pro_install_and_its_skills(tmp_path):
    home = tmp_path / "home"
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    (home / ".assistant").mkdir()
    config(home).write_text(json.dumps({"slack": {"target": "C1"}}))
    for name in mode.PRO_SKILLS:
        (skills / name).symlink_to(REPO / "skills" / name)
    env = {**installer_env(home), "ASSISTANT_SELF_UPDATE": "1"}
    run_installer(env)
    assert json.loads(config(home).read_text()) == {"slack": {"target": "C1"}, "mode": "pro"}
    assert all((skills / n).is_symlink() for n in mode.PRO_SKILLS)


def test_installer_leaves_an_unreadable_config_alone(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    broken = '{"slack": {"target": "C1",},}'
    config(home).write_text(broken)
    proc = run_installer(installer_env(home), "--pro")
    assert config(home).read_text() == broken
    assert "mode not changed" in proc.stderr


def test_installer_restarts_the_dashboard_server_only_when_its_code_changes(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    env = installer_env(home)
    label = "com.assistant.assistant-todo-server"
    log = home / "launchctl.log"
    run_installer(env)
    log.write_text("")
    first = run_installer({**env, "ASSISTANT_SELF_UPDATE": "1"})
    assert f"will restart {label}" not in first.stdout
    (home / ".assistant/todo-server-code.sha256").write_text("old\n")
    log.write_text("")
    second = run_installer({**env, "ASSISTANT_SELF_UPDATE": "1"})
    assert f"will restart {label}" in second.stdout
    assert any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())
