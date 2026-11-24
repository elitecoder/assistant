"""src/assistant/redact.py keeps tokens out of what services log, and loads on the system Python."""
from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assistant import redact as R

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "src/assistant/redact.py"
BODY = "AbCdEfGhIjKlMnOpQrStUv0123456789"  # the secret part: must never survive


@pytest.mark.parametrize(("text", "expected"), [
    (f"xoxb-111-222-{BODY}", "xoxb-<redacted>"),
    (f"xoxp-111-{BODY}", "xoxp-<redacted>"),
    (f"xoxc-111-{BODY}", "xoxc-<redacted>"),
    (f"app xapp-1-A0123-{BODY} up", "app xapp-<redacted> up"),
    (f"ghp_{BODY}", "ghp_<redacted>"),
    (f"gho_{BODY}", "gho_<redacted>"),
    (f"github_pat_11ABCDEFG0_{BODY}", "github_pat_<redacted>"),
    (f"ya29.a0Af_{BODY}", "ya29.<redacted>"),
    (f"refresh 1//0g{BODY}", "refresh 1//<redacted>"),
    ("key AKIAIOSFODNN7EXAMPLE end", "key AKIA<redacted> end"),
    ("key ASIAIOSFODNN7EXAMPLE end", "key ASIA<redacted> end"),
    (f"sk-ant-api03-{BODY}", "sk-ant-<redacted>"),
    (f"sk-{BODY}", "sk-<redacted>"),
    ("Bearer abcdefghijkl1234", "Bearer <redacted>"),
    ("bearer abcdefghijkl1234", "bearer <redacted>"),
    ("Authorization: Basic dXNlcjpwYXNz", "Authorization: <redacted>"),
    ("Authorization: opaque-value-1", "Authorization: <redacted>"),
    ("'Authorization': 'Bearer abcdefghijkl1234',", "'Authorization': '<redacted>',"),
    ("Authorization: Bearer xoxb-1-abcdefgh\\r\\nHost: x", "Authorization: <redacted>\\r\\nHost: x"),
    ("GET /x?token=abc123&y=1", "GET /x?token=<redacted>&y=1"),
    ("access_token=abc&refresh_token=def&client_secret=ghi", "access_token=<redacted>&refresh_token=<redacted>&client_secret=<redacted>"),
    ('{"access_token": "abc", "refresh_token":"def", "ok": true}', '{"access_token": "<redacted>", "refresh_token":"<redacted>", "ok": true}'),
    ("data: 'token=xoxb-1-abcdefgh',", "data: 'token=<redacted>',"),
    ("csrf_token=zzz", "csrf_token=<redacted>"),
])
def test_every_token_shape_is_redacted(text, expected):
    assert R.redact(text) == expected
    assert BODY not in R.redact(text)


@pytest.mark.parametrize("text", [
    "the token expired; request a new token",
    "token: expired",
    "commit 3f786850e387550fdab836ed7e6dc881de23001b",
    "id 123e4567-e89b-12d3-a456-426614174000",
    "https://example.com/a/b.html?x=1&y=2#frag",
    "max_tokens=100 input_tokens=5",
    "risk-assessment-for-the-quarterly-planning-document-2026",
    "task-runner-configuration-for-the-nightly-build-job",
    "see http://host//1//abcdefghijklmnopqrstuvwxyz",
    "AKIA is a prefix; so is xoxb",
    "Bearer of bad news",
    "2026-01-02 12:00:00 pulse INFO resumed 3 workspaces",
])
def test_look_alikes_and_ordinary_lines_are_unchanged(text):
    assert R.redact(text) == text


def test_redact_never_raises_and_never_returns_the_original(monkeypatch):
    assert R.redact(None) == "None"

    class Boom:
        def sub(self, *_):
            raise RuntimeError("boom")

    monkeypatch.setattr(R, "_RULES", ((Boom(), ""),))
    assert R.redact(f"xoxb-{BODY}") == R.WITHHELD
    assert BODY not in R.WITHHELD


def test_a_hostile_megabyte_line_is_fast():
    started = time.monotonic()
    for unit in ("Bearer ", "token=", "xoxb-", "authorization: ", "a" * 64 + " ", "token" + " " * 7, "sk-", "1//", "x" * 40 + "-"):
        R.redact((unit * (1_000_000 // len(unit) + 1))[:1_000_000])
    assert time.monotonic() - started < 10


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
    assert out.count("withheld") == 1


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


def test_a_filter_failure_withholds_the_text_instead_of_leaking_it():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    record = logging.LogRecord("n", logging.ERROR, __file__, 1, "%s", (Unprintable(),), (RuntimeError, RuntimeError("x"), None))
    record.stack_info = f"token={BODY}"
    assert R.RedactingFilter().filter(record) is True
    assert record.msg == R.WITHHELD and record.exc_info is None and record.stack_info is None


def test_the_module_has_no_possessive_quantifier_or_atomic_group():
    source = MODULE.read_text()
    patterns = [text for text in re.findall(r'r"((?:[^"\\]|\\.)*)"', source)]
    assert patterns, "expected to find the regex literals"
    for pattern in patterns:
        assert not re.search(r"(?<!\\)(?:[*+?]|\})\+", pattern), pattern
        assert "(?>" not in pattern, pattern
    assert not re.search(r"\(\?>", source)
    for rule, _ in R._RULES:
        assert not re.search(r"[*+]\s*$|\{\d+,\}", rule.pattern), rule.pattern  # every repeat is bounded


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python here")
def test_it_loads_and_redacts_on_the_system_python():
    version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                             capture_output=True, text=True)
    if version.returncode != 0 or version.stdout.strip() != "True":
        pytest.skip("the system Python is not older than 3.11")
    probe = ("import importlib.util, sys\n"
             "s = importlib.util.spec_from_file_location('r', sys.argv[1])\n"
             "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
             "print(m.redact('x Bearer abcdefghijkl1234 token=zzz xoxb-1-abcdefgh'))\n")
    run = subprocess.run(["/usr/bin/python3", "-I", "-c", probe, str(MODULE)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "x Bearer <redacted> token=<redacted> xoxb-<redacted>"
