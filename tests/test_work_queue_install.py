"""install.sh loads the Work queue only in Pro mode and offers source linking only on an interactive run.

Every run uses the installer sandbox: a temporary HOME and a recording launchctl.
"""
from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from installer_sandbox import assert_recorded_services, installer_env
from test_pro_mode import fake_launchctl, run_installer
from work_queue_fakes import fake_tool

REPO = Path(__file__).resolve().parents[1]
LABEL = "com.assistant.work-queue"
pytestmark = pytest.mark.real_mode


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return home


def service_calls(home: Path, verb: str) -> list[str]:
    return [c for c in (home / "launchctl.log").read_text().splitlines() if c.startswith(verb) and LABEL in c]


def test_default_mode_copies_the_work_queue_but_never_loads_it(home):
    proc = run_installer(installer_env(home))
    assert (home / f"Library/LaunchAgents/{LABEL}.plist").exists()
    assert service_calls(home, "bootstrap") == [] and service_calls(home, "load") == []
    assert service_calls(home, "bootout") == [f"bootout gui/{os.getuid()}/{LABEL}"]
    assert f"FEATURE {LABEL}.plist — Pro mode only (copied, NOT loaded; turn on with --pro)" in proc.stdout
    assert "Pro mode is off, so the Work queue isn't installed (turn it on with --pro)" in proc.stdout
    assert not (home / ".assistant/work-queue").exists()


def test_pro_mode_loads_a_localhost_only_server_and_never_prompts_headless(home):
    proc = run_installer(installer_env(home), "--pro")
    assert service_calls(home, "bootstrap") == [f"bootstrap gui/{os.getuid()} {home}/Library/LaunchAgents/{LABEL}.plist"]
    assert service_calls(home, "bootout") == [f"bootout gui/{os.getuid()}/{LABEL}"]
    assert "non-interactive, so not linking Work queue sources (run bin/work-queue-setup.py link <source>)" in proc.stdout
    assert "Link slack to the Work queue?" not in proc.stdout + proc.stderr
    assert "./bin/work-queue-setup.py link <source> (see docs/work-queue.md)." in proc.stdout
    plist = (home / f"Library/LaunchAgents/{LABEL}.plist").read_text()
    assert f"<string>{REPO}:{REPO}/src</string>" in plist and "<string>work_queue.server</string>" in plist
    assert "__" not in plist.replace("__main__", "")


def test_a_pro_dry_run_lists_the_sources_and_changes_nothing(home):
    env = installer_env(home)
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for plist in (REPO / "launchagents").glob("*.plist"):
        (agents / plist.name).write_text("<!-- an older copy -->\n")
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--pro"], capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert f"would reload {LABEL}" in proc.stdout
    assert "would offer to link these Work queue sources:" in proc.stdout
    assert "    Slack: not linked. Run bin/work-queue-setup.py link slack" in proc.stdout
    assert_recorded_services(home, apply=False)
    assert (agents / f"{LABEL}.plist").read_text() == "<!-- an older copy -->\n"
    assert not (home / ".assistant/config.json").exists()


def test_a_dry_run_before_the_first_install_plans_the_new_service(home):
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--pro"], capture_output=True, text=True,
                          env=installer_env(home), stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"would load {LABEL} (bootstrap {home}/Library/LaunchAgents/{LABEL}.plist once it's copied)" in proc.stdout
    assert "Dry-run complete" in proc.stdout
    assert_recorded_services(home, apply=False)


def test_switching_modes_loads_and_unloads_the_work_queue(home):
    env = installer_env(home)
    run_installer(env)
    (home / "launchctl.log").write_text("")
    run_installer(env, "--pro")
    assert service_calls(home, "bootstrap"), "an already-copied plist must still load when Pro mode turns on"
    (home / "launchctl.log").write_text("")
    run_installer(env, "--no-pro")
    assert service_calls(home, "bootstrap") == [] and service_calls(home, "bootout")
    assert json.loads((home / ".assistant/config.json").read_text())["mode"] == "default"


def test_the_work_queue_restarts_when_its_code_changes(home, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(".git", ".venv", ".worktrees", "__pycache__"))
    env = {**installer_env(home), "ASSISTANT_SELF_UPDATE": "1"}
    (home / ".assistant/config.json").write_text('{"mode": "pro"}')
    log = fake_launchctl(env, running={LABEL})
    run_installer(env, repo=repo)
    log.write_text("")
    run_installer(env, repo=repo)
    assert f"kickstart -k gui/{os.getuid()}/{LABEL}" not in log.read_text()
    with (repo / "work_queue/server.py").open("a") as f:
        f.write("\n# changed\n")
    run_installer(env, repo=repo)
    assert f"kickstart -k gui/{os.getuid()}/{LABEL}" in log.read_text()


def test_help_names_the_work_queue(tmp_path):
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--help"], capture_output=True, text=True,
                          env=installer_env(tmp_path / "home"), stdin=subprocess.DEVNULL, timeout=30)
    assert "Also loads the Work queue page (docs/work-queue.md)." in proc.stdout


def test_an_interactive_pro_install_offers_each_unlinked_source(home):
    env = installer_env(home)
    (home / ".assistant/feature-opt-in").write_text("memory=no\ncrash-resume=no\nws-numberer=no\n")
    (home / ".assistant/memory-repo-config.json").write_text("{}")
    (home / ".assistant/machine-config-configured").write_text("set\n")
    fake_tool(home / "stubbin", "gh", [("api user", "octo\n")])
    answers = {src: b"y\n" if src == "github" else b"n\n" for src in ("slack", "gmail", "calendar", "outlook", "github")}
    primary, secondary = pty.openpty()
    proc = subprocess.Popen(["bash", str(REPO / "install.sh"), "--apply", "--pro"], stdin=secondary,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    os.close(secondary)
    out, deadline = b"", time.monotonic() + 180
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 1)
        if ready:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            out += chunk
        for src in [s for s in answers if f"Link {s} to the Work queue? [y/N] ".encode() in out]:
            os.write(primary, answers.pop(src))
    proc.wait(30)
    os.close(primary)
    text = out.decode()
    assert proc.returncode == 0, text[-3000:]
    assert answers == {}, text[-3000:]
    assert "    GitHub: linked (github.com)" in text
    saved = json.loads((home / ".assistant/work-queue/sources.json").read_text())
    assert saved == {"github": {"skip_authors": []}}
