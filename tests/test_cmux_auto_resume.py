from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "cmux_auto_resume_test", REPO / "hooks" / "cmux-auto-resume.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_factory_payload_uses_factory_hook_and_droid_fallback(tmp_path):
    module = load_module()
    lessons = tmp_path / ".claude/CLAUDE.md"
    lessons.parent.mkdir(parents=True)
    lessons.write_text("rules")
    payload = {
        "session_id": "factory-session",
        "cwd": "/tmp/project with space",
        "transcript_path": "/Users/test/.factory/sessions/project/session.jsonl",
    }
    env = {
        "HOME": str(tmp_path),
        "CMUX_SURFACE_ID": "surface-1",
        "CMUX_BUNDLED_CLI_PATH": "/bin/cmux",
        "CMUX_AGENT_LAUNCH_EXECUTABLE": "/usr/local/bin/droid",
    }
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch.object(module.subprocess, "run") as run,
    ):
        module.main()

    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == [
        "/bin/cmux", "hooks", "factory", "session-start",
    ]
    fallback = run.call_args_list[1].args[0]
    assert fallback[fallback.index("--kind") + 1] == "factory"
    assert fallback[fallback.index("--name") + 1] == "Factory Droid"
    shell = fallback[fallback.index("--shell") + 1]
    assert "/usr/local/bin/droid" in shell
    assert "--resume factory-session" in shell
    assert "droid-glm-settings.json" in shell
    assert "--auto high" in shell
    assert "--append-system-prompt-file" in shell


def test_claude_payload_uses_claude_hook_and_binding():
    module = load_module()
    payload = {
        "sessionId": "claude-session",
        "cwd": "/tmp/repo",
        "provider": "claude",
    }
    env = {
        "CMUX_SURFACE_ID": "surface-1",
        "CMUX_AGENT_LAUNCH_EXECUTABLE": "claude-custom",
    }
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch.object(module.subprocess, "run") as run,
    ):
        module.main()

    assert run.call_args_list[0].args[0][1:4] == [
        "hooks", "claude", "session-start",
    ]
    fallback = run.call_args_list[1].args[0]
    assert fallback[fallback.index("--kind") + 1] == "claude"
    assert fallback[fallback.index("--name") + 1] == "Claude Code"
    shell = fallback[fallback.index("--shell") + 1]
    assert "claude-custom --resume claude-session" in shell
    assert "droid-glm-settings.json" not in shell


def test_launch_kind_infers_factory_and_wrapper_avoids_fallback():
    module = load_module()
    env = {
        "CMUX_SURFACE_ID": "surface-1",
        "CMUX_AGENT_LAUNCH_KIND": "droid",
        "CMUX_AGENT_LAUNCH_ARGV_B64": "present",
    }
    payload = {"session_id": "sid", "cwd": "/tmp/repo"}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch.object(module.subprocess, "run") as run,
    ):
        module.main()

    run.assert_called_once()
    assert run.call_args.args[0][1:4] == ["hooks", "factory", "session-start"]
