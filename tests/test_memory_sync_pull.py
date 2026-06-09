"""Tests for bin/memory-sync-pull.py.

Loaded by file path (it's a hyphenated CLI, not an importable module). Every
test runs against tmp paths — the module-level constants (MEM_CONFIG,
COMMS_CONFIG, SYNC_PULL, LAST_RUN_PATH, HOME, TG_SEND, DISCORD_SEND) are
monkeypatched under tmp_path, and the module's `subprocess.run` is always
replaced with a fake so no real bash / sync-pull.sh / tg-send / discord-send is
ever invoked. The real ~/.assistant is never touched.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_BIN / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load("memory_sync_pull", "memory-sync-pull.py")


# --------------------------------------------------------------------------- fakes


class FakeRun:
    """Records argv of every subprocess.run call and returns a configurable
    CompletedProcess-like result. An optional side_effect callable runs before
    the result is returned (used to mutate the claude_md / memories files so the
    after-counts differ from the before-counts)."""

    def __init__(self, returncode=0, stdout="", stderr="", side_effect=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.side_effect = side_effect
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if self.side_effect is not None:
            self.side_effect()
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point every module-level path constant under tmp_path and stub out
    subprocess.run by default (overridden per-test where the calls matter)."""
    home = tmp_path / "home"
    (home / ".assistant" / "comms").mkdir(parents=True)
    (home / ".assistant" / "mem0").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "MEM_CONFIG", home / ".assistant" / "memory-repo-config.json")
    monkeypatch.setattr(mod, "COMMS_CONFIG", home / ".assistant" / "comms" / "config.json")
    monkeypatch.setattr(mod, "SYNC_PULL", scripts / "sync-pull.sh")
    monkeypatch.setattr(mod, "LAST_RUN_PATH", home / ".assistant" / "memory-sync-pull-last.json")
    monkeypatch.setattr(mod, "TG_SEND", tmp_path / "bin" / "tg-send.py")
    monkeypatch.setattr(mod, "DISCORD_SEND", tmp_path / "bin" / "discord-send.py")

    fake = FakeRun()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    return {
        "home": home,
        "tmp": tmp_path,
        "scripts": scripts,
        "fake": fake,
        "monkeypatch": monkeypatch,
    }


# --------------------------------------------------------------------------- load_config


def test_load_config_valid(env):
    mod.MEM_CONFIG.write_text(json.dumps({"sync": {"pull_interval_seconds": 60}}))
    assert mod.load_config() == {"sync": {"pull_interval_seconds": 60}}


def test_load_config_missing(env):
    assert not mod.MEM_CONFIG.exists()
    assert mod.load_config() == {}


def test_load_config_malformed(env):
    mod.MEM_CONFIG.write_text("{not json")
    assert mod.load_config() == {}


# --------------------------------------------------------------------------- load_comms_config


def test_load_comms_config_valid(env):
    mod.COMMS_CONFIG.write_text(json.dumps({"transport": "discord"}))
    assert mod.load_comms_config() == {"transport": "discord"}


def test_load_comms_config_missing(env):
    mod.COMMS_CONFIG.unlink(missing_ok=True)
    assert mod.load_comms_config() == {}


def test_load_comms_config_malformed(env):
    mod.COMMS_CONFIG.write_text("}}garbage")
    assert mod.load_comms_config() == {}


# --------------------------------------------------------------------------- count_lessons


def test_count_lessons_counts_markers(env):
    f = env["tmp"] / "CLAUDE.md"
    f.write_text("intro\n<!-- lesson: a -->\nmid\n<!-- lesson: b -->\nend\n")
    assert mod.count_lessons(f) == 2


def test_count_lessons_missing(env):
    assert mod.count_lessons(env["tmp"] / "nope.md") == 0


# --------------------------------------------------------------------------- count_memories


def test_count_memories_counts_nonblank(env):
    f = env["tmp"] / "memories.jsonl"
    f.write_text('{"a":1}\n\n   \n{"b":2}\n{"c":3}\n')
    assert mod.count_memories(f) == 3


def test_count_memories_missing(env):
    assert mod.count_memories(env["tmp"] / "nope.jsonl") == 0


# --------------------------------------------------------------------------- send_notification


def test_send_notification_discord_with_channel(env):
    mod.COMMS_CONFIG.write_text(json.dumps({
        "transport": "discord",
        "discord": {"channel_id": 12345},
    }))
    assert mod.send_notification("hi") is True
    assert len(env["fake"].calls) == 1
    argv = env["fake"].calls[0]
    assert str(mod.DISCORD_SEND) in argv
    assert "--channel" in argv and argv[argv.index("--channel") + 1] == "12345"
    assert "--kind" in argv and argv[argv.index("--kind") + 1] == "info"
    assert "--text" in argv and argv[argv.index("--text") + 1] == "hi"


def test_send_notification_discord_no_channel(env):
    mod.COMMS_CONFIG.write_text(json.dumps({
        "transport": "discord",
        "discord": {},
    }))
    assert mod.send_notification("hi") is False
    assert env["fake"].calls == []


def test_send_notification_telegram_with_chat_ids(env):
    mod.COMMS_CONFIG.write_text(json.dumps({
        "transport": "telegram",
        "telegram": {"chat_ids": [1, 2]},
    }))
    assert mod.send_notification("yo") is True
    assert len(env["fake"].calls) == 1
    argv = env["fake"].calls[0]
    assert str(mod.TG_SEND) in argv
    assert "--kind" in argv and argv[argv.index("--kind") + 1] == "info"
    assert "--chat" not in argv


def test_send_notification_default_transport_is_telegram(env):
    # No transport key → defaults to telegram; chat_ids present → sends.
    mod.COMMS_CONFIG.write_text(json.dumps({"telegram": {"chat_ids": [7]}}))
    assert mod.send_notification("yo") is True
    argv = env["fake"].calls[0]
    assert str(mod.TG_SEND) in argv


def test_send_notification_telegram_no_chat_ids(env):
    mod.COMMS_CONFIG.write_text(json.dumps({"transport": "telegram", "telegram": {}}))
    assert mod.send_notification("yo") is False
    assert env["fake"].calls == []


def test_send_notification_no_config_at_all(env):
    # Missing comms config → load_comms_config() == {} → defaults telegram, no
    # chat_ids → False.
    mod.COMMS_CONFIG.unlink(missing_ok=True)
    assert mod.send_notification("yo") is False
    assert env["fake"].calls == []


# --------------------------------------------------------------------------- main(): throttle


def test_main_throttle_skips_sync(env):
    # Recent last-run within the default 3600s interval → skip, no subprocess.
    mod.LAST_RUN_PATH.write_text(json.dumps({"ts": time.time()}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")  # exists, but should not be run
    assert mod.main() == 0
    assert env["fake"].calls == []


def test_main_throttle_respects_config_interval(env):
    # last run 100s ago, configured interval 60s → 100 >= 60 → NOT throttled →
    # proceeds to run sync.
    mod.MEM_CONFIG.write_text(json.dumps({"sync": {"pull_interval_seconds": 60}}))
    mod.LAST_RUN_PATH.write_text(json.dumps({"ts": time.time() - 100}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")
    assert mod.main() == 0
    assert len(env["fake"].calls) == 1  # sync was run


def test_main_throttle_malformed_last_run_proceeds(env):
    # Corrupt last-run file → exception swallowed → not throttled → runs sync.
    mod.LAST_RUN_PATH.write_text("{not json")
    mod.SYNC_PULL.write_text("#!/bin/bash\n")
    assert mod.main() == 0
    assert len(env["fake"].calls) == 1


# --------------------------------------------------------------------------- main(): sync-pull missing


def test_main_sync_pull_missing(env, capsys):
    assert not mod.SYNC_PULL.exists()
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "sync-pull not found" in err
    assert env["fake"].calls == []


# --------------------------------------------------------------------------- main(): sync failure


def test_main_sync_nonzero_returncode(env, monkeypatch):
    mod.SYNC_PULL.write_text("#!/bin/bash\n")
    fake = FakeRun(returncode=7, stderr="boom")
    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert mod.main() == 7
    # last-run still written, with the rc recorded.
    data = json.loads(mod.LAST_RUN_PATH.read_text())
    assert data["rc"] == 7
    assert "ts" in data


def test_main_sync_nonzero_prints_stderr(env, monkeypatch, capsys):
    mod.SYNC_PULL.write_text("#!/bin/bash\n")
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=3, stderr="kaboom"))
    assert mod.main() == 3
    assert "kaboom" in capsys.readouterr().err


# --------------------------------------------------------------------------- main(): success, no new content


def test_main_success_no_new_content(env, monkeypatch):
    # claude_md + memories.jsonl stay constant across the (fake) sync → counts
    # unchanged → returns 0, no notification.
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("<!-- lesson: a -->\n")
    (env["home"] / ".assistant" / "mem0" / "memories.jsonl").write_text('{"x":1}\n')
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0))

    assert mod.main() == 0
    assert sent == []  # nothing new → no ping
    assert json.loads(mod.LAST_RUN_PATH.read_text())["rc"] == 0


# --------------------------------------------------------------------------- main(): success, new content + pluralization


def test_main_one_new_lesson_singular(env, monkeypatch):
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("<!-- lesson: a -->\n")
    memories = env["home"] / ".assistant" / "mem0" / "memories.jsonl"
    memories.write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        claude_md.write_text("<!-- lesson: a -->\n<!-- lesson: b -->\n")  # +1 lesson

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    assert len(sent) == 1
    assert "1 new lesson absorbed" in sent[0]
    assert "lessons" not in sent[0]  # singular
    assert "memor" not in sent[0]  # no memory clause


def test_main_two_new_lessons_plural(env, monkeypatch):
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("")
    (env["home"] / ".assistant" / "mem0" / "memories.jsonl").write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        claude_md.write_text("<!-- lesson: a -->\n<!-- lesson: b -->\n")  # +2

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    assert "2 new lessons absorbed" in sent[0]


def test_main_one_new_memory_singular(env, monkeypatch):
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("")
    memories = env["home"] / ".assistant" / "mem0" / "memories.jsonl"
    memories.write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        memories.write_text('{"a":1}\n')  # +1 memory

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    assert "1 new memory added" in sent[0]
    assert "memories" not in sent[0]  # singular


def test_main_two_new_memories_plural(env, monkeypatch):
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("")
    memories = env["home"] / ".assistant" / "mem0" / "memories.jsonl"
    memories.write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        memories.write_text('{"a":1}\n{"b":2}\n')  # +2

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    assert "2 new memories added" in sent[0]


def test_main_both_lessons_and_memories(env, monkeypatch):
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("")
    memories = env["home"] / ".assistant" / "mem0" / "memories.jsonl"
    memories.write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        claude_md.write_text("<!-- lesson: a -->\n")  # +1 lesson
        memories.write_text('{"a":1}\n{"b":2}\n')  # +2 memories

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    msg = sent[0]
    assert msg.startswith("Memory sync pulled from another machine: ")
    assert "1 new lesson absorbed into CLAUDE.md" in msg
    assert "2 new memories added to the store" in msg
    assert msg.endswith(".")


def test_main_success_new_content_real_send_argv(env, monkeypatch):
    # Let send_notification run for real (transport=discord) and assert the
    # inner subprocess argv carries the pluralized message.
    claude_md = env["tmp"] / "CLAUDE.md"
    claude_md.write_text("")
    (env["home"] / ".assistant" / "mem0" / "memories.jsonl").write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({"stores": {"claude_md": str(claude_md)}}))
    mod.COMMS_CONFIG.write_text(json.dumps({
        "transport": "discord", "discord": {"channel_id": 555},
    }))
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        claude_md.write_text("<!-- lesson: a -->\n")  # +1 lesson

    fake = FakeRun(returncode=0, side_effect=grow)
    monkeypatch.setattr(mod.subprocess, "run", fake)

    assert mod.main() == 0
    # Two subprocess calls: the bash sync, then the discord-send.
    assert len(fake.calls) == 2
    send_argv = fake.calls[1]
    assert str(mod.DISCORD_SEND) in send_argv
    text = send_argv[send_argv.index("--text") + 1]
    assert "1 new lesson absorbed into CLAUDE.md" in text


def test_main_claude_md_fallback_path(env, monkeypatch):
    # No stores.claude_md in config → falls back to HOME/.claude/CLAUDE.md.
    fallback = env["home"] / ".claude" / "CLAUDE.md"
    fallback.write_text("")
    memories = env["home"] / ".assistant" / "mem0" / "memories.jsonl"
    memories.write_text("")
    mod.MEM_CONFIG.write_text(json.dumps({}))  # no stores key
    mod.SYNC_PULL.write_text("#!/bin/bash\n")

    def grow():
        fallback.write_text("<!-- lesson: a -->\n")  # +1 lesson at the fallback path

    sent = []
    monkeypatch.setattr(mod, "send_notification", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(returncode=0, side_effect=grow))

    assert mod.main() == 0
    assert len(sent) == 1  # proves the fallback path was the one counted
    assert "1 new lesson" in sent[0]
