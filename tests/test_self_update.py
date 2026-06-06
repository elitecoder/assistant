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


if __name__ == "__main__":
    unittest.main()
