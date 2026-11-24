"""Self-update counts a repeating failure and stops retrying it blindly."""
import logging
import sys

import pytest

from assistant import failwatch
from test_self_update import advance_remote, git, make_repos, su

NOW = 1_700_000_000
HOUR = 3600


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A clone whose remote is gone, a marker file, and a count of fetches."""
    clone, remote = make_repos(tmp_path)
    good_url = git(clone, "remote", "get-url", "origin")
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    fetches = []
    real = su._git

    def counting(repo, *args, **kw):
        if args[0] == "fetch":
            fetches.append(args)
        return real(repo, *args, **kw)

    monkeypatch.setattr(su, "_git", counting)

    class World:
        pass

    w = World()
    w.clone, w.remote, w.good_url, w.fetches, w.tmp = clone, remote, good_url, fetches, tmp_path
    w.marker = tmp_path / "m.json"
    w.run = lambda now, **kw: su.maybe_update(clone, interval_sec=0, marker_path=w.marker, now=now, **kw)
    return w


def test_three_identical_fetch_failures_pause_the_fourth_attempt(world, caplog):
    for i in range(3):
        assert world.run(NOW + i)["skipped_reason"] == "fetch-failed"
    assert len(world.fetches) == 3
    (row,) = failwatch.repeated(now=NOW + 10)
    assert row["count"] == 3 and row["component"] == "self-update"

    log = logging.getLogger("t")
    with caplog.at_level(logging.INFO, logger="t"):
        assert world.run(NOW + 600, log=log) is None
    assert len(world.fetches) == 3, "a paused attempt must not touch the network"
    assert "paused after repeating the same failure" in caplog.text


def test_after_the_window_it_tries_again_and_a_success_clears_the_pause(world):
    for i in range(3):
        world.run(NOW + i)
    assert world.run(NOW + HOUR + 2)["skipped_reason"] == "fetch-failed"  # window of one hour is over
    assert len(world.fetches) == 4
    assert failwatch.backed_off("self-update", now=NOW + HOUR + 3) == NOW + HOUR + 2 + 2 * HOUR  # doubled

    git(world.clone, "remote", "set-url", "origin", world.good_url)
    assert world.run(NOW + 4 * HOUR)["skipped_reason"] is None
    assert failwatch.backed_off("self-update", now=NOW + 4 * HOUR + 1) is None
    assert failwatch.history()[0]["streak"] == 0


def test_force_ignores_the_pause(world):
    for i in range(3):
        world.run(NOW + i)
    assert world.run(NOW + 600, force=True)["skipped_reason"] == "fetch-failed"
    assert len(world.fetches) == 4


def test_the_pause_is_stamped_so_the_hourly_throttle_still_applies(world):
    for i in range(3):
        world.run(NOW + i)
    world.run(NOW + 600)
    assert su._read_marker(world.marker)["last_attempt_ts"] == NOW + 600


def test_a_refused_commit_counts_once_and_the_quiet_repeat_does_not(tmp_path):
    clone, remote = make_repos(tmp_path)
    advance_remote(tmp_path, remote, {"bin/broken.py": "def (:\n"}, "broken")
    marker = tmp_path / "m.json"
    first = su.maybe_update(clone, interval_sec=0, marker_path=marker, now=NOW)
    assert first["skipped_reason"] == "syntax-fail"
    second = su.maybe_update(clone, interval_sec=0, marker_path=marker, now=NOW + 60)
    assert second["skipped_reason"] == "syntax-fail-known"
    (row,) = failwatch.repeated(now=NOW + 100, min_count=1)
    assert row["count"] == 1 and "broken.py" in row["sample"]
    assert row["streak"] == 1, "a quiet repeat of a refused commit must not look like a success"


def test_a_failed_install_counts(tmp_path):
    clone, remote = make_repos(tmp_path)
    advance_remote(tmp_path, remote, {"skills/a/SKILL.md": "# a\n"}, "skill")
    result = su.maybe_update(clone, interval_sec=0, marker_path=tmp_path / "m.json", now=NOW,
                             install_sh=tmp_path / "missing-install.sh")
    assert "install.sh not found" in result["error"]
    (row,) = failwatch.repeated(now=NOW + 1, min_count=1)
    assert "install.sh not found" in row["sample"]


def test_a_failure_with_no_counter_available_still_returns_the_result(world, monkeypatch):
    monkeypatch.setattr(su, "_failwatch", lambda: None)
    for i in range(4):
        assert world.run(NOW + i)["skipped_reason"] == "fetch-failed"
    assert len(world.fetches) == 4


def test_the_counter_is_loaded_even_when_src_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if not p.endswith("/src")])
    assert su._failwatch() is failwatch
    assert any(p.endswith("/src") for p in sys.path)


def test_an_unloadable_counter_is_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "assistant.failwatch", None)
    monkeypatch.delattr(sys.modules["assistant"], "failwatch")
    assert su._failwatch() is None


def test_the_pause_never_runs_past_four_hours(world):
    for i in range(10):
        world.run(NOW + i * 5 * HOUR)  # each try after the last pause ended
    (row,) = failwatch.repeated(now=NOW + 9 * 5 * HOUR + 10)
    assert row["streak"] == 10 and row["backoff_until"] - row["last_ts"] == 4 * HOUR


def test_the_same_branch_stays_paused_but_a_different_one_tries_at_once(world):
    for i in range(3):
        world.run(NOW + i)
    assert world.run(NOW + 600) is None
    fetches = len(world.fetches)
    git(world.clone, "remote", "set-url", "origin", world.good_url)
    git(world.clone, "branch", "-m", "main", "other")
    git(world.clone, "branch", "--unset-upstream", "other")
    assert world.run(NOW + 900) is not None, "a changed branch lifts the pause"
    assert len(world.fetches) == fetches + 1


def test_a_different_remote_also_tries_at_once(world):
    for i in range(3):
        world.run(NOW + i)
    git(world.clone, "remote", "add", "second", str(world.tmp / "gone2.git"))
    git(world.clone, "remote", "rename", "origin", "first")
    assert world.run(NOW + 900) is not None


def test_failing_on_a_new_branch_starts_a_new_streak(world):
    for i in range(3):
        world.run(NOW + i)
    git(world.clone, "branch", "-m", "main", "other")
    git(world.clone, "branch", "--unset-upstream", "other")
    assert world.run(NOW + 900)["skipped_reason"] == "fetch-failed"
    (row,) = failwatch.history()
    assert row["streak"] == 1 and row["context"] == "origin/other"


def test_with_no_remote_the_context_is_none_and_a_pause_for_one_holds(tmp_path):
    clone, _ = make_repos(tmp_path)
    git(clone, "remote", "remove", "origin")
    for i in range(3):
        failwatch.record_failure("self-update", "x", now=NOW + i, cap=4 * HOUR, context="none")
    assert su.maybe_update(clone, interval_sec=0, marker_path=tmp_path / "m.json", now=NOW + 100) is None
