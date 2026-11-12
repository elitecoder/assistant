"""Every coding-agent launch goes through bin/agent_session.py, and each one
runs GitHub Copilot when the user picks it.

Two kinds of test:
  * a source gate that fails when any file outside the seam builds a claude,
    copilot, or droid command line;
  * one test per launch site that picks Copilot in ~/.assistant/comms/config.json
    and checks the site starts Copilot (and starts Claude without the pick).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import agent_session as ag
import comms_lib as cl
import comms_session as cs
import llm_runner

import agent_launch_gate as gate

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
NO_TOOLS = f"--available-tools={ag.COPILOT_NO_TOOLS}"


# ── helpers ───────────────────────────────────────────────────────────────────

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pick(home: Path, provider: str, **extra) -> None:
    config = home / ".assistant" / "comms" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"llm": {"provider": provider,
                                          "copilot": {"model": "gpt-test"}},
                                  **extra}))


def _stub(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def copilot_stdout(text: str, exit_code: int = 0) -> str:
    """The JSONL `copilot --output-format json` prints (shape captured from
    Copilot CLI 1.0.108)."""
    events = [
        {"type": "session.start", "data": {"sessionId": "cp-1"}},
        {"type": "user.message", "data": {"content": "prompt"}},
        {"type": "assistant.message", "data": {"content": text, "toolRequests": []}},
        {"type": "session.usage_checkpoint", "data": {"providerModelMetrics": [
            {"metrics": {"usage": {"inputTokens": 120, "outputTokens": 7}}}]}},
        {"type": "result", "sessionId": "cp-1", "exitCode": exit_code},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".assistant").mkdir(parents=True)
    (h / ".claude" / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    for var in ("CLAUDE_BIN", "COPILOT_BIN", "DROID_BIN", "COPILOT_MODEL",
                "ASSISTANT_DISPATCH_AGENT", "ASSISTANT_COMMS_AGENT"):
        monkeypatch.delenv(var, raising=False)
    for feature in ("TRIAGE", "OBSERVER", "OBSERVER_AUDIT", "STRATEGIST",
                    "NARRATOR", "LESSONS", "SLACK_JUDGE"):
        monkeypatch.delenv(f"{feature}_LLM_PROVIDER", raising=False)
    return h


@pytest.fixture
def copilot_bin(home, monkeypatch):
    stub = _stub(home / "tools" / "copilot")
    monkeypatch.setenv("COPILOT_BIN", str(stub))
    return stub


# ── the source gate ───────────────────────────────────────────────────────────

def test_no_file_outside_the_seam_builds_an_agent_command():
    problems = gate.scan(REPO)
    assert problems == [], "route these through bin/agent_session.py:\n" + "\n".join(problems)


CAUGHT = {
    "py": [
        'cmd = ["claude", "--model", model, "--print"]',
        'cmd = [claude_bin, "-p", prompt]',
        'subprocess.run(["copilot", "-p", "hi"])',
        'subprocess.run([COPILOT, "--resume", sid])',
        'argv = [exe, "--allow-all"]',
        'os.execvp("claude", ["claude", "--resume", sid])',
        'cmd = f"{bin} --dangerously-skip-permissions"',
        'cmd = f"cd {cwd} && claude --resume {sid}"',
        'launch = "droid exec --model glm"',
        'CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")',
        'path = HOME / ".local/bin/claude"',
        'p = shutil.which("copilot")',
        'subprocess.Popen("copilot --yolo", shell=True)',
        'cmd = ("copilot", "--model", m)',
        'cmd += ["--available-tools=x"]',
        'x = {"--command": "claude --resume abc"}',
        'os.system(f"{exe} --json-schema x")',
    ],
    "sh": [
        'claude -p "hi"',
        '"$CLAUDE_BIN" --print x',
        'exec copilot --allow-all',
        'X=1 droid exec --model m',
        'cmux new-workspace --command "claude --resume $SID"',
        'LAUNCH_CMD="zsh -lic \'claude --dangerously-skip-permissions\'"',
        'out=$(copilot -p "$PROMPT")',
        '${COPILOT_BIN} --resume "$id"',
        'foo && ~/.local/bin/droid --auto high',
        'run_it \\\n  --x 1 && claude \\\n  --print',
    ],
    "js": [
        "spawn('claude', ['-p', prompt])",
        'execFile("copilot", args)',
        "const cmd = `${bin} --allow-all`;",
        'const argv = ["claude", "--print"];',
    ],
    "md": ['text\n```bash\nclaude --resume "$S"\n```\n'],
    "plist": ['<array><string>/Users/x/.local/bin/claude</string><string>-p</string></array>'],
}
PASSED = {
    "py": [
        'provider = "claude"',
        'AGENTS = ("claude", "copilot", "droid")',
        'value_flags = {"--resume", "-r"}',
        'ap.add_argument("--print", action="store_true")',
        'subprocess.run(["git", "log", "-p"])',
        'print("copilot --version")',
        'agent_session.headless_argv("claude", binary_path=b, model=m, run_dir=d)',
        'log.info("dispatch %s using claude", tid)',
        'def f():\n    """Runs claude --print under the hood."""\n    return 1',
        'x = 1  # claude --dangerously-skip-permissions',
    ],
    "sh": [
        'command -v copilot >/dev/null',
        'copilot --version',
        '"$CMUX_CLI" hooks copilot install --yes',
        'echo "claude is the default # not a comment"',
        'python3 "$SEAM" agent --agent "$ENGINE"',
        '# claude --print old way',
    ],
    "js": ["// spawn('claude', ['-p'])", "const name = 'claude';"],
    "md": ['Run `claude --resume` by hand if you like.\n'],
    "plist": ['<string>/usr/bin/python3</string><!-- <string>claude</string> -->'],
}
SCANNERS = {"py": gate.python_problems, "sh": gate.shell_problems, "js": gate.js_problems,
            "md": gate.markdown_problems, "plist": gate.plist_problems}


@pytest.mark.parametrize("kind,snippet", [(k, c) for k, cs in CAUGHT.items() for c in cs])
def test_the_gate_catches_a_reintroduced_launch(kind, snippet):
    assert SCANNERS[kind](snippet), snippet


@pytest.mark.parametrize("kind,snippet", [(k, c) for k, cs in PASSED.items() for c in cs])
def test_the_gate_ignores_code_that_only_names_an_agent(kind, snippet):
    assert SCANNERS[kind](snippet) == [], snippet


def test_the_gate_scans_every_known_launch_site():
    gated = {str(p.relative_to(REPO)) for p in gate.gated_files(REPO)}
    for rel in ("bin/pulse.py", "bin/comms_session.py", "bin/llm_runner.py",
                "bin/lesson-extractor.py", "bin/narrate-brief.py", "bin/strategist.py",
                "bin/slack-focus.py", "bin/cmux-restore-sessions.py",
                "hooks/cmux-auto-resume.py", "evals/fleet/run.py", "evals/observer/run.py",
                "skills/spawn-claude-workspace/SKILL.md", "install-bootstrap.sh",
                "install.sh", ".github/workflows/quality.yml"):
        assert rel in gated, rel
    assert any(rel.endswith(".plist") for rel in gated)


# ── the user's pick ───────────────────────────────────────────────────────────

def test_one_knob_picks_copilot_for_every_seam(home):
    _pick(home, "copilot")
    assert ag.dispatch_agent() == ag.COPILOT
    assert ag.warm_agent() == ag.COPILOT
    for feature in ("triage", "observer", "observer_audit", "strategist",
                    "narrator", "lessons", "slack_judge"):
        route = llm_runner.load_route_config(llm_runner.config_path(home=home), feature)
        assert route.provider == ag.COPILOT
        assert route.model_for("claude-sonnet") == "gpt-test"


def test_copilot_falls_back_to_claude_when_not_installed(home, monkeypatch):
    monkeypatch.setattr(ag.shutil, "which", lambda _b: None)
    assert ag.available_or_claude(ag.COPILOT) == ag.CLAUDE
    monkeypatch.setenv("COPILOT_BIN", str(_stub(home / "x" / "copilot")))
    assert ag.available_or_claude(ag.COPILOT) == ag.COPILOT


def test_assistant_llm_set_copilot_writes_the_knob(home):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_seam")
    assert cli.main(["set", "copilot"]) == 0
    doc = json.loads((home / ".assistant/comms/config.json").read_text())
    assert doc["llm"]["provider"] == "copilot"
    assert cli.status(llm_runner.config_path(home=home))["global_provider"] == "copilot"


# ── headless one-shot calls ───────────────────────────────────────────────────

def test_llm_runner_runs_copilot_tools_off_and_parses_its_output(home, copilot_bin, tmp_path):
    seen = {}

    def run(cmd, *, input_text=None, timeout=30, merge_bedrock=False):
        seen.update(cmd=cmd, prompt=input_text, bedrock=merge_bedrock)
        return 0, copilot_stdout('{"ok": true}'), ""

    result = llm_runner.invoke(
        provider="copilot", prompt="judge", model="gpt-test", run_dir=tmp_path,
        timeout=30, run=run, disable_tools=True, json_schema={"type": "object"})
    assert seen["cmd"][0] == str(copilot_bin)
    assert NO_TOOLS in seen["cmd"] and "--allow-all" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "gpt-test"
    assert seen["bedrock"] is False
    assert seen["prompt"].startswith("judge") and '{"type":"object"}' in seen["prompt"]
    assert (result.usable, result.result_text, result.tokens_in, result.tokens_out,
            result.session_id, result.usage_source) == (
        True, '{"ok": true}', 120, 7, "cp-1", "cli")


def test_llm_runner_copilot_with_tools_gets_write_access_to_its_run_dir(home, copilot_bin, tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return 0, copilot_stdout("done"), ""

    llm_runner.invoke(provider="copilot", prompt="p", model="", run_dir=tmp_path,
                      timeout=30, run=run, extra_dirs=[tmp_path / "extra"])
    cmd = seen["cmd"]
    assert "--allow-all" in cmd and NO_TOOLS not in cmd and "--model" not in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert str(tmp_path / "extra") in cmd


def test_llm_runner_copilot_failure_is_unusable(home, copilot_bin, tmp_path):
    result = llm_runner.invoke(
        provider="copilot", prompt="p", model="m", run_dir=tmp_path, timeout=30,
        run=lambda cmd, **kw: (1, copilot_stdout("", exit_code=1), "boom"))
    assert result.usable is False and result.result_text == ""


def test_llm_runner_missing_copilot_is_one_clear_failure(home, tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_BIN", str(tmp_path / "gone" / "copilot"))
    calls = []
    result = llm_runner.invoke(provider="copilot", prompt="p", model="m",
                               run_dir=tmp_path, timeout=30,
                               run=lambda *a, **k: calls.append(a))
    assert (result.rc, result.usable, calls) == (127, False, [])
    assert "copilot binary not found" in result.stderr


def _triage(mod, home):
    event = {"schema": "world-event/1", "id": "eid-1", "source": "github",
             "kind": "mystery", "external_id": "x:1", "title": "e", "snippet": "?",
             "refs": {}}
    mod.call_triage_batch([event], 1)


def _observer(mod, home):
    prompt = home / "obs.md"
    prompt.write_text("# observer\n")
    mod.OBSERVER_BATCH_PROMPT = prompt
    mod.call_observer_batch([{"ws_ref": "workspace:1", "title": "t", "cwd": str(home)}],
                            pulse_idx=1, batch_idx=0)


def _narrator(mod, home):
    mod._spawn("prompt", home / "narr-run", "out.json")


def _strategist(mod, home):
    mod._spawn("prompt", home / "strat-run", "draft.json")


def _slack_judge(mod, home):
    mod.call_model("judge these")


def _lessons(mod, home):
    mod._claude_oneshot("draft a lesson")


HEADLESS_SITES = [
    ("pulse triage", "pulse.py", "run", _triage, "triage"),
    ("pulse observer", "pulse.py", "run", _observer, "observer"),
    ("brief narrator", "narrate-brief.py", "run", _narrator, "narrator"),
    ("strategist", "strategist.py", "run", _strategist, "strategist"),
    ("slack judge", "slack-focus.py", "run", _slack_judge, "slack_judge"),
    ("lesson extractor", "lesson-extractor.py", "_run", _lessons, "lessons"),
]


@pytest.mark.parametrize("label,script,run_attr,drive,feature", HEADLESS_SITES,
                         ids=[s[0] for s in HEADLESS_SITES])
@pytest.mark.parametrize("provider", ["copilot", "claude"])
def test_every_headless_site_runs_the_picked_agent(home, copilot_bin, monkeypatch,
                                                   label, script, run_attr, drive,
                                                   feature, provider):
    _pick(home, provider)
    mod = _load(BIN / script, f"seam_{script.replace('-', '_')}_{provider}")
    if hasattr(mod, "self_update_pulse"):
        mod.self_update_pulse = lambda pulse_idx: None
    commands = []

    def fake(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd and cmd[0] == str(copilot_bin):
            return 0, copilot_stdout("[]"), ""
        return 0, json.dumps({"type": "result", "subtype": "success", "result": "[]",
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), ""

    monkeypatch.setattr(mod, run_attr, fake)
    drive(mod, home)
    agent_calls = [c for c in commands if Path(c[0]).name in ag.AGENTS]
    assert len(agent_calls) == 1, (label, commands)
    cmd = agent_calls[0]
    if provider == "copilot":
        assert cmd[0] == str(copilot_bin)
        assert NO_TOOLS in cmd and cmd[cmd.index("--model") + 1] == "gpt-test"
    else:
        assert cmd[0] == ag.binary(ag.CLAUDE) and "--print" in cmd


# ── interactive sessions ──────────────────────────────────────────────────────

def _copilot_session(home: Path, sid: str, cwd: str, events: list[dict]) -> Path:
    d = home / ".copilot" / "session-state" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.yaml").write_text(f"id: {sid}\ncwd: {cwd}\n")
    path = d / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_pulse_dispatch_spawns_copilot_and_confirms_from_its_transcript(home, copilot_bin, monkeypatch):
    _pick(home, "copilot")
    cwd = home / "dev"
    cwd.mkdir()
    monkeypatch.setenv("DISPATCH_CWD", str(cwd))
    pulse = _load(BIN / "pulse.py", "seam_pulse_dispatch")
    pulse.self_update_pulse = lambda pulse_idx: None
    commands, keys = [], []

    def run(cmd, *a, **k):
        commands.append(cmd)
        if "new-workspace" in cmd:
            return 0, "OK workspace:9\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:9\n", ""
        return 0, "", ""

    def rpc(method, params, timeout=15):
        keys.append((method, params.get("text") or params.get("key")))
        if method == "surface.send_text" and params["text"].startswith("Read "):
            _copilot_session(home, "cp-dispatch", str(cwd), [
                {"type": "user.message", "data": {"content": params["text"]}}])
        return {}

    screens = iter(["Do you trust the files in this folder?"])
    monkeypatch.setattr(pulse, "run", run)
    monkeypatch.setattr(pulse, "_cmux_rpc", rpc)
    monkeypatch.setattr(pulse, "_surface_read_text",
                        lambda ref, lines=200: next(screens, "Interactive · Allow All · / commands · ? help"))
    monkeypatch.setattr(pulse, "_load_todo_item", lambda tid: {"id": tid, "title": "x"})
    monkeypatch.setattr(pulse, "_mark_todo_dispatched", lambda tid, ws: True)
    monkeypatch.setattr(pulse.time, "sleep", lambda s: None)
    assert pulse.dispatch_todo("td-1") is True
    spawn = next(c for c in commands if "new-workspace" in c)
    launch = spawn[spawn.index("--command") + 1]
    assert launch.startswith(f"{copilot_bin} --allow-all --model gpt-test")
    assert ("surface.send_text", "1") in keys, "copilot trust prompt answered"


def _dispatch(home, monkeypatch, name, screens, transcript):
    cwd = home / "dev"
    cwd.mkdir(exist_ok=True)
    monkeypatch.setenv("DISPATCH_CWD", str(cwd))
    pulse = _load(BIN / "pulse.py", name)
    pulse.self_update_pulse = lambda pulse_idx: None
    commands = []

    def run(cmd, *a, **k):
        commands.append(cmd)
        if "new-workspace" in cmd:
            return 0, "OK workspace:9\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:9\n", ""
        return 0, "", ""

    def rpc(method, params, timeout=15):
        if method == "surface.send_text" and params["text"].startswith("Read "):
            transcript(cwd, params["text"])
        return {}

    monkeypatch.setattr(pulse, "run", run)
    monkeypatch.setattr(pulse, "_cmux_rpc", rpc)
    monkeypatch.setattr(pulse, "_surface_read_text", lambda ref, lines=200: screens)
    monkeypatch.setattr(pulse, "_load_todo_item", lambda tid: {"id": tid, "title": "x"})
    monkeypatch.setattr(pulse, "_mark_todo_dispatched", lambda tid, ws: True)
    monkeypatch.setattr(pulse.time, "sleep", lambda s: None)
    ok = pulse.dispatch_todo("td-3")
    spawn = next(c for c in commands if "new-workspace" in c)
    return ok, spawn[spawn.index("--command") + 1]


def _claude_transcript(home):
    def write(cwd, text):
        d = home / ".claude" / "projects" / ag.claude_project_slug(cwd)
        d.mkdir(parents=True, exist_ok=True)
        (d / "c-1.jsonl").write_text(json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    return write


def test_pulse_dispatch_uses_claude_without_the_pick(home, monkeypatch):
    _pick(home, "claude")
    ok, launch = _dispatch(home, monkeypatch, "seam_pulse_claude",
                           "Claude Code v9", _claude_transcript(home))
    assert (ok, launch) == (True, "claude")


def test_pulse_dispatch_falls_back_to_claude_when_copilot_does_not_start(home, monkeypatch):
    _pick(home, "copilot")
    broken = home / "tools" / "copilot"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("COPILOT_BIN", str(broken))
    ok, launch = _dispatch(home, monkeypatch, "seam_pulse_broken_copilot",
                           "Claude Code v9", _claude_transcript(home))
    assert (ok, launch) == (True, "claude")


def test_comms_warm_session_spawns_copilot_when_picked(home, copilot_bin, monkeypatch):
    _pick(home, "copilot")
    monkeypatch.delenv("COPILOT_BIN")
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})
    commands, waits = [], []

    def run_cmd(cmd, timeout=30):
        commands.append(cmd)
        if "new-workspace" in cmd:
            return 0, "workspace:310\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:310\n", ""
        return 0, "", ""

    monkeypatch.setattr(cl, "run_cmd", run_cmd)
    monkeypatch.setattr(cs, "await_ready",
                        lambda **kw: waits.append((kw["ready_re"], kw["trust_marker"])) or (True, False))
    monkeypatch.setattr(cs, "deliver_boot", lambda *a, **k: "/copilot-events.jsonl")
    monkeypatch.setattr(cs, "reconcile_warm_workspaces", lambda p, keep, log=None: None)
    sess = cs.spawn_session(paths, Path("/boot.md"))
    spawn = next(c for c in commands if "new-workspace" in c)
    assert spawn[spawn.index("--command") + 1].startswith("command copilot --allow-all --model gpt-test")
    assert sess["agent"] == ag.COPILOT
    assert waits[0] == (ag.ready_re(ag.COPILOT), ag.trust_marker(ag.COPILOT))


def test_comms_finds_and_reads_copilot_replies_by_cwd(home, monkeypatch):
    monkeypatch.setattr(cs, "HOME", home)
    cwd = str(home / "dev")
    other = _copilot_session(home, "other", "/elsewhere", [
        {"type": "user.message", "data": {"content": "msg_ts=1 [boot x]"}}])
    mine = _copilot_session(home, "mine", cwd, [
        {"type": "user.message", "data": {"content": "hello msg_ts=1"}},
        {"type": "assistant.message", "data": {"content": "", "toolRequests": [{}]}},
        {"type": "assistant.message", "data": {"content": "the reply"}},
    ])
    os.utime(other, (mine.stat().st_mtime + 60,) * 2)
    found = cs.find_submission(cs.project_dir_for_cwd(cwd, ag.COPILOT), "msg_ts=1", 0,
                               agent=ag.COPILOT, cwd=cwd)
    assert found == str(mine)
    assert cs.last_assistant_text(mine) == "the reply"


def test_comms_reads_copilots_prompt_box():
    screen = "\n".join([
        " ● MCP Servers reloaded",
        "╻▄▄▄▄▄▄▄▄",
        "┃ [boot 123] read the prompt",
        "╹▀▀▀▀▀▀▀▀",
        " Interactive · Allow All · / commands · ? help",
    ])
    assert cs.input_box_text(screen) == "[boot 123] read the prompt"
    assert cs.box_has_marker(cs.input_box_text(screen), "[boot 123]")


def test_comms_clears_a_full_copilot_session_by_respawning(home, monkeypatch):
    transcript = _copilot_session(home, "big", "/c", [])
    transcript.write_bytes(b"x" * 10)
    assert cs.should_clear(transcript, agent=ag.COPILOT, droid_clear_bytes=5)
    assert not cs.should_clear(transcript, agent=ag.COPILOT, droid_clear_bytes=50)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})
    closed, spawned = [], []
    monkeypatch.setattr(cs, "close_own_workspace", lambda p, w, log=None: closed.append(w))
    monkeypatch.setattr(cs, "spawn_session",
                        lambda p, b, log=None, agent=None: spawned.append(agent) or {"agent": agent})
    sess = {"ws_ref": "workspace:5", "surface_ref": "surface:5", "cwd": "/c"}
    assert cs.clear_session(paths, sess, Path("/boot.md"), agent=ag.COPILOT) == {"agent": ag.COPILOT}
    assert closed == ["workspace:5"] and spawned == [ag.COPILOT]


def test_seam_cli_prints_the_picked_launch_for_the_spawn_skill(home, copilot_bin):
    _pick(home, "copilot")
    env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
    run = lambda *args: subprocess.run([sys.executable, str(BIN / "agent_session.py"), *args],
                                       capture_output=True, text=True, env=env)
    assert run("agent").stdout.strip() == "copilot"
    assert run("agent", "--agent", "claude").stdout.strip() == "claude"
    missing = run("agent", "--agent", "droid")
    assert (missing.returncode, missing.stdout) == (1, "")
    assert run("launch-command").stdout.strip() == f"{copilot_bin} --allow-all --model gpt-test"
    assert run("launch-command", "--agent", "claude").stdout.strip() == "claude"
    assert run("launch-command", "--agent", "claude", "--unattended").stdout.strip() == \
        "zsh -lic 'claude --dangerously-skip-permissions'"
    assert run("launch-command", "--agent", "copilot", "--unattended").stdout.strip() == \
        f"{copilot_bin} --allow-all --model gpt-test"
    ready = subprocess.run([sys.executable, str(BIN / "agent_session.py"), "is-ready"],
                           input="Allow All · / commands · ? help", capture_output=True,
                           text=True, env=env)
    assert ready.returncode == 0
    cwd = str(home / "dev")
    _copilot_session(home, "skill", cwd, [
        {"type": "user.message", "data": {"content": "Read /p/prompt-1.md in full"}}])
    assert run("submitted", "--cwd", cwd, "--needle", "/p/prompt-1.md").returncode == 0
    assert run("submitted", "--cwd", cwd, "--needle", "/p/prompt-2.md").returncode == 1
    later = str(time.time() + 60)
    assert run("submitted", "--cwd", cwd, "--needle", "/p/prompt-1.md",
               "--since", later).returncode == 1
    assert run("copilot-chosen").returncode == 0
    _pick(home, "claude")
    assert run("copilot-chosen").returncode == 1


def _skill_block(n: int) -> str:
    text = (REPO / "skills/spawn-claude-workspace/SKILL.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    return blocks[n]


@pytest.fixture
def skill_home(home):
    link = home / ".claude" / "skills" / "spawn-claude-workspace"
    link.parent.mkdir(parents=True)
    link.symlink_to(REPO / "skills" / "spawn-claude-workspace")
    tools = home / "tools"
    _stub(tools / "cmux")
    _stub(tools / "droid")
    (home / ".assistant" / "droid-glm-settings.json").write_text("{}")
    return home


@pytest.mark.parametrize("shell", ["zsh", "bash"])
@pytest.mark.parametrize("engine,expect", [("claude", "claude"), ("", "droid"),
                                           ("copilot", None)])
def test_spawn_skill_resolves_the_engine_in_every_shell(skill_home, shell, engine, expect):
    env = {"HOME": str(skill_home), "PATH": f"{skill_home / 'tools'}:/usr/bin:/bin",
           "ENGINE": engine}
    script = _skill_block(0) + 'printf "ENGINE=%s\\n" "$ENGINE"\n'
    done = subprocess.run([shell, "-e", "-c", script], capture_output=True, text=True, env=env)
    if expect is None:
        assert done.returncode != 0 and "ENGINE=" not in done.stdout, done.stdout
    else:
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip().splitlines()[-1] == f"ENGINE={expect}"


@pytest.mark.parametrize("engine,launch", [
    ("claude", "zsh -lic 'claude --dangerously-skip-permissions'"),
    ("droid", None),
])
def test_spawn_skill_launches_the_engine_like_before(skill_home, engine, launch):
    env = {"HOME": str(skill_home), "PATH": f"{skill_home / 'tools'}:/usr/bin:/bin",
           "ENGINE": engine}
    script = _skill_block(0) + _skill_block(1).split("OUT=")[0] + 'printf "%s" "$LAUNCH_CMD"'
    done = subprocess.run(["zsh", "-e", "-c", script], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    expected = launch or ag.launch_command(ag.DROID, home=skill_home)
    assert done.stdout.strip().splitlines()[-1] == expected


def test_spawn_skill_takes_every_command_from_the_seam():
    text = (REPO / "skills/spawn-claude-workspace/SKILL.md").read_text()
    for sub in ("agent", "launch-command", "is-ready", "trust-marker", "submitted"):
        assert f'"$SEAM" {sub}' in text


# ── resume ────────────────────────────────────────────────────────────────────

def test_restore_sessions_resumes_a_copilot_panel_with_copilot(home):
    restore = _load(BIN / "cmux-restore-sessions.py", "seam_restore")
    panel = {"id": "p", "directory": "/repo", "terminal": {
        "surfaceId": "s", "resumeBinding": {},
        "agent": {"kind": "copilot", "sessionId": "cp-9", "workingDirectory": "/repo",
                  "launchCommand": {"arguments": ["/bin/copilot", "--model", "gpt-x",
                                                  "--resume", "old"]}}}}
    cwd, command = restore.build_resume_cmd(panel)
    assert cwd == "/repo"
    assert command == "cd /repo && /bin/copilot --resume cp-9 --model gpt-x"
    panel["terminal"]["agent"]["launchCommand"]["arguments"].insert(1, "--allow-all")
    assert restore.build_resume_cmd(panel)[1] == \
        "cd /repo && /bin/copilot --resume cp-9 --allow-all --model gpt-x"
    assert restore.infer_ledger_provider(
        {"transcript_path": f"{home}/.copilot/session-state/cp-9/events.jsonl"}) == "copilot"


def test_resume_hook_binds_claude_and_droid_resumes(home):
    hook = _load(REPO / "hooks" / "cmux-auto-resume.py", "seam_hook")
    assert hook.fallback_resume("claude", None, "c-1", "/w") == "cd /w && claude --resume c-1"
    assert hook.fallback_resume("factory", None, "d-1", "/w").startswith(
        "cd /w && droid --resume d-1 --settings ")


# ── doctor ────────────────────────────────────────────────────────────────────

def test_doctor_checks_copilot_only_when_picked(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor")
    monkeypatch.setattr(doctor, "HOME", home)
    monkeypatch.setattr(ag.shutil, "which", lambda _b: None)
    _pick(home, "claude")
    assert doctor.check_copilot_binary().status == doctor.SKIP
    _pick(home, "copilot")
    assert doctor.check_copilot_binary().status == doctor.FAIL
    assert doctor.check_claude_bin().name == "copilot binary (warm session)"
    broken = home / "t" / "copilot"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("COPILOT_BIN", str(broken))
    assert doctor.check_copilot_binary().status == doctor.FAIL
    monkeypatch.setenv("COPILOT_BIN", str(_stub(home / "t" / "copilot")))
    assert doctor.check_copilot_binary().status == doctor.PASS
    names = {c.name for c in doctor.run_checks("all")}
    assert {"copilot binary", "copilot cmux hooks"} <= names


def test_doctor_checks_copilot_for_a_feature_pick(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor_feature")
    monkeypatch.setattr(doctor, "HOME", home)
    _pick(home, "claude")
    config = home / ".assistant/comms/config.json"
    doc = json.loads(config.read_text())
    doc["llm"]["features"] = {"observer": {"provider": "copilot"}}
    config.write_text(json.dumps(doc))
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: None)
    assert doctor.check_copilot_binary().status == doctor.FAIL


def test_doctor_flags_missing_copilot_cmux_hooks(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor_hooks")
    monkeypatch.setattr(doctor, "HOME", home)
    monkeypatch.setattr(ag, "HOME", home)
    _pick(home, "claude")
    assert doctor.check_copilot_hooks().status == doctor.SKIP
    _pick(home, "copilot")
    assert doctor.check_copilot_hooks().status == doctor.FAIL
    (home / ".copilot").mkdir()
    (home / ".copilot" / "config.json").write_text('{"model": "gpt-x"}')
    assert doctor.check_copilot_hooks().status == doctor.FAIL
    (home / ".copilot" / "config.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": "cmux hooks enqueue copilot stop"}]}]}}')
    assert doctor.check_copilot_hooks().status == doctor.PASS


def test_pulse_dispatch_answers_a_trust_prompt_that_renders_late(home, copilot_bin, monkeypatch):
    """Copilot showed its folder-trust dialog about 4 s into boot. A single
    check right after launch misses it, and the session never gets ready."""
    _pick(home, "copilot")
    cwd = home / "dev"
    cwd.mkdir()
    monkeypatch.setenv("DISPATCH_CWD", str(cwd))
    pulse = _load(BIN / "pulse.py", "seam_pulse_late_trust")
    pulse.self_update_pulse = lambda pulse_idx: None
    state = {"reads": 0, "trusted": False}

    def screen(ref, lines=200):
        state["reads"] += 1
        if state["trusted"]:
            return "Interactive · Allow All · / commands · ? help"
        return "" if state["reads"] <= 4 else "Do you trust the files in this folder?"

    def rpc(method, params, timeout=15):
        if method == "surface.send_text" and params["text"] == "1":
            state["trusted"] = True
        if method == "surface.send_text" and params["text"].startswith("Read "):
            _copilot_session(home, "cp-late", str(cwd), [
                {"type": "user.message", "data": {"content": params["text"]}}])
        return {}

    def run(cmd, *a, **k):
        if "new-workspace" in cmd:
            return 0, "OK workspace:9\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:9\n", ""
        return 0, "", ""

    monkeypatch.setattr(pulse, "run", run)
    monkeypatch.setattr(pulse, "_cmux_rpc", rpc)
    monkeypatch.setattr(pulse, "_surface_read_text", screen)
    monkeypatch.setattr(pulse, "_load_todo_item", lambda tid: {"id": tid, "title": "x"})
    monkeypatch.setattr(pulse, "_mark_todo_dispatched", lambda tid, ws: True)
    monkeypatch.setattr(pulse.time, "sleep", lambda s: None)
    assert pulse.dispatch_todo("td-2") is True
    assert state["trusted"]


# ── Copilot under launchd, and Copilot's output ───────────────────────────────

def _npm_copilot(home: Path) -> tuple[Path, Path]:
    """An npm-style Copilot install: a node loader script on bin/, and the
    standalone build inside its package."""
    pkg = home / ".nvm/versions/node/v24.0.0/lib/node_modules/@github/copilot"
    loader = pkg / "npm-loader.js"
    loader.parent.mkdir(parents=True)
    loader.write_text("#!/usr/bin/env node\nimport x from 'y';\n")
    loader.chmod(0o755)
    native = _stub(pkg / "node_modules/@github/copilot-darwin-arm64/copilot")
    link = home / ".nvm/versions/node/v24.0.0/bin/copilot"
    link.parent.mkdir(parents=True)
    link.symlink_to(loader)
    return link, native


def test_copilot_resolves_to_its_standalone_build_without_node(home, monkeypatch):
    link, native = _npm_copilot(home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert ag.binary(ag.COPILOT) == str(native)
    monkeypatch.setenv("PATH", f"{link.parent}:/usr/bin:/bin")
    assert ag.binary(ag.COPILOT) == str(native)
    assert ag.agent_available(ag.COPILOT)


def test_headless_copilot_runs_the_standalone_build_in_its_run_dir(home, monkeypatch, tmp_path):
    _, native = _npm_copilot(home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        out = copilot_stdout("", exit_code=1) + json.dumps(
            {"type": "session.error", "data": {"message": "quota exceeded"}}) + "\n"
        return 1, out, "boom"

    result = llm_runner.invoke(provider="copilot", prompt="p", model="m", run_dir=tmp_path,
                               timeout=30, run=run, disable_tools=True)
    cmd = seen["cmd"]
    assert cmd[0] == str(native)
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert "boom" in result.stderr and "quota exceeded" in result.stderr


def test_copilot_tools_off_turns_off_every_tool_source(tmp_path):
    cmd = ag.headless_argv(ag.COPILOT, binary_path="/c", model="", run_dir=tmp_path,
                           disable_tools=True)
    for flag in (NO_TOOLS, "--disable-builtin-mcps", "--no-custom-instructions",
                 "--no-ask-user"):
        assert flag in cmd, flag
    assert "--allow-all" not in cmd


@pytest.mark.parametrize("text,exit_code,usable", [
    ("answer", 0, True), ("", 0, False), ("answer", 1, False)])
def test_copilot_result_needs_a_reply_and_a_clean_exit(text, exit_code, usable):
    assert (ag.parse_headless(ag.COPILOT, copilot_stdout(text, exit_code)) is not None) is usable


def test_copilot_reply_is_the_last_message_and_tokens_the_last_checkpoint():
    events = [
        {"type": "assistant.message", "data": {"content": "first"}},
        {"type": "session.usage_checkpoint", "data": {"providerModelMetrics": [
            {"metrics": {"usage": {"inputTokens": 10, "outputTokens": 1}}}]}},
        {"type": "assistant.message", "data": {"content": "final"}},
        {"type": "session.usage_checkpoint", "data": {"providerModelMetrics": [
            {"metrics": {"usage": {"inputTokens": 30, "outputTokens": 4}}}]}},
        {"type": "result", "sessionId": "s", "exitCode": 0},
    ]
    parsed = ag.parse_headless(ag.COPILOT, "\n".join(json.dumps(e) for e in events))
    assert (parsed["result_text"], parsed["tokens_in"], parsed["tokens_out"]) == ("final", 30, 4)


def test_copilot_sessions_match_by_real_and_quoted_cwd(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    a = _copilot_session(home, "a", str(alias), [])
    d = home / ".copilot/session-state/b"
    d.mkdir(parents=True)
    (d / "workspace.yaml").write_text(f'cwd: "{real}"\n')
    (d / "events.jsonl").write_text("")
    assert sorted(ag.copilot_transcripts(real, home=home)) == sorted([a, d / "events.jsonl"])


def test_feature_copilot_model_beats_the_global_one(home, monkeypatch):
    monkeypatch.setenv("COPILOT_MODEL", "global")
    monkeypatch.setenv("TRIAGE_COPILOT_MODEL", "triage-only")
    assert ag.copilot_model("triage") == "triage-only"
    assert ag.copilot_model("observer") == "global"


def test_copilot_ready_marker_survives_a_narrow_pane_but_not_the_trust_dialog():
    ready = ag.ready_re(ag.COPILOT)
    assert ready.search(" ← open sidebar · Interactive · Allow Al")
    assert ready.search("Interactive · Allow All · / commands · ? help · tab next tab")
    assert not ready.search("Do you trust the files in this folder?\n 1. Yes\n 2. No\n ? for help")
    assert not ready.search("Droid ready  ? for help")


def test_comms_reads_the_live_copilot_box_not_an_old_one_or_a_rule():
    screen = "\n".join([
        "╻▄▄▄", "┃ an old prompt", "╹▀▀▀",
        "────────────", "❯ quoted claude box", "────────────",
        " output", "╻▄▄▄", "┃ [boot 9] live prompt", "╹▀▀▀",
        " ← open sidebar · Interactive",
    ])
    assert cs.input_box_text(screen) == "[boot 9] live prompt"


def test_comms_waits_for_copilots_prompt_box_before_typing():
    assert cs._INPUT_READY_RE[ag.COPILOT] is ag.ready_re(ag.COPILOT)


def test_comms_copilot_warm_session_reaches_the_folders_claude_does(home, monkeypatch):
    _pick(home, "copilot")
    command = cs._warm_launch(ag.COPILOT)
    for d in (cs.REPO_ROOT, cs.HOME / ".assistant", cs.HOME / ".architect", "/tmp"):
        assert f"--add-dir {d}" in command, d


def test_screen_reader_flags_a_copilot_error_and_names_copilot_panes():
    bwc = _load(BIN / "build-ws-context.py", "seam_bwc")
    assert bwc._ERROR_LINE_RE.match(" ● API Error: 500 upstream")
    assert not bwc._ERROR_LINE_RE.match(" ● MCP Servers reloaded: 1 server connected")
    assert bwc._agent_for_screen(" ← open sidebar · Interactive · Allow") == "copilot"


def test_watcher_drops_copilots_status_bar_from_snippets():
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher")
    assert watcher._is_tui_noise(
        " ← open sidebar · Interactive · Allow All · / commands · ? help · tab next tab")
    assert watcher._is_tui_noise(" /repo [⎇ main]      Run on: This computer ctrl+e change environment")
    assert not watcher._is_tui_noise(" ● Copilot finished the refactor")


def test_world_scanner_sees_a_foreground_copilot(monkeypatch):
    scanner = _load(BIN / "world-scanner.py", "seam_scanner_fg")
    line = "41 41 41 Wed Nov 12 08:00:00 2026 /x/@github/copilot-darwin-arm64/copilot\n"
    monkeypatch.setattr(scanner.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, line, ""))
    assert scanner.foreground_provider_process("ttys001", "copilot")[0] == 41
    assert scanner.foreground_provider_process("ttys001", "claude") is None


def test_seam_cli_submitted_reads_the_named_agents_transcripts(home, copilot_bin):
    _pick(home, "copilot")
    cwd = str(home / "dev")
    _copilot_session(home, "s", cwd, [
        {"type": "user.message", "data": {"content": "Read /p/x.md"}}])
    env = {**os.environ, "HOME": str(home)}
    run = lambda *args: subprocess.run([sys.executable, str(BIN / "agent_session.py"), *args],
                                       capture_output=True, text=True, env=env)
    assert run("submitted", "--agent", "copilot", "--cwd", cwd, "--needle", "/p/x.md").returncode == 0
    assert run("submitted", "--agent", "claude", "--cwd", cwd, "--needle", "/p/x.md").returncode == 1


def test_assistant_llm_set_copilot_installs_cmux_hooks(home, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_hooks")
    calls = []
    monkeypatch.setattr(ag.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert cli.main(["set", "claude"]) == 0
    assert calls == []
    assert cli.main(["set", "copilot"]) == 0
    assert calls == [[ag.CMUX_CLI, "hooks", "copilot", "install", "--yes"]]


def test_doctor_checks_copilot_when_only_dispatch_picks_it(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor_dispatch")
    monkeypatch.setattr(doctor, "HOME", home)
    _pick(home, "claude", dispatch={"agent": "copilot"})
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: None)
    assert doctor.check_copilot_binary().status == doctor.FAIL


def test_droid_resume_keeps_one_settings_flag():
    argv = ag.resume_argv(ag.DROID, "d-1", ["--settings=/old.json", "--auto=high", "-v"],
                          home=Path("/h"))
    assert argv.count("--settings") == 1 and not any(a.startswith("--settings=") for a in argv)
    assert "-v" in argv
