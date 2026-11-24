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
SHOWN = 3
WORDS = {"limit": "Slack asked us to slow down", "sign_in": "Slack needs you to sign in again",
         "silent": "Slack didn't answer", "ledger": "Assistant couldn't record the change (is your disk full?)",
         "other": "Slack refused the change"}
SIGN_IN_ERRORS = ("invalid_auth", "not_authed", "token_expired")
SILENT_AFTER = 80


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


def plain(reason) -> str:
    """What a reason reads as on the page, whatever its raw text: the raw text is never shown, because it can be a
    Slack error code, a traceback or a sentence meant for another place."""
    text = str(reason or "").strip()
    if text == "ratelimited":
        return WORDS["limit"]
    if text in SIGN_IN_ERRORS or text.startswith(SIGN_IN):
        return WORDS["sign_in"]
    if text.startswith("Assistant couldn't record"):
        return WORDS["ledger"]
    if text.startswith(("TimeoutError", "Error")) or len(text) > SILENT_AFTER:
        return WORDS["silent"]
    return WORDS["other"]


def well_formed(held) -> bool:
    """An entry this module wrote: its ids and send time are text, and a save still waiting has a time to be tried."""
    return (isinstance(held, dict) and all(isinstance(held.get(f), str) for f in ("cid", "ts", "at"))
            and (held.get("gave_up") is True or (isinstance(held.get("next"), (int, float))
                                                 and not isinstance(held.get("next"), bool))))


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
            held[key] = {"cid": cand["cid"], "ts": cand["ts"], "at": cand["at"], "link": cand.get("link") or "",
                         "tries": tries, "reason": " ".join(str(why).split())[:REASON_MAX],
                         **({"gave_up": True} if tries > len(DELAYS) else {"next": at + DELAYS[tries - 1]})}
        write(held)


def due(at: float) -> list[tuple]:
    """(key, channel, message time, when it was sent, link) for each waiting save whose time has come. An entry that
    isn't well formed is skipped, so one bad entry never stops the others from being tried."""
    with locked():
        return [(k, h["cid"], h["ts"], h["at"], h.get("link") if isinstance(h.get("link"), str) else "")
                for k, h in read().items() if well_formed(h) and not h.get("gave_up") and h["next"] <= at]


def drop(keep) -> None:
    """Forget every save `keep(key)` is false for: it was marked, or Slack no longer has it."""
    with locked():
        held = read()
        left = {k: h for k, h in held.items() if keep(k)}
        if left != held:
            write(left)


def line() -> dict:
    """The line the page shows, {"text", "links", "more"}, or {} when there is nothing to say. It covers the saves still
    unmarked after the last try, and at once any that need you to sign in, because trying again can't fix that."""
    shown = [h for h in read().values()
             if well_formed(h) and (h.get("gave_up") or plain(h.get("reason")) == WORDS["sign_in"])]
    if not shown:
        return {}
    why = list(dict.fromkeys(plain(h.get("reason")) for h in shown))
    many = len(shown) > 1
    links = [h["link"] for h in shown[:SHOWN] if isinstance(h.get("link"), str) and h["link"].startswith("https://")]
    text = (f"{len(shown)} save{'s' if many else ''} you sent to an agent couldn't be marked done in Slack: "
            + "; ".join(why[:2]) + ". "
            + ("Sign in to Slack again, then mark " if WORDS["sign_in"] in why else "Mark ")
            + ("them" if many else "it") + " done in Slack yourself."
            + ("" if links else " They are still open in your Slack Later list."))
    return {"text": text, "links": links, "more": max(0, len(shown) - SHOWN) if links else 0}
