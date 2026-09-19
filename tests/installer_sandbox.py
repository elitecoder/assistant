"""Keep installer tests away from the host's services."""

import os
import shutil
import sys
from pathlib import Path


def installer_env(home: Path) -> dict[str, str]:
    home = home.resolve()
    if home == Path.home().resolve():
        raise ValueError("Installer tests require an isolated HOME")
    stubbin = home / "stubbin"
    stubbin.mkdir(parents=True)
    scratch = home / "scratch"
    scratch.mkdir()
    launchctl = stubbin / "launchctl"
    launchctl.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
        'case "$1" in\n'
        '  print) exit 1 ;;\n'
        '  bootout|bootstrap|load) exit 0 ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
    )
    launchctl.chmod(0o755)
    cmux = stubbin / "cmux"
    cmux.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$HOME/cmux.log"\n'
        'exit 0\n'
    )
    cmux.chmod(0o755)
    bash_env = home / "installer-test.bash"
    # install.sh calls cmux by absolute path when installing Factory hooks.
    bash_env.write_text(
        '/bin/launchctl() { "$ASSISTANT_TEST_LAUNCHCTL" "$@"; }\n'
        '/usr/bin/launchctl() { "$ASSISTANT_TEST_LAUNCHCTL" "$@"; }\n'
        '/Applications/cmux.app/Contents/Resources/bin/cmux() {\n'
        '  "$CMUX_BIN" "$@"\n'
        '}\n'
        'mktemp() {\n'
        '  [[ "$#" == 1 && "$1" == -d ]] || return 2\n'
        '  mkdir "$TMPDIR/plist-stage" || return\n'
        '  printf "%s\\n" "$TMPDIR/plist-stage"\n'
        '}\n'
    )
    path = os.pathsep.join([
        str(stubbin), str(Path(sys.executable).parent),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        "/usr/sbin", "/sbin",
    ])
    if shutil.which("launchctl", path=path) != str(launchctl):
        raise RuntimeError("Installer tests must use the recording launchctl")
    return {
        "HOME": str(home),
        "PATH": path,
        "TMPDIR": str(scratch),
        "BASH_ENV": str(bash_env),
        "ASSISTANT_TEST_LAUNCHCTL": str(launchctl),
        "CMUX_BIN": str(cmux),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def assert_recorded_services(home: Path, *, apply: bool) -> None:
    calls = (home / "launchctl.log").read_text().splitlines()
    assert any(call.startswith("print ") for call in calls)
    loads = [call for call in calls if call.startswith(("bootstrap ", "load "))]
    if apply:
        assert loads, "The installer must reach the recording service loader"
        assert all(str(home / "Library/LaunchAgents") in call for call in loads)
    else:
        assert not loads, "A dry run must not load services"
