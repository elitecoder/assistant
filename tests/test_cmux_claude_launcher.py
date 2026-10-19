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
SURFACE = "11111111-2222-3333-4444-555555555555"
AUTO_ALIAS = "alias claude='claude --model \"opus[1m]\" --permission-mode auto --add-dir ~/dev'\n"

FAKE_CMUX = r'''#!/usr/bin/env python3
"""Fake cmux: logs calls; a submitted paste lands in the transcript the way Claude Code writes it."""
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_CMUX_STATE"])
with (state / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[0] == "new-workspace":
    print("OK workspace:7")
elif args[0] == "list-pane-surfaces":
    print("* 11111111-2222-3333-4444-555555555555  Claude Code  [selected]")
elif args[0] == "read-screen":
    reads = state / "reads"
    n = int(reads.read_text()) + 1 if reads.exists() else 1
    reads.write_text(str(n))
    blank = n <= int(os.environ.get("FAKE_BLANK_READS", "0"))
    print("" if blank else os.environ.get("FAKE_SCREEN", "⏵⏵ auto mode on (shift+tab to cycle)"))
elif args[0] == "paste":
    if os.environ.get("FAKE_PASTE_REFUSE") == "1":
        print("Error: draft_present", file=sys.stderr)
        sys.exit(1)
    text = args[args.index("--") + 1]
    target = Path(os.environ["FAKE_CMUX_TRANSCRIPT"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as fh:
        if os.environ.get("FAKE_BUSY") != "1":
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, separators=(",", ":")) + "\n")
        fh.write(json.dumps({"type": "last-prompt", "lastPrompt": text}, separators=(",", ":")) + "\n")
'''


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".assistant" / "spawn-prompts").mkdir(parents=True)
    (home / ".zprofile").write_text(AUTO_ALIAS)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    cmux = fake_bin / "cmux"
    cmux.write_text(FAKE_CMUX)
    cmux.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CMUX_", "ZDOTDIR"))}
    e.update(HOME=str(home), PATH=f"{fake_bin}:{os.environ['PATH']}", FAKE_CMUX_STATE=str(state))
    return e


def prompt(env, name="prompt-x.md") -> Path:
    p = Path(env["HOME"]) / ".assistant" / "spawn-prompts" / name
    p.write_text("Do the thing.\n")
    return p


def calls(env):
    log = Path(env["FAKE_CMUX_STATE"]) / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def project_dir(env, cwd: Path) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in str(cwd.resolve()))
    return Path(env["HOME"]) / ".claude" / "projects" / slug


def run(env, script, *args, timeout=240):
    return subprocess.run(["zsh", str(script), *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout)


def test_spawn_launches_unfocused_auto_mode_claude_and_returns_the_new_transcript(tmp_path, env):
    work = tmp_path / "my.repo"
    work.mkdir()
    transcript = project_dir(env, work) / "abc.jsonl"
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    pf = prompt(env)
    r = run(env, SPAWN, "--env", "FFP_CHANNEL=beta", "--env", "FFP_EXP_LIGHT_LANE=on", work, "FFP-1 beta", pf)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["workspace:7", SURFACE, str(transcript)]
    assert [c for c in calls(env) if c[0] == "new-workspace"] == [[
        "new-workspace", "--cwd", str(work.resolve()), "--name", "FFP-1 beta", "--focus", "false",
        "--env", "FFP_CHANNEL=beta", "--env", "FFP_EXP_LIGHT_LANE=on", "--command", "zsh -lic claude"]]
    assert [c for c in calls(env) if c[0] == "paste"] == [
        ["paste", "--surface", SURFACE, "--submit", "--", f"Read {pf} and follow it."]]
    assert not [c for c in calls(env) if c[0] == "send-key"]


def test_spawn_ignores_transcripts_that_existed_before_launch(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    pf = prompt(env)
    old = project_dir(env, work) / "0-old.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"type": "user", "message": {"content": f"Read {pf} and follow it."}},
                              separators=(",", ":")) + "\n")
    new = project_dir(env, work) / "new.jsonl"
    env["FAKE_CMUX_TRANSCRIPT"] = str(new)
    r = run(env, SPAWN, work, "t", pf)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[2] == str(new)


def test_spawn_wakes_a_blank_surface_then_waits_for_auto_mode(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    env["FAKE_CMUX_TRANSCRIPT"] = str(project_dir(env, work) / "s.jsonl")
    env["FAKE_BLANK_READS"] = "4"
    r = run(env, SPAWN, work, "t", prompt(env))
    assert r.returncode == 0, r.stderr
    kinds = [c[0] for c in calls(env)]
    assert ["send-key", "--surface", SURFACE, "enter"] in calls(env)
    assert kinds.index("send-key") < kinds.index("paste")


def test_spawn_types_nothing_when_a_dialog_is_showing(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    env["FAKE_SCREEN"] = "Do you trust the files in this folder?\n> 1. Yes, proceed"
    r = run(env, SPAWN, work, "t", prompt(env))
    assert (r.returncode, r.stdout.split()) == (1, ["workspace:7", SURFACE, "not-ready"])
    assert not [c for c in calls(env) if c[0] in ("paste", "send-key")]


def test_spawn_reports_a_refused_paste(tmp_path, env):
    work = tmp_path / "w"
    work.mkdir()
    env["FAKE_PASTE_REFUSE"] = "1"
    r = run(env, SPAWN, work, "t", prompt(env))
    assert (r.returncode, r.stdout.split()) == (1, ["workspace:7", SURFACE, "paste-refused"])


@pytest.mark.parametrize("bad", [
    "PATH=/tmp/evil:/usr/bin", "ANTHROPIC_BASE_URL=https://example.com", "NODE_OPTIONS=-r/tmp/x.js",
    "FFP_CHANNEL=beta;id", "FFP_CHANNEL=Beta", "FFP_EXP_X=", "FFP_CHANNEL",
])
def test_spawn_refuses_env_other_than_ffp_experiment_switches(tmp_path, env, bad):
    r = run(env, SPAWN, "--env", bad, tmp_path, "t", prompt(env))
    assert r.returncode == 2
    assert "bad --env value" in r.stderr
    assert calls(env) == []


def test_spawn_refuses_prompt_files_outside_the_staging_folder(tmp_path, env):
    outside = tmp_path / "prompt.md"
    outside.write_text("x")
    staged = prompt(env)
    escape = staged.parent / ".." / ".." / ".." / "prompt.md"
    for pf in (outside, escape, prompt(env, "has space.md"), staged.parent / "missing.md"):
        r = run(env, SPAWN, tmp_path, "t", pf)
        assert r.returncode == 2, pf
        assert "prompt file must be" in r.stderr
    assert calls(env) == []


def test_spawn_refuses_when_the_claude_alias_bypasses_permissions(tmp_path, env):
    (Path(env["HOME"]) / ".zprofile").write_text(
        "alias claude='claude --model x --dangerously-skip-permissions --permission-mode auto'\n")
    r = run(env, SPAWN, tmp_path, "t", prompt(env))
    assert r.returncode == 3
    assert "isn't auto mode" in r.stderr
    assert calls(env) == []


def test_send_confirms_delivery_and_ignores_last_prompt_copies(tmp_path, env):
    pf = prompt(env)
    msg = f"Read {pf} and follow it."
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps({"type": "last-prompt", "lastPrompt": msg}) + "\n" for _ in range(3)))
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    r = run(env, SEND, SURFACE, transcript, pf)
    assert (r.returncode, r.stdout.strip()) == (0, "delivered"), r.stderr
    assert [c for c in calls(env) if c[0] == "paste"] == [["paste", "--surface", SURFACE, "--submit", "--", msg]]


def test_send_does_not_count_a_last_prompt_record_as_delivery(tmp_path, env):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    env["FAKE_CMUX_TRANSCRIPT"] = str(transcript)
    env["FAKE_BUSY"] = "1"
    r = run(env, SEND, SURFACE, transcript, prompt(env))
    assert (r.returncode, r.stdout.strip()) == (1, "not-delivered")
    assert len([c for c in calls(env) if c[0] == "paste"]) == 1


def test_send_reports_a_refused_paste(tmp_path, env):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    env["FAKE_PASTE_REFUSE"] = "1"
    r = run(env, SEND, SURFACE, transcript, prompt(env))
    assert (r.returncode, r.stdout.strip()) == (1, "not-delivered")


def test_send_refuses_its_own_surface_and_short_refs(tmp_path, env):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    pf = prompt(env)
    own = {**env, "CMUX_SURFACE_ID": SURFACE.upper()}
    r = run(own, SEND, SURFACE, transcript, pf)
    assert r.returncode == 2 and "own surface" in r.stderr
    r = run(env, SEND, "surface:9", transcript, pf)
    assert r.returncode == 2 and "must be a UUID" in r.stderr
    assert calls(env) == []
