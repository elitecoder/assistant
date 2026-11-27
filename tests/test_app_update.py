"""Assistant updates by downloading main from GitHub, with no git, once
{"self_update": {"download": true}} is in config.json."""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import plistlib
import runpy
import shutil
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
mkdir -p "$HOME/.claude" && ln -sfn "$here/bin" "$HOME/.claude/bin"
sleep "${FAKE_INSTALL_SLEEP:-0}"
exit "${FAKE_INSTALL_RC:-0}"
"""


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def au(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "self_update", load("self_update", REPO / "bin/self_update.py"))
    mod = load("app_update_mod", REPO / "bin/app_update.py")
    monkeypatch.setattr(mod, "HERE", (tmp_path / ".assistant/app/running/bin/app_update.py").resolve())
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "launchctl").write_text(FAKE_LAUNCHCTL)
    (fake_bin / "launchctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    return mod


def turn_on(home: Path, **extra) -> None:
    (home / ".assistant").mkdir(exist_ok=True)
    (home / ".assistant/config.json").write_text(json.dumps({"self_update": {"download": True, **extra}}))


def archive(sha: str, files: dict[str, str] | None = None, entries=()) -> bytes:
    """A codeload-style tar.gz: one top folder holding `files`, plus raw `entries`."""
    files = {"install.sh": INSTALL, "bin/ok.py": "x = 1\n", "bin/pulse.py": "", "bin/app_update.py": "",
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


def set_up(au, home: Path, sha: str = OLD, **extra) -> None:
    """Turn the setting on and run the first download by hand, as the docs say."""
    turn_on(home, **extra)
    result = au.maybe_update(now=1000, from_pulse=False, opener=GitHub(sha, {sha: archive(sha)}))
    assert result["changed"] and result["install_rc"] == 0


def test_the_first_download_by_hand_unpacks_main_points_current_at_it_and_installs_from_current(au, tmp_path):
    turn_on(tmp_path)
    result = au.maybe_update(now=1000, from_pulse=False, opener=GitHub(NEW, {NEW: archive(NEW)}))
    app = tmp_path / ".assistant/app"
    assert result["changed"] and result["to_sha"] == NEW[:12] and result["install_rc"] == 0
    assert os.readlink(app / "current") == NEW and (app / NEW / "bin/ok.py").read_text() == "x = 1\n"
    assert not (app / "previous").exists()
    assert runs(tmp_path) == [f"{app}/current 0 --apply"]
    assert os.access(app / NEW / "install.sh", os.X_OK)


def test_the_pulse_never_makes_the_first_download(au, tmp_path, monkeypatch):
    turn_on(tmp_path)
    monkeypatch.setattr(au, "HERE", REPO / "bin/app_update.py")
    gh = GitHub(NEW, {NEW: archive(NEW)})
    result = au.maybe_update(now=1000, opener=gh)
    assert result["skipped_reason"] == "not-set-up" and "--force" in result["error"]
    assert gh.urls == [] and not (tmp_path / ".assistant/app/current").exists()


def test_a_pulse_still_running_from_the_checkout_never_updates(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    monkeypatch.setattr(au, "HERE", REPO / "bin/app_update.py")
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=5000, opener=gh)["skipped_reason"] == "not-set-up"
    assert gh.urls == [] and len(runs(tmp_path)) == 1


def test_a_manual_run_reinstalls_when_the_links_point_elsewhere(au, tmp_path):
    set_up(au, tmp_path)
    link = tmp_path / ".claude/bin"
    link.unlink()
    link.symlink_to(REPO / "bin")
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["installed"] and len(runs(tmp_path)) == 2
    assert link.resolve() == (tmp_path / ".assistant/app/current/bin").resolve()


def test_a_service_restart_that_keeps_failing_is_reported(au, tmp_path):
    set_up(au, tmp_path)
    (tmp_path / ".assistant/service-restart-failed").write_text("com.assistant.x\n")
    result = au.maybe_update(now=5000, opener=GitHub(OLD))
    assert "couldn't restart" in result["error"]


@pytest.mark.parametrize("missing", ["install.sh", "bin/pulse.py", "bin/app_update.py"])
def test_a_commit_missing_a_core_file_is_refused(au, tmp_path, missing):
    set_up(au, tmp_path)
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, {missing: None})}))
    assert result["skipped_reason"] == "syntax-fail" and result["syntax_error"] == f"{missing} is missing"


def test_the_pulse_updates_and_installs_with_the_pulse_flag(au, tmp_path):
    set_up(au, tmp_path)
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    app = tmp_path / ".assistant/app"
    assert result["changed"] and result["from_sha"] == OLD[:12] and result["to_sha"] == NEW[:12]
    assert runs(tmp_path)[-1] == f"{app}/current 1 --apply"
    assert os.readlink(app / "current") == NEW and os.readlink(app / "previous") == OLD


def test_a_new_commit_moves_previous_and_deletes_older_folders(au, tmp_path):
    set_up(au, tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    result = au.maybe_update(now=9000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert os.readlink(app / "current") == NEWER and os.readlink(app / "previous") == NEW
    assert result["pruned"] == [OLD] and not (app / OLD).exists()
    assert result["from_sha"] == NEW[:12] and result["self_plist_reload_deferred"]


def test_nothing_is_deleted_while_a_service_restart_is_owed(au, tmp_path):
    set_up(au, tmp_path)
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    (tmp_path / ".assistant/service-restart-failed").write_text("com.assistant.x\n")
    result = au.maybe_update(now=9000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert result["changed"] and "pruned" not in result
    assert (tmp_path / ".assistant/app" / OLD).is_dir()


def test_up_to_date_downloads_nothing_and_installs_nothing(au, tmp_path):
    set_up(au, tmp_path, NEW)
    gh = GitHub(NEW, {NEW: archive(NEW)})
    result = au.maybe_update(now=5000, opener=gh)
    assert not result["changed"] and result["skipped_reason"] is None and "error" not in result
    assert gh.urls == ["https://api.github.com/repos/elitecoder/assistant/commits/main"]
    assert len(runs(tmp_path)) == 1


def test_a_failed_install_is_tried_again_next_time(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    monkeypatch.setenv("FAKE_INSTALL_RC", "3")
    assert au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))["install_rc"] == 3
    monkeypatch.delenv("FAKE_INSTALL_RC")
    result = au.maybe_update(now=9000, opener=GitHub(NEW))
    assert result["install_rc"] == 0 and "error" not in result
    assert len(runs(tmp_path)) == 3
    au.maybe_update(now=13000, opener=GitHub(NEW))
    assert len(runs(tmp_path)) == 3


def test_a_service_restart_that_failed_is_tried_again_next_time(au, tmp_path):
    set_up(au, tmp_path)
    (tmp_path / ".assistant/service-restart-failed").write_text("com.assistant.x\n")
    result = au.maybe_update(now=5000, opener=GitHub(OLD))
    assert result["installed"] and len(runs(tmp_path)) == 2


def test_an_unchanged_pulse_plist_is_not_reported_as_needing_a_reload(au, tmp_path):
    turn_on(tmp_path)
    plist = {"launchagents/com.assistant.assistant-pulse.plist": "<plist/>\n"}
    au.maybe_update(now=1000, from_pulse=False, opener=GitHub(OLD, {OLD: archive(OLD, plist)}))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, plist)}))
    assert result["changed"] and "self_plist_reload_deferred" not in result


@pytest.mark.parametrize("files,problem", [
    ({"bin/pulse.py": "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> b\n"}, "conflict marker at bin/pulse.py:1"),
    ({"src/assistant/x.py": "def broken(:\n"}, "src/assistant/x.py"),
    ({"install.sh": "<<<<<<< HEAD\n"}, "conflict marker at install.sh:1"),
])
def test_a_broken_commit_is_refused_and_current_stays(au, tmp_path, files, problem):
    set_up(au, tmp_path)
    app = tmp_path / ".assistant/app"
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, files)}))
    assert result["skipped_reason"] == "syntax-fail" and problem in result["syntax_error"]
    assert os.readlink(app / "current") == OLD and not (app / NEW).exists()
    assert len(runs(tmp_path)) == 1


def test_a_refused_commit_is_not_downloaded_again_for_a_day(au, tmp_path):
    set_up(au, tmp_path)
    broken = {NEW: archive(NEW, {"bin/x.py": "def (:\n"})}
    au.maybe_update(now=1000, force=True, opener=GitHub(NEW, broken))
    gh = GitHub(NEW, broken)
    assert au.maybe_update(now=1000 + 3600, opener=gh)["skipped_reason"] == "syntax-fail-known"
    assert len(gh.urls) == 1
    assert au.maybe_update(now=1000 + 86400, opener=gh)["skipped_reason"] == "syntax-fail"
    fixed = au.maybe_update(now=1000 + 90000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert fixed["changed"]


def test_a_refusal_holds_only_for_the_python_that_made_it(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    broken = {NEW: archive(NEW, {"bin/x.py": "def (:\n"})}
    au.maybe_update(now=1000, force=True, opener=GitHub(NEW, broken))
    monkeypatch.setattr(au.sys, "version_info", (3, 99, 0))
    assert au.maybe_update(now=5000, opener=GitHub(NEW, broken))["skipped_reason"] == "syntax-fail"


def entry(name: str, kind=tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.linkname = kind, 1 if kind == tarfile.REGTYPE else 0, linkname
    return info


@pytest.mark.parametrize("bad,why", [
    (entry(f"assistant-{NEW}/../escape"), "outside its folder"),
    (entry("/abs"), "top folders"),
    (entry(f"/assistant-{NEW}/x"), "top folders"),
    (entry("other/x"), "top folders"),
    (entry(f"assistant-{NEW}/bin/out", tarfile.SYMTYPE, "../../../outside"), "isn't a plain file or folder"),
    (entry(f"assistant-{NEW}/bin/alias.py", tarfile.SYMTYPE, "ok.py"), "isn't a plain file or folder"),
    (entry(f"assistant-{NEW}/bin/hard", tarfile.LNKTYPE, f"assistant-{NEW}/bin/ok.py"), "isn't a plain file or folder"),
    (entry(f"assistant-{NEW}/bin/fifo", tarfile.FIFOTYPE), "isn't a plain file or folder"),
])
def test_an_archive_with_anything_but_files_and_folders_inside_it_is_refused(au, tmp_path, bad, why):
    set_up(au, tmp_path)
    app = tmp_path / ".assistant/app"
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, entries=[bad])}))
    assert result["skipped_reason"] == "fetch-failed" and why in result["error"]
    assert sorted(p.name for p in app.iterdir()) == [OLD, "current"]
    assert not (tmp_path / ".assistant/escape").exists() and len(runs(tmp_path)) == 1


def test_the_link_escape_the_data_filter_would_catch_is_refused_before_it(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    monkeypatch.delattr(au.tarfile, "data_filter", raising=False)
    top = f"assistant-{NEW}"
    chain = [entry(f"{top}/a", tarfile.SYMTYPE, "."), entry(f"{top}/a/a/a/b", tarfile.SYMTYPE, "../../.."),
             entry(f"{top}/b/PWNED")]
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW, entries=chain)}))
    assert result["skipped_reason"] == "fetch-failed"
    assert not list(tmp_path.rglob("PWNED"))


def test_a_cut_off_archive_is_a_reported_failure(au, tmp_path):
    set_up(au, tmp_path)
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)[:200]}))
    assert result["skipped_reason"] == "fetch-failed" and result["error"]


@pytest.mark.parametrize("head", ["<html>rate limited</html>", "", "a" * 39])
def test_an_answer_that_isnt_a_commit_id_is_a_fetch_failure(au, tmp_path, head):
    set_up(au, tmp_path)
    result = au.maybe_update(now=5000, opener=GitHub(head))
    assert result["skipped_reason"] == "fetch-failed" and "instead of a commit id" in result["error"]


def test_a_network_failure_is_reported_and_nothing_changes(au, tmp_path):
    set_up(au, tmp_path)
    result = au.maybe_update(now=5000, opener=GitHub(NEW))
    assert result["skipped_reason"] == "fetch-failed" and "404" in result["error"]
    assert os.readlink(tmp_path / ".assistant/app/current") == OLD


def test_a_bad_repo_setting_is_reported_without_a_request(au, tmp_path):
    turn_on(tmp_path, repo="https://evil.example/x")
    gh = GitHub(NEW)
    result = au.maybe_update(now=1000, from_pulse=False, opener=gh)
    assert "owner/name" in result["error"] and gh.urls == []


def test_another_repo_can_be_named(au, tmp_path):
    turn_on(tmp_path, repo="me/fork")
    urls = []

    def opener(req, timeout):
        urls.append(req.full_url)
        raise OSError("offline")
    au.maybe_update(now=1000, from_pulse=False, opener=opener)
    assert urls == ["https://api.github.com/repos/me/fork/commits/main"]


@pytest.mark.parametrize("config", [None, {}, {"self_update": {"download": "true"}},
                                    {"self_update": {"download": 1}}, {"self_update": True}, "not json"])
def test_it_does_nothing_unless_the_setting_is_true(au, tmp_path, config):
    (tmp_path / ".assistant").mkdir()
    if config is not None:
        (tmp_path / ".assistant/config.json").write_text(config if isinstance(config, str) else json.dumps(config))
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=1000, from_pulse=False, opener=gh) is None and gh.urls == []
    assert mode.download_updates() is None


def test_it_waits_an_hour_between_tries_unless_forced(au, tmp_path):
    set_up(au, tmp_path)
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=1000 + 3599, opener=gh) is None and gh.urls == []
    assert au.maybe_update(now=1000 + 3599, force=True, opener=gh)["changed"]


def test_three_identical_failures_pause_it_and_force_still_tries(au, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE", str(tmp_path / "failures.json"))
    set_up(au, tmp_path)
    for i in range(3):
        result = au.maybe_update(now=2000 + i * 100, interval_sec=0, opener=GitHub(NEW))
        assert result["skipped_reason"] == "fetch-failed"
    gh = GitHub(NEW, {NEW: archive(NEW)})
    assert au.maybe_update(now=2300, interval_sec=0, opener=gh) is None and gh.urls == []
    assert au.maybe_update(now=2300, force=True, opener=gh)["changed"]
    assert not failwatch.backed_off("self-update", now=2301, context="download:elitecoder/assistant")


def test_a_failed_install_is_reported(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    monkeypatch.setenv("FAKE_INSTALL_RC", "3")
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["install_rc"] == 3 and result["error"] and "pruned" not in result
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW


def test_a_second_update_while_one_runs_does_nothing(au, tmp_path):
    set_up(au, tmp_path)
    held = au._locked()
    try:
        gh = GitHub(NEW, {NEW: archive(NEW)})
        assert au.maybe_update(now=1000, force=True, opener=gh) is None and gh.urls == []
        assert "another update" in au.rollback()["error"]
    finally:
        held.close()


def test_rollback_swaps_current_and_previous_and_installs(au, tmp_path):
    set_up(au, tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    result = au.rollback()
    assert result == {"rolled_back": True, "from_sha": NEW[:12], "to_sha": OLD[:12], "install_rc": 0}
    assert os.readlink(app / "current") == OLD and os.readlink(app / "previous") == NEW
    assert runs(tmp_path)[-1] == f"{app}/current 0 --apply"


def test_a_rollback_holds_until_main_moves(au, tmp_path):
    set_up(au, tmp_path)
    app = tmp_path / ".assistant/app"
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    au.rollback()
    installs = len(runs(tmp_path))
    gh = GitHub(NEW, {NEW: archive(NEW)})
    result = au.maybe_update(now=9000, opener=gh)
    assert result["skipped_reason"] == "rolled-back" and not result["changed"]
    assert os.readlink(app / "current") == OLD and len(runs(tmp_path)) == installs
    newer = au.maybe_update(now=13000, opener=GitHub(NEWER, {NEWER: archive(NEWER)}))
    assert newer["changed"] and os.readlink(app / "current") == NEWER and "ended_hold" not in newer
    assert "held" not in json.loads((tmp_path / ".assistant/app-update.json").read_text())


def test_rollback_with_no_previous_version_changes_nothing(au, tmp_path):
    set_up(au, tmp_path, NEW)
    assert au.rollback()["rolled_back"] is False
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW


def test_the_command_refuses_to_run_while_the_setting_is_off(au, tmp_path, capsys):
    assert au.main([]) == 2
    assert '"self_update": {"download": true}' in capsys.readouterr().err


def test_the_command_rolls_back(au, tmp_path, capsys):
    set_up(au, tmp_path)
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert au.main(["--rollback"]) == 0
    assert json.loads(capsys.readouterr().out)["to_sha"] == OLD[:12]


def test_the_command_says_why_nothing_ran(au, tmp_path, capsys):
    set_up(au, tmp_path)
    held = au._locked()
    try:
        assert au.main(["--force"]) == 1
    finally:
        held.close()
    assert "another update is running" in capsys.readouterr().err


def test_the_command_fails_on_a_refused_commit(au, tmp_path, monkeypatch, capsys):
    set_up(au, tmp_path)
    monkeypatch.setattr(au.urllib.request, "urlopen", GitHub(NEW, {NEW: archive(NEW, {"bin/x.py": "def (:\n"})}))
    assert au.main(["--force"]) == 1
    assert json.loads(capsys.readouterr().out)["skipped_reason"] == "syntax-fail"


@pytest.fixture
def curator_in_copy(au, tmp_path, monkeypatch):
    """The checkout's lessons tool, with its Assistant stores moved into a
    downloaded copy under this test's home. No audit or sync runs."""
    set_up(au, tmp_path)
    copy = (tmp_path / ".assistant/app" / OLD).resolve()
    curator = load("curator_mod", REPO / "bin/assistant-curator.py")
    monkeypatch.setattr(curator, "_run_lesson_audit_async", lambda: None)
    monkeypatch.setattr(curator, "_sync_to_memory_repo", lambda: None)
    monkeypatch.setattr(curator, "APP_ROOT", tmp_path / ".assistant/app")
    for name, rel in (("assistant", "prompts/observer-batch-prompt.md"),
                      ("strategist", "prompts/strategist-draft-prompt.md"),
                      ("assistant-repo", ".claude/rules/assistant-lessons.md")):
        monkeypatch.setitem(curator.TARGETS[name], "path", copy / rel)
    monkeypatch.setitem(curator.TARGETS["claude"], "path", tmp_path / ".claude/CLAUDE.md")
    return curator, copy


def test_the_lessons_tool_refuses_to_write_into_a_downloaded_copy(curator_in_copy, tmp_path, monkeypatch):
    curator, copy = curator_in_copy
    for target in ("assistant", "strategist", "assistant-repo"):
        assert "downloaded copy" in curator._downloaded_copy_refusal(target), target
    monkeypatch.setattr(sys, "argv", ["assistant-curator.py", "write", "--target", "assistant-repo", "--rule", "Do Y"])
    assert curator.main() == 2
    assert not (copy / ".claude").exists()
    monkeypatch.setattr(sys, "argv", ["assistant-curator.py", "write", "--target", "claude", "--rule", "Do Y", "--slug", "do-y"])
    assert curator.main() == 0 and "do-y" in (tmp_path / ".claude/CLAUDE.md").read_text()


def test_the_lessons_tool_refuses_to_remove_a_lesson_from_a_downloaded_copy(curator_in_copy, monkeypatch):
    curator, copy = curator_in_copy
    rules = copy / ".claude/rules/assistant-lessons.md"
    rules.parent.mkdir(parents=True)
    shutil.copy(REPO / ".claude/rules/assistant-lessons.md", rules)
    before = rules.read_text()
    slug = next(curator.iter_lessons(before))["slug"]
    monkeypatch.setattr(sys, "argv", ["assistant-curator.py", "rm", slug, "--target", "assistant-repo"])
    assert curator.main() == 2 and rules.read_text() == before


def test_the_lessons_tool_refuses_to_open_a_downloaded_copy_store_for_trimming(curator_in_copy, monkeypatch):
    curator, _ = curator_in_copy
    monkeypatch.setattr(curator.os, "execvp", lambda *a: pytest.fail("opened an editor"))
    monkeypatch.setattr(sys, "argv", ["assistant-curator.py", "trim", "--target", "assistant"])
    assert curator.main() == 2


def test_the_lessons_tool_in_a_checkout_writes_as_before(au, tmp_path):
    curator = load("curator_in_checkout", REPO / "bin/assistant-curator.py")
    assert curator._downloaded_copy_refusal("assistant-repo") is None


def test_the_slack_session_finds_the_lessons_tool_next_to_itself():
    import comms_lib
    assert comms_lib.Paths.from_env({"HOME": "/nowhere"}).curator == REPO / "bin/assistant-curator.py"


def test_a_download_left_by_an_interrupted_run_is_used_without_downloading_again(au, tmp_path):
    set_up(au, tmp_path)
    au.download("elitecoder/assistant", NEW, tmp_path / ".assistant/app", GitHub(NEW, {NEW: archive(NEW)}))
    gh = GitHub(NEW)
    result = au.maybe_update(now=5000, opener=gh)
    assert result["changed"] and result["install_rc"] == 0
    assert gh.urls == ["https://api.github.com/repos/elitecoder/assistant/commits/main"]


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


def test_an_install_that_hangs_is_stopped_and_reported(au, tmp_path, monkeypatch):
    set_up(au, tmp_path)
    monkeypatch.setattr(au, "INSTALL_TIMEOUT_SEC", 1)
    monkeypatch.setenv("FAKE_INSTALL_SLEEP", "5")
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["install_rc"] == -1 and "timed out after 1s" in result["error"]


def test_a_pulse_plist_the_old_version_lacked_counts_as_changed(au, tmp_path):
    turn_on(tmp_path)
    au.maybe_update(now=1000, from_pulse=False,
                    opener=GitHub(OLD, {OLD: archive(OLD, {"launchagents/com.assistant.assistant-pulse.plist": None})}))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["self_plist_reload_deferred"]


def test_each_attempt_and_each_pause_is_logged(au, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE", str(tmp_path / "failures.json"))
    set_up(au, tmp_path)
    log = logging.getLogger("app_update_test")
    with caplog.at_level(logging.INFO, logger="app_update_test"):
        for i in range(4):
            au.maybe_update(now=2000 + i, interval_sec=0, opener=GitHub(NEW), log=log)
    messages = [r.getMessage() for r in caplog.records]
    assert sum('"skipped_reason": "fetch-failed"' in m for m in messages) == 3
    assert messages[-1] == "self-update: paused after repeating the same failure"


def test_the_command_updates_now(au, tmp_path, monkeypatch, capsys):
    set_up(au, tmp_path)
    monkeypatch.setattr(au.urllib.request, "urlopen", GitHub(NEW, {NEW: archive(NEW)}))
    assert au.main(["--force"]) == 0
    assert json.loads(capsys.readouterr().out)["to_sha"] == NEW[:12]
    monkeypatch.setattr(au.urllib.request, "urlopen", GitHub(NEWER))
    assert au.main(["--force"]) == 1


def test_running_the_file_runs_the_command(au, tmp_path):
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(REPO / "bin/app_update.py"), run_name="__main__")
    assert stop.value.code == 2


FAKE_LAUNCHCTL = """#!/bin/bash
echo "$*" >> "$HOME/launchctl-calls"
label="${2##*/}"
case "$1" in
  print) [ -f "$HOME/loaded/$label" ] || exit 113; cat "$HOME/loaded/$label" ;;
  bootout) rm -f "$HOME/loaded/$label" ;;
  bootstrap) label="$(basename "$3" .plist)"; echo "arguments = { $HOME/.assistant/app/current/bin/x }" > "$HOME/loaded/$label" ;;
  load) label="$(basename "$2" .plist)"; echo "arguments = { $HOME/.assistant/app/current/bin/x }" > "$HOME/loaded/$label" ;;
esac
"""


def agent(home: Path, label: str, program: str, loaded_from: str | None) -> None:
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/python3", program]}))
    if loaded_from:
        (home / "loaded").mkdir(exist_ok=True)
        (home / "loaded" / label).write_text(f"arguments = {{ {loaded_from} }}\n")


@pytest.fixture
def launchctl(au, tmp_path):
    return lambda: (tmp_path / "launchctl-calls").read_text().splitlines() if (tmp_path / "launchctl-calls").exists() else []


def test_a_manual_run_moves_services_still_loaded_from_the_checkout(au, tmp_path, launchctl):
    current = f"{tmp_path}/.assistant/app/current"
    agent(tmp_path, "com.assistant.slack-reactor", f"{current}/bin/spawn-slack-reactor.sh", "/Users/me/dev/assistant/bin/x")
    agent(tmp_path, "com.assistant.comms", f"{current}/bin/comms.py", f"{current}/bin/comms.py")
    agent(tmp_path, "com.assistant.unloaded", f"{current}/bin/u.py", None)
    agent(tmp_path, "com.assistant.elsewhere", "/Users/me/dev/assistant/bin/e.py", "/Users/me/dev/assistant/bin/e.py")
    set_up(au, tmp_path)
    uid = os.getuid()
    calls = launchctl()
    assert f"bootout gui/{uid}/com.assistant.slack-reactor" in calls
    assert f"bootstrap gui/{uid} {tmp_path}/Library/LaunchAgents/com.assistant.slack-reactor.plist" in calls
    assert not any("bootout" in c and ("comms" in c or "unloaded" in c or "elsewhere" in c) for c in calls)
    assert not any("elsewhere" in c for c in calls)


def test_the_pulse_never_reloads_services_itself(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.assistant.slack-reactor", f"{tmp_path}/.assistant/app/current/bin/r.sh", "/old/bin/r.sh")
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert not [c for c in launchctl() if c.startswith(("bootout", "bootstrap", "load"))]


def test_a_manual_run_when_up_to_date_still_moves_services(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.assistant.slack-reactor", f"{tmp_path}/.assistant/app/current/bin/r.sh", "/old/bin/r.sh")
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.assistant.slack-reactor"]


def test_a_service_that_wont_start_is_named_with_the_reason(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.assistant.slack-reactor", f"{tmp_path}/.assistant/app/current/bin/r.sh", "/old/bin/r.sh")
    (tmp_path / "fake-bin/launchctl").write_text(FAKE_LAUNCHCTL.replace(
        'bootstrap) label', 'bootstrap) echo "Bootstrap failed: 5" >&2; exit 5; label').replace(
        'load) label', 'load) echo "Load failed: 5" >&2; exit 5; label'))
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.assistant.slack-reactor (couldn't start: Load failed: 5)"]
    assert result["error"] == "couldn't start com.assistant.slack-reactor (couldn't start: Load failed: 5)"
    assert [c.split()[0] for c in launchctl()].count("bootstrap") >= 2


def test_a_service_that_bootstrap_refuses_is_loaded_instead(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.assistant.slack-reactor", f"{tmp_path}/.assistant/app/current/bin/r.sh", "/old/bin/r.sh")
    (tmp_path / "fake-bin/launchctl").write_text(FAKE_LAUNCHCTL.replace(
        'bootstrap) label', 'bootstrap) echo "Bootstrap failed: 5" >&2; exit 5; label'))
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.assistant.slack-reactor"] and "error" not in result
    assert f"load {tmp_path}/Library/LaunchAgents/com.assistant.slack-reactor.plist" in launchctl()


def test_an_unreadable_launch_agent_is_left_alone(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    (tmp_path / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Library/LaunchAgents/com.assistant.broken.plist").write_text("not a plist")
    assert au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))["moved_services"] == []


@pytest.mark.parametrize("on,status", [(True, "PASS"), (False, "FAIL")])
def test_the_setup_check_needs_git_only_for_git_updates(au, tmp_path, monkeypatch, on, status):
    if on:
        turn_on(tmp_path)
    doctor = load("doctor_mod", REPO / "bin/assistant-doctor.py")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    check = doctor.check_git()
    assert check.status == getattr(doctor, status) and check.core


def test_with_the_setting_off_a_downloaded_copy_never_runs_the_git_updater(pulse, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "self_update", fake("self_update", None, calls))
    monkeypatch.setattr(pulse, "REPO", tmp_path / ".assistant/app" / NEW)
    pulse.self_update_pulse(7)
    pulse.self_update_pulse(8)
    assert calls == []
    rows = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert len(rows) == 1 and "run install.sh --apply from your checkout" in rows[0]["evidence"]


def test_the_off_note_comes_back_after_a_day(pulse, tmp_path):
    assert pulse._download_off_result(now=1000)["skipped_reason"] == "download-off"
    assert pulse._download_off_result(now=1000 + 86399) is None
    assert pulse._download_off_result(now=1000 + 86400)["skipped_reason"] == "download-off"


def test_a_held_rollback_is_quiet_in_the_ledger(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    held = {"attempted": True, "changed": False, "installed": False, "skipped_reason": "rolled-back",
            "source": "download", "from_sha": OLD[:12], "to_sha": NEW[:12]}
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", held, []))
    pulse.self_update_pulse(7)
    assert [r for r in ledger(tmp_path) if r["kind"].startswith("self-update")] == []



def test_the_git_update_is_still_described_as_a_pull(pulse, tmp_path, monkeypatch):
    pulled = {"attempted": True, "changed": True, "installed": False, "skipped_reason": None,
              "from_sha": OLD[:12], "to_sha": NEW[:12], "files_changed": ["bin/x.py", "docs/y.md"]}
    monkeypatch.setitem(sys.modules, "self_update", fake("self_update", pulled, []))
    pulse.self_update_pulse(7)
    (row,) = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert row["evidence"] == f"pulled {OLD[:12]}..{NEW[:12]} (2 file(s)); install=not-needed"


def test_an_archive_of_only_absolute_paths_is_refused(au, tmp_path):
    set_up(au, tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("/pwned.txt", "/x/y.txt"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: buf.getvalue()}))
    assert result["skipped_reason"] == "fetch-failed" and "not one named folder" in result["error"]
    assert not list(tmp_path.rglob("pwned.txt"))


def test_the_services_with_the_older_names_are_moved_too(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.mukul.assistant-cmux-watcher", f"{tmp_path}/.assistant/app/current/bin/cmux-watcher.py",
          "/Users/me/dev/assistant/bin/cmux-watcher.py")
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.mukul.assistant-cmux-watcher"]


def test_a_running_daemon_stops_every_update(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    (tmp_path / "loaded").mkdir(exist_ok=True)
    (tmp_path / "loaded/com.mukul.assistant-daemon").write_text("\tstate = running\n")
    gh = GitHub(NEW, {NEW: archive(NEW)})
    for from_pulse in (True, False):
        result = au.maybe_update(now=5000, force=True, from_pulse=from_pulse, opener=gh)
        assert result["skipped_reason"] == "daemon-running"
        assert "launchctl disable" in result["error"] and "bootout" in result["error"]
    assert gh.urls == [] and os.readlink(tmp_path / ".assistant/app/current") == OLD


def test_a_daemon_that_is_loaded_but_not_running_doesnt_stop_updates(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    (tmp_path / "loaded").mkdir(exist_ok=True)
    (tmp_path / "loaded/com.mukul.assistant-daemon").write_text("\tstate = not running\n")
    assert au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))["changed"]


def test_only_a_forced_manual_run_ends_a_rollback_hold_and_says_so(au, tmp_path):
    set_up(au, tmp_path)
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    au.rollback()
    kept = au.maybe_update(now=9000, from_pulse=False, opener=GitHub(NEW))
    assert kept["skipped_reason"] == "rolled-back" and os.readlink(tmp_path / ".assistant/app/current") == OLD
    result = au.maybe_update(now=9000, force=True, from_pulse=False, opener=GitHub(NEW))
    assert result["changed"] and result["ended_hold"] == NEW[:12]
    assert os.readlink(tmp_path / ".assistant/app/current") == NEW
    assert "held" not in json.loads((tmp_path / ".assistant/app-update.json").read_text())


def test_a_commit_whose_updater_wont_start_is_refused(au, tmp_path):
    set_up(au, tmp_path)
    broken = archive(NEW, {"bin/app_update.py": "import no_such_module_for_this_test\n"})
    result = au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: broken}))
    assert result["skipped_reason"] == "syntax-fail" and "won't start" in result["syntax_error"]
    assert "no_such_module_for_this_test" in result["syntax_error"]
    assert os.readlink(tmp_path / ".assistant/app/current") == OLD


def test_the_real_updater_starts_from_a_copy_of_this_tree(au, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", ".venv", ".worktrees", "__pycache__", "node_modules"))
    assert au.starts(copy) == (True, "ok")


def test_an_update_whose_restart_failed_is_a_failed_ledger_row(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    done = {"attempted": True, "changed": True, "installed": True, "install_rc": 0, "skipped_reason": None,
            "source": "download", "from_sha": OLD[:12], "to_sha": NEW[:12],
            "error": "install.sh couldn't restart a running service; the next run tries again"}
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", done, []))
    pulse.self_update_pulse(7)
    (row,) = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert row["outcome"] == "failed" and "couldn't restart" in row["evidence"]


def test_the_lessons_tool_still_opens_a_checkout_store_for_trimming(au, tmp_path, monkeypatch):
    curator = load("curator_trim_checkout", REPO / "bin/assistant-curator.py")
    monkeypatch.setitem(curator.TARGETS["claude"], "path", tmp_path / ".claude/CLAUDE.md")
    opened = []
    monkeypatch.setattr(curator.os, "execvp", lambda editor, argv: opened.append(argv))
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr(sys, "argv", ["assistant-curator.py", "trim", "--target", "claude"])
    curator.main()
    assert opened == [["true", str(tmp_path / ".claude/CLAUDE.md")]]


def test_the_manual_first_run_from_the_checkout_is_not_refused(au, tmp_path, monkeypatch):
    turn_on(tmp_path)
    monkeypatch.setattr(au, "HERE", REPO / "bin/app_update.py")
    result = au.maybe_update(now=1000, from_pulse=False, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["changed"] and result["install_rc"] == 0


def test_a_manual_install_has_no_time_limit(au, tmp_path, monkeypatch):
    turn_on(tmp_path)
    monkeypatch.setattr(au, "INSTALL_TIMEOUT_SEC", 1)
    monkeypatch.setenv("FAKE_INSTALL_SLEEP", "2")
    result = au.maybe_update(now=1000, from_pulse=False, opener=GitHub(NEW, {NEW: archive(NEW)}))
    assert result["install_rc"] == 0 and "error" not in result


def test_rollback_with_no_current_version_says_so(au, tmp_path):
    set_up(au, tmp_path)
    au.maybe_update(now=5000, opener=GitHub(NEW, {NEW: archive(NEW)}))
    (tmp_path / ".assistant/app/current").unlink()
    assert au.rollback()["rolled_back"] is False


def test_an_updater_that_crashes_is_a_failed_ledger_row_once_an_hour(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    broken = type(sys)("app_update")

    def crash(**_):
        raise ImportError("no module named self_update")
    broken.maybe_update = crash
    monkeypatch.setitem(sys.modules, "app_update", broken)
    pulse.self_update_pulse(7)
    pulse.self_update_pulse(8)
    rows = [r for r in ledger(tmp_path) if r["kind"] == "self-update"]
    assert len(rows) == 1 and rows[0]["outcome"] == "failed"
    assert "the updater stopped with ImportError: no module named self_update" in rows[0]["evidence"]


def test_download_updates_clear_the_git_updaters_dirty_clock(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    marker = tmp_path / ".assistant/self-update.json"
    marker.write_text(json.dumps({"last_attempt_ts": 5, "dirty_since_ts": 1}))
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", None, []))
    pulse.self_update_pulse(7)
    assert json.loads(marker.read_text()) == {"last_attempt_ts": 5}


def test_an_unreadable_config_in_a_downloaded_copy_says_to_fix_the_file(pulse, tmp_path):
    (tmp_path / ".assistant/config.json").write_text("{not json")
    result = pulse._download_off_result(now=1000)
    assert "config.json can't be read" in result["error"] and "install.sh" not in result["error"]


def test_a_launch_agent_with_a_comment_xml_parsers_reject_is_still_moved(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    current = f"{tmp_path}/.assistant/app/current"
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    text = (REPO / "launchagents/com.assistant.connector-outlook.plist").read_text()
    assert "--" in text.split("<!--", 1)[1].split("-->", 1)[0], "the real file still has the comment this test is about"
    (agents / "com.assistant.connector-outlook.plist").write_text(text.replace("__REPO__", current))
    (tmp_path / "loaded").mkdir(exist_ok=True)
    (tmp_path / "loaded/com.assistant.connector-outlook").write_text("arguments = { /Users/me/dev/assistant/bin/x }\n")
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.assistant.connector-outlook"]


def test_a_load_that_exits_zero_but_starts_nothing_fails_the_run(au, tmp_path, launchctl):
    set_up(au, tmp_path)
    agent(tmp_path, "com.assistant.slack-reactor", f"{tmp_path}/.assistant/app/current/bin/r.sh", "/old/bin/r.sh")
    (tmp_path / "fake-bin/launchctl").write_text(FAKE_LAUNCHCTL.replace(
        'bootstrap) label', 'bootstrap) echo "Bootstrap failed: 5" >&2; exit 5; label').replace(
        'load) label', 'load) exit 0; label'))
    result = au.maybe_update(now=5000, from_pulse=False, opener=GitHub(OLD))
    assert result["moved_services"] == ["com.assistant.slack-reactor (couldn't start: it isn't loaded from current afterwards)"]
    assert result["error"]


def test_a_dirty_clock_that_cant_be_cleared_never_stops_the_update(pulse, tmp_path, monkeypatch):
    turn_on(tmp_path)
    marker = tmp_path / ".assistant/self-update.json"
    marker.write_text(json.dumps({"dirty_since_ts": 1}))
    calls = []
    monkeypatch.setitem(sys.modules, "app_update", fake("app_update", None, calls))
    (tmp_path / ".assistant").chmod(0o500)
    try:
        pulse.self_update_pulse(7)
    finally:
        (tmp_path / ".assistant").chmod(0o700)
    assert calls == [("app_update", ())]
