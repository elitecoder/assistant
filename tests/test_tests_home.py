"""The test run never uses the real home: conftest.py moves HOME to a throwaway folder before anything is imported."""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import conftest

REPO = Path(__file__).resolve().parent.parent
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def test_home_is_a_throwaway_folder_for_the_whole_run():
    assert conftest.TESTS_HOME.resolve() != REAL_HOME and REAL_HOME not in conftest.TESTS_HOME.resolve().parents
    assert conftest.TESTS_HOME.is_dir()
    assert conftest._REAL_HOME.resolve() == REAL_HOME, "the guard for the real home still knows where it is"


def test_a_script_that_reads_home_when_imported_gets_the_throwaway_one():
    """The lesson extractor works out its log's path when it is imported. In a fresh run of the test setup, with the
    real HOME in the environment, that path must not be in the real home."""
    code = ("import os, sys, importlib.util; sys.path.insert(0, 'tests'); import conftest; "
            "spec = importlib.util.spec_from_file_location('le', 'bin/lesson-extractor.py'); "
            "le = importlib.util.module_from_spec(spec); spec.loader.exec_module(le); "
            "print(le.AUDIT_LOG); print(conftest.TESTS_HOME); print(os.environ['PLAYWRIGHT_BROWSERS_PATH'])")
    env = {k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_BROWSERS_PATH"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
                         env={**env, "HOME": str(REAL_HOME)})
    log, home, browsers = out.stdout.split()
    assert Path(log).resolve().is_relative_to(Path(home).resolve()), log
    assert not Path(log).resolve().is_relative_to(REAL_HOME)
    assert Path(browsers).resolve().is_relative_to(REAL_HOME), "the test browser is still looked for in the real home"


def test_a_test_that_sets_no_home_of_its_own_never_writes_the_real_one():
    """Whatever HOME is by now (some older tests set it and never put it back), it is not the real home."""
    marker = Path.home() / ".assistant" / "written-by-a-test"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x")
    assert not marker.resolve().is_relative_to(REAL_HOME)
    assert not (REAL_HOME / ".assistant" / "written-by-a-test").exists()
