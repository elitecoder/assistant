"""Assistant updates by downloading main from GitHub, with no git, once
{"self_update": {"download": true}} is in config.json."""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import runpy
import sys
import tarfile
from pathlib import Path

import pytest

from assistant import failwatch, mode

REPO = Path(__file__).resolve().parent.parent
OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40
INSTALL = """#!/usr/bin/env bash
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '%s %s %s\\n' "$here" "${ASSISTANT_SELF_UPDATE:-0}" "$*" >> "$HOME/install-runs"
sleep "${FAKE_INSTALL_SLEEP:-0}"
exit "${FAKE_INSTALL_RC:-0}"
"""


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def au(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "self_update", load("self_update", REPO / "bin/self_update.py"))
    return load("app_update_mod", REPO / "bin/app_update.py")


def turn_on(home: Path, **extra) -> None:
    (home / ".assistant").mkdir(exist_ok=True)
    (home / ".assistant/config.json").write_text(json.dumps({"self_update": {"download": True, **extra}}))


def archive(sha: str, files: dict[str, str] | None = None, entries=()) -> bytes:
    """A codeload-style tar.gz: one top folder holding `files`, plus raw `entries`."""
    files = {"install.sh": INSTALL, "bin/ok.py": "x = 1\n",
             "launchagents/com.assistant.assistant-pulse.plist": f"<plist>{sha[:4]}</plist>\n",
             **(files or {})}
    files = {name: text for name, text in files.items() if text is not None}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        top = tarfile.TarInfo(f"assistant-{sha}")
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        for name, text in files.items():
            info = tarfile.TarInfo(f"assistant-{sha}/{name}")
            data = text.encode()
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        for info in entries:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return buf.getvalue()


class GitHub:
    """Answers the two URLs app_update asks for, and records each one."""

    def __init__(self, head: str, archives: dict[str, bytes] | None = None):
        self.head, self.archives, self.urls = head, archives or {}, []

    def __call__(self, req, timeout):
        url = req.full_url
        self.urls.append(url)
        if url == "https://api.github.com/repos/elitecoder/assistant/commits/main":
            assert req.get_header("Accept") == "application/vnd.github.sha"
            return io.BytesIO(self.head.encode())
        sha = url.rsplit("/", 1)[1]
        assert url == f"https://codeload.github.com/elitecoder/assistant/tar.gz/{sha}"
        if sha not in self.archives:
            raise OSError("HTTP Error 404: Not Found")
        return io.BytesIO(self.archives[sha])


def runs(home: Path) -> list[str]:
    path = home / "install-runs"
    return path.read_text().splitlines() if path.exists() else []


def test_the_first_update_unpacks_main_points_current_at_it_and_installs_from_current(au, tmp_path):
    turn_on(tmp_path)
    gh = GitHub(NEW, {NEW: archive(NEW)})
    result = au.maybe_update(now=1000, opener=gh)
    app = tmp_path / ".assistant/app"
    assert result["changed"] and result["to_sha"] == NEW[:12] and result["install_rc"] == 0
    assert os.readlink(app / "current") == NEW and (app / NEW / "bin/ok.py").read_text() == "x = 1\n"
    assert not (app / "previous").exists()
    assert runs(tmp_path) == [f"{app}/current 1 --apply"]
    assert os.access(app / NEW / "install.sh", os.X_OK)


def test_a_new_commit_moves_previous_and_deletes_older_folders(au, tmp_path):
    turn_on(tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    result = au.maybe_update(now=9000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert os.readlink(app / "current") == NEWER and os.readlink(app / "previous") == NEW
    assert result["pruned"] == [OLD] and not (app / OLD).exists()
    assert result["from_sha"] == NEW[:12] and result["self_plist_reload_deferred"]


def test_up_to_date_downloads_nothing_and_installs_nothing(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    gh = GitHub(NEW, {NEW: archive(NEW)})
    result = au.maybe_update(now=5000, opener=gh)
    assert not result["changed"] and result["skipped_reason"] is None and "error" not in result
    assert gh.urls == ["https://api.github.com/repos/elitecoder/assistant/commits/main"]
    assert len(runs(tmp_path)) == 1


def test_an_unchanged_pulse_plist_is_not_reported_as_needing_a_reload(au, tmp_path):
    turn_on(tmp_path)
    plist = {"launchagents/com.assistant.assistant-pulse.plist": "<plist/>\n"}
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD, plist)}))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, plist)}))
    assert result["changed"] and "self_plist_reload_deferred" not in result


@pytest.mark.parametrize("files,problem", [
    ({"bin/pulse.py": "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> b\n"}, "conflict marker at bin/pulse.py:1"),
    ({"src/assistant/x.py": "def broken(:\n"}, "src/assistant/x.py"),
    ({"install.sh": "<<<<<<< HEAD\n"}, "conflict marker at install.sh:1"),
])
def test_a_broken_commit_is_refused_and_current_stays(au, tmp_path, files, problem):
    turn_on(tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, files)}))
    assert result["skipped_reason"] == "syntax-fail" and problem in result["syntax_error"]
    assert os.readlink(app / "current") == OLD and not (app / NEW).exists()
    assert len(runs(tmp_path)) == 1


def test_a_refused_commit_is_not_downloaded_again_for_a_day(au, tmp_path):
    turn_on(tmp_path)
    broken = {NEW: archive(NEW, {"bin/x.py": "def (:\n"})}
    au.maybe_update(now=1000, opener=GitHub(NEW, broken))
    gh = GitHub(NEW, broken)
    assert au.maybe_update(now=1000 + 3600, opener=gh)["skipped_reason"] == "syntax-fail-known"
    assert len(gh.urls) == 1
    assert au.maybe_update(now=1000 + 86400, opener=gh)["skipped_reason"] == "syntax-fail"
    fixed = au.maybe_update(now=1000 + 90000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert fixed["changed"]


def link(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info


def entry(name: str, kind=tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size = kind, 1 if kind == tarfile.REGTYPE else 0
    return info


@pytest.mark.parametrize("bad,why", [
    (entry(f"assistant-{NEW}/../escape"), "outside its folder"),
    (entry("/abs"), "top folders"),
    (entry("other/x"), "top folders"),
    (link(f"assistant-{NEW}/bin/out", "../../../outside"), "archive link 'assistant-"),
    (link(f"assistant-{NEW}/bin/abs", "/etc/passwd"), "archive link 'assistant-"),
    (entry(f"assistant-{NEW}/bin/hard", tarfile.LNKTYPE), "isn't a file, folder, or link"),
    (entry(f"assistant-{NEW}/bin/fifo", tarfile.FIFOTYPE), "isn't a file, folder, or link"),
])
def test_an_archive_that_reaches_outside_its_folder_is_refused(au, tmp_path, bad, why):
    turn_on(tmp_path)
    result = au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW, entries=[bad])}))
    assert result["skipped_reason"] == "fetch-failed" and why in result["error"]
    app = tmp_path / ".assistant/app"
    assert sorted(p.name for p in app.iterdir()) == [] and not (tmp_path / ".assistant/escape").exists()
    assert runs(tmp_path) == []


def test_a_link_inside_the_folder_is_kept(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW, entries=[link(f"assistant-{NEW}/bin/alias.py", "ok.py")])}))
    assert (tmp_path / ".assistant/app" / NEW / "bin/alias.py").read_text() == "x = 1\n"


@pytest.mark.parametrize("head", ["<html>rate limited</html>", "", "a" * 39])
def test_an_answer_that_isnt_a_commit_id_is_a_fetch_failure(au, tmp_path, head):
    turn_on(tmp_path)
    result = au.maybe_update(now=1000, opener=GitHub(head))
    assert result["skipped_reason"] == "fetch-failed" and "instead of a commit id" in result["error"]


def test_a_network_failure_is_reported_and_nothing_changes(au, tmp_path):
    turn_on(tmp_path)
    result = au.maybe_update(now=1000, opener=GitHub(NEW))
    assert result["skipped_reason"] == "fetch-failed" and "404" in result["error"]
    assert not (tmp_path / ".assistant/app/current").exists()


def test_a_bad_repo_setting_is_reported_without_a_request(au, tmp_path):
    turn_on(tmp_path, repo="https://evil.example/x")
    gh = GitHub(NEW)
    result = au.maybe_update(now=1000, opener=gh)
    assert "owner/name" in result["error"] and gh.urls == []


def test_another_repo_can_be_named(au, tmp_path):
    turn_on(tmp_path, repo="me/fork")
    urls = []

    def opener(req, timeout):
        urls.append(req.full_url)
        raise OSError("offline")
    au.maybe_update(now=1000, opener=opener)
    assert urls == ["https://api.github.com/repos/me/fork/commits/main"]


@pytest.mark.parametrize("config", [None, {}, {"self_update": {"download": "true"}},
                                    {"self_update": {"download": 1}}, {"self_update": True}, "not json"])
def test_it_does_nothing_unless_the_setting_is_true(au, tmp_path, config):
    (tmp_path / ".assistant").mkdir()
    if config is not None:
        (tmp_path / ".assistant/config.json").write_text(config if isinstance(config, str) else json.dumps(config))
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=1000, opener=gh) is None and gh.urls == []
    assert mode.download_updates() is None


def test_it_waits_an_hour_between_tries_unless_forced(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=1000 + 3599, opener=gh) is None and gh.urls == []
    assert au.maybe_update(now=1000 + 3599, force=True, opener=gh)["changed"]


def test_three_identical_failures_pause_it_and_force_still_tries(au, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE", str(tmp_path / "failures.json"))
    turn_on(tmp_path)
    for i in range(3):
        result = au.maybe_update(now=1000 + i * 100, interval_sec=0, opener=GitHub(NEW))
        assert result["skipped_reason"] == "fetch-failed"
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=1300, interval_sec=0, opener=gh) is None and gh.urls == []
    assert au.maybe_update(now=1300, force=True, opener=gh)["changed"]
    assert not failwatch.backed_off("self-update", now=1301, context="download:elitecoder/assistant")


def test_a_failed_install_is_reported(au, tmp_path, monkeypatch):
    turn_on(tmp_path)
    monkeypatch.setenv("FAKE_INSTALL_RC", "3")
    result = au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["install_rc"] == 3 and result["error"]
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW


def test_a_second_update_while_one_runs_does_nothing(au, tmp_path):
    turn_on(tmp_path)
    held = au._locked()
    try:
        gh = GitHub(NEW, {NEW: archive(NEW)})
        assert au.maybe_update(now=1000, opener=gh) is None and gh.urls == []
        assert "another update" in au.rollback()["error"]
    finally:
        held.close()


def test_rollback_swaps_current_and_previous_and_installs(au, tmp_path):
    turn_on(tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    result = au.rollback()
    assert result == {"rolled_back": True, "from_sha": NEW[:12], "to_sha": OLD[:12], "install_rc": 0}
    assert os.readlink(app / "current") == OLD and os.readlink(app / "previous") == NEW
    assert runs(tmp_path)[-1] == f"{app}/current 0 --apply"


def test_rollback_with_no_previous_version_changes_nothing(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert au.rollback()["rolled_back"] is False
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW


def test_a_manual_run_installs_without_the_pulse_flag(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, from_pulse=False, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert runs(tmp_path) == [f"{tmp_path}/.assistant/app/current 0 --apply"]


def test_the_command_refuses_to_run_while_the_setting_is_off(au, tmp_path, capsys):
    assert au.main([]) == 2
    assert '"self_update": {"download": true}' in capsys.readouterr().err


def test_the_command_rolls_back(au, tmp_path, capsys):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert au.main(["--rollback"]) == 0
    assert json.loads(capsys.readouterr().out)["to_sha"] == OLD[:12]


# --- the pulse picks the updater from the setting


@pytest.fixture
def pulse(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OBSERVER_AUDIT", "0")
    (tmp_path / ".assistant").mkdir()
    return load("pulse_download_mod", REPO / "bin/pulse.py")


def fake(name: str, result: dict, calls: list):
    mod = type(sys)(name)

    def maybe_update(*args, **kwargs):
        calls.append((name, args))
        return result
    mod.maybe_update = maybe_update
    return mod


def ledger(home: Path) -> list[dict]:
    path = home / ".assistant/actions-ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_with_the_setting_on_the_pulse_downloads_and_never_runs_the_git_updater(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    calls = []
    done = {"attempted": True, "changed": True, "installed": True, "install_rc": 0,
            "skipped_reason": None, "source": "download", "from_sha": OLD[:12], "to_sha": NEW[:12]}
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", done, calls))
    monkeypatch.setitem(sys.modules, "self_update", fake("self_update", {}, calls))
    pulse.self_update_pulse(7)
    assert calls == [("app_update", ())]
    (row,) = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert row["evidence"] == f"downloaded {OLD[:12]}..{NEW[:12]}; install=ran"


def test_with_the_setting_off_the_pulse_uses_the_git_updater(pulse, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", {}, calls))
    monkeypatch.setitem(sys.modules, "self_update", fake("self_update", None, calls))
    pulse.self_update_pulse(7)
    assert calls == [("self_update", (pulse.REPO,))]


def test_a_first_download_is_named_in_the_ledger(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    first = {"attempted": True, "changed": True, "installed": True, "install_rc": 0,
             "skipped_reason": None, "source": "download", "from_sha": "", "to_sha": NEW[:12]}
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", first, []))
    pulse.self_update_pulse(7)
    (row,) = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert row["evidence"].startswith(f"downloaded first version..{NEW[:12]}")


def test_going_forward_after_a_rollback_reuses_the_kept_folder(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD)}))
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    au.rollback()
    gh = GitHub(NEW)
    result = au.maybe_update(now=9000, opener=gh)
    assert result["changed"] and result["install_rc"] == 0
    assert gh.urls == ["https://api.github.com/repos/elitecoder/assistant/commits/main"]
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW


def test_an_install_that_hangs_is_stopped_and_reported(au, tmp_path, monkeypatch):
    turn_on(tmp_path)
    monkeypatch.setattr(au, "INSTALL_TIMEOUT_SEC", 1)
    monkeypatch.setenv("FAKE_INSTALL_SLEEP", "5")
    result = au.maybe_update(now=1000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["install_rc"] == -1 and "timed out after 1s" in result["error"]


def test_a_pulse_plist_the_old_version_lacked_counts_as_changed(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, opener=GitHub(OLD, {OLD: archive(OLD, {"launchagents/com.assistant.assistant-pulse.plist": None})}))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["self_plist_reload_deferred"]


def test_each_attempt_and_each_pause_is_logged(au, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE", str(tmp_path / "failures.json"))
    turn_on(tmp_path)
    log = logging.getLogger("app_update_test")
    with caplog.at_level(logging.INFO, logger="app_update_test"):
        for i in range(4):
            au.maybe_update(now=1000 + i, interval_sec=0, opener=GitHub(NEW), log=log)
    messages = [r.getMessage() for r in caplog.records]
    assert sum('"skipped_reason": "fetch-failed"' in m for m in messages) == 3
    assert messages[-1] == "self-update: paused after repeating the same failure"


def test_the_command_updates_now(au, tmp_path, monkeypatch, capsys):
    turn_on(tmp_path)
    monkeypatch.setattr(au.urllib.request, "urlopen", GitHub(NEW, {NEW: archive(NEW)}))
    assert au.main(["--force"]) == 0
    assert json.loads(capsys.readouterr().out)["to_sha"] == NEW[:12]
    monkeypatch.setattr(au.urllib.request, "urlopen", GitHub(NEWER))
    assert au.main(["--force"]) == 1


def test_running_the_file_runs_the_command(au, tmp_path):
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(REPO / "bin/app_update.py"), run_name="__main__")
    assert stop.value.code == 2
