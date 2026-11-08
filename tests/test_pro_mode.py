"""Default mode hides and stops the Pro features; Pro mode brings them back."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from installer_sandbox import installer_env

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import mode  # noqa: E402

PRO_TABS = tuple(f'<button class="tab" data-tab="{n}"' for n in ("brief", "decisions", "fleet", "connections"))
DEFAULT_TABS = tuple(f'<button class="tab" data-tab="{n}"' for n in ("overview", "workspaces", "slack", "todos"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASSISTANT_MODE", raising=False)
    (tmp_path / ".assistant").mkdir()
    return tmp_path


def set_mode(home: Path, value) -> None:
    (home / ".assistant/config.json").write_text(json.dumps({"slack": {}, "mode": value}))


@pytest.mark.parametrize("config, expected", [
    (None, False), ("{torn", False), ('["pro"]', False),
    (json.dumps({"mode": "default"}), False), (json.dumps({"mode": "pro"}), True),
])
def test_mode_reads_the_config_file(home, config, expected):
    if config is not None:
        (home / ".assistant/config.json").write_text(config)
    assert mode.is_pro() is expected


def test_environment_overrides_the_config_file(home, monkeypatch):
    set_mode(home, "pro")
    monkeypatch.setenv("ASSISTANT_MODE", "default")
    assert not mode.is_pro()
    monkeypatch.setenv("ASSISTANT_MODE", "pro")
    set_mode(home, "default")
    assert mode.is_pro()


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


def render_page(home: Path) -> str:
    (home / ".claude/cache").mkdir(parents=True)
    (home / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    spec = importlib.util.spec_from_file_location("render_pro_mode", REPO / "bin/render-assistant-page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.render()
    return (home / ".claude/assistant-dashboard.html").read_text()


def test_default_dashboard_shows_only_default_tabs(home):
    page = render_page(home)
    assert all(tab in page for tab in DEFAULT_TABS)
    assert not any(tab in page for tab in PRO_TABS)
    assert 'data-panel="brief"' not in page


def test_pro_dashboard_shows_every_tab(home):
    set_mode(home, "pro")
    page = render_page(home)
    assert all(tab in page for tab in DEFAULT_TABS + PRO_TABS)
    order = [page.index(tab) for tab in (DEFAULT_TABS[0], PRO_TABS[0], DEFAULT_TABS[1], DEFAULT_TABS[3])]
    assert order == sorted(order)


def run_installer(home: Path, env: dict, *flags: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--apply", *flags],
                          capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
    return proc


def test_installer_links_pro_skills_only_in_pro_mode(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    skills = home / ".claude/skills"
    env = installer_env(home)
    run_installer(home, env)
    assert (skills / "todo").is_symlink() and (skills / "cleanup").is_symlink()
    assert not any((skills / name).exists() for name in ("goal", "attend", "back-off", "lesson"))

    run_installer(home, env, "--pro")
    assert json.loads((home / ".assistant/config.json").read_text())["mode"] == "pro"
    assert all((skills / name).is_symlink() for name in ("goal", "attend", "back-off", "lesson"))

    run_installer(home, env)
    assert (skills / "goal").is_symlink()

    run_installer(home, env, "--no-pro")
    assert json.loads((home / ".assistant/config.json").read_text())["mode"] == "default"
    assert not (skills / "goal").exists() and (skills / "todo").is_symlink()
