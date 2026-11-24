"""src/assistant/logcap.py: cap logs in place, touch nothing else."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from assistant import logcap  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.delenv(logcap.CAP_ENV, raising=False)
    monkeypatch.delenv(logcap.KEEP_ENV, raising=False)
    assistant = tmp_path / ".assistant"
    claude = tmp_path / ".claude"
    for d in (assistant / "logs", assistant / "comms", claude / "logs"):
        d.mkdir(parents=True)
    return assistant, claude


def _lines(n_bytes: int) -> bytes:
    line = b"2026-11-23 INFO something happened here\n"
    return line * (n_bytes // len(line) + 1)


def _big(path: Path, size: int = 25 * MB + 13) -> bytes:  # not a whole number of lines
    data = _lines(size)[:size]
    path.write_bytes(data)
    return data


def test_a_25mb_log_is_cut_to_its_last_5mb_and_the_tail_moves_to_dot_1(tree):
    assistant, claude = tree
    path = assistant / "logs" / "world-scanner.out"
    data = _big(path)
    summary = logcap.cap_logs(assistant, claude)
    kept = path.read_bytes()
    assert summary["capped"] == ["world-scanner.out"]
    assert 4.9 * MB < len(kept) <= 5 * MB
    assert data.endswith(kept)
    assert kept.startswith(b"2026-11-23")               # starts at a line boundary
    assert (assistant / "logs" / "world-scanner.out.1").read_bytes() == kept
    assert summary["freed"] == len(data) - len(kept)
    assert oct((assistant / "logs" / "world-scanner.out.1").stat().st_mode & 0o777) == "0o600"


def test_an_open_append_descriptor_keeps_working_and_lands_at_the_end(tree):
    assistant, claude = tree
    path = assistant / "logs" / "x.launchd.err"
    _big(path)
    writer = open(path, "ab")
    writer.write(b"before\n")
    writer.flush()
    logcap.cap_logs(assistant, claude)
    writer.write(b"after the cap\n")
    writer.close()
    content = path.read_bytes()
    assert content.endswith(b"before\nafter the cap\n")
    assert len(content) < 5 * MB + 100
    assert b"\0" not in content                      # no hole from a stale offset


def test_an_append_between_the_read_and_the_truncate_never_corrupts(tree, monkeypatch):
    assistant, claude = tree
    path = assistant / "logs" / "a.log"
    _big(path, 6 * MB)
    monkeypatch.setenv(logcap.CAP_ENV, "1")
    monkeypatch.setenv(logcap.KEEP_ENV, "0.5")
    real_replace = os.replace
    writer = open(path, "ab")

    def racing_replace(src, dst):
        writer.write(b"raced line\n")                # lands after our read
        writer.flush()
        return real_replace(src, dst)
    monkeypatch.setattr(logcap.os, "replace", racing_replace)
    logcap.cap_logs(assistant, claude)
    monkeypatch.undo()
    writer.write(b"later line\n")
    writer.close()
    content = path.read_bytes()
    assert content.endswith(b"later line\n")
    assert b"\0" not in content
    assert all(line.startswith((b"2026", b"later", b"raced")) or line == b""
               for line in content.split(b"\n"))


def test_a_log_under_the_cap_is_left_alone(tree):
    assistant, claude = tree
    path = assistant / "assistant-pulse.log"
    path.write_bytes(b"x\n" * 100)
    assert logcap.cap_logs(assistant, claude) == {"capped": [], "freed": 0}
    assert path.read_bytes() == b"x\n" * 100
    assert not (assistant / "assistant-pulse.log.1").exists()


def test_every_listed_location_is_capped(tree):
    assistant, claude = tree
    paths = [assistant / "logs" / "a.launchd.out", assistant / "logs" / "b.err",
             assistant / "daemon.log", assistant / "comms" / "comms-listen.log",
             claude / "logs" / "workspace-watcher.log"]
    for p in paths:
        _big(p, 22 * MB)
    summary = logcap.cap_logs(assistant, claude)
    assert sorted(summary["capped"]) == sorted(p.name for p in paths)
    for p in paths:
        assert p.stat().st_size <= 5 * MB


def test_nothing_else_is_ever_touched(tree, tmp_path):
    assistant, claude = tree
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "big.log"
    _big(target)
    untouched = {
        assistant / "logs" / "events.jsonl": 25 * MB,
        assistant / "logs" / "state.json": 25 * MB,
        assistant / "logs" / "pulse.lock": 25 * MB,
        assistant / "logs" / "a.log.1": 25 * MB,
        assistant / "actions-ledger.jsonl": 25 * MB,
        assistant / "notes.txt": 25 * MB,
        assistant / "stray.out": 25 * MB,       # .out counts only under logs/
        assistant / "comms" / "other.log": 25 * MB,         # only comms-listen.log is listed
        assistant / "comms" / "messages.jsonl": 25 * MB,
        claude / "logs" / "other.log": 25 * MB,
        claude / "settings.log": 25 * MB,
        assistant / "logs" / "subdir" / "deep.log": 25 * MB,
        assistant / "subdir" / "deep.log": 25 * MB,
    }
    for p, size in untouched.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        _big(p, size)
    (assistant / "logs" / "dir.log").mkdir()
    (assistant / "logs" / "link.log").symlink_to(target)
    (assistant / "link2.log").symlink_to(target)
    (claude / "logs" / "workspace-watcher.log").symlink_to(target)
    before = {p: p.stat().st_size for p in untouched}
    summary = logcap.cap_logs(assistant, claude)
    assert summary == {"capped": [], "freed": 0}
    assert {p: p.stat().st_size for p in untouched} == before
    assert target.stat().st_size == 25 * MB + 13
    assert not (outside / "big.log.1").exists()


def test_a_symlink_swapped_in_after_the_check_is_refused(tree, tmp_path, monkeypatch):
    assistant, claude = tree
    target = tmp_path / "victim.log"
    _big(target)
    link = assistant / "logs" / "a.log"
    _big(link)
    real_lstat = os.lstat

    def swap(path, *a, **k):
        result = real_lstat(path, *a, **k)
        if str(path) == str(link) and not link.is_symlink():
            link.unlink()
            link.symlink_to(target)
        return result
    monkeypatch.setattr(logcap.os, "lstat", swap)
    assert logcap.cap_logs(assistant, claude)["capped"] == []
    assert target.stat().st_size == 25 * MB + 13


def test_a_non_regular_file_opened_after_the_check_is_left_alone(tree, monkeypatch):
    assistant, claude = tree
    _big(assistant / "a.log")
    monkeypatch.setattr(logcap.os, "fstat", lambda fd: os.stat_result((0o040755,) + (0,) * 9))
    assert logcap.cap_logs(assistant, claude)["capped"] == []


def test_one_unreadable_file_does_not_stop_the_others(tree, monkeypatch):
    assistant, claude = tree
    bad = assistant / "logs" / "a.log"
    good = assistant / "logs" / "b.log"
    _big(bad)
    _big(good)
    real_open = os.open

    def open_(path, *a, **k):
        if str(path) == str(bad):
            raise PermissionError("denied")
        return real_open(path, *a, **k)
    monkeypatch.setattr(logcap.os, "open", open_)
    summary = logcap.cap_logs(assistant, claude)
    assert summary["capped"] == ["b.log"]
    assert bad.stat().st_size == 25 * MB + 13


def test_a_failed_archive_write_leaves_the_log_whole_and_no_temp_file(tree, monkeypatch):
    assistant, claude = tree
    path = assistant / "a.log"
    data = _big(path)
    monkeypatch.setattr(logcap.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    assert logcap.cap_logs(assistant, claude)["capped"] == []
    assert path.read_bytes() == data
    assert not (assistant / "a.log.1.tmp").exists()


def test_a_leftover_temp_file_that_cannot_be_removed_does_not_hide_the_error(tree, monkeypatch):
    assistant, claude = tree
    _big(assistant / "a.log")
    monkeypatch.setattr(logcap.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    monkeypatch.setattr(logcap.os, "unlink", lambda p: (_ for _ in ()).throw(OSError("busy")))
    assert logcap.cap_logs(assistant, claude)["capped"] == []


def test_a_cap_of_zero_turns_the_capper_off(tree, monkeypatch):
    assistant, claude = tree
    path = assistant / "a.log"
    _big(path)
    monkeypatch.setenv(logcap.CAP_ENV, "0")
    assert logcap.cap_logs(assistant, claude) == {"capped": [], "freed": 0}
    assert path.stat().st_size == 25 * MB + 13


def test_limits_come_from_the_environment_and_bad_values_fall_back(tree, monkeypatch):
    assert logcap.limits() == (20 * MB, 5 * MB)
    monkeypatch.setenv(logcap.CAP_ENV, "10")
    monkeypatch.setenv(logcap.KEEP_ENV, "2.5")
    assert logcap.limits() == (10 * MB, int(2.5 * MB))
    monkeypatch.setenv(logcap.CAP_ENV, "lots")
    monkeypatch.setenv(logcap.KEEP_ENV, "-3")
    assert logcap.limits() == (20 * MB, 5 * MB)
    monkeypatch.setenv(logcap.CAP_ENV, "1")
    monkeypatch.setenv(logcap.KEEP_ENV, "9")
    assert logcap.limits() == (1 * MB, 1 * MB)          # keep never exceeds the cap


def test_a_long_last_stretch_with_no_newline_leaves_an_empty_log(tree, monkeypatch):
    assistant, claude = tree
    path = assistant / "a.log"
    path.write_bytes(b"y" * (3 * MB))
    monkeypatch.setenv(logcap.CAP_ENV, "1")
    monkeypatch.setenv(logcap.KEEP_ENV, "0.5")
    assert logcap.cap_logs(assistant, claude)["capped"] == ["a.log"]
    assert path.read_bytes() == b""


def test_missing_directories_are_fine(tmp_path):
    assert logcap.cap_logs(tmp_path / "none", tmp_path / "nada") == {"capped": [], "freed": 0}


def test_cap_logs_never_raises(tree, monkeypatch):
    assistant, claude = tree
    monkeypatch.setattr(logcap, "_candidates", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert logcap.cap_logs(assistant, claude) == {"capped": [], "freed": 0}
