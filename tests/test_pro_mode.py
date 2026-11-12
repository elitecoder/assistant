"""Default mode hides and stops the Pro features; Pro mode brings them back.

Every test here reads the real mode setting (the `real_mode` marker turns off
conftest's forced Pro mode).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
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
    assert mode.saved_mode() is None
    config(home).write_text("[]")
    with pytest.raises(ValueError):
        mode.save("pro")
    with pytest.raises(ValueError):
        mode.save("turbo")


def test_an_unreadable_config_means_default_mode_and_names_the_file(home):
    (home / ".claude").mkdir()
    (home / ".claude/assistant-goals.json").write_text('{"goals": []}')
    assert mode.current() == "pro" and mode.config_error() is None
    for broken in ("{torn", "[]"):
        config(home).write_text(broken)
        assert mode.current() == "default"
        assert str(config(home)) in mode.config_error()
    set_mode(home, "turbo")
    assert mode.current() == "default"


def test_switch_saves_nothing_when_it_cant_change_the_skills(home):
    set_mode(home, "default")
    (home / ".claude").mkdir()
    (home / ".claude/skills").write_text("not a folder")
    with pytest.raises(OSError):
        mode.switch("pro")
    assert mode.saved_mode() == "default"
    (home / ".claude/skills").unlink()
    config(home).write_text("{torn")
    with pytest.raises(ValueError):
        mode.switch("pro")
    assert not (home / ".claude/skills/goal").exists()
    set_mode(home, "pro")
    mode.switch("pro")
    with pytest.raises(ValueError):
        mode.switch("turbo")
    assert (home / ".claude/skills/goal").is_symlink()


def test_a_looping_skill_link_never_breaks_the_mode(home, monkeypatch):
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "goal").symlink_to(skills / "goal")
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if self == skills / "goal":
            raise RuntimeError("Symlink loop")
        return real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert mode.current() == "default"


def test_an_early_installers_real_skill_copy_counts_as_pro(home):
    lesson = home / ".claude/skills/lesson"
    lesson.mkdir(parents=True)
    (home / ".claude/skills/goal").mkdir()
    (home / ".claude/assistant-goals.json").write_text("")
    assert mode.current() == "default"
    (lesson / "SKILL.md").write_text("---\nname: lesson\ndescription: someone else's rule keeper\n---\n")
    assert mode.current() == "default"
    (lesson / "SKILL.md").write_text((REPO / "skills/lesson/SKILL.md").read_text())
    assert mode.current() == "pro"


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
    mode.switch("default")
    assert (skills / "goal").is_symlink()
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


def test_cli_commands(home, capsys):
    spec = importlib.util.spec_from_file_location("assistant_mode_cli", REPO / "bin/assistant-mode.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main([]) == 0 and capsys.readouterr().out == "default\n"
    assert cli.main(["--ensure"]) == 0 and mode.saved_mode() == "default"
    set_mode(home, "pro")
    assert cli.main(["--ensure"]) == 0 and capsys.readouterr().out.endswith("pro\n")
    assert cli.main(["default"]) == 0 and capsys.readouterr().out == "default\n"
    assert cli.main(["pro"]) == 0 and "linked goal" in capsys.readouterr().out
    assert cli.main(["turbo"]) == 2
    config(home).write_text("{torn")
    assert cli.main(["default"]) == 1 and "Couldn't change the mode" in capsys.readouterr().err


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


def test_default_mode_runs_the_session_check_only_when_opted_in(home, monkeypatch):
    pulse = load_pulse()
    monkeypatch.delenv("ASSISTANT_WATCHER", raising=False)
    assert not pulse.job_enabled("watcher")
    monkeypatch.setenv("ASSISTANT_WATCHER", "1")
    monkeypatch.setenv("ASSISTANT_LESSON_EXTRACTOR", "1")
    assert pulse.job_enabled("watcher")
    assert not pulse.job_enabled("lessons")
    assert not pulse.job_enabled("triage")


def test_default_mode_session_check_never_merges_or_sends(home, monkeypatch):
    pulse = load_pulse()
    calls = []
    monkeypatch.setattr(pulse, "cmux_send", lambda *a, **k: calls.append(a) or {"outcome": "sent"})
    monkeypatch.setattr(pulse, "run_merge_pr_dispatch",
                        lambda *a, **k: calls.append(a) or {"outcome": "submitted"})
    monkeypatch.setattr(pulse, "previous_send_ingested", lambda *a: True)
    ws = {"ref": "workspace:1", "title": "t", "cwd": "/", "pr_refs": ["7"]}
    merge = {"verdict": "ready_for_merge", "summary": "s", "next": "n"}
    nudge = {"verdict": "stranded", "nudge_text": "go", "summary": "s", "next": "n"}
    assert pulse.execute_verdict(ws, merge, [])["kind"] == "skipped"
    assert pulse.execute_verdict(ws, nudge, [])["kind"] == "skipped"
    assert calls == []
    set_mode(home, "pro")
    pulse.execute_verdict(ws, merge, [])
    assert calls == [("workspace:1", "7")]


def test_default_mode_skips_the_alert_backlog_and_expires_old_requests(home):
    pulse = load_pulse()
    from assistant import decisions, triage
    events = triage.events_path()
    events.write_text("".join(json.dumps({"id": f"e{i}"}) + "\n" for i in range(5)))
    with mock.patch.object(decisions, "expire_open", return_value=[]) as expire:
        pulse.run_triage_step(1)
    assert json.loads(triage.cursor_path().read_text())["offset"] == events.stat().st_size
    expire.assert_called_once()


def test_default_mode_triage_housekeeping_never_breaks_the_pulse(home):
    pulse = load_pulse()
    from assistant import decisions
    with mock.patch.object(decisions, "expire_open", side_effect=RuntimeError("locked")), \
            mock.patch.object(pulse.log, "warning") as warning:
        assert pulse.run_triage_step(1) == {"cards": []}
    assert "housekeeping failed" in warning.call_args[0][0]


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

def render_page(home: Path, state: dict | None = None, workspaces: list | None = None) -> str:
    (home / ".claude/cache").mkdir(parents=True, exist_ok=True)
    (home / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": workspaces or [], "todo": {}}))
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


def test_dashboard_shows_an_unreadable_settings_file_and_a_failed_restart(home):
    assert "can't be read" not in render_page(home)
    config(home).write_text("{torn")
    (home / ".assistant/service-restart-failed").write_text(
        "com.assistant.connector-gmail\ncom.assistant.assistant-todo-server\n")
    page = render_page(home)
    switch = page[page.index('<div class="mode-switch"'):]
    assert "Your settings file can't be read" in switch and str(config(home)) in switch
    assert ("couldn't restart com.assistant.connector-gmail, com.assistant.assistant-todo-server"
            in page)


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


def test_each_paused_workspace_card_shows_the_unpause_command(home):
    (home / ".assistant/observer-summaries").mkdir()
    (home / ".assistant/observer-summaries/workspace_1.json").write_text(json.dumps(
        {"ws_ref": "workspace:1", "title": "Other work", "verdict": "active", "ts": 1}))
    (home / ".assistant/back-off.json").write_text(json.dumps(
        {"workspaces": [{"ws_ref": "workspace:3", "reason": "too noisy"}]}))
    page = render_page(home, workspaces=[{"ws_ref": "workspace:1"}, {"ws_ref": "workspace:3"}])
    card = page[page.index('<div class="backoff-card">'):]
    assert "back-off.py remove workspace:3" in card[card.index('class="backoff-hint"'):]


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
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(server.SRC_DIR)])
    config(home).write_text("{torn")
    ok, message = server.set_mode("default")
    assert not ok and message.startswith("Couldn't change the mode") and str(server.SRC_DIR) in sys.path
    assert str(config(home)) in message


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
    heartbeat = json.loads(fake.heartbeat_path().read_text())
    assert heartbeat["paused"] is True and heartbeat["ok"] is True


def test_a_resumed_connector_skips_what_happened_while_paused(home):
    from assistant import connector
    now = time.time()

    class Fake(connector.Connector):
        polls = 0

        def poll_once(self, now=None):
            Fake.polls += 1
            for name, epoch in ((f"old-{Fake.polls}", time.time() - 3600),
                                (f"new-{Fake.polls}", time.time() + 1)):
                self.emit(connector.build_world_event(
                    source="fake", kind="note", external_id=name, ts_epoch=epoch))
            return {"status": "error"}

    set_mode(home, "default")
    delays = []
    fake = Fake("fake", "fake", config={"cadence_sec": 60})

    def sleep(seconds):
        delays.append(seconds)
        set_mode(home, "pro")

    fake.run_forever(max_iterations=3, sleep=sleep)
    drops = sorted(json.loads(p.read_text())["external_id"] for p in fake.inbox_dir().glob("evt-*.json"))
    assert drops == ["new-1", "new-2"]
    assert fake.skip_events_before >= now
    assert delays == [60, 60]


def test_a_connector_restarted_after_a_pause_skips_what_happened_while_paused(home):
    from assistant import connector

    class Fake(connector.Connector):
        def poll_once(self, now=None):
            for name, epoch in (("old", time.time() - 3600), ("new", time.time() + 1)):
                self.emit(connector.build_world_event(
                    source="fake", kind="note", external_id=name, ts_epoch=epoch))
            return {"status": "error"}

    set_mode(home, "default")
    Fake("fake", "fake", config={"cadence_sec": 60}).run_forever(max_iterations=1)
    set_mode(home, "pro")
    fake = Fake("fake", "fake", config={"cadence_sec": 60})
    fake.run_forever(max_iterations=1)
    drops = [json.loads(p.read_text())["external_id"] for p in fake.inbox_dir().glob("evt-*.json")]
    assert drops == ["new"]


def test_a_paused_connector_retries_at_the_normal_pace_after_it_resumes(home):
    from assistant import connector

    class Failing(connector.Connector):
        def poll_once(self, now=None):
            return {"status": "error"}

    delays = []
    modes = iter([True, True, False, True, True])
    fake = Failing("fake", "fake", config={"cadence_sec": 60})
    with mock.patch.object(mode, "is_pro", lambda: next(modes)):
        fake.run_forever(max_iterations=5, sleep=delays.append)
    assert delays == [60, 120, 60, 60]


def test_a_connector_keeps_the_last_mode_when_it_cant_read_it(home):
    from assistant import connector

    class Fake(connector.Connector):
        polls = 0

        def poll_once(self, now=None):
            Fake.polls += 1
            return {"status": "ok"}

    fake = Fake("fake", "fake", config={"cadence_sec": 1})
    reads = iter([True, RuntimeError("symlink loop"), KeyError("HOME")])

    def is_pro():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    with mock.patch.object(mode, "is_pro", is_pro):
        fake.run_forever(max_iterations=3, sleep=lambda s: None)
    assert Fake.polls == 3


# ─── the installer ───────────────────────────────────────────────────────────

def run_installer(env: dict, *flags: str, repo: Path = REPO) -> subprocess.CompletedProcess:
    proc = subprocess.run(["bash", str(repo / "install.sh"), "--apply", *flags],
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

    proc = run_installer(env, "--pro")
    assert "run ./install.sh --apply --pro (or --no-pro)" in proc.stdout
    assert json.loads(config(home).read_text())["mode"] == "pro"
    assert all((skills / n).is_symlink() for n in mode.PRO_SKILLS)

    run_installer(env, "--no-pro")
    assert json.loads(config(home).read_text())["mode"] == "default"
    assert not (skills / "goal").exists() and (skills / "todo").is_symlink()


def test_help_documents_pro_mode(tmp_path):
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--help"], capture_output=True, text=True,
                          env=installer_env(tmp_path / "home"), stdin=subprocess.DEVNULL, timeout=30)
    assert "  --pro             Turn on Pro mode" in proc.stdout
    assert "  --no-pro          Switch to default mode and unlink the Pro skills." in proc.stdout


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


def fake_launchctl(env: dict, running: set[str], failing: set[str] = frozenset(),
                   stopped: set[str] = frozenset()) -> Path:
    """Answer `print` for running and loaded-but-stopped labels, and fail
    `kickstart` for failing ones."""
    home = Path(env["HOME"])
    (home / "running").write_text("\n".join(running) + "\n")
    (home / "stopped").write_text("\n".join(stopped) + "\n")
    (home / "failing").write_text("\n".join(failing) + "\n")
    Path(env["ASSISTANT_TEST_LAUNCHCTL"]).write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
        'label="${2##*/}"\n'
        'case "$1" in\n'
        '  print) if grep -qx "$label" "$HOME/running"; then echo "	state = running";\n'
        '         elif grep -qx "$label" "$HOME/stopped"; then echo "	state = not running";\n'
        '         else exit 1; fi ;;\n'
        '  kickstart) label="${3##*/}"; ! grep -qx "$label" "$HOME/failing" ;;\n'
        '  bootout|bootstrap|load) exit 0 ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n')
    return home / "launchctl.log"


def test_installer_restarts_running_services_only_when_their_code_changes(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    env = {**installer_env(home), "ASSISTANT_SELF_UPDATE": "1"}
    server, gmail = "com.assistant.assistant-todo-server", "com.assistant.connector-gmail"
    log = fake_launchctl(env, running={server, gmail}, stopped={"com.assistant.connector-github"})
    run_installer(env)
    stamp = home / ".assistant/service-code.sha256"
    note = home / ".assistant/service-restart-failed"
    stamp.write_text("old\n")
    log.write_text("")
    run_installer(env)
    kicks = [line for line in log.read_text().splitlines() if line.startswith("kickstart")]
    assert sorted(kicks) == sorted(f"kickstart -k gui/{os.getuid()}/{label}" for label in (server, gmail))

    log.write_text("")
    run_installer(env)
    assert "kickstart" not in log.read_text()

    stamp.write_text("old\n")
    fake_launchctl(env, running={gmail}, failing={gmail})
    log.write_text("")
    proc = run_installer(env)
    assert f"couldn't restart {gmail}" in proc.stderr
    assert f"kickstart -k gui/{os.getuid()}/{server}" not in log.read_text()
    assert stamp.read_text() == "old\n"
    assert note.read_text() == f"{gmail}\n"

    fake_launchctl(env, running={gmail})
    run_installer(env)
    assert stamp.read_text() != "old\n"
    assert not note.exists()


def test_the_installer_restarts_a_service_when_any_of_its_code_changes(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(".git", ".venv", ".worktrees", "__pycache__"))
    env = {**installer_env(home), "ASSISTANT_SELF_UPDATE": "1"}
    server = "com.assistant.assistant-todo-server"
    log = fake_launchctl(env, running={server})
    run_installer(env, repo=repo)
    for path in ("src/assistant/mode.py", "bin/connectors/gmail.py", "bin/todo-server.py"):
        log.write_text("")
        with (repo / path).open("a") as f:
            f.write("\n# changed\n")
        run_installer(env, repo=repo)
        assert f"kickstart -k gui/{os.getuid()}/{server}" in log.read_text(), path


def test_the_hourly_update_never_starts_workspace_numbers_you_stopped(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    env = installer_env(home)
    label = "com.assistant.cmux-ws-numberer"
    plist = home / f"Library/LaunchAgents/{label}.plist"
    log = fake_launchctl(env, running=set())
    run_installer(env)
    assert any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())
    log.write_text("")
    run_installer(env)
    assert any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())

    plist.write_text("<plist>an older copy</plist>")
    log.write_text("")
    run_installer({**env, "ASSISTANT_SELF_UPDATE": "1"})
    assert not any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())
    assert "an older copy" not in plist.read_text()

    plist.write_text("<plist>an older copy</plist>")
    log.write_text("")
    run_installer({**env, "ASSISTANT_SELF_UPDATE": "1"}, "--with-ws-numberer")
    assert any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())

    plist.write_text("<plist>an older copy</plist>")
    fake_launchctl(env, running={label})
    log.write_text("")
    run_installer({**env, "ASSISTANT_SELF_UPDATE": "1"})
    assert any(line.startswith("bootstrap") and label in line for line in log.read_text().splitlines())
