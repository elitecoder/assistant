"""Checks that the test run never uses the real home (tests/conftest.py).

No entry here may point a run at the real home: each mutant is caught by a test in which a temporary folder
stands in for it."""

CHECKS = (
    (
        "the test run moves HOME to a throwaway folder before anything is imported",
        "tests/conftest.py",
        'os.environ["HOME"] = str(TESTS_HOME)\n',
        "",
        "tests/test_tests_home.py::test_a_script_that_reads_home_when_imported_gets_the_throwaway_one",
    ),
    (
        "the throwaway home is a new folder of its own, not a shared one",
        "tests/conftest.py",
        "TESTS_HOME = Path(_MADE)\n",
        "TESTS_HOME = Path(tempfile.gettempdir())\n",
        "tests/test_tests_home.py::test_home_is_a_throwaway_folder_of_its_own",
    ),
    (
        "the test browser is still found in the real home",
        "tests/conftest.py",
        '_KEPT = {"PLAYWRIGHT_BROWSERS_PATH": "Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright",\n         "npm_config_cache": ".npm"}',
        '_KEPT = {"npm_config_cache": ".npm"}',
        "tests/test_tests_home.py::test_a_script_that_reads_home_when_imported_gets_the_throwaway_one",
    ),
    (
        "the installer tests refuse the real home whatever HOME says",
        "tests/installer_sandbox.py",
        "    if home in (real_home(), Path.home().resolve()):\n",
        "    if home == Path.home().resolve():\n",
        "tests/test_install_optin_prompt.py::test_installer_environment_rejects_real_home",
    ),
    (
        "the throwaway home is removed when the run ends",
        "tests/conftest.py",
        "atexit.register(_remove_tests_home)\n",
        "",
        "tests/test_tests_home.py::test_the_throwaway_folder_is_removed_when_the_run_ends",
    ),
    (
        "the clean-up never removes a folder that isn't a throwaway home",
        "tests/conftest.py",
        "    if os.getpid() == pid and Path(made).name.startswith(_TESTS_HOME_PREFIX):\n",
        "    if os.getpid() == pid:\n",
        "tests/test_tests_home.py::test_removal_only_ever_touches_the_folder_the_run_made",
    ),
    (
        "a forked child never removes the run's throwaway home",
        "tests/conftest.py",
        "    if os.getpid() == pid and Path(made).name.startswith(_TESTS_HOME_PREFIX):\n",
        "    if Path(made).name.startswith(_TESTS_HOME_PREFIX):\n",
        "tests/test_tests_home.py::test_removal_only_ever_touches_the_folder_the_run_made",
    ),
    (
        "data folders named for the login session are dropped before the tests start",
        "tests/conftest.py",
        '    os.environ.pop(_place, None)\n',
        '    pass\n',
        "tests/test_tests_home.py::test_data_folders_named_for_the_login_session_do_not_reach_the_tests",
    ),
    (
        "every test starts in the run's throwaway home, whatever the one before it left",
        "tests/conftest.py",
        '    monkeypatch.setenv("HOME", str(TESTS_HOME))\n',
        "    pass\n",
        "tests/test_tests_home.py::test_a_test_that_changes_home_and_never_puts_it_back_does_not_move_the_next_one",
    ),
    (
        "uv is left alone when XDG_CACHE_HOME already says where its cache is",
        "tests/conftest.py",
        'if not os.environ.get("XDG_CACHE_HOME"):\n',
        "if True:\n",
        "tests/test_tests_home.py::test_uv_is_left_alone_when_xdg_variables_already_say_where_it_keeps_things",
    ),
    (
        "uv is left alone when XDG_DATA_HOME already says where its Pythons are",
        "tests/conftest.py",
        'if not os.environ.get("XDG_DATA_HOME"):\n',
        "if True:\n",
        "tests/test_tests_home.py::test_uv_is_left_alone_when_xdg_variables_already_say_where_it_keeps_things",
    ),
)
