"""Stand-ins for the feedback tests: another process that holds the feedback lock, and a call that must come back."""
from __future__ import annotations

import subprocess
import sys
import threading

HOLDER = """
import fcntl, os, sys
os.makedirs(sys.argv[1], exist_ok=True)
fd = os.open(os.path.join(sys.argv[1], "feedback.lock"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()  # holds the lock until the test lets go (or dies)
"""


def hold_lock(folder):
    """Another process takes feedback.lock in `folder` and keeps it. Returns the function that lets go. It is another
    process, because a lock held by this one can be asked for again (a hang this way would not be a test failure)."""
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"

    def release():
        child.stdin.close()
        child.wait(30)
        child.stdout.close()
    return release


def comes_back(call, seconds=30):
    """`call()` in a thread: ("returned", value) or ("raised", exception). A call still waiting after `seconds` fails the
    test, which is what a lock that blocks for ever must do instead of hanging the run."""
    out = []

    def run():
        try:
            out.append(("returned", call()))
        except Exception as exc:  # noqa: BLE001 - the caller looks at what was raised
            out.append(("raised", exc))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"still waiting after {seconds} s: the lock is being waited for without a limit"
    return out[0]
