"""Each Python service that logs puts redaction on its log, and the Slack reactor's output can't carry a token."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SECRET = "AbCdEfGhIjKlMnOpQrStUv0123456789"
LINE = f"call failed token={SECRET} Authorization: Bearer xoxb-1-{SECRET}"


def clean(text: str) -> bool:
    return SECRET not in text and "<redacted>" in text


def probe(tmp_path, script: str, body: str) -> str:
    """Import a service in a fresh process under a temporary HOME and run `body` (which prints what to check)."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('svc', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['svc'] = mod\n"
        "spec.loader.exec_module(mod)\n" + body
    )
    env = {**os.environ, "HOME": str(tmp_path), "ASSISTANT_DIR": str(tmp_path / ".assistant"),
           "CMUX_WATCHER_ASSISTANT_DIR": str(tmp_path / ".assistant")}
    run = subprocess.run([sys.executable, "-c", code, str(REPO / script)], capture_output=True, text=True,
                         env=env, timeout=60, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    return run.stdout + run.stderr


def logged(tmp_path, script, logger_name, log_file):
    (tmp_path / ".assistant").mkdir(exist_ok=True)
    probe(tmp_path, script, f"import logging\nlogging.getLogger({logger_name!r}).info({LINE!r})\n"
                            "for h in logging.getLogger().handlers: h.flush()\n")
    return Path(log_file).read_text()


def test_pulse_log(tmp_path):
    assert clean(logged(tmp_path, "bin/pulse.py", "pulse", tmp_path / ".assistant/assistant-pulse.log"))


def test_workspace_watcher_log(tmp_path):
    assert clean(logged(tmp_path, "bin/workspace-watcher.py", "workspace-watcher",
                        tmp_path / ".claude/logs/workspace-watcher.log"))


def test_cmux_ws_numberer_log(tmp_path):
    assert clean(logged(tmp_path, "bin/cmux-ws-numberer.py", "cmux-ws-numberer",
                        tmp_path / ".assistant/logs/cmux-ws-numberer.log"))


def test_daemon_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from assistant import __main__ as entry
    from assistant.config import Config
    config = Config.load(home=tmp_path)
    root = entry._setup_logging(config)
    try:
        logging.getLogger("assistant.daemon").info(LINE)
        for handler in root.handlers:
            handler.flush()
        assert clean(config.log_path.read_text())
    finally:
        root.handlers[:] = []
        root.filters[:] = []


def test_comms_listen_log_writes_neither_file_nor_stderr_with_a_token(tmp_path):
    out = probe(tmp_path, "bin/comms-listen.py", f"mod.log({LINE!r})\n")
    assert clean(out)
    written = next((tmp_path / ".assistant").rglob("comms-listen.log")).read_text()
    assert clean(written)


def test_cmux_watcher_log(tmp_path):
    out = probe(tmp_path, "bin/cmux-watcher.py", f"mod.log({LINE!r})\n")
    assert clean(out)
    assert clean((tmp_path / ".assistant/cmux-watcher.log").read_text())


def test_world_scanner_log(tmp_path):
    probe(tmp_path, "bin/world-scanner.py", f"mod.log({LINE!r})\n")
    assert clean((tmp_path / ".assistant/logs/world-scanner.out").read_text())


def test_todo_server_request_log(tmp_path):
    out = probe(tmp_path, "bin/todo-server.py",
                f"h = mod.Handler.__new__(mod.Handler)\nh.log_message('%s', {LINE!r})\n")
    assert clean(out)


def test_connector_log_lines_and_dry_run_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from assistant import connector
    seen = []
    c = connector.Connector("github", "github", config={}, dry_run=True, log=seen.append)
    c._log(LINE)
    assert clean(seen[0])
    c.emit(connector.build_world_event(source="github", kind="mention", external_id="e1", ts_epoch=1783000000,
                                       actor="a", title="t", snippet=f"pasted token={SECRET}", url="u", refs={}),
           raw={"id": "1"})
    assert clean(capsys.readouterr().out)


def test_connector_without_a_log_callback_still_accepts_log_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from assistant import connector
    connector.Connector("github", "github", config={})._log(LINE)


# --- the Slack reactor -------------------------------------------------------

NODE = shutil.which("node")
REACTOR_SRC = REPO / "slack-reactor/src"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_reactor_node_tests_pass():
    run = subprocess.run([NODE, "--test", *sorted(str(p) for p in (REPO / "slack-reactor/test").glob("*.test.js"))],
                         capture_output=True, text=True, timeout=120, cwd=REPO)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]


ERROR_NAMES = r"(?:e|err|error|ex|exc|exception|reason|cause|rej|failure)"
RAW_ERROR_CALL = re.compile(r"console\s*\.\s*\w+\s*\((?:[^()]*?,\s*)?" + ERROR_NAMES + r"\s*[,)]")


def test_no_reactor_source_prints_a_bare_error_object():
    for source in sorted(REACTOR_SRC.glob("*.js")):
        if source.name == "safe-output.js":
            continue
        for number, line in enumerate(source.read_text().splitlines(), 1):
            assert not RAW_ERROR_CALL.search(line), f"{source.name}:{number} prints a raw error object: {line.strip()}"


def test_the_fence_pattern_catches_what_it_is_for():
    for bad in ("console.error(e)", "console.error('x', err)", "console.warn(error, 1)", "console.log(reason)"):
        assert RAW_ERROR_CALL.search(bad), bad
    for good in ("console.error(`x ${e.message}`)", "console.error(e.message)", "console.error('failed', e.data?.error ?? e.message)"):
        assert not RAW_ERROR_CALL.search(good), good


def test_output_protection_is_the_first_thing_the_reactor_loads():
    imports = [l for l in (REACTOR_SRC / "index.js").read_text().splitlines() if l.startswith("import ")]
    assert imports[0].startswith("import './safe-output-boot.js'")
    assert "installSafeOutput()" in (REACTOR_SRC / "safe-output-boot.js").read_text()
    assert not [l for l in imports if "process.on" in l]
