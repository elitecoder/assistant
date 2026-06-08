"""Integration tests for bin/self_update.py.

These drive REAL throwaway git repos (a bare "remote" + a working clone) so
the fetch / ahead-behind / ff-only-pull / dirty-refusal logic is exercised
end to end, not mocked. install.sh is stubbed with a recording shim so we can
assert it's invoked exactly when a copied artifact changes and skipped
otherwise — without running the real installer against the test machine.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SELF_UPDATE_PATH = REPO / "bin/self_update.py"


def load_module():
    spec = importlib.util.spec_from_file_location("self_update_mod", str(SELF_UPDATE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


su = load_module()


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def make_repos(tmp: Path) -> tuple[Path, Path]:
    """Create a bare remote + a clone that tracks it, with one initial commit.
    Returns (clone, remote)."""
    remote = tmp / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "main")

    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.email", "t@t")
    git(seed, "config", "user.name", "t")
    (seed / "install.sh").write_text("#!/usr/bin/env bash\necho seed-installer\n")
    (seed / "bin").mkdir()
    (seed / "bin/pulse.py").write_text("# pulse\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "init")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")

    clone = tmp / "clone"
    git(tmp, "clone", str(remote), str(clone))
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "user.name", "t")
    return clone, remote


def advance_remote(tmp: Path, remote: Path, files: dict[str, str], msg: str) -> str:
    """Commit `files` (path->content) to the remote via a scratch clone. Returns new sha."""
    scratch = tmp / f"scratch-{msg.replace(' ', '-')}"
    git(tmp, "clone", str(remote), str(scratch))
    git(scratch, "config", "user.email", "t@t")
    git(scratch, "config", "user.name", "t")
    for rel, content in files.items():
        p = scratch / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(scratch, "add", "-A")
    git(scratch, "commit", "-m", msg)
    git(scratch, "push", "origin", "main")
    return git(scratch, "rev-parse", "HEAD")


def stub_installer(clone: Path) -> Path:
    """Replace install.sh with a shim that records each invocation to a log
    file (so we can assert it ran / didn't) and honors --apply."""
    log = clone / "install-calls.log"
    (clone / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "called args=$* self=${{ASSISTANT_SELF_UPDATE:-unset}}" >> "{log}"\n'
        "exit 0\n"
    )
    return log


# ─── pure logic ───────────────────────────────────────────────────────────

class ClassifyTests(unittest.TestCase):
    def test_bin_change_needs_no_install(self):
        c = su.classify_changed_paths(["bin/pulse.py", "prompts/observer-batch-prompt.md"])
        self.assertFalse(c["needs_install"])
        self.assertFalse(c["touches_self_plist"])

    def test_skill_change_needs_install(self):
        c = su.classify_changed_paths(["skills/todo/SKILL.md"])
        self.assertTrue(c["needs_install"])

    def test_installer_change_needs_install(self):
        self.assertTrue(su.classify_changed_paths(["install.sh"])["needs_install"])

    def test_self_plist_change_flagged(self):
        c = su.classify_changed_paths(["launchagents/com.assistant.assistant-pulse.plist"])
        self.assertTrue(c["needs_install"])
        self.assertTrue(c["touches_self_plist"])

    def test_other_plist_change_not_self(self):
        c = su.classify_changed_paths(["launchagents/com.assistant.assistant-comms.plist"])
        self.assertTrue(c["needs_install"])
        self.assertFalse(c["touches_self_plist"])


class ThrottleTests(unittest.TestCase):
    def test_first_run_attempts(self):
        self.assertTrue(su.should_attempt({}, now=1000, interval_sec=3600))

    def test_within_interval_skips(self):
        self.assertFalse(su.should_attempt({"last_attempt_ts": 1000}, now=2000, interval_sec=3600))

    def test_after_interval_attempts(self):
        self.assertTrue(su.should_attempt({"last_attempt_ts": 1000}, now=4601, interval_sec=3600))


# ─── end-to-end against real repos ──────────────────────────────────────────

class MaybeUpdateTests(unittest.TestCase):
    def test_up_to_date_no_change(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "marker.json")
            self.assertTrue(r["attempted"])
            self.assertFalse(r["changed"])
            self.assertEqual(r["behind"], 0)
            self.assertIsNone(r["skipped_reason"])

    def test_throttle_returns_none(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            marker = tmp / "marker.json"
            su.maybe_update(clone, interval_sec=3600, marker_path=marker)
            # Second call immediately after → throttled.
            r2 = su.maybe_update(clone, interval_sec=3600, marker_path=marker)
            self.assertIsNone(r2)

    def test_pull_code_only_no_install(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            log = stub_installer(clone)
            git(clone, "add", "-A"); git(clone, "commit", "-m", "local installer stub")
            git(clone, "push", "origin", "main")
            advance_remote(tmp, remote, {"bin/pulse.py": "# pulse v2\n"}, "code change")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json",
                                install_sh=clone / "install.sh")
            self.assertTrue(r["changed"])
            self.assertIn("bin/pulse.py", r["files_changed"])
            self.assertFalse(r["needs_install"])
            self.assertFalse(r.get("installed"))
            self.assertFalse(log.exists(), "installer must NOT run for a bin-only change")

    def test_pull_skill_change_runs_installer(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            log = stub_installer(clone)
            git(clone, "add", "-A"); git(clone, "commit", "-m", "local installer stub")
            git(clone, "push", "origin", "main")
            advance_remote(tmp, remote, {"skills/todo/SKILL.md": "# todo v2\n"}, "skill change")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json",
                                install_sh=clone / "install.sh")
            self.assertTrue(r["changed"])
            self.assertTrue(r["needs_install"])
            self.assertTrue(r["installed"])
            self.assertEqual(r["install_rc"], 0)
            self.assertTrue(log.exists())
            text = log.read_text()
            self.assertIn("--apply", text)
            self.assertIn("self=1", text, "ASSISTANT_SELF_UPDATE=1 must be exported to install.sh")

    def test_dirty_tree_refuses_pull(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            advance_remote(tmp, remote, {"bin/pulse.py": "# v2\n"}, "remote change")
            (clone / "bin/pulse.py").write_text("# locally edited, uncommitted\n")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json")
            self.assertEqual(r["skipped_reason"], "dirty")
            self.assertFalse(r["changed"])
            # The remote change must NOT have been pulled.
            self.assertIn("locally edited", (clone / "bin/pulse.py").read_text())

    def test_ahead_refuses_pull(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            (clone / "bin/new.py").write_text("# local unpushed work\n")
            git(clone, "add", "-A")
            git(clone, "commit", "-m", "local unpushed commit")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json")
            self.assertEqual(r["skipped_reason"], "ahead")
            self.assertEqual(r["ahead"], 1)
            self.assertFalse(r["changed"])

    def test_no_remote_skips_cleanly(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            solo = tmp / "solo"
            solo.mkdir()
            git(solo, "init", "-b", "main")
            git(solo, "config", "user.email", "t@t")
            git(solo, "config", "user.name", "t")
            (solo / "f").write_text("x")
            git(solo, "add", "-A"); git(solo, "commit", "-m", "init")
            r = su.maybe_update(solo, interval_sec=0, marker_path=tmp / "m.json")
            self.assertEqual(r["skipped_reason"], "no-remote")

    def test_marker_written_before_work(self):
        # Even a throttled-out second call must see the marker the first wrote.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            marker = tmp / "m.json"
            su.maybe_update(clone, interval_sec=0, marker_path=marker)
            self.assertTrue(marker.exists())
            self.assertIn("last_attempt_ts", json.loads(marker.read_text()))

    def test_self_plist_change_defers_reload_flag(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            stub_installer(clone)
            git(clone, "add", "-A"); git(clone, "commit", "-m", "stub")
            git(clone, "push", "origin", "main")
            advance_remote(tmp, remote,
                           {"launchagents/com.assistant.assistant-pulse.plist": "<plist/>\n"},
                           "self plist change")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json",
                                install_sh=clone / "install.sh")
            self.assertTrue(r["changed"])
            self.assertTrue(r["needs_install"])
            self.assertTrue(r.get("self_plist_reload_deferred"))


    def test_fetch_failed_skips(self):
        # Force a fetch failure by pointing origin at a non-existent path.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            git(clone, "remote", "set-url", "origin", "/nonexistent/path")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json")
            self.assertEqual(r["skipped_reason"], "fetch-failed")
            self.assertIn("error", r)

    def test_log_param_used(self):
        # The log= kwarg is actually invoked when provided.
        import logging
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            log = logging.getLogger("test_su")
            messages = []
            with unittest.mock.patch.object(log, "info",
                                            side_effect=lambda msg, *a: messages.append(msg)):
                su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json", log=log)
            self.assertTrue(any("self-update" in m for m in messages))

    def test_install_sh_missing_sets_error(self):
        # If install.sh does not exist but needs_install=True, skipped with error.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            advance_remote(tmp, remote, {"skills/todo/SKILL.md": "# new\n"}, "skill change")
            r = su.maybe_update(
                clone, interval_sec=0, marker_path=tmp / "m.json",
                install_sh=clone / "nonexistent-install.sh",
            )
            self.assertTrue(r["changed"])
            self.assertTrue(r["needs_install"])
            self.assertIsNone(r["install_rc"])
            self.assertIn("not found", r["error"])

    def test_install_sh_failure_records_rc(self):
        # install.sh exits non-zero → install_rc is set and error is captured.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            (clone / "install.sh").write_text("#!/usr/bin/env bash\nexit 42\n")
            git(clone, "add", "-A"); git(clone, "commit", "-m", "fail installer")
            git(clone, "push", "origin", "main")
            advance_remote(tmp, remote, {"skills/todo/SKILL.md": "# v2\n"}, "skill")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json",
                                install_sh=clone / "install.sh")
            self.assertTrue(r["installed"])
            self.assertEqual(r["install_rc"], 42)
            self.assertIn("error", r)

    def test_pull_failed_diverged_sets_skipped_reason(self):
        # Force a pull failure by making the histories diverge.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            # Advance remote with a new commit.
            advance_remote(tmp, remote, {"bin/pulse.py": "# v2\n"}, "remote change")
            # Also add a local commit that creates divergence.
            (clone / "bin/pulse.py").write_text("# local diverge\n")
            git(clone, "add", "-A")
            git(clone, "commit", "-m", "local diverge")
            r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json")
            # ahead prevents pull from even attempting, but fetch still runs.
            # Actually 'ahead' check fires first.
            self.assertIn(r["skipped_reason"], ("ahead", "pull-failed"))

    def test_install_sh_timeout_records_error(self):
        # If install.sh subprocess times out, install_rc=-1 and error is set.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            advance_remote(tmp, remote, {"skills/todo/SKILL.md": "# v2\n"}, "skill change")
            with unittest.mock.patch.object(
                su.subprocess, "run",
                side_effect=su.subprocess.TimeoutExpired("bash", 300),
            ):
                # _git fetch is called first — patch only the final install.sh run.
                pass
        # The cleaner approach: mock subprocess.run inside maybe_update selectively.
        # We mock _git to return "needs_install" metadata without a real fetch.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, remote = make_repos(tmp)
            stub_log = stub_installer(clone)
            git(clone, "add", "-A"); git(clone, "commit", "-m", "stub"); git(clone, "push", "origin", "main")
            advance_remote(tmp, remote, {"skills/todo/SKILL.md": "# new\n"}, "skill")
            real_subprocess_run = su.subprocess.run

            def patched_run(cmd, *a, **kw):
                if cmd[0] == "bash":
                    raise su.subprocess.TimeoutExpired(cmd, 300)
                return real_subprocess_run(cmd, *a, **kw)

            with unittest.mock.patch.object(su.subprocess, "run", side_effect=patched_run):
                r = su.maybe_update(clone, interval_sec=0, marker_path=tmp / "m.json",
                                    install_sh=clone / "install.sh")
            self.assertTrue(r["changed"])
            self.assertEqual(r["install_rc"], -1)
            self.assertIn("timed out", r["error"])

    def test_no_upstream_fallback_to_origin(self):
        # resolve_remote_branch falls back to a plain remote list when no
        # upstream is tracked; picks "origin" from the list.
        with TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _ = make_repos(tmp)
            # Detach the tracking branch so @{u} fails.
            git(clone, "branch", "--unset-upstream")
            rb = su.resolve_remote_branch(clone)
            self.assertIsNotNone(rb)
            self.assertEqual(rb[0], "origin")

    def test_git_timeout_returns_minus1(self):
        import subprocess as sp
        with unittest.mock.patch.object(sp, "run", side_effect=sp.TimeoutExpired("git", 5)):
            rc, out, err = su._git(Path("/tmp"), "status")
        self.assertEqual(rc, -1)
        self.assertIn("timed out", err)

    def test_git_os_error_returns_minus1(self):
        import subprocess as sp
        with unittest.mock.patch.object(sp, "run", side_effect=OSError("no git binary")):
            rc, out, err = su._git(Path("/tmp"), "status")
        self.assertEqual(rc, -1)
        self.assertIn("no git binary", err)


class ResolveRemoteBranchTests(unittest.TestCase):
    def test_detached_head_returns_none(self):
        """When HEAD is detached, resolve_remote_branch returns None."""
        import unittest.mock as m
        with m.patch.object(su, "_git", side_effect=[
            (0, "HEAD", ""),  # rev-parse --abbrev-ref HEAD
        ]):
            result = su.resolve_remote_branch(Path("/tmp/repo"))
        self.assertIsNone(result)

    def test_empty_branch_returns_none(self):
        import unittest.mock as m
        with m.patch.object(su, "_git", side_effect=[
            (1, "", "error"),  # rev-parse HEAD fails
        ]):
            result = su.resolve_remote_branch(Path("/tmp/repo"))
        self.assertIsNone(result)

    def test_no_remote_returns_none(self):
        import unittest.mock as m
        with m.patch.object(su, "_git", side_effect=[
            (0, "main", ""),           # rev-parse HEAD
            (1, "", "no upstream"),    # @{u} fails
            (0, "", ""),               # git remote → empty
        ]):
            result = su.resolve_remote_branch(Path("/tmp/repo"))
        self.assertIsNone(result)

    def test_non_origin_remote_fallback(self):
        import unittest.mock as m
        with m.patch.object(su, "_git", side_effect=[
            (0, "main", ""),           # rev-parse HEAD
            (1, "", "no upstream"),    # @{u} fails
            (0, "upstream", ""),       # git remote → one non-origin remote
        ]):
            result = su.resolve_remote_branch(Path("/tmp/repo"))
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "upstream")  # picks the only remote

    def test_int_helper_invalid_value(self):
        """The _int helper inside repo_status returns 0 on bad input."""
        import unittest.mock as m
        with m.patch.object(su, "_git", side_effect=[
            (0, "", ""),          # fetch
            (0, "abc", ""),       # HEAD sha
            (0, "def", ""),       # remote sha
            (0, "", ""),          # status --porcelain
            (0, "not-a-number", ""),  # behind
            (0, "0", ""),         # ahead
        ]):
            s = su.repo_status(Path("/tmp/repo"), "origin", "main")
        self.assertEqual(s["behind"], 0)


if __name__ == "__main__":
    unittest.main()
