from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "cmux_session_ledger_test", REPO / "hooks" / "cmux-session-ledger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded_argv(*args):
    return base64.b64encode("\0".join(args).encode()).decode()


def test_factory_start_records_provider_agent_pid_and_launch_args(tmp_path):
    module = load_module()
    module.LEDGER = str(tmp_path / "ledger.jsonl")
    module.LOCK = module.LEDGER + ".lock"
    payload = {
        "session_id": "factory-session",
        "cwd": "/tmp/repo",
        "transcript_path": "/Users/test/.factory/sessions/repo/session.jsonl",
    }
    env = {
        "CMUX_DROID_PID": "4321",
        "CMUX_PANEL_ID": "panel-1",
        "CMUX_SURFACE_ID": "surface-1",
        "CMUX_WORKSPACE_ID": "workspace-1",
        "CMUX_AGENT_LAUNCH_ARGV_B64": encoded_argv(
            "droid", "--settings", "/tmp/settings.json", "--auto", "high"
        ),
    }
    identify = mock.Mock(returncode=0, stdout=json.dumps({
        "caller": {"workspace": {"title": "Droid work"}},
    }))
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "argv", ["ledger", "start"]),
        mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch("subprocess.run", return_value=identify),
    ):
        module.main()

    entry = json.loads(Path(module.LEDGER).read_text())
    assert entry["provider"] == "factory"
    assert entry["agent"] == "droid"
    assert entry["agent_pid"] == 4321
    assert entry["pid"] == 4321
    assert entry["launch_argv"][0] == "droid"
    assert entry["ws_title"] == "Droid work"


def test_factory_pid_alias_and_end_event(tmp_path):
    module = load_module()
    module.LEDGER = str(tmp_path / "ledger.jsonl")
    module.LOCK = module.LEDGER + ".lock"
    env = {
        "CMUX_FACTORY_PID": "987",
        "CMUX_AGENT_LAUNCH_EXECUTABLE": "droid",
    }
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "argv", ["ledger", "end"]),
        mock.patch.object(
            module.sys, "stdin", io.StringIO('{"sessionId":"factory-session"}')
        ),
    ):
        module.main()

    entry = json.loads(Path(module.LEDGER).read_text())
    assert entry["event"] == "end"
    assert entry["provider"] == "factory"
    assert entry["agent"] == "droid"
    assert entry["agent_pid"] == 987


def test_claude_pid_remains_supported(tmp_path):
    module = load_module()
    module.LEDGER = str(tmp_path / "ledger.jsonl")
    module.LOCK = module.LEDGER + ".lock"
    env = {"CMUX_CLAUDE_PID": "111", "CMUX_PANEL_ID": "panel-1"}
    payload = {"session_id": "claude-session", "provider": "claude"}
    identify = mock.Mock(returncode=1, stdout="")
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "argv", ["ledger", "start"]),
        mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch("subprocess.run", return_value=identify),
    ):
        module.main()

    entry = json.loads(Path(module.LEDGER).read_text())
    assert entry["provider"] == "claude"
    assert entry["agent"] == "claude"
    assert entry["agent_pid"] == 111
