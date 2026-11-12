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
import io
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
        'AGENT = "claude"\nsubprocess.run([AGENT, "-p", prompt])',
        'tool = "copilot"\nsubprocess.run([tool, "--resume", sid])',
        'subprocess.run(["cmux", "new-workspace", "--command", "claude"])',
        'subprocess.run(["zsh", "-lic", "claude"])',
        'subprocess.run([str(HOME / ".local" / "bin" / "claude"), "-p", p])',
        'subprocess.run([agent_session.binary("claude"), "-p", p])',
        'cmd = [exe, "-p", p, "--allow-all-paths"]',
        'root = Path.home() / ".claude" / "projects"',
        'glob = HOME / ".copilot" / "session-state"',
        'path = "~/.factory/sessions/x.jsonl"',
        'cmd = "claude" + " --resume " + sid',
        'cmd = "{} --resume {}".format("claude", sid)',
        'cmd = ["claude"]\ncmd += ["--resume", sid]',
        'cmd = ["copilot"]\ncmd.extend(["--model", m])',
        'root = Path.home().joinpath(".factory", "sessions")',
        'root = os.path.join(HOME, ".copilot", "session-state")',
        'launch = "claude"\nrun([CMUX, "new-workspace", "--command", launch])',
        'run(["env", "CLAUDE_CODE_USE_BEDROCK=1", "claude", "-p", "hi"])',
        'AGENT = "claude"\nrun([AGENT] + ["-p", p])',
        'run(["npx", "@anthropic-ai/claude-code", "-p", p])',
        'root = Path(HOME, ".copilot", "session-state")',
        'subprocess.run(shlex.split("claude -p hi"))',
        'exe = os.environ.get("X") or "claude"\nsubprocess.run([exe, "--print", p])',
        'CLAUDE = os.environ.get("CLAUDE_BIN", "claude")',
        'base = ["claude"]\nrun([*base, "--resume", sid])',
        'agent = "copilot"\nrun(shlex.split(f"{agent} --resume {sid}"))',
        'run([BINS["claude"], "-p", p])',
        'cmd = "%s --print" % "claude"',
        'cmd = "{exe} --resume {sid}".format(exe="droid", sid=s)',
        't = d.get("type")\nif t == "assistant":\n    pass',
        'if m.get("role") == "assistant":\n    pass',
        'n = usage.get("cache_read_input_tokens")',
        'files = (Path.home() / ".claude").glob("projects/*/*.jsonl")',
        'exe = "droid" if kind == "factory" else "claude"\nrun([exe, "--resume", sid, *rest])',
        'exe = "droid" if d else "claude"\nq = shlex.quote(exe)\ncmd = f"{q} --resume {sid}"',
        'role = msg.get("role")\nif role == "user":\n    pass',
        'if d["message"]["role"] == "user":\n    pass',
        'if rec.get("type") == "assistant":\n    pass',
        'if d["type"] in ("user", "assistant"):\n    pass',
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
        'AGENT=claude; "$AGENT" -p hello',
        'A="copilot"\n${A} --resume "$id"',
        'CLI=claude; $CLI -p x',
        'xargs claude -p',
        'cmux new-workspace --command claude',
        'zsh -lic claude',
        'echo hi | xargs -I{} claude -p {}',
        'A=claude\neval "$A -p hi"',
        'printf "claude -p hi" | sh',
        'BIN=$(command -v claude); "$BIN" -p hi',
        'zsh -l <<EOF\nclaude -p hi\nEOF',
        'if claude -p x; then :; fi',
        'while droid exec --model m; do :; done',
        'until claude --print x; do sleep 1; done',
        'exec "${AGENT:-claude}" -p "$x"',
        "python3 <<PYEOF\nimport subprocess\nsubprocess.run(['claude', '-p', 'x'])\nPYEOF",
        "python3 - <<'PY'\nrun = 'droid exec --model m'\nPY",
        'python3 -c "import subprocess; subprocess.run([\'claude\', \'-p\', \'x\'])"',
    ],
    "js": [
        "spawn('claude', ['-p', prompt])",
        'execFile("copilot", args)',
        "const cmd = `${bin} --allow-all`;",
        'const argv = ["claude", "--print"];',
        'const bin = "claude";\nspawn(bin, ["-p", p]);',
    ],
    "md": ['text\n```bash\nclaude --resume "$S"\n```\n',
           'Run this:\n```\ndroid exec --model x\n```\n',
           '```python\nsubprocess.run(["claude", "-p", "x"])\n```\n'],
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
        'CLAUDE_PATH = HOME / ".claude"',
        'print("Run `claude --resume <id>` to pick it up")',
        'ap.add_argument("--json-schema")',
        'log.warning("droid exec --model failed: %s", err)',
        'parser.add_argument("--x", help="passes --allow-all to copilot")',
        'label = "agent: " + provider',
        'msg = "{} finished".format("claude")',
        'path = os.path.join(HOME, ".claude", "CLAUDE.md")',
        'print("run " + "claude --resume " + sid)',
        'lines.append("- `claude --resume <id>` resumes")',
        'reply("droid exec --help is the docs")',
        'PAT = re.compile(r"--allow-all")',
        'NAMES = ("claude", "-", "copilot")',
        'if rec.get("type") == "result":\n    pass',
        'raise RuntimeError("run claude --resume to recover")',
        'raise RuntimeError("claude --resume failed")',
        'fmt = "the %s field"\nlabel = fmt % "role"',
        'def h():\n    return "Use claude --model opus"',
        'if recent[-1]["role"] == "user":\n    pass',
        'role = agent_session.record_role(d)\nif role == "user":\n    pass',
    ],
    "sh": [
        'command -v copilot >/dev/null',
        'copilot --version',
        '"$CMUX_CLI" hooks copilot install --yes',
        'echo "claude is the default # not a comment"',
        'python3 "$SEAM" agent --agent "$ENGINE"',
        '# claude --print old way',
        'echo "tip: run claude --resume"',
        'grep -- "--dangerously-skip-permissions" ~/.zprofile',
        'printf "%s\\n" "copilot --allow-all is set"',
        "python3 <<PYEOF\nprint('claude is the default agent')\nPYEOF",
        'python3 -c "import json; print(1)"',
        'cat <<EOF\nrun claude --resume by hand\nEOF',
        'HELP="run claude -p to test"',
    ],
    "js": ["// spawn('claude', ['-p'])", "const name = 'claude';"],
    "md": ['Run `claude --resume` by hand if you like.\n',
           '```python\nx = 1\n```\n', '```\n{"a": 1}\n```\n'],
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

    (tmp_path / "extra").mkdir()
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
    logged = _record_log(pulse, monkeypatch)
    assert pulse.dispatch_todo("td-1") is True
    assert any(m.startswith("dispatch td-1 → workspace:9") for m in logged["info"])
    assert not any("unconfirmed" in m for m in logged["warning"])
    spawn = next(c for c in commands if "new-workspace" in c)
    launch = spawn[spawn.index("--command") + 1]
    assert launch.startswith(f"{copilot_bin} --allow-all --no-ask-user --model gpt-test")
    assert ("surface.send_text", "2") in keys, "copilot trust prompt answered, remembered"


def _record_log(module, monkeypatch) -> dict:
    """Replace `module.log` with one that keeps each formatted message."""
    logged = {"info": [], "warning": [], "error": []}

    class Log:
        def __getattr__(self, level):
            return lambda msg, *a, **k: logged.setdefault(level, []).append(msg % a if a else msg)

    monkeypatch.setattr(module, "log", Log())
    return logged


def _dispatch(home, monkeypatch, name, screens, transcript, item=None):
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
    monkeypatch.setattr(pulse, "_load_todo_item", lambda tid: item or {"id": tid, "title": "x"})
    monkeypatch.setattr(pulse, "_mark_todo_dispatched", lambda tid, ws: True)
    monkeypatch.setattr(pulse.time, "sleep", lambda s: None)
    logged = _record_log(pulse, monkeypatch)
    ok = pulse.dispatch_todo("td-3")
    spawn = next(c for c in commands if "new-workspace" in c)
    assert not any("unconfirmed" in m for m in logged["warning"]), logged["warning"]
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
    monkeypatch.setattr(cs, "HOME", home)
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
    assert spawn[spawn.index("--command") + 1].startswith("command copilot --allow-all --no-ask-user --model gpt-test")
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
    assert run("launch-command").stdout.strip() == f"{copilot_bin} --allow-all --no-ask-user --model gpt-test"
    assert run("launch-command", "--agent", "claude").stdout.strip() == "claude"
    assert run("launch-command", "--agent", "claude", "--unattended").stdout.strip() == \
        "zsh -lic 'claude --dangerously-skip-permissions'"
    assert run("launch-command", "--agent", "copilot", "--unattended").stdout.strip() == \
        f"{copilot_bin} --allow-all --no-ask-user --model gpt-test"
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

def test_restore_sessions_leaves_copilot_panels_alone_unless_picked(home):
    _pick(home, "claude")
    restore = _load(BIN / "cmux-restore-sessions.py", "seam_restore_unpicked")
    panel = {"id": "p", "directory": "/repo", "terminal": {
        "surfaceId": "s", "resumeBinding": {},
        "agent": {"kind": "copilot", "sessionId": "cp-9", "workingDirectory": "/repo",
                  "launchCommand": {"arguments": ["/bin/copilot"]}}}}
    assert restore.build_resume_cmd(panel) == (None, None)
    assert restore.infer_ledger_provider(
        {"transcript_path": f"{home}/.copilot/session-state/cp-9/events.jsonl"}) == ""


def test_restore_sessions_resumes_a_copilot_panel_with_copilot(home):
    _pick(home, "copilot")
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
        if state["trusted"] and state["reads"] > 8:
            return "Interactive · Allow All · / commands · ? help"
        return "" if state["reads"] <= 4 else "Do you trust the files in this folder?"

    def rpc(method, params, timeout=15):
        if method == "surface.send_text" and params["text"] == "2":
            state["trusted"] = True
            state["answers"] = state.get("answers", 0) + 1
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
    assert state["trusted"] and state["answers"] == 1, "answered once, though it stayed up"


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
    monkeypatch.setattr(cs, "HOME", home)
    (home / ".architect").mkdir()
    command = cs._warm_launch(ag.COPILOT)
    for d in (cs.REPO_ROOT, home / ".assistant", home / ".architect", "/tmp"):
        assert f"--add-dir {d}" in command, d


def test_screen_reader_flags_a_copilot_error_and_names_copilot_panes():
    bwc = _load(BIN / "build-ws-context.py", "seam_bwc")
    assert bwc._ERROR_LINE_RE.match(" ● API Error: 500 upstream")
    assert not bwc._ERROR_LINE_RE.match(" ● MCP Servers reloaded: 1 server connected")
    assert bwc._agent_for_screen("output\n ← open sidebar · Interactive · Allow") == "copilot"
    trust = ("│  ╰─╯╰─╯  Copilot v1.0.109 uses AI.  │\n"
             "Do you trust the files in this folder?\n 1. Yes")
    assert bwc._agent_for_screen(trust) == "copilot"


def test_screen_reader_keeps_a_claude_pane_that_talks_about_copilot():
    bwc = _load(BIN / "build-ws-context.py", "seam_bwc_claude")
    screen = ("⏺ The banner reads Copilot v1.0.109 uses AI.\n"
              "  ⎿ match: ← open sidebar · Interactive · / commands · ? help\n"
              "⏺ Done.\n\n❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    assert bwc._agent_for_screen(screen) == "claude"


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


CMUX_COPILOT_CONFIG = {"hooks": {
    "SessionStart": [{"hooks": [{"command": "cmux hooks enqueue copilot session-start",
                                 "timeout": 5000, "type": "command"}]}],
    "Stop": [{"hooks": [{"command": "cmux hooks enqueue copilot stop",
                         "timeout": 5000, "type": "command"}]}],
}}


def _fake_cmux_install(calls):
    """Stands in for `cmux hooks copilot install`: it rewrites
    $COPILOT_HOME/config.json, refusing one that isn't plain JSON."""
    def run(cmd, **kw):
        calls.append(cmd)
        config = Path(kw["env"]["COPILOT_HOME"]) / "config.json"
        try:
            doc = json.loads(config.read_text())
        except ValueError:
            return subprocess.CompletedProcess(cmd, 1, "", "config.json is not valid JSON")
        config.write_text(json.dumps({**doc, **CMUX_COPILOT_CONFIG}))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_assistant_llm_set_copilot_installs_cmux_hooks(home, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_hooks")
    calls = []
    monkeypatch.setattr(ag.subprocess, "run", _fake_cmux_install(calls))
    assert cli.main(["set", "claude"]) == 0
    assert calls == []
    assert cli.main(["set", "copilot"]) == 0
    cmux = os.environ.get("CMUX_BIN") or ag.CMUX_CLI
    assert calls == [[cmux, "hooks", "copilot", "install", "--yes"]]
    assert ag.copilot_hooks_installed(home)


def test_copilot_hooks_go_to_the_hooks_folder_and_leave_config_alone(home, monkeypatch):
    config = home / ".copilot" / "config.json"
    config.parent.mkdir(parents=True)
    managed = "// This file is managed automatically.\n{\"trustedFolders\": []}\n"
    config.write_text(managed)
    monkeypatch.setattr(ag.subprocess, "run", _fake_cmux_install([]))
    assert ag.install_copilot_hooks() is None
    assert config.read_text() == managed
    written = json.loads((home / ".copilot" / "hooks" / "cmux.json").read_text())
    assert written == {"version": 1, "hooks": {
        "SessionStart": [{"type": "command", "timeoutSec": 5,
                          "bash": "cmux hooks enqueue copilot session-start"}],
        "Stop": [{"type": "command", "timeoutSec": 5,
                  "bash": "cmux hooks enqueue copilot stop"}],
    }}
    assert ag.copilot_hooks_installed(home)


def test_copilot_hooks_report_a_cmux_that_writes_nothing(home, monkeypatch):
    monkeypatch.setattr(ag.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    assert ag.install_copilot_hooks() == "cmux wrote no Copilot hooks"
    assert not (home / ".copilot" / "hooks" / "cmux.json").exists()


def test_doctor_checks_copilot_when_only_dispatch_picks_it(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor_dispatch")
    monkeypatch.setattr(doctor, "HOME", home)
    _pick(home, "claude", dispatch={"agent": "copilot"})
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: None)
    assert doctor.check_copilot_binary().status == doctor.FAIL


def test_set_droid_saves_a_path_the_daemons_can_run(home, monkeypatch):
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: None)
    assert ag.binary(ag.DROID) == str(home / ".local" / "bin" / "droid")
    assert ag.binary(ag.COPILOT) == "copilot"


def test_droid_launch_reads_home_at_call_time(home, tmp_path, monkeypatch):
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(other))
    assert "--append-system-prompt-file" not in ag.session_argv(ag.DROID)
    (other / ".claude" / "CLAUDE.md").write_text("rules")
    assert "--append-system-prompt-file" in ag.session_argv(ag.DROID)


def test_droid_resume_keeps_one_settings_flag():
    argv = ag.resume_argv(ag.DROID, "d-1", ["--settings=/old.json", "--auto=high", "-v"],
                          home=Path("/h"))
    assert argv.count("--settings") == 1 and not any(a.startswith("--settings=") for a in argv)
    assert "-v" in argv


def test_copilot_home_moves_where_assistant_reads_copilot(home, monkeypatch, tmp_path):
    elsewhere = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(elsewhere))
    target = elsewhere / "session-state" / "s1"
    target.mkdir(parents=True)
    (target / "workspace.yaml").write_text("cwd: /w\n")
    (target / "events.jsonl").write_text("")
    assert ag.copilot_transcripts("/w", home=home) == [target / "events.jsonl"]
    assert ag.transcript_for_session(ag.COPILOT, "s1", home=home) == str(target / "events.jsonl")
    (elsewhere / "config.json").write_text("cmux hooks enqueue copilot stop")
    assert ag.copilot_hooks_installed(home)


def test_spending_reads_copilot_output_as_a_real_successful_run(home):
    import metering
    out = copilot_stdout('{"headline": "x"}')
    assert metering.parse_cli_result(out) == {"tokens_in": 120, "tokens_out": 7, "cost_usd": None}
    usage = metering.observer_usage(out, prompt_chars=10_000, model="")
    assert (usage["tokens_in"], usage["tokens_out"]) == (120, 7)
    assert metering.parse_cli_result(copilot_stdout("", exit_code=1)) is None
    assert metering.parse_cli_usage(copilot_stdout("", exit_code=1))["tokens_in"] == 120


def test_restore_resumes_copilot_with_copilot_when_no_launch_was_recorded(home):
    _pick(home, "copilot")
    restore = _load(BIN / "cmux-restore-sessions.py", "seam_restore_bare")
    panel = {"id": "p", "directory": "/repo", "terminal": {
        "surfaceId": "s", "resumeBinding": {},
        "agent": {"kind": "copilot", "sessionId": "cp-9", "workingDirectory": "/repo"}}}
    assert restore.build_resume_cmd(panel)[1] == "cd /repo && command copilot --resume cp-9"


def test_seam_names_the_agent_behind_a_transcript(home, monkeypatch, tmp_path):
    assert ag.agent_from_transcript(f"{home}/.claude/projects/x/s.jsonl") == ag.CLAUDE
    assert ag.agent_from_transcript(f"{home}/.factory/sessions/x/s.jsonl") == ag.DROID
    assert ag.agent_from_transcript(f"{home}/.copilot/session-state/s/events.jsonl") == ag.COPILOT
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    assert ag.agent_from_transcript(f"{tmp_path}/cp/session-state/s/events.jsonl") == ag.COPILOT
    assert ag.agent_from_transcript("/var/log/x.jsonl") is None


def test_copilot_answers_trust_with_a_lasting_yes():
    assert (ag.trust_answer(ag.CLAUDE), ag.trust_answer(ag.COPILOT)) == ("1", "2")


def test_spawn_skill_types_the_seams_trust_answer():
    block = _skill_block(2)
    assert '"$SEAM" trust-answer --agent "$ENGINE"' in block
    assert '"$TRUST_ANSWER"' in block and '"text":"1"' not in block


@pytest.mark.parametrize("screen", [
    "output\n ◎ Working · 2.6 KiB ctrl+c interrupt",
    "╹▀▀▀\n Interactive · Allow All · @ files/sessions · # issues",
])
def test_screen_reader_names_a_busy_or_typing_copilot_pane(screen):
    assert ag.shows_copilot(screen)


def test_copilot_gets_only_folders_that_exist(tmp_path):
    there = tmp_path / "there"
    there.mkdir()
    argv = ag.session_argv(ag.COPILOT, model="", add_dirs=(there, tmp_path / "missing"))
    assert argv.count("--add-dir") == 1 and str(there) in argv
    cmd = ag.headless_argv(ag.COPILOT, binary_path="/c", model="", run_dir=tmp_path,
                           extra_dirs=[there, tmp_path / "missing"])
    assert str(tmp_path / "missing") not in cmd and str(there) in cmd


def _usage_record(input_tokens, max_tokens=272000):
    return json.dumps({"type": "session.usage_record", "data": {"usage": {
        "inputTokens": input_tokens, "maxPromptTokens": max_tokens}}}) + "\n"


def test_comms_clears_copilot_by_context_use_not_file_size(home, tmp_path):
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(_usage_record(200000) + "x" * 10 + "\n" + _usage_record(20000))
    assert ag.copilot_context_fraction(transcript) == pytest.approx(20000 / 272000)
    assert not cs.should_clear(transcript, agent=ag.COPILOT, droid_clear_bytes=5)
    transcript.write_text(_usage_record(150000))
    assert cs.should_clear(transcript, agent=ag.COPILOT, droid_clear_bytes=10**9)


def test_copilot_lookup_skips_old_sessions_when_given_a_start_time(home):
    cwd = str(home / "dev")
    old = _copilot_session(home, "old", cwd, [])
    os.utime(old, (1, 1))
    new = _copilot_session(home, "new", cwd, [])
    assert ag.copilot_transcripts(cwd, home=home, since=1000) == [new]
    assert sorted(ag.copilot_transcripts(cwd, home=home)) == sorted([old, new])


def test_newest_nvm_copilot_wins(home, monkeypatch):
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: None)
    for version in ("v9.0.0", "v24.1.0", "v18.2.0"):
        _stub(home / ".nvm/versions/node" / version / "bin/copilot")
    assert ag.binary(ag.COPILOT) == str(home / ".nvm/versions/node/v24.1.0/bin/copilot")


def test_comms_answers_copilots_trust_prompt_with_a_lasting_yes(home, monkeypatch):
    _pick(home, "copilot")
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    def run_cmd(cmd, timeout=30):
        if "new-workspace" in cmd:
            return 0, "workspace:311\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:311\n", ""
        return 0, "", ""

    sent, screens = [], iter(["Do you trust the files in this folder?"] * 2)
    monkeypatch.setattr(cl, "run_cmd", run_cmd)
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, method, params, **k: sent.append(
        (method, params.get("text"))) or {})
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: next(
        screens, " ← open sidebar · Interactive · Allow All · / commands · ? help"))
    monkeypatch.setattr(cs, "send_enter", lambda p, ref: None)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    monkeypatch.setattr(cs, "deliver_boot", lambda *a, **k: "/copilot-events.jsonl")
    monkeypatch.setattr(cs, "reconcile_warm_workspaces", lambda p, keep, log=None: None)
    assert cs.spawn_session(paths, Path("/boot.md"))["agent"] == ag.COPILOT
    assert [t for m, t in sent if m == "surface.send_text"] == ["2"]


def test_copilot_hooks_report_a_cmux_that_writes_an_empty_hook_list(home, monkeypatch):
    def run(cmd, **kw):
        (Path(kw["env"]["COPILOT_HOME"]) / "config.json").write_text('{"hooks": {}}')
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(ag.subprocess, "run", run)
    assert ag.install_copilot_hooks() == "cmux wrote no Copilot hooks"


def test_memory_seeds_read_the_repos_own_transcript_folders():
    seeds = _load(BIN / "tools" / "memory_seeds.py", "seam_seeds")
    assert seeds.TRANSCRIPT_DIR == Path.home() / ".claude/projects" / ag.claude_project_slug(REPO)
    assert seeds.DROID_TRANSCRIPT_DIR == Path.home() / ".factory/sessions" / ag.project_slug(REPO)


def _cmux_record(agent, sid, updated):
    import cmux_sessions
    return cmux_sessions.Session(
        session_id=sid, agent=agent, workspace_id="w", surface_id="S", cwd="/tmp/w",
        launch_cwd="/tmp/w", pid=None, transcript_path=None, started_at=1.0,
        updated_at=updated, active_for_surface=True,
        launch_argv=(f"/bin/{agent}",))


@pytest.mark.parametrize("pick,expected", [
    ("claude", ("claude-sess", "claude")), ("copilot", ("copilot-sess", "copilot"))])
def test_restore_ignores_a_hand_run_copilot_record_unless_picked(home, monkeypatch, pick, expected):
    _pick(home, pick)
    restore = _load(BIN / "cmux-restore-sessions.py", f"seam_restore_records_{pick}")
    monkeypatch.setattr(restore.cmux_sessions, "list_sessions", lambda: [
        _cmux_record("claude", "claude-sess", 1.0), _cmux_record("copilot", "copilot-sess", 2.0)])
    monkeypatch.setattr(restore.cmux_sessions, "registry_fallback", lambda reg, recs: [])
    entry = restore.load_recorded()["S"]
    assert (entry["session_id"], entry["provider"]) == expected


def test_spending_keeps_a_claude_stream_out_of_the_copilot_parser():
    claude_stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "subtype": "success", "result": "ok"})])
    assert ag.parse_usage(ag.COPILOT, claude_stream) is None
    import metering
    assert metering.parse_cli_usage(claude_stream) is None


def test_headless_droid_prefers_its_home_install_like_before(home, monkeypatch):
    local = _stub(home / ".local" / "bin" / "droid")
    elsewhere = _stub(home / "brew" / "droid")
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: str(elsewhere))
    assert ag.binary(ag.DROID) == str(local)
    local.unlink()
    assert ag.binary(ag.DROID) == str(elsewhere)


def test_comms_reads_claudes_box_when_a_stray_copilot_glyph_follows():
    screen = "\n".join(["────────────", "❯ my prompt", "────────────",
                        "╻ a quoted glyph", "  some output", "╹ another glyph"])
    assert cs.input_box_text(screen) == "my prompt"


def test_pulse_answers_trust_before_trusting_a_ready_marker_on_the_same_screen(home, copilot_bin, monkeypatch):
    _pick(home, "copilot")
    both = "Do you trust the files in this folder?\n ← open sidebar · Interactive"
    sent = []

    def transcript(cwd, text):
        _copilot_session(home, "cp-both", str(cwd), [
            {"type": "user.message", "data": {"content": text}}])

    pulse_screens = iter([both])
    cwd = home / "dev"
    cwd.mkdir(exist_ok=True)
    monkeypatch.setenv("DISPATCH_CWD", str(cwd))
    pulse = _load(BIN / "pulse.py", "seam_pulse_both")
    pulse.self_update_pulse = lambda pulse_idx: None
    monkeypatch.setattr(pulse, "run", lambda cmd, *a, **k: (
        (0, "OK workspace:9\n", "") if "new-workspace" in cmd else
        (0, "surface:9\n", "") if "list-pane-surfaces" in cmd else (0, "", "")))

    def rpc(method, params, timeout=15):
        sent.append((method, params.get("text")))
        if method == "surface.send_text" and params["text"].startswith("Read "):
            transcript(cwd, params["text"])
        return {}

    monkeypatch.setattr(pulse, "_cmux_rpc", rpc)
    monkeypatch.setattr(pulse, "_surface_read_text", lambda ref, lines=200: next(
        pulse_screens, " ← open sidebar · Interactive · / commands · ? help"))
    monkeypatch.setattr(pulse, "_load_todo_item", lambda tid: {"id": tid, "title": "x"})
    monkeypatch.setattr(pulse, "_mark_todo_dispatched", lambda tid, ws: True)
    monkeypatch.setattr(pulse.time, "sleep", lambda s: None)
    assert pulse.dispatch_todo("td-4") is True
    texts = [t for m, t in sent if m == "surface.send_text"]
    assert texts[0] == "2" and texts[1].startswith("Read ")


def test_pulse_sends_ffp_work_to_claude_even_when_copilot_is_picked(home, copilot_bin, monkeypatch):
    _pick(home, "copilot")
    item = {"id": "td-5", "title": "Fix trim snapping in Squirrel",
            "url": "https://git.corp.adobe.com/Adobe-Firefly/firefly-platform/issues/1"}
    ok, launch = _dispatch(home, monkeypatch, "seam_pulse_ffp", "Claude Code v9",
                           _claude_transcript(home), item=item)
    assert (ok, launch) == (True, "claude")


def test_dispatch_prompt_warns_a_non_claude_agent_off_claude_plugins():
    pulse = _load(BIN / "pulse.py", "seam_pulse_prompt")
    item = {"id": "td-6", "title": "Tidy the README"}
    assert not pulse._needs_claude(item)
    assert "NEEDS CLAUDE" in pulse._build_dispatch_prompt(item, ag.COPILOT)
    assert "NEEDS CLAUDE" not in pulse._build_dispatch_prompt(item, ag.CLAUDE)
    assert pulse._needs_claude({"title": "x", "tags": ["FFP"]})


def _copilot_reply_log(tmp_path, padding_bytes=0):
    events = [
        {"type": "user.message", "timestamp": "t1", "data": {"content": "/merge-when-ready 12"}},
        {"type": "assistant.message", "timestamp": "t2", "data": {"content": "Queued PR 12."}},
        {"type": "model.messages_snapshot", "data": {"blob": "x" * padding_bytes}},
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_transcript_tail_reads_copilots_last_turns(tmp_path):
    tail = _load(BIN / "transcript-tail.py", "seam_transcript_tail")
    user, reply = tail.read_tail(_copilot_reply_log(tmp_path), 200_000)
    assert (user["text"], user["ts"]) == ("/merge-when-ready 12", "t1")
    assert reply["text"] == "Queued PR 12."


def test_dashboard_shows_copilots_latest_reply_past_its_model_log(tmp_path):
    page = _load(BIN / "render-assistant-page.py", "seam_render_page")
    assert page.last_activity(str(_copilot_reply_log(tmp_path, padding_bytes=40_000))) == "Queued PR 12."


@pytest.mark.parametrize("effort,flag", [("high", "high"), ("max", "max"), ("off", "low")])
def test_headless_copilot_runs_at_the_configured_thinking_effort(home, copilot_bin, tmp_path, effort, flag):
    seen = {}
    llm_runner.invoke(provider="copilot", prompt="p", model="", run_dir=tmp_path, timeout=30,
                      run=lambda cmd, **k: seen.update(cmd=cmd) or (0, copilot_stdout("x"), ""),
                      disable_tools=True, reasoning_effort=effort)
    assert seen["cmd"][seen["cmd"].index("--reasoning-effort") + 1] == flag


def test_a_running_copilot_counts_as_an_agent_process(monkeypatch):
    import cmux_sessions
    comm = "/x/@github/copilot-darwin-arm64/copilot\n"
    monkeypatch.setattr(cmux_sessions.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, comm, ""))
    assert cmux_sessions.agent_process(41)
    comm = "/usr/bin/node\n"
    assert not cmux_sessions.agent_process(41)


def test_set_copilot_names_the_jobs_still_pinned_elsewhere(home, capsys, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_pins")
    monkeypatch.setattr(ag.subprocess, "run", _fake_cmux_install([]))
    config = home / ".assistant/comms/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"llm": {"features": {"slack_judge": {"provider": "claude"}}}}))
    assert cli.main(["set", "copilot"]) == 0
    assert "slack_judge stays on claude" in capsys.readouterr().err


def test_the_slack_session_alone_can_run_copilot(home, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_comms")
    monkeypatch.setattr(ag.subprocess, "run", _fake_cmux_install([]))
    assert cli.main(["set", "copilot", "--feature", "comms"]) == 0
    assert (ag.warm_agent(), ag.dispatch_agent()) == (ag.COPILOT, ag.CLAUDE)
    assert cli.main(["set", "local", "--feature", "comms"]) == 2


def test_ffp_routing_leaves_horizon_squirrel_and_droid_alone(home, monkeypatch):
    pulse = _load(BIN / "pulse.py", "seam_pulse_ffp_scope")
    assert not pulse._needs_claude({"title": "Port Horizon squirrel ruler fix in hz-bazel",
                                     "detail": "DVAWV-1234"})
    assert not pulse._needs_claude({"title": "Update the diffpatch tool"})
    assert pulse._needs_claude({"title": "FFP trim bug", "detail": ""})
    assert "NEEDS CLAUDE" not in pulse._build_dispatch_prompt({"id": "t", "title": "x"}, ag.DROID)


def test_droid_keeps_ffp_dispatches_like_before(home, monkeypatch):
    _pick(home, "droid")
    _stub(home / ".local" / "bin" / "droid")
    (home / ".assistant" / "droid-glm-settings.json").write_text("{}")
    monkeypatch.setattr(ag.shutil, "which", lambda *a, **k: str(home / ".local/bin/droid"))
    item = {"id": "td-7", "title": "FFP trim bug"}

    def droid_transcript(cwd, text):
        d = home / ".factory" / "sessions" / ag.project_slug(cwd)
        d.mkdir(parents=True, exist_ok=True)
        (d / "s.jsonl").write_text(json.dumps(
            {"type": "message", "message": {"role": "user", "content": text}}) + "\n")

    ok, launch = _dispatch(home, monkeypatch, "seam_pulse_droid_ffp", "GLM-5.2", droid_transcript,
                           item=item)
    assert ok and launch.startswith("droid --settings")


def test_a_droid_or_claude_pane_with_copilots_banner_in_scrollback_stays_itself():
    bwc = _load(BIN / "build-ws-context.py", "seam_bwc_scrollback")
    banner = "│ ⬢  Copilot v1.0.3 uses AI.  │\n"
    assert bwc._agent_for_screen(banner + "output\n  ? for help  GLM-5.2") == "droid"
    assert bwc._agent_for_screen(banner + "⏺ done\n  ⏵⏵ bypass permissions on") == "claude"


def test_comms_reads_the_box_of_the_agent_it_runs():
    claude_screen = "\n".join(["────────────", "❯ typed", "────────────",
                               "╻▄▄", "┃ todo list item", "╹▀▀"])
    assert cs.input_box_text(claude_screen, ag.CLAUDE) == "typed"
    assert cs.input_box_text(claude_screen, ag.COPILOT) == "todo list item"


def test_comms_types_into_the_session_with_its_agents_box(monkeypatch):
    boxes = []
    monkeypatch.setattr(cs, "submit_until_confirmed", lambda **kw: boxes.append(kw["read_box"]()) or True)
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=60: "\n".join(
        ["────────────", "❯ quoted", "────────────", "╻▄", "┃ live", "╹▀"]))
    cs.submit(None, "surface:1", "hi", "m", lambda: True, agent=ag.CLAUDE)
    cs.submit(None, "surface:1", "hi", "m", lambda: True, agent=ag.COPILOT)
    assert boxes == ["quoted", "live"]


def test_background_claude_runs_the_home_install_or_claude_bin(home, monkeypatch):
    assert ag.binary(ag.CLAUDE) == str(home / ".local" / "bin" / "claude")
    monkeypatch.setenv("CLAUDE_BIN", "/opt/agents/claude")
    assert ag.binary(ag.CLAUDE) == "/opt/agents/claude"


@pytest.mark.parametrize("frame", ["○", "◉", "◎", "●"])
def test_every_copilot_spinner_frame_marks_a_working_pane(frame):
    assert ag.shows_copilot(f"output\n {frame} Working · 1.2 KiB ctrl+c interrupt")
    assert ag.shows_copilot(f"output\n {frame} Retrying model request")
    bwc = _load(BIN / "build-ws-context.py", f"seam_bwc_spin_{ord(frame)}")
    assert bwc._agent_for_screen(
        f"Claude Opus 4.7 is the model\n {frame} Working · 3s ctrl+c interrupt") == "copilot"


def test_headless_copilot_never_updates_itself(tmp_path):
    for tools_off in (True, False):
        cmd = ag.headless_argv(ag.COPILOT, binary_path="/c", model="", run_dir=tmp_path,
                               disable_tools=tools_off)
        assert "--no-auto-update" in cmd


def test_doctors_hook_fix_is_the_one_that_works(home, monkeypatch):
    doctor = _load(BIN / "assistant-doctor.py", "seam_doctor_fix")
    monkeypatch.setattr(doctor, "HOME", home)
    _pick(home, "copilot")
    check = doctor.check_copilot_hooks()
    assert check.remedy == "python3 bin/agent_session.py install-copilot-hooks"
    assert "hooks/cmux.json" in check.detail


def test_prompts_name_copilots_working_signs_and_dont_say_only_claude():
    observer = (REPO / "prompts/observer-batch-prompt.md").read_text()
    assert "ctrl+c interrupt" in observer and "Retrying model request" in observer
    warm = (REPO / "prompts/prompt-assistant-comms-warm.md").read_text()
    assert "warm Claude session" not in warm and "fleet of Claude workspaces" not in warm


def test_the_conversation_slice_agrees_with_the_seam_on_every_agents_roles():
    from assistant import transcript_slice
    records = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}},
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {"type": "message", "message": {"role": "assistant", "content": "yo"}},
        {"type": "user.message", "data": {"content": "hi"}},
        {"type": "assistant.message", "data": {"content": "yo"}},
        {"type": "summary", "summary": "x"},
        {"type": "session.start", "data": {}},
    ]
    for rec in records:
        turn = transcript_slice._turn(rec)
        assert (turn[0] if turn else None) == ag.record_role(rec), rec
    assert gate.ROLE_PARSERS == {"src/assistant/transcript_slice.py"}


def test_spending_and_context_reads_come_from_the_seam(tmp_path):
    import comms_lib
    import metering
    claude = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5,
                         "usage": {"input_tokens": 1, "cache_read_input_tokens": 2,
                                   "cache_creation_input_tokens": 3, "output_tokens": 4}})
    assert metering.parse_cli_result(claude) == {"tokens_in": 6, "tokens_out": 4, "cost_usd": 0.5}
    failed = json.dumps({"type": "result", "subtype": "error", "usage": {"input_tokens": 1}})
    assert metering.parse_cli_result(failed) is None
    assert metering.parse_cli_usage(failed)["tokens_in"] == 1
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}},
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 20, "cache_creation_input_tokens": 1,
                                                    "cache_read_input_tokens": 100}}},
    ]))
    assert comms_lib.read_context_tokens(transcript) == 121


def test_merge_flow_keeps_claudes_notices_as_user_text_like_before(tmp_path):
    tail = _load(BIN / "transcript-tail.py", "seam_tail_notice")
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "/cleanup"}},
        {"type": "attachment", "message": {"role": "user", "content": "Tool loaded."}},
    ]))
    user, _ = tail.read_tail(path, 100_000)
    assert user["text"] == "Tool loaded."
    assert ag.message_role({"type": "user.message", "data": {"content": "x"}}) == "user"


def test_comms_finds_no_claude_box_without_its_rules():
    assert cs.input_box_text("╻▄\n┃ copilot glyphs\n╹▀", ag.CLAUDE) is None
    assert cs.input_box_text("╻▄\n┃ copilot glyphs\n╹▀") == "copilot glyphs"


def test_dispatch_waits_as_long_as_before_for_a_ready_pane():
    pulse = _load(BIN / "pulse.py", "seam_pulse_polls")
    assert pulse.READY_POLLS == 32


@pytest.mark.parametrize("line", [
    '✗ Execution failed: Model "no-such-model-zz" is not available.',
    "✗ API error: 500",
])
def test_screen_reader_flags_copilots_error_banner(line):
    bwc = _load(BIN / "build-ws-context.py", "seam_bwc_err_" + str(abs(hash(line))))
    assert bwc._ERROR_LINE_RE.match(line)


def test_a_copilot_turn_that_ended_in_an_error_reads_as_stopped(tmp_path):
    from assistant import transcript_slice
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_err")
    records = [{"type": "user.message", "timestamp": "t1", "data": {"content": "go"}},
               {"type": "session.error", "timestamp": "t2",
                "data": {"message": 'Model "x" is not available.'}}]
    assert watcher.ended_on_api_error(records)
    assert watcher.ended_on_api_error(records[:1]) is False
    assert transcript_slice._turn(records[1])[2] == '[error] Model "x" is not available.'


def test_dashboard_marks_a_copilot_tool_call_as_working():
    page = _load(BIN / "render-assistant-page.py", "seam_render_working")
    copilot_mid_tool = {"last_assistant": {"text": "Yes."}, "pending_tool_use": True}
    assert page.session_agent_status(copilot_mid_tool) == "working"
    assert page.session_agent_status({"last_assistant": {"text": "[tool_use: Bash]"}}) == "working"
    assert page.session_agent_status({"last_assistant": {"text": "Done."},
                                      "pending_tool_use": False}) == "idle"
    assert page.session_agent_status(None) is None


def test_watcher_snippets_drop_copilots_footer_and_spinner():
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_footer")
    assert watcher._is_tui_noise(" …/work [⎇ main%]  Session: 6.35 AIC used")
    assert watcher._is_tui_noise(" ◎ Working · 112 B ctrl+c interrupt      GPT-6.1 Sol")
    assert watcher._is_tui_noise(" /work [⎇ main]")
    assert not watcher._is_tui_noise(" ● Updated the README [⎇ is a branch glyph]")


def test_copilot_sessions_never_open_a_question_form():
    assert "--no-ask-user" in ag.session_argv(ag.COPILOT, model="")


def test_watcher_titles_a_copilot_session_by_its_own_name(home):
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_title")
    d = home / ".copilot" / "session-state" / "s1"
    d.mkdir(parents=True)
    (d / "workspace.yaml").write_text("cwd: /w\nname: Fix the trim bug\n")
    (d / "events.jsonl").write_text("")
    assert watcher.session_title([], d / "events.jsonl") == "Fix the trim bug"


def test_copilot_usage_adds_up_every_model_in_a_checkpoint():
    events = [
        {"type": "assistant.message", "data": {"content": "x"}},
        {"type": "session.usage_checkpoint", "data": {"providerModelMetrics": [
            {"metrics": {"usage": {"inputTokens": 100, "outputTokens": 1}}},
            {"metrics": {"usage": {"inputTokens": 20, "outputTokens": 3}}}]}},
        {"type": "result", "sessionId": "s", "exitCode": 0},
    ]
    parsed = ag.parse_usage(ag.COPILOT, "\n".join(json.dumps(e) for e in events))
    assert (parsed["tokens_in"], parsed["tokens_out"]) == (120, 4)


def test_copilot_hook_timeouts_carry_over_in_seconds(home, monkeypatch):
    def run(cmd, **kw):
        (Path(kw["env"]["COPILOT_HOME"]) / "config.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"command": "cmux hooks enqueue copilot stop", "timeout": 3000}]}]}}))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(ag.subprocess, "run", run)
    assert ag.install_copilot_hooks() is None
    hooks = json.loads((home / ".copilot/hooks/cmux.json").read_text())["hooks"]
    assert hooks["Stop"][0]["timeoutSec"] == 3


def test_copilot_bar_counts_when_it_isnt_the_last_line():
    assert ag.shows_copilot(" ← open sidebar · Interactive · Allow All\n  GPT-6.1 Sol")


def test_resume_hook_logs_and_exits_cleanly_when_the_seam_wont_load(tmp_path):
    tree = tmp_path / "repo"
    (tree / "hooks").mkdir(parents=True)
    (tree / "bin").mkdir()
    (tree / "hooks" / "cmux-auto-resume.py").write_text(
        (REPO / "hooks" / "cmux-auto-resume.py").read_text())
    (tree / "bin" / "agent_session.py").write_text("def (\n")
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        ["/usr/bin/python3", str(tree / "hooks" / "cmux-auto-resume.py")],
        input=json.dumps({"session_id": "s-1", "cwd": "/w"}), capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "CMUX_SURFACE_ID": "S1"}, timeout=30)
    assert (done.returncode, done.stdout) == (0, "")
    [entry] = [json.loads(l) for l in (home / ".assistant/actions-ledger.jsonl").read_text().splitlines()]
    assert entry["kind"] == "cmux-session-forward-fail"
    assert "agent_session.py failed to load" in entry["evidence"] and "SyntaxError" in entry["evidence"]


@pytest.mark.parametrize("runner", ["evals/fleet/run.py", "evals/observer/run.py"])
def test_eval_fingerprints_cover_the_agent_seam(runner):
    mod = _load(REPO / runner, "seam_eval_" + runner.split("/")[1])
    assert any(str(p).endswith("agent_session.py") for p in mod.qualification_input_hashes())


def _feed_id(kind, sid):
    import base64
    enc = lambda s: base64.b64encode(s.encode()).decode()
    return f"cmux-feed-v1:{enc(kind)}:{enc(sid)}"


def test_watcher_reads_cmuxs_feed_session_ids(home, tmp_path):
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_feed")
    sid = "9d49bf4e-1111-2222-3333-444455556666"
    copilot_dir = tmp_path / "session-state"
    (copilot_dir / sid).mkdir(parents=True)
    events = copilot_dir / sid / "events.jsonl"
    events.write_text("")
    projects = tmp_path / "projects"
    (projects / "-w").mkdir(parents=True)
    claude = projects / "-w" / f"{sid}.jsonl"
    claude.write_text("")
    assert watcher.payload_session(_feed_id("copilot", sid)) == ("copilot", sid)
    assert watcher.transcript_path("/w", _feed_id("copilot", sid), projects, copilot_dir) == events
    assert watcher.transcript_path("/w", _feed_id("claude", sid), projects, copilot_dir) == claude
    assert watcher.transcript_path("/w", f"claude-{sid}", projects, copilot_dir) == claude
    assert watcher.payload_session("cmux-feed-v1:%%%:zz") == (None, None)


def test_copilot_sessions_get_the_users_rules(home):
    assert "COPILOT_CUSTOM_INSTRUCTIONS_DIRS" not in ag.session_command(ag.COPILOT, model="")
    rules = home / ".claude" / "CLAUDE.md"
    rules.write_text("# rules\n")
    folder = home / ".assistant" / "copilot-instructions"
    command = ag.session_command(ag.COPILOT, model="")
    assert command.startswith(f"COPILOT_CUSTOM_INSTRUCTIONS_DIRS={folder} command copilot ")
    link = folder / ".github" / "instructions" / "lessons.instructions.md"
    assert link.is_symlink() and link.resolve() == rules.resolve()
    assert f"&& COPILOT_CUSTOM_INSTRUCTIONS_DIRS={folder} " in ag.resume_command(ag.COPILOT, "s", "/w")
    assert "COPILOT_CUSTOM_INSTRUCTIONS_DIRS" not in ag.session_command(ag.CLAUDE)
    assert ag.copilot_instructions_dir() == folder


def test_comms_copilot_runs_the_model_picked_for_comms(home, monkeypatch):
    monkeypatch.setattr(cs, "HOME", home)
    monkeypatch.setenv("COPILOT_MODEL", "everyone")
    monkeypatch.setenv("COMMS_COPILOT_MODEL", "comms-only")
    assert "--model comms-only" in cs._warm_launch(ag.COPILOT)


def test_picking_copilot_for_the_slack_session_by_env_counts_as_picked(home, monkeypatch):
    _pick(home, "claude")
    assert not ag.copilot_chosen()
    monkeypatch.setenv("ASSISTANT_COMMS_AGENT", "copilot")
    assert ag.copilot_chosen()


def test_a_default_copilot_path_names_copilot_when_copilot_home_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "elsewhere"))
    assert ag.agent_from_transcript("/Users/x/.copilot/session-state/s/events.jsonl") == ag.COPILOT


def test_copilot_block_style_names_read_as_text(tmp_path):
    (tmp_path / "workspace.yaml").write_text("cwd: /w\nname: |-\n  Fix the trim\n  snapping bug\nbranch: main\n")
    assert ag.copilot_session_field(tmp_path, "name") == "Fix the trim\nsnapping bug"
    (tmp_path / "workspace.yaml").write_text("name: >-\n  Fold\n  me\n")
    assert ag.copilot_session_field(tmp_path, "name") == "Fold me"


def test_watcher_snippets_drop_copilots_typing_bar():
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_typing")
    assert watcher._is_tui_noise(" Interactive · Allow All · @ files/sessions · # issues")


def test_status_names_the_agent_the_slack_session_really_runs(home, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_status_comms")
    _pick(home, "local")
    assert cli.status(llm_runner.config_path(home=home))["features"]["comms"]["provider"] == "claude"


def test_saved_copilot_resumes_get_the_users_rules(home):
    saved = "cd -- '/p' 2>/dev/null || [ ! -d '/p' ] && '/n/copilot-darwin-arm64/copilot' '--resume' 'id'"
    assert ag.with_copilot_rules(saved) == saved
    (home / ".claude" / "CLAUDE.md").write_text("# rules\n")
    rules = home / ".assistant" / "copilot-instructions"
    with_rules = ag.with_copilot_rules(saved)
    assert with_rules == saved.replace("&& '/n/", f"&& COPILOT_CUSTOM_INSTRUCTIONS_DIRS={rules} '/n/")
    assert ag.with_copilot_rules(with_rules) == with_rules
    for other in ("cd /w && claude --resume s", "cd /w && droid --resume s --settings /x/copilot.json"):
        assert ag.with_copilot_rules(other) == other


def test_restore_gives_a_saved_copilot_binding_the_users_rules(home):
    _pick(home, "copilot")
    (home / ".claude" / "CLAUDE.md").write_text("# rules\n")
    restore = _load(BIN / "cmux-restore-sessions.py", "seam_restore_rules")
    saved = "cd /repo && '/n/copilot' '--resume' 'cp-9' '--allow-all'"
    panel = {"id": "p", "directory": "/repo", "terminal": {
        "surfaceId": "s", "resumeBinding": {"command": saved, "checkpointId": "cp-9", "cwd": "/repo"},
        "agent": {"kind": "copilot", "sessionId": "cp-9", "workingDirectory": "/repo"}}}
    assert "COPILOT_CUSTOM_INSTRUCTIONS_DIRS=" in restore.build_resume_cmd(panel)[1]


def test_comms_clears_copilot_exactly_at_the_threshold(tmp_path):
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(_usage_record(136000, 272000))
    assert cs.should_clear(transcript, threshold=0.5, agent=ag.COPILOT, droid_clear_bytes=10**9)


def test_watcher_never_reads_a_claude_file_for_a_copilot_feed_id(tmp_path):
    watcher = _load(BIN / "cmux-watcher.py", "seam_watcher_kind_only")
    sid = "9d49bf4e-1111-2222-3333-444455556666"
    projects = tmp_path / "projects"
    (projects / "-w").mkdir(parents=True)
    (projects / "-w" / f"{sid}.jsonl").write_text("")
    assert watcher.transcript_path("/w", _feed_id("copilot", sid), projects, tmp_path / "none") is None


def test_rules_link_is_left_alone_once_right(home):
    (home / ".claude" / "CLAUDE.md").write_text("# rules\n")
    folder = ag.copilot_instructions_dir()
    link = folder / ".github" / "instructions" / "lessons.instructions.md"
    first = link.lstat().st_ino
    ag.copilot_instructions_dir()
    assert link.lstat().st_ino == first


def test_set_copilot_names_a_dispatch_pin(home, capsys, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_dispatch_pin")
    monkeypatch.setattr(ag.subprocess, "run", _fake_cmux_install([]))
    config = home / ".assistant/comms/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"dispatch": {"agent": "claude"}}))
    assert cli.main(["set", "copilot"]) == 0
    assert "task dispatch stays on claude" in capsys.readouterr().err


def test_gate_points_at_the_right_line_of_a_python_fence():
    text = "# Skill\n\nIntro.\n\n```python\nsubprocess.run(['claude', '-p', 'x'])\n```\n"
    assert [line for line, _ in gate.markdown_problems(text)] == [6]
    bash = "# Skill\n\n```bash\nclaude -p x\n```\n"
    assert [line for line, _ in gate.markdown_problems(bash)] == [4]


def test_status_shows_the_slack_sessions_agent_without_job_settings(home, capsys, monkeypatch):
    cli = _load(BIN / "assistant-llm.py", "assistant_llm_status_text")
    _pick(home, "droid")
    assert cli.main(["status"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("comms:"))
    assert line == "comms: droid (interactive session; its own launch settings)"
