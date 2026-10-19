"""cmux-claude-spawn.sh and cmux-claude-send.sh against a fake cmux that plays Claude Code."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPAWN = REPO / "bin" / "cmux-claude-spawn.sh"
SEND = REPO / "bin" / "cmux-claude-send.sh"

FAKE_CMUX = r'''#!/usr/bin/env python3
"""Fake cmux: logs every call; on Enter after typed text, appends the text to a transcript like Claude Code does."""
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_CMUX_STATE"])
log = state / "calls.jsonl"
with log.open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[0] == "new-workspace":
    print("OK workspace:7")
elif args[0] == "list-pane-surfaces":
    print("* surface:9  zsh -lic claude  [selected]")
elif args[:2] == ["rpc", "surface.read_text"]:
    print(json.dumps({"text": "⏵⏵ auto mode on (shift+tab to cycle)"}))
elif args[:2] == ["rpc", "surface.send_text"]:
    (state / "typed").write_text(json.loads(args[2])["text"])
elif args[:2] == ["rpc", "surface.send_key"]:
    typed = state / "typed"
    if typed.exists() and os.environ.get("FAKE_CMUX_DROP") != "1":
        target = Path(os.environ["FAKE_CMUX_TRANSCRIPT"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": typed.read_text()}}) + "\n")
        typed.unlink()
    print("{}")
'''


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    cmux = fake_bin / "cmux"
    cmux.write_text(FAKE_CMUX)
    cmux.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_CMUX_STATE": str(state),
    }


def calls(env):
    lines = (Path(env["FAKE_CMUX_STATE"]) / "calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def project_dir(env, cwd: Path) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in str(cwd.resolve()))
    return Path(env["HOME"]) / ".claude" / "projects" / slug


def test_spawn_launches_unfocused_claude_and_returns_the_new_transcript(tmp_path, env):
    work = tmp_path / "my.repo"
    work.mkdir()
    transcript = project_dir(env, work) / "abc.jsonl"
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    msg = '/archffp --mode=fix FFP-1 "quoted"'
    r = subprocess.run(["zsh", str(SPAWN), "--env", "FFP_CHANNEL=beta", "--env", "FFP_EXP_X=on",
                        str(work), "FFP-1 beta", msg],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["workspace:7", "surface:9", str(transcript)]
    new_ws = next(c for c in calls(env) if c[0] == "new-workspace")
    assert new_ws == ["new-workspace", "--cwd", str(work.resolve()), "--name", "FFP-1 beta", "--focus", "false",
                      "--command", "zsh -lic 'export FFP_CHANNEL=beta FFP_EXP_X=on; claude'"]
    sent = [json.loads(c[2]) for c in calls(env) if c[:2] == ["rpc", "surface.send_text"]]
    assert sent == [{"surface_id": "surface:9", "text": msg}]


def test_spawn_without_env_runs_plain_claude(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    env["FAKE_CMUX_TRANSCRIPT"] = str(project_dir(env, work) / "s.jsonl")
    r = subprocess.run(["zsh", str(SPAWN), str(work), "t", "hello"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    new_ws = next(c for c in calls(env) if c[0] == "new-workspace")
    assert new_ws[-1] == "zsh -lic 'claude'"


def test_spawn_ignores_transcripts_that_existed_before_launch(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    old = project_dir(env, work) / "old.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"message": {"content": "hello"}}) + "\n")
    new = project_dir(env, work) / "new.jsonl"
    env["FAKE_CMUX_TRANSCRIPT"] = str(new)
    r = subprocess.run(["zsh", str(SPAWN), str(work), "t", "hello"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[2] == str(new)


@pytest.mark.parametrize("bad", ["A=1; rm -rf x", "A=$(id)", "A='x'", "1A=x", "A"])
def test_spawn_refuses_env_values_that_could_break_the_launch_command(tmp_path, env, bad):
    r = subprocess.run(["zsh", str(SPAWN), "--env", bad, str(tmp_path), "t", "m"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2
    assert "bad --env value" in r.stderr
    assert not (Path(env["FAKE_CMUX_STATE"]) / "calls.jsonl").exists()


def test_send_confirms_delivery_from_the_transcript(tmp_path, env):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"content": "earlier"}}) + "\n")
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    msg = 'Decision: proceed with "route b".\nSecond line.'
    r = subprocess.run(["zsh", str(SEND), "surface:9", str(transcript), msg],
                       capture_output=True, text=True, env=env, timeout=60)
    assert (r.returncode, r.stdout.strip()) == (0, "delivered"), r.stderr
    sent = [json.loads(c[2]) for c in calls(env) if c[:2] == ["rpc", "surface.send_text"]]
    assert sent == [{"surface_id": "surface:9", "text": msg}]


def test_send_does_not_count_an_earlier_copy_of_the_same_message(tmp_path, env):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"content": "proceed"}}) + "\n")
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    env["FAKE_CMUX_DROP"] = "1"
    r = subprocess.run(["zsh", str(SEND), "surface:9", str(transcript), "proceed"],
                       capture_output=True, text=True, env=env, timeout=90)
    assert (r.returncode, r.stdout.strip()) == (1, "not-delivered")
    enters = [c for c in calls(env) if c[:2] == ["rpc", "surface.send_key"]]
    assert len(enters) == 2
