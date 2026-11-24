"""Slack saves the worker couldn't mark done after a send, kept so they are tried again.

A row sent to an agent leaves the page, so a save that couldn't be marked would otherwise stay open in Slack with no
sign. Each one waits here, in `slack-held.json` in the Work queue's data folder, and is tried again after 1, 5 and 15
minutes. After the last try it stays, marked `gave_up`, and the page says so until the save is marked or Slack no
longer has it. The file is read and written under a file lock, because the server and the refresh are separate
processes, and it is written whole and renamed into place, so a restart keeps what was waiting.
"""
from __future__ import annotations

import contextlib
import fcntl

from work_queue import config

DELAYS = (60, 300, 900)
REASON_MAX = 300
SIGN_IN = "Slack sign-in needed"


@contextlib.contextmanager
def locked():
    path = config.data_dir() / "slack-held.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def read() -> dict:
    found = config.read_json(config.data_dir() / "slack-held.json", {})
    return found if isinstance(found, dict) else {}


def write(held: dict) -> None:
    config.write_json(config.data_dir() / "slack-held.json", held)


def hold(failures: dict, at: float) -> None:
    """Keep each save that couldn't be marked, {key: (its queue entry, why)}, and say when to try it next: after the
    last try, never."""
    if not failures:
        return
    with locked():
        held = read()
        for key, (cand, why) in failures.items():
            prior = held.get(key)
            tries = (prior.get("tries") if isinstance(prior, dict) and isinstance(prior.get("tries"), int) else 0) + 1
            held[key] = {"cid": cand["cid"], "ts": cand["ts"], "at": cand["at"], "tries": tries,
                         "reason": " ".join(str(why).split())[:REASON_MAX],
                         **({"gave_up": True} if tries > len(DELAYS) else {"next": at + DELAYS[tries - 1]})}
        write(held)


def due(at: float) -> list[tuple]:
    """(key, channel, message time, when it was sent) for each waiting save whose time has come."""
    with locked():
        return [(k, h["cid"], h["ts"], h["at"]) for k, h in read().items()
                if isinstance(h, dict) and not h.get("gave_up") and h.get("next", 0) <= at]


def drop(keep) -> None:
    """Forget every save `keep(key)` is false for: it was marked, or Slack no longer has it."""
    with locked():
        held = read()
        left = {k: h for k, h in held.items() if keep(k)}
        if left != held:
            write(left)


def message() -> str:
    """The one line the page shows for saves still unmarked after the last try, or "" when there are none."""
    gave_up = [h for h in read().values() if isinstance(h, dict) and h.get("gave_up")]
    if not gave_up:
        return ""
    why = list(dict.fromkeys(r.rstrip(".") for h in gave_up if (r := str(h.get("reason") or "").strip())))[:2]
    many = len(gave_up) > 1
    return (f"{len(gave_up)} save{'s' if many else ''} you sent to an agent couldn't be marked done in Slack"
            + (": " + "; ".join(why) if why else "") + ". "
            + ("Sign in to Slack again, then mark " if any(w.startswith(SIGN_IN) for w in why) else "Mark ")
            + ("them" if many else "it") + " done in Slack yourself.")
