"""src/assistant/redact.py keeps tokens out of what services log, and loads on the system Python."""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from assistant import redact as R

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "src/assistant/redact.py"
BODY = "AbCdEfGhIjKlMnOpQrStUv0123456789"  # the secret part: must never survive


B = "AbCdEfGhIjKlMnOpQrStUv0123456789"
# Made-up secrets are built from parts so GitHub's push protection doesn't take them for real ones.
SECRETS = {
    "SLACK_BOT": "-".join(["xoxb", "111111111", "222222222", B]),
    "SLACK_USER": "-".join(["xoxp", "111111111", B]),
    "SLACK_SESSION": "-".join(["xoxc", "111111111", B]),
    "SLACK_APP": "-".join(["xapp", "1", "A0123456789", "9876543210", "0123456789abcdef0123456789abcdef"]),
    "SLACK_ENCODED": "-".join(["xoxb", "1111%2F2222%2B3333", B]),
    "SLACK_PCT_PREFIX": "%2D".join(["xoxb", "1111-" + B]),
    "GHP": "_".join(["ghp", B]),
    "PAT": "_".join(["github", "pat", "11ABCDEFG0", B]),
    "GOOG": ".".join(["ya29", "a0Af_" + B]),
    "REFRESH": "//".join(["1", "0g" + B]),
    "GOCSPX": "-".join(["GOCSPX", "abcdefghijklmnopqrstuvwx"]),
    "AWS": "".join(["AK", "IAIOSFODNN7EXAMPLE"]),
    "ANT": "-".join(["sk", "ant", "api03", B]),
    "SK": "-".join(["sk", B]),
    "JWT": ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "c2lnbmF0dXJlMTIzNDU2"]),
    "WEBHOOK": "/".join(["hooks.slack.com", "services", "T0AAAA", "B0BBBB", "abcdEFGH12345678ijklMNOP"]),
}
CASES = json.loads((REPO / "slack-reactor/test/fixtures/redact-cases.json").read_text())


def expand(text: str) -> str:
    return re.sub(r"\{([A-Z_]+)\}", lambda m: SECRETS[m.group(1)], text)


@pytest.mark.parametrize(("name", "text", "expected"), CASES["redact"], ids=[c[0] for c in CASES["redact"]])
def test_every_secret_shape_is_redacted(name, text, expected):
    assert R.redact(expand(text)) == expand(expected)
    assert R.redact(R.redact(expand(text))) == expand(expected)  # redacting twice changes nothing


@pytest.mark.parametrize("text", CASES["keep"])
def test_look_alikes_and_ordinary_lines_are_unchanged(text):
    assert R.redact(text) == text


def test_the_secret_part_never_survives():
    for name, secret in SECRETS.items():
        out = R.redact(f"x {secret} y")
        assert B not in out and "abcdEFGH12345678ijklMNOP" not in out, name


def test_redact_never_raises_and_never_returns_the_original(monkeypatch):
    assert R.redact(None) == "None"

    class Boom:
        def sub(self, *_):
            raise RuntimeError("boom")

    monkeypatch.setattr(R, "_RULES", ((Boom(), ""),))
    assert R.redact(f"xoxb-{BODY}") == R.WITHHELD
    assert BODY not in R.WITHHELD


ADVERSARIAL_UNITS = [
    "Bearer ", "Bearer a", "authorization: ", "authorization: [ '", "Cookie: ", "http://a:", "https://" + "u" * 200 + ":",
    "hooks.slack.com/services/T/B/", "token=", "token='", "token=\\\"", "api_key=", "password: \"", "client_secret:",
    "A_TOKEN=", "xoxb-", "xoxb-1%2F", "xapp-", "ghp_", "github_pat_", "ya29.", "1//", "AKIA", "GOCSPX-", "sk-ant-",
    "sk-", "sk-" + "0" * 40 + "/", "eyJ", "eyJ" + "a" * 20 + ".", "eyJ" + "a" * 2000 + ".b.", "a" * 64 + " ",
    "token" + " " * 7, "x" * 40 + "-", " ", "\\", "%", "-", "A" * 60 + "_",
]


def test_a_hostile_megabyte_line_is_fast_for_every_rule():
    started = time.monotonic()
    slowest = 0.0
    for unit in ADVERSARIAL_UNITS:
        line = (unit * (1_000_000 // len(unit) + 1))[:1_000_000]
        one = time.monotonic()
        R.redact(line)
        slowest = max(slowest, time.monotonic() - one)
    assert slowest < 2, slowest
    assert time.monotonic() - started < 20


def logger_with_stream(name):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_filter_cleans_percent_args_dict_args_and_literal_percent():
    logger, stream = logger_with_stream("redact.test.args")
    R.install(logger)
    logger.info("call failed for %s with %d%%", f"token={BODY}", 50)
    logger.info("headers %(h)s", {"h": f"Authorization: Bearer {BODY}"})
    logger.info("100%% sure, token=%s", BODY)
    logger.info("bad format %s %s", "only-one")  # logging prints its own error; must not leak or raise
    out = stream.getvalue()
    assert BODY not in out
    assert "call failed for token=<redacted> with 50%" in out
    assert "100% sure, token=<redacted>" in out
    assert "bad format %s %s" in out  # a bad format call keeps its line


def test_filter_cleans_tracebacks_and_stack_text():
    logger, stream = logger_with_stream("redact.test.tb")
    R.install(logger)
    try:
        raise RuntimeError(f"request failed: token={BODY}")
    except RuntimeError:
        logger.exception("boom")
    logger.info("here", stack_info=True)
    out = stream.getvalue()
    assert BODY not in out
    assert "RuntimeError: request failed: token=<redacted>" in out
    assert "Traceback" in out and "Stack (most recent call last)" in out


def test_stack_text_is_cleaned():
    record = logging.LogRecord("n", logging.ERROR, __file__, 1, "m", None, None)
    record.stack_info = f"File x, line 1\n  call(token={BODY})"
    R.RedactingFilter().filter(record)
    assert BODY not in record.stack_info and "token=<redacted>" in record.stack_info


def test_exception_text_already_cached_on_the_record_is_cleaned():
    record = logging.LogRecord("n", logging.ERROR, __file__, 1, "m", None, None)
    record.exc_text = f"Traceback...\nValueError: token={BODY}"
    assert R.RedactingFilter().filter(record) is True
    assert BODY not in record.exc_text


def test_install_covers_child_loggers_and_is_idempotent():
    parent, stream = logger_with_stream("redact.test.parent")
    R.install(parent)
    R.install(parent)
    handler = parent.handlers[0]
    assert sum(isinstance(f, R.RedactingFilter) for f in handler.filters) == 1
    assert sum(isinstance(f, R.RedactingFilter) for f in parent.filters) == 1
    logging.getLogger("redact.test.parent.child").warning("token=%s", BODY)
    assert BODY not in stream.getvalue()
    assert "token=<redacted>" in stream.getvalue()


def test_install_defaults_to_the_root_logger():
    root = logging.getLogger()
    before = list(root.filters)
    try:
        assert R.install() is root
        assert any(isinstance(f, R.RedactingFilter) for f in root.filters)
    finally:
        root.filters[:] = before


def test_a_bad_format_call_keeps_its_line_and_its_traceback():
    logger, stream = logger_with_stream("redact.test.badformat")
    R.install(logger)
    try:
        raise RuntimeError(f"boom token={BODY}")
    except RuntimeError:
        logger.exception("bad %s %s token=" + BODY, "only-one")
    out = stream.getvalue()
    assert BODY not in out
    assert "bad %s %s token=<redacted>" in out and "RuntimeError: boom token=<redacted>" in out


def test_a_filter_failure_withholds_the_text_instead_of_leaking_it():
    record = logging.LogRecord("n", logging.ERROR, __file__, 1, f"token={BODY}", None, (RuntimeError, "not an exception", None))
    record.stack_info = f"token={BODY}"
    assert R.RedactingFilter().filter(record) is True
    assert record.msg == R.WITHHELD and record.exc_info is None and record.stack_info is None


def test_an_uncaught_exception_prints_its_traceback_redacted(capsys):
    try:
        raise RuntimeError(f"failed with token={BODY}")
    except RuntimeError:
        info = sys.exc_info()
    R.install(logging.getLogger("redact.test.hook"))
    sys.excepthook(*info)
    err = capsys.readouterr().err
    assert BODY not in err and "RuntimeError: failed with token=<redacted>" in err and "Traceback" in err


def test_an_uncaught_exception_in_a_thread_prints_its_traceback_redacted(capsys):
    R.install(logging.getLogger("redact.test.hook"))

    def boom():
        raise RuntimeError(f"thread failed token={BODY}")

    thread = threading.Thread(target=boom, name="worker-1")
    thread.start()
    thread.join()
    err = capsys.readouterr().err
    assert BODY not in err and "Exception in thread worker-1" in err and "token=<redacted>" in err

    def leave():
        raise SystemExit(0)

    thread = threading.Thread(target=leave)
    thread.start()
    thread.join()
    assert capsys.readouterr().err == ""


def test_the_hooks_are_installed_once_and_never_chain_the_default(capsys, monkeypatch):
    R.install(logging.getLogger("redact.test.hook"))
    first = (sys.excepthook, threading.excepthook)
    R.install(logging.getLogger("redact.test.hook"))
    assert (sys.excepthook, threading.excepthook) == first
    monkeypatch.setattr(R, "redact", lambda text: (_ for _ in ()).throw(RuntimeError("no")))
    sys.excepthook(RuntimeError, RuntimeError("x"), None)
    assert R.WITHHELD in capsys.readouterr().err


def test_the_module_has_no_possessive_quantifier_or_atomic_group():
    source = MODULE.read_text()
    patterns = [text for text in re.findall(r'r"((?:[^"\\]|\\.)*)"', source)]
    assert patterns, "expected to find the regex literals"
    for pattern in patterns:
        assert not re.search(r"(?<!\\)(?:[*+?]|\})\+", pattern), pattern
        assert "(?>" not in pattern, pattern
    assert not re.search(r"\(\?>", source)
    for rule, _ in R._RULES:
        bare = re.sub(r"\\.", "", rule.pattern)
        bare = re.sub(r"\[(?:\\.|[^\]\\])*\]", "[]", bare)  # a character class, escapes removed
        assert not re.search(r"[*+]|\{\d+,\}", bare), rule.pattern  # every repeat is bounded


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python here")
def test_it_loads_and_redacts_on_the_system_python():
    version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                             capture_output=True, text=True)
    if version.returncode != 0 or version.stdout.strip() != "True":
        pytest.skip("the system Python is not older than 3.11")
    probe = ("import importlib.util, sys\n"
             "s = importlib.util.spec_from_file_location('r', sys.argv[1])\n"
             "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
             "print(m.redact('x Bearer abcdefghijkl1234 token=zzzzzzzz ' + 'xo' + 'xb-1-abcdefgh'))\n")
    run = subprocess.run(["/usr/bin/python3", "-I", "-c", probe, str(MODULE)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "x Bearer <redacted> token=<redacted> xoxb-<redacted>"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_python_and_javascript_rules_are_the_same():
    run = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e",
         "import { SPECS } from './slack-reactor/src/redact.js'; console.log(JSON.stringify(SPECS))"],
        cwd=REPO, capture_output=True, text=True, timeout=30, check=True)
    javascript = [tuple(spec) for spec in json.loads(run.stdout)]
    python = [(pattern, "i" if flags & re.IGNORECASE else "", template) for pattern, flags, template in R._SPECS]
    assert python == javascript


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python here")
def test_the_shared_cases_give_the_same_answers_on_the_system_python():
    version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                             capture_output=True, text=True)
    if version.returncode != 0 or version.stdout.strip() != "True":
        pytest.skip("the system Python is not older than 3.11")
    pairs = [(expand(text), expand(expected)) for _, text, expected in CASES["redact"]] + [(t, t) for t in CASES["keep"]]
    probe = ("import importlib.util, json, sys\n"
             "s = importlib.util.spec_from_file_location('r', sys.argv[1])\n"
             "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
             "print(json.dumps([m.redact(a) for a, _ in json.load(sys.stdin)]))\n")
    run = subprocess.run(["/usr/bin/python3", "-I", "-c", probe, str(MODULE)], input=json.dumps(pairs),
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [expected for _, expected in pairs]
