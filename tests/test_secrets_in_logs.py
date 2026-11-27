"""Each Python service that logs puts redaction on its log, and the Slack reactor's output can't carry a token."""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from assistant import redact

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
@pytest.mark.skipif(sys.platform != "darwin", reason="the reactor takes the task-list lock with a macOS flag")
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


# --- connector scripts -------------------------------------------------------

CONNECTOR_SCRIPTS = ["gcal", "github-notifications", "gmail", "outlook", "slack-events"]


def load_connector_script(name):
    spec = importlib.util.spec_from_file_location("conn_" + name.replace("-", "_"), REPO / "bin/connectors" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", CONNECTOR_SCRIPTS)
def test_connector_script_result_line_is_redacted(name, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    module = load_connector_script(name)
    from assistant import connector
    kinds = [v for v in vars(module).values()
             if isinstance(v, type) and issubclass(v, connector.Connector) and v is not connector.Connector]
    monkeypatch.setattr(kinds[0], "poll_once", lambda self: {"error": f"failed token={SECRET}"})
    assert module.main(["--once"]) == 0
    assert clean(capsys.readouterr().err)
    assert sys.excepthook is redact._excepthook and threading.excepthook is redact._thread_excepthook


@pytest.mark.parametrize("name", ["gcal", "gmail", "outlook"])
def test_connector_script_authorization_failure_is_redacted(name, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    module = load_connector_script(name)
    from assistant import connector

    def refuse(*args, **kwargs):
        raise connector.OAuthError(f"exchange failed token={SECRET}")

    monkeypatch.setattr(module, "authorize", refuse)
    assert module.main(["--authorize", "--client-secrets", "x.json"]) == 1
    assert clean(capsys.readouterr().err)


IMPORT_FROM_TESTS = re.compile(r"""\b(?:from|import|require)\s*\(?\s*['"](?:[^'"]*/)?test/""")


def test_reactor_source_never_imports_from_the_reactor_tests():
    # The gate treats slack-reactor/test/ as test code; source that imported from there would never be measured.
    sources = [p for pattern in ("*.js", "*.mjs", "*.cjs") for p in REACTOR_SRC.rglob(pattern)]
    assert sources
    for source in sorted(sources):
        for number, line in enumerate(source.read_text().splitlines(), 1):
            assert not IMPORT_FROM_TESTS.search(line), f"{source}:{number}: {line.strip()}"


def test_the_import_fence_catches_what_it_is_for():
    for bad in ("import x from '../test/helper.js'", "const x = await import('../test/x.js')", "require('../test/x')",
                "import '../test/x.js'", 'import "./test/side-effect.js"', "export { a } from '../../test/a.js'"):
        assert IMPORT_FROM_TESTS.search(bad), bad
    for good in ("import { redact } from './redact.js'", "import './safe-output-boot.js'", "import t from './latest/x.js'"):
        assert not IMPORT_FROM_TESTS.search(good), good


# --- crash hooks --------------------------------------------------------------

# Every service that redacts its output also redacts an uncaught exception: (file, function that starts it).
WITH_CRASH_HOOKS = [
    ("bin/comms-listen.py", "main"), ("bin/cmux-watcher.py", "main"), ("bin/world-scanner.py", "build"),
    ("bin/todo-server.py", "main"),
    *[(f"bin/connectors/{name}.py", "main") for name in CONNECTOR_SCRIPTS],
]
# Installed through redact.install(), which also puts the filter on the logger's handlers.
WITH_INSTALL = [("bin/pulse.py", "<module>"), ("bin/workspace-watcher.py", "<module>"),
                ("bin/cmux-ws-numberer.py", "<module>"), ("src/assistant/__main__.py", "_setup_logging")]


def calls_in(path, function):
    tree = ast.parse((REPO / path).read_text())
    scope = tree if function == "<module>" else next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    body = scope.body if function != "<module>" else tree.body
    names = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                func = node.func
                names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


@pytest.mark.parametrize(("path", "function"), WITH_CRASH_HOOKS)
def test_service_installs_the_crash_hooks_when_it_starts(path, function):
    assert "install_crash_hooks" in calls_in(path, function)


@pytest.mark.parametrize(("path", "function"), WITH_INSTALL)
def test_service_installs_the_log_filter_and_crash_hooks(path, function):
    assert "install" in calls_in(path, function)


def test_todo_server_main_installs_the_crash_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("todo_server_hooks", REPO / "bin/todo-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Server:
        def __init__(self, *args):
            pass

        def serve_forever(self):
            pass

    monkeypatch.setattr(module, "HTTPServer", Server)
    module.main()
    assert sys.excepthook is redact._excepthook and threading.excepthook is redact._thread_excepthook


def test_the_changelog_names_exactly_the_services_with_crash_hooks():
    named = {"pulse", "comms listener", "cmux watcher", "world scanner", "to-do server", "workspace watcher",
             "connectors", "background service", "numberer"}
    text = (REPO / "CHANGELOG.md").read_text()
    entry = text[text.index("**If the Slack reactor ran"):]
    entry = entry[:entry.index("\n- ", 5)]
    for name in named:
        assert name in " ".join(entry.split()), name


def load_service(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hooks_installed():
    return sys.excepthook is redact._excepthook and threading.excepthook is redact._thread_excepthook


def test_comms_listen_main_installs_the_crash_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_DIR", str(tmp_path / ".assistant"))
    module = load_service("comms_listen_hooks", "comms-listen.py")
    assert module.main() == 1  # no config yet: it says so and stops, after installing the hooks
    assert hooks_installed()


def test_cmux_watcher_main_installs_the_crash_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CMUX_WATCHER_ASSISTANT_DIR", str(tmp_path / ".assistant"))
    module = load_service("cmux_watcher_hooks", "cmux-watcher.py")
    monkeypatch.setattr(module, "cmux_available", lambda: False)
    assert module.main() == 0
    assert hooks_installed()
