"""The test run never uses the real home: conftest.py moves HOME to a throwaway folder before anything is imported.

Nothing here may reach the real home even when the protection is broken on purpose (a mutation check does that):
where a test needs "the real home", a temporary folder stands in for it."""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import conftest
import installer_sandbox

REPO = Path(__file__).resolve().parent.parent
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def fresh_run(code: str, home: Path, **env) -> list[str]:
    """Run code in a new interpreter that loads the test setup, with HOME set to the given folder."""
    set_by_the_setup = ("PLAYWRIGHT_BROWSERS_PATH", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")
    kept = {k: v for k, v in os.environ.items() if k not in set_by_the_setup}
    done = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tests'); import conftest; " + code],
                          cwd=REPO, capture_output=True, text=True, check=True, env={**kept, **env, "HOME": str(home)})
    return done.stdout.splitlines()


def test_home_is_a_throwaway_folder_of_its_own():
    home = conftest.TESTS_HOME.resolve()
    assert home.is_dir() and home.name.startswith("assistant-tests-home-"), "a new folder, not a shared one"
    assert home != REAL_HOME and REAL_HOME not in home.parents
    assert conftest._REAL_HOME.resolve() == REAL_HOME == installer_sandbox.real_home()


def test_a_test_that_sets_no_home_of_its_own_is_in_the_throwaway_one():
    assert Path.home().resolve() == conftest.TESTS_HOME.resolve()


def test_a_script_that_reads_home_when_imported_gets_the_throwaway_one(tmp_path):
    """The lesson extractor works out its log's path when it is imported. In a fresh run of the test setup that
    starts with HOME somewhere else, the path must be in the throwaway folder."""
    elsewhere = tmp_path / "stands-in-for-the-real-home"
    elsewhere.mkdir()
    log, home, browsers = fresh_run(
        "import importlib.util, os; spec = importlib.util.spec_from_file_location('le', 'bin/lesson-extractor.py'); "
        "le = importlib.util.module_from_spec(spec); spec.loader.exec_module(le); "
        "print(le.AUDIT_LOG); print(conftest.TESTS_HOME); print(os.environ['PLAYWRIGHT_BROWSERS_PATH'])", elsewhere)
    assert Path(log).resolve().is_relative_to(Path(home).resolve()), log
    assert not Path(log).resolve().is_relative_to(elsewhere.resolve())
    assert list(elsewhere.iterdir()) == []
    assert Path(browsers).resolve().is_relative_to(REAL_HOME), "the test browser is still looked for in the real home"


def test_the_throwaway_folder_is_removed_when_the_run_ends(tmp_path):
    made, = fresh_run("print(conftest.TESTS_HOME)", tmp_path)
    assert Path(made).name.startswith("assistant-tests-home-") and not Path(made).exists()


def test_removal_only_ever_touches_the_folder_the_run_made(tmp_path):
    """Whatever it is handed, the clean-up leaves alone a folder that isn't one of these throwaway homes, and a
    child process a test forked never removes its parent's."""
    other = tmp_path / "somebody-elses-folder"
    other.mkdir()
    conftest._remove_tests_home(made=str(other))
    assert other.is_dir()
    ours = tmp_path / "assistant-tests-home-example"
    ours.mkdir()
    conftest._remove_tests_home(made=str(ours), pid=os.getpid() + 1)
    assert ours.is_dir(), "another process's folder"
    conftest._remove_tests_home(made=str(ours), pid=os.getpid())
    assert not ours.exists()


def test_uv_is_left_alone_when_xdg_variables_already_say_where_it_keeps_things(tmp_path):
    shown = "import os; print(os.environ.get('UV_CACHE_DIR', '-')); print(os.environ.get('UV_PYTHON_INSTALL_DIR', '-'))"
    cache, pythons = fresh_run(shown, tmp_path, XDG_CACHE_HOME="", XDG_DATA_HOME="")
    assert (Path(cache), Path(pythons)) == (conftest._REAL_HOME / ".cache/uv", conftest._REAL_HOME / ".local/share/uv/python")
    cache, pythons = fresh_run(shown, tmp_path, XDG_CACHE_HOME=str(tmp_path / "c"), XDG_DATA_HOME=str(tmp_path / "d"))
    assert (cache, pythons) == ("-", "-"), "uv works both out from the XDG variables itself"


def test_data_folders_named_for_the_login_session_do_not_reach_the_tests(tmp_path):
    names = ("ASSISTANT_DIR", "CMUX_WATCHER_ASSISTANT_DIR", "WQ_DATA")
    shown = fresh_run("import os; print([n for n in %r if n in os.environ])" % (names,), tmp_path,
                      **{name: str(tmp_path / "real-data") for name in names})
    assert shown == ["[]"]


def test_a_test_that_changes_home_and_never_puts_it_back_does_not_move_the_next_one(tmp_path):
    """Two tests in a run of their own: the first sets HOME the way some older tests do, the second must still
    be in the run's throwaway folder."""
    case = tmp_path / "test_two_in_a_row.py"
    case.write_text(
        "import os\nfrom pathlib import Path\nimport conftest\n\n"
        "def test_one_sets_home_and_leaves_it(tmp_path):\n    os.environ['HOME'] = str(tmp_path)\n\n"
        "def test_two_is_still_in_the_runs_home():\n    assert Path.home() == conftest.TESTS_HOME\n")
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "conftest", "-p", "no:cacheprovider", str(case)],
                          cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(REPO / "tests"), "HOME": str(tmp_path)})
    assert done.returncode == 0 and "2 passed" in done.stdout, done.stdout + done.stderr
