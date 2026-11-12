"""Every coding-agent launch goes through bin/agent_session.py, and each one
runs GitHub Copilot when the user picks it.

Two kinds of test:
  * a source gate that fails when any file outside the seam builds a claude,
    copilot, or droid command line;
  * one test per launch site that picks Copilot in ~/.assistant/comms/config.json
    and checks the site starts Copilot (and starts Claude without the pick).
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

import agent_session as ag
import comms_lib as cl
import comms_session as cs
import llm_runner

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

# Constructs that build an agent command line or pin an agent binary. Only
# bin/agent_session.py may contain them.
FORBIDDEN = [
    (re.compile(r"--dangerously-skip-permissions"), "claude permission flag"),
    (re.compile(r"""(?<!add_argument\()["']--print["']|\bclaude\s+(-p|--print)\b"""), "claude headless flag"),
    (re.compile(r"""["']--json-schema["']|["']--strict-mcp-config["']"""), "claude headless flag"),
    (re.compile(r"--allow-all\b|--available-tools|--yolo\b"), "copilot permission flag"),
    (re.compile(r"""["']exec["'],\s*["']--model["']|--list-tools"""), "droid headless argv"),
    (re.compile(r"""--auto\s+high|["']--auto["'],\s*["']high["']"""), "droid autonomy flag"),
    (re.compile(r"\.local/bin/(claude|droid|copilot)\b"), "hardcoded agent binary"),
    (re.compile(r"\b(CLAUDE|DROID|COPILOT)_BIN\s*=(?!=)"), "per-file agent binary"),
    (re.compile(r"""--command["',\s]+["']?(zsh -lic ')?(claude|droid|copilot)\b"""),
     "hand-written cmux launch"),
    (re.compile(r"""LAUNCH_CMD=["'](zsh -lic ')?(claude|droid|copilot)\b"""), "hand-written cmux launch"),
    (re.compile(r"""\[\s*["'](claude|copilot)["'],\s*["']--"""), "hand-built agent argv"),
]


def _gated_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.split()
    files = []
    for rel in out:
        if rel == "bin/agent_session.py" or rel.startswith(("tests/", "docs/")):
            continue
        if "/reports/" in rel or "/fixtures/" in rel:
            continue
        if rel.endswith((".py", ".sh", ".js", ".mjs", ".ts")) or rel.endswith("SKILL.md") \
                or rel.startswith(("hooks/", "launchagents/")):
            files.append(REPO / rel)
    return files


def _code_only(path: Path, text: str) -> str:
    """`text` with comments and docstrings blanked, so prose that names a flag
    doesn't count; line numbers are kept."""
    lines = text.splitlines()
    if path.suffix == ".py":
        blank: set[int] = set()
        for node in ast.walk(ast.parse(text)):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                blank.update(range(body[0].lineno, body[0].end_lineno + 1))
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
        return "\n".join("" if i + 1 in blank else line for i, line in enumerate(lines))
    if path.suffix == ".sh":
        return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in lines)
    return text


def _violations(text: str) -> list[str]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for pattern, why in FORBIDDEN:
            if pattern.search(line):
                found.append(f"{n}: {why}: {line.strip()[:120]}")
    return found


def test_no_file_outside_the_seam_builds_an_agent_command():
    problems = []
    for path in _gated_files():
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        problems += [f"{path.relative_to(REPO)}:{v}"
                     for v in _violations(_code_only(path, text))]
    assert problems == [], "route these through bin/agent_session.py:\n" + "\n".join(problems)


@pytest.mark.parametrize("snippet", [
    'cmd = ["claude", "--model", model, "--print"]',
    'CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))',
    'LAUNCH_CMD="zsh -lic \'claude --dangerously-skip-permissions\'"',
    'run([cmux, "new-workspace", "--command", "copilot --allow-all"])',
    'cmd = [droid, "exec", "--model", m, "--auto", "high"]',
])
def test_the_gate_catches_a_reintroduced_launch(snippet):
    assert _violations(snippet), snippet


def test_the_gate_scans_every_known_launch_site():
    gated = {str(p.relative_to(REPO)) for p in _gated_files()}
    for rel in ("bin/pulse.py", "bin/comms_session.py", "bin/llm_runner.py",
                "bin/lesson-extractor.py", "bin/narrate-brief.py", "bin/strategist.py",
                "bin/slack-focus.py", "bin/cmux-restore-sessions.py",
                "hooks/cmux-auto-resume.py", "evals/fleet/run.py", "evals/observer/run.py",
                "skills/spawn-claude-workspace/SKILL.md", "install-bootstrap.sh"):
        assert rel in gated


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


def test_pulse_dispatch_uses_claude_without_the_pick(home, monkeypatch):
    _pick(home, "claude")
    assert ag.launch_command(ag.dispatch_agent()) == "claude"


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
    env = {**os.environ, "HOME": str(home)}
    run = lambda *args: subprocess.run([sys.executable, str(BIN / "agent_session.py"), *args],
                                       capture_output=True, text=True, env=env)
    assert run("agent").stdout.strip() == "copilot"
    assert run("launch-command").stdout.strip() == f"{copilot_bin} --allow-all --model gpt-test"
    assert run("launch-command", "--agent", "claude").stdout.strip() == "claude"
    ready = subprocess.run([sys.executable, str(BIN / "agent_session.py"), "is-ready"],
                           input="Allow All · / commands · ? help", capture_output=True,
                           text=True, env=env)
    assert ready.returncode == 0
    cwd = str(home / "dev")
    _copilot_session(home, "skill", cwd, [
        {"type": "user.message", "data": {"content": "Read /p/prompt-1.md in full"}}])
    assert run("submitted", "--cwd", cwd, "--needle", "/p/prompt-1.md").returncode == 0
    assert run("submitted", "--cwd", cwd, "--needle", "/p/prompt-2.md").returncode == 1


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
    assert command == "cd /repo && /bin/copilot --resume cp-9 --model gpt-x --allow-all"
    assert restore.infer_ledger_provider(
        {"transcript_path": f"{home}/.copilot/session-state/cp-9/events.jsonl"}) == "copilot"


def test_resume_hook_binds_a_copilot_resume(home):
    hook = _load(REPO / "hooks" / "cmux-auto-resume.py", "seam_hook")
    assert hook.fallback_resume("copilot", None, "cp-1", "/w") == \
        "cd /w && command copilot --resume cp-1 --allow-all"
    assert hook.fallback_resume("claude", None, "c-1", "/w") == "cd /w && claude --resume c-1"


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
    monkeypatch.setenv("COPILOT_BIN", str(_stub(home / "t" / "copilot")))
    assert doctor.check_copilot_binary().status == doctor.PASS
    assert "copilot binary" in {c.name for c in doctor.run_checks("all")}


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
