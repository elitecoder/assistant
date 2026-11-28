"""Checks that the installer links the idle-return hook into ~/.claude/hooks."""

CHECKS = (
    (
        "the installer links the idle-return hook",
        "install.sh",
        'ensure_symlink "$HOME_DIR/.claude/hooks/idle-return.py"         "$REPO_ROOT/hooks/idle-return.py"\n',
        "",
        "tests/test_install_cmux_sessions.py::test_fresh_apply_links_and_registers_the_session_hooks",
    ),
)
