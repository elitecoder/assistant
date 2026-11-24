"""Checks that the test run never uses the real home (tests/conftest.py)."""

CHECKS = (
    (
        "the test run moves HOME to a throwaway folder before anything is imported",
        "tests/conftest.py",
        'os.environ["HOME"] = str(TESTS_HOME)\n',
        "",
        "tests/test_tests_home.py::test_a_script_that_reads_home_when_imported_gets_the_throwaway_one",
    ),
    (
        "the throwaway home is a new folder, not the real one",
        "tests/conftest.py",
        'TESTS_HOME = Path(tempfile.mkdtemp(prefix="assistant-tests-home-"))\n',
        "TESTS_HOME = _REAL_HOME\n",
        "tests/test_tests_home.py::test_home_is_a_throwaway_folder_for_the_whole_run",
    ),
    (
        "the test browser is still found in the real home",
        "tests/conftest.py",
        '(("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS), ',
        "(",
        "tests/test_tests_home.py::test_a_script_that_reads_home_when_imported_gets_the_throwaway_one",
    ),
    (
        "the installer tests refuse the real home whatever HOME says",
        "tests/installer_sandbox.py",
        "    if home in (real_home(), Path.home().resolve()):\n",
        "    if home == Path.home().resolve():\n",
        "tests/test_install_optin_prompt.py::test_installer_environment_rejects_real_home",
    ),
)
