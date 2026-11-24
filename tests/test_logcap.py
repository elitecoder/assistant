"""src/assistant/logcap.py: cap logs in place, touch nothing else."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from assistant import logcap  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def tree(tmp_path):
    assistant = tmp_path / ".assistant"
    claude = tmp_path / ".claude"
    for d in (assistant / "logs", assistant / "comms", claude / "logs", claude / "cache"):
        d.mkdir(parents=True)
    # An explicit `logs` entry means "trim at once" (no notice period); the
    # notice-period tests remove it.
    (assistant / "config.json").write_text('{"logs": {}}')
    return assistant, claude


def _config(assistant: Path, value) -> None:
    (assistant / "config.json").write_text(value if isinstance(value, str) else json.dumps(value))


def _lines(n_bytes: int) -> bytes:
    line = b"2026-11-23 INFO something happened here\n"
    return line * (n_bytes // len(line) + 1)


def _big(path: Path, size: int = 25 * MB + 13) -> bytes:  # not a whole number of lines
    data = _lines(size)[:size]
    path.write_bytes(data)
    return data


def test_a_25mb_log_is_cut_to_its_last_5mb_and_older_lines_are_gone(tree):
    assistant, claude = tree
    path = assistant / "logs" / "world-scanner.out"
    data = _big(path)
    summary = logcap.cap_logs(assistant, claude)
    kept = path.read_bytes()
    assert summary["capped"] == ["world-scanner.out"]
    assert 4.9 * MB < len(kept) <= 5 * MB
    assert data.endswith(kept)
    assert kept.startswith(b"2026-11-23")               # starts at a line boundary
    assert summary["freed"] == len(data) - len(kept)
    assert summary["keep"] == 5 * MB
    # Trimmed in place: nothing else is written next to it.
    assert sorted(p.name for p in (assistant / "logs").iterdir()) == ["world-scanner.out"]


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


_WRITER = r"""
import os, sys, time
path, stop, ready = sys.argv[1:4]
fd = os.open(path, os.O_WRONLY | os.O_APPEND)
n = 10_000_000
while not os.path.exists(stop):
    os.write(fd, b"C%08d\n" % n)             # one append-mode write per line
    n += 1
    if n == 10_000_050:
        open(ready, "w").close()
    time.sleep(0.0002)
print(n - 1)
"""


def test_a_second_process_appending_while_the_capper_runs_never_corrupts_the_log(tree, tmp_path):
    assistant, claude = tree
    path = assistant / "logs" / "busy.log"
    path.write_bytes(b"".join(b"W%08d\n" % i for i in range(700_000)))     # 6.3 MB
    _config(assistant, {"logs": {"cap_mb": 1, "keep_mb": 0.5}})
    stop, ready = tmp_path / "stop", tmp_path / "ready"
    writer = subprocess.Popen([sys.executable, "-c", _WRITER, str(path), str(stop), str(ready)],
                              stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert logcap.cap_logs(assistant, claude)["capped"] == ["busy.log"]
        time.sleep(0.3)                          # the writer keeps appending afterwards
    finally:
        stop.touch()
        last = int(writer.communicate(timeout=20)[0])
    content = path.read_bytes()
    assert b"\0" not in content
    lines = content.split(b"\n")
    assert lines.pop() == b""                    # the file ends on a whole line
    assert all(re.fullmatch(rb"[WC]\d{8}", line) for line in lines)
    numbers = [int(line[1:]) for line in lines]
    assert numbers == sorted(numbers)            # only whole stretches were lost
    assert numbers[-1] == last                   # later appends land at the end
    assert len(content) < 0.5 * MB + 300_000     # about keep, plus what the writer added


def test_no_dot_1_copy_is_made_and_an_existing_one_is_left_alone(tree):
    assistant, claude = tree
    old = assistant / "logs" / "a.log.1"
    old.write_bytes(b"older rotation\n")
    _big(assistant / "logs" / "a.log")
    logcap.cap_logs(assistant, claude)
    assert sorted(p.name for p in (assistant / "logs").iterdir()) == ["a.log", "a.log.1"]
    assert old.read_bytes() == b"older rotation\n"


def test_a_log_under_the_cap_is_left_alone(tree):
    assistant, claude = tree
    path = assistant / "assistant-pulse.log"
    path.write_bytes(b"x\n" * 100)
    summary = logcap.cap_logs(assistant, claude)
    assert (summary["capped"], summary["freed"], summary["keep"]) == ([], 0, 5 * MB)
    assert path.read_bytes() == b"x\n" * 100


def test_every_listed_location_is_capped(tree):
    assistant, claude = tree
    paths = [assistant / "logs" / "a.launchd.out", assistant / "logs" / "b.err",
             assistant / "daemon.log", assistant / "comms" / "comms-listen.log",
             claude / "logs" / "workspace-watcher.log",
             claude / "cache" / "todo-server.log", claude / "cache" / "todo-server.err"]
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
        assistant / "logs" / "a.log.2": 25 * MB,
        assistant / "actions-ledger.jsonl": 25 * MB,
        assistant / "notes.txt": 25 * MB,
        assistant / "stray.out": 25 * MB,       # .out counts only under logs/
        assistant / "comms" / "other.log": 25 * MB,         # only comms-listen.log is listed
        assistant / "comms" / "messages.jsonl": 25 * MB,
        claude / "logs" / "other.log": 25 * MB,
        claude / "cache" / "other.log": 25 * MB,
        claude / "cache" / "todo-server.json": 25 * MB,
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
    (claude / "cache" / "todo-server.err").symlink_to(target)
    before = {p: p.stat().st_size for p in untouched}
    summary = logcap.cap_logs(assistant, claude)
    assert summary["capped"] == [] and summary["freed"] == 0
    assert {p: p.stat().st_size for p in untouched} == before
    assert target.stat().st_size == 25 * MB + 13


def test_a_symlinked_logs_directory_is_skipped(tree, tmp_path):
    assistant, claude = tree
    real = tmp_path / "elsewhere"
    real.mkdir()
    big = real / "a.log"
    _big(big)
    (assistant / "logs").rmdir()
    (assistant / "logs").symlink_to(real)
    assert logcap.cap_logs(assistant, claude)["capped"] == []
    assert big.stat().st_size == 25 * MB + 13


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


# --- limits come from config.json ---------------------------------------------

def _no_config(assistant):
    (assistant / "config.json").unlink()


def test_the_defaults_apply_without_a_config_file_or_without_a_logs_key(tree):
    assistant, _ = tree
    _no_config(assistant)
    absent = logcap.settings(assistant)
    assert (absent["cap"], absent["keep"], absent["explicit"], absent["problem"]) == (20 * MB, 5 * MB, False, None)
    _config(assistant, {"mode": "pro"})
    assert logcap.settings(assistant) == absent


def test_limits_come_from_the_config_file(tree):
    assistant, _ = tree
    _config(assistant, {"logs": {"cap_mb": 10, "keep_mb": 2.5}})
    got = logcap.settings(assistant)
    assert (got["cap"], got["keep"], got["explicit"], got["problem"]) == (10 * MB, int(2.5 * MB), True, None)
    _config(assistant, {"mode": "pro", "logs": {"cap_mb": 10}})      # keep falls back to its default
    assert logcap.settings(assistant)["keep"] == 5 * MB


def test_a_cap_of_zero_turns_the_capper_off_without_a_complaint(tree):
    assistant, claude = tree
    path = assistant / "a.log"
    _big(path)
    _config(assistant, {"logs": {"cap_mb": 0}})
    summary = logcap.cap_logs(assistant, claude)
    assert summary["capped"] == [] and summary["problem"] is None and summary["notice"] is None
    assert path.stat().st_size == 25 * MB + 13


def test_keep_is_never_larger_than_the_cap(tree):
    assistant, _ = tree
    _config(assistant, {"logs": {"cap_mb": 1, "keep_mb": 9}})
    got = logcap.settings(assistant)
    assert (got["cap"], got["keep"]) == (1 * MB, 1 * MB)


@pytest.mark.parametrize("config, reason", [
    ("{ not json", "config.json isn't valid JSON"),
    ("[1, 2]", "config.json isn't a JSON object"),
    ({"logs": "big"}, "logs in config.json isn't an object"),
    ({"logs": ["cap_mb", 5]}, "logs in config.json isn't an object"),
    ({"logs": {"cap_mb": "0"}}, 'logs.cap_mb in config.json is "0" in quotes, not a number'),
    ({"logs": {"cap_mb": 20, "keep_mb": "2"}}, 'logs.keep_mb in config.json is "2" in quotes, not a number'),
    ({"logs": {"cap_mb": True}}, "logs.cap_mb in config.json is true, not a number"),
    ({"logs": {"cap_mb": None}}, "logs.cap_mb in config.json is null, not a number"),
    ({"logs": {"keep_mb": [1]}}, "logs.keep_mb in config.json is [1], not a number"),
    ({"logs": {"cap_mb": -5}}, "logs.cap_mb in config.json is -5, not a number of 0 or more"),
    ('{"logs": {"cap_mb": NaN}}', "logs.cap_mb in config.json is NaN, not a number of 0 or more"),
    ('{"logs": {"keep_mb": Infinity}}', "logs.keep_mb in config.json is Infinity, not a number of 0 or more"),
])
def test_a_setting_that_cannot_be_read_turns_trimming_off_and_says_why(tree, config, reason):
    assistant, claude = tree
    path = assistant / "a.log"
    _big(path)
    _config(assistant, config)
    got = logcap.settings(assistant)
    assert got["cap"] == 0 and got["problem"] == reason
    summary = logcap.cap_logs(assistant, claude)
    assert summary["capped"] == [] and summary["problem"] == reason and summary["announce"] is True
    assert path.stat().st_size == 25 * MB + 13


def test_a_config_that_exists_but_cannot_be_read_turns_trimming_off(tree):
    assistant, claude = tree
    (assistant / "config.json").unlink()
    (assistant / "config.json").mkdir()
    _big(assistant / "a.log")
    summary = logcap.cap_logs(assistant, claude)
    assert summary["capped"] == []
    assert summary["problem"].startswith("config.json can't be read")


def test_an_unreadable_setting_is_announced_once_per_reason_per_day(tree):
    assistant, claude = tree
    _config(assistant, {"logs": {"cap_mb": "0"}})
    first = logcap.cap_logs(assistant, claude, now=1_000_000)
    again = logcap.cap_logs(assistant, claude, now=1_000_000 + 3600)
    other = (_config(assistant, "{ nope"), logcap.cap_logs(assistant, claude, now=1_000_000 + 3700))[1]
    next_day = logcap.cap_logs(assistant, claude, now=1_000_000 + 86400 + 1)
    assert [first["announce"], again["announce"], other["announce"], next_day["announce"]] == [
        True, False, True, False]            # the first reason isn't the current one any more
    assert again["problem"] == first["problem"]
    _config(assistant, {"logs": {"cap_mb": "0"}})
    assert logcap.cap_logs(assistant, claude, now=1_000_000 + 86400 + 2)["announce"] is True


def test_a_problem_whose_last_announcement_is_in_the_future_is_announced_again(tree):
    assistant, claude = tree
    _config(assistant, "{ nope")
    logcap.cap_logs(assistant, claude, now=5_000_000)
    assert logcap.cap_logs(assistant, claude, now=1_000)["announce"] is True


# --- the notice before the first trim -------------------------------------------

DAY = 24 * 3600


def _unnoticed(tree):
    assistant, claude = tree
    _no_config(assistant)
    return assistant, claude


def test_with_no_logs_entry_the_first_run_trims_nothing_and_gives_a_notice(tree):
    assistant, claude = _unnoticed(tree)
    big = [assistant / "logs" / "a.log", claude / "logs" / "workspace-watcher.log"]
    for p in big:
        _big(p, 22 * MB)
    small = assistant / "logs" / "small.log"
    small.write_bytes(b"x\n")
    summary = logcap.cap_logs(assistant, claude, now=1_000_000)
    assert summary["capped"] == []
    assert summary["notice"] == {"trim_at": 1_000_000 + DAY, "cap": 20 * MB, "keep": 5 * MB,
                                 "names": ["a.log", "workspace-watcher.log"], "bytes": 44 * MB}
    assert all(p.stat().st_size == 22 * MB for p in big)
    state = assistant / "log-cap-state.json"
    assert json.loads(state.read_text())["first_over"] == 1_000_000
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert not list(assistant.glob("*.tmp"))


def test_during_the_notice_period_it_only_reports(tree):
    assistant, claude = _unnoticed(tree)
    path = assistant / "logs" / "a.log"
    _big(path)
    logcap.cap_logs(assistant, claude, now=1_000_000)
    later = logcap.cap_logs(assistant, claude, now=1_000_000 + DAY - 1)
    assert later["capped"] == [] and later["notice"]["trim_at"] == 1_000_000 + DAY
    assert path.stat().st_size == 25 * MB + 13


def test_after_24_hours_it_trims_with_no_further_notice(tree):
    assistant, claude = _unnoticed(tree)
    path = assistant / "logs" / "a.log"
    _big(path)
    logcap.cap_logs(assistant, claude, now=1_000_000)
    summary = logcap.cap_logs(assistant, claude, now=1_000_000 + DAY)
    assert summary["capped"] == ["a.log"] and summary["notice"] is None
    assert path.stat().st_size <= 5 * MB
    _big(path)                                   # it grows again: trimmed at once, no new notice
    again = logcap.cap_logs(assistant, claude, now=1_000_000 + DAY + 300)
    assert again["capped"] == ["a.log"] and again["notice"] is None


def test_an_explicit_logs_entry_skips_the_notice(tree):
    assistant, claude = tree
    _config(assistant, {"mode": "pro", "logs": {"cap_mb": 20}})
    _big(assistant / "a.log")
    summary = logcap.cap_logs(assistant, claude, now=1_000_000)
    assert summary["capped"] == ["a.log"] and summary["notice"] is None
    assert not (assistant / "log-cap-state.json").exists()


def test_nothing_over_the_limit_means_no_notice_and_no_state_file(tree):
    assistant, claude = _unnoticed(tree)
    (assistant / "a.log").write_bytes(b"x\n")
    summary = logcap.cap_logs(assistant, claude, now=1_000_000)
    assert summary["notice"] is None and not (assistant / "log-cap-state.json").exists()


def test_the_notice_clock_survives_a_new_process(tree, tmp_path):
    assistant, claude = _unnoticed(tree)
    _big(assistant / "a.log")
    logcap.cap_logs(assistant, claude, now=1_000_000)
    code = ("import sys; sys.path.insert(0, %r); from assistant import logcap; "
            "r = logcap.cap_logs(%r, %r, now=%d); print(r['notice'] is None, r['capped'])"
            % (str(Path(logcap.__file__).parent.parent), str(assistant), str(claude), 1_000_000 + DAY + 5))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True ['a.log']"


def test_a_clock_set_back_never_postpones_the_trim_past_24_hours(tree):
    assistant, claude = _unnoticed(tree)
    _big(assistant / "a.log")
    logcap.cap_logs(assistant, claude, now=1_000_000)
    # The clock jumps back a week: the recorded time is in the future, so it counts as now.
    back = logcap.cap_logs(assistant, claude, now=1_000_000 - 7 * DAY)
    assert back["notice"]["trim_at"] == 1_000_000 - 7 * DAY + DAY
    assert json.loads((assistant / "log-cap-state.json").read_text())["first_over"] == 1_000_000 - 7 * DAY
    done = logcap.cap_logs(assistant, claude, now=1_000_000 - 6 * DAY)
    assert done["capped"] == ["a.log"]


@pytest.mark.parametrize("junk", ['"yesterday"', "true", "null", "[1]"])
def test_an_unusable_recorded_time_starts_the_notice_over(tree, junk):
    assistant, claude = _unnoticed(tree)
    _big(assistant / "a.log")
    (assistant / "log-cap-state.json").write_text('{"first_over": %s}' % junk)
    assert logcap.cap_logs(assistant, claude, now=2_000_000)["notice"]["trim_at"] == 2_000_000 + DAY


def test_a_state_file_that_is_not_an_object_is_ignored(tree):
    assistant, claude = _unnoticed(tree)
    _big(assistant / "a.log")
    (assistant / "log-cap-state.json").write_text("[1]")
    assert logcap.cap_logs(assistant, claude, now=2_000_000)["notice"] is not None


def test_a_state_file_that_cannot_be_written_never_trims(tree, monkeypatch):
    assistant, claude = _unnoticed(tree)
    path = assistant / "a.log"
    _big(path)
    real = os.replace

    def refuse(src, dst):
        raise OSError("read-only")
    monkeypatch.setattr(logcap.os, "replace", refuse)
    for now in (1_000_000, 1_000_000 + 5 * DAY):
        summary = logcap.cap_logs(assistant, claude, now=now)
        assert summary["capped"] == [] and summary["notice"] is not None
    assert path.stat().st_size == 25 * MB + 13
    assert not list(assistant.glob("*.tmp"))


def test_a_tmp_file_that_cannot_be_removed_after_a_failed_write_is_tolerated(tree, monkeypatch):
    assistant, claude = _unnoticed(tree)
    _big(assistant / "a.log")
    monkeypatch.setattr(logcap.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(logcap.os, "unlink", lambda p: (_ for _ in ()).throw(OSError("busy")))
    assert logcap.cap_logs(assistant, claude, now=1)["notice"] is not None


def test_a_log_that_vanishes_before_it_is_measured_is_not_over(tmp_path):
    assert logcap._size_over(tmp_path / "gone.log", 10) == 0


def test_a_long_last_stretch_with_no_newline_leaves_an_empty_log(tree):
    assistant, claude = tree
    path = assistant / "a.log"
    path.write_bytes(b"y" * (3 * MB))
    _config(assistant, {"logs": {"cap_mb": 1, "keep_mb": 0.5}})
    assert logcap.cap_logs(assistant, claude)["capped"] == ["a.log"]
    assert path.read_bytes() == b""


def test_missing_directories_are_fine(tmp_path):
    assert logcap.cap_logs(tmp_path / "none", tmp_path / "nada")["capped"] == []


def test_cap_logs_never_raises(tree, monkeypatch):
    assistant, claude = tree
    monkeypatch.setattr(logcap, "_candidates", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert logcap.cap_logs(assistant, claude)["capped"] == []


def test_a_directory_that_cannot_be_listed_is_skipped(tree, monkeypatch):
    assistant, claude = tree
    _big(assistant / "a.log")
    real = os.listdir

    def listdir(path):
        if str(path).endswith("logs"):
            raise PermissionError("denied")
        return real(path)
    monkeypatch.setattr(logcap.os, "listdir", listdir)
    assert logcap.cap_logs(assistant, claude)["capped"] == ["a.log"]
