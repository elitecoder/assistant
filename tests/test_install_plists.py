"""Regression test for the install.sh LaunchAgent-plist COPY vs LOAD contract
(D8).

The installer keeps a PLIST_SKIP list of opt-in daemons (the single-process
com.mukul.assistant-daemon and all six connector daemons) that must be COPIED
into ~/Library/LaunchAgents/ — so the documented
``launchctl load ~/Library/LaunchAgents/<label>.plist`` can succeed — but NEVER
auto-loaded (the pulse self-update re-runs install.sh, and an auto-load would
start a network daemon, and OAuth refreshes, behind the owner's back).

The bug: the PLIST_SKIP `continue` ran BEFORE the stage+copy, so a skipped plist
never LANDED and the documented manual load failed for every connector AND the
daemon. This drives the REAL install.sh in a throwaway $HOME with launchctl
stubbed on PATH and asserts each skipped plist is copied but NOT loaded, while a
non-skip plist IS loaded.

Runs install.sh as a subprocess (bash), so it is python-version agnostic.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

# The opt-in daemons install.sh must copy-but-not-load (must mirror PLIST_SKIP).
SKIP_PLISTS = (
    "com.mukul.assistant-daemon.plist",
    "com.assistant.connector-github.plist",
    "com.assistant.connector-gmail.plist",
    "com.assistant.connector-gcal.plist",
    "com.assistant.connector-slack.plist",
    "com.assistant.connector-outlook.plist",
)


@unittest.skipUnless(shutil.which("bash"), "bash required to run install.sh")
class InstallPlistCopyContractTests(unittest.TestCase):
    def _run_installer(self, home: Path) -> Path:
        """Run `install.sh --apply` against an isolated $HOME with launchctl
        stubbed. Returns the path to the launchctl call log."""
        stubbin = home / "stubbin"
        stubbin.mkdir(parents=True)
        launchctl_log = home / "launchctl.log"
        stub = stubbin / "launchctl"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "launchctl $*" >> "{launchctl_log}"\n'
            "exit 0\n")
        stub.chmod(0o755)
        # patch-settings.py backs up an existing settings.json; give it one so
        # the (unrelated) section-4 step completes and the installer returns 0.
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "settings.json").write_text("{}\n")

        env = {
            "HOME": str(home),
            "PATH": f"{stubbin}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        proc = subprocess.run(
            ["bash", str(REPO / "install.sh"), "--apply"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(proc.returncode, 0,
                         msg=f"install.sh failed:\n{proc.stdout[-2000:]}")
        return launchctl_log

    def test_skip_plists_are_copied_but_not_loaded(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            launchctl_log = self._run_installer(home)
            agents = home / "Library" / "LaunchAgents"
            loaded = launchctl_log.read_text() if launchctl_log.exists() else ""

            for base in SKIP_PLISTS:
                # COPIED: the file must actually land so a manual load can work.
                self.assertTrue((agents / base).exists(),
                                msg=f"{base} was not copied into {agents}")
                # NOT LOADED: its label must never appear in a launchctl call.
                label = base[:-len(".plist")]
                self.assertNotIn(label, loaded,
                                 msg=f"{base} must be copied but NOT loaded")

    def test_a_non_skip_plist_is_still_loaded(self):
        # Sanity: the copy-always change must not break loading of the plists
        # that SHOULD be reloaded (e.g. the pulse agent).
        with TemporaryDirectory() as td:
            home = Path(td)
            launchctl_log = self._run_installer(home)
            loaded = launchctl_log.read_text() if launchctl_log.exists() else ""
            self.assertIn("com.assistant.assistant-pulse", loaded,
                          msg="non-skip plists must still be launchctl-loaded")


if __name__ == "__main__":
    unittest.main()
