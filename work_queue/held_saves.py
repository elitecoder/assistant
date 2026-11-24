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
import re
import urllib.parse

from assistant import effects

from work_queue import config

DELAYS = (60, 300, 900)
REASON_MAX = 300
SIGN_IN = "Slack sign-in needed"
SHOWN = 3
SAVE_PATH = re.compile(r"/archives/([CDG][A-Z0-9]{8,11})/p(\d{10})(\d{6})")
# What a reason reads as on the page. The Work queue server fills LEDGER with the ledger's own sentences, each naming
# its phrase, so a reason is matched to a sentence the ledger wiring wrote, never to a prefix or a length.
LEDGER: dict = {}
WORKER_PREFIX = "Couldn't mark the save done: "
WORDS = {"limit": "Slack asked us to slow down", "sign_in": "Slack needs you to sign in again",
         "silent": "Slack didn't answer", "other": "Slack refused the change",
         "record": "Assistant couldn't record the change (is your disk full?)",
         "fault": "Assistant hit a fault recording the change", "progress": "an earlier try may still be running",
         "approval": "Assistant couldn't confirm your approval", "worker": "Assistant couldn't finish marking it"}
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


def slack_save(link: str) -> tuple[str, str] | None:
    """The channel and message of the Later save a Slack permalink points at, or None for any other link."""
    parts = urllib.parse.urlsplit(link)
    found = SAVE_PATH.fullmatch(parts.path) if parts.scheme == "https" and (parts.hostname or "").endswith(".slack.com") \
        else None
    return (found.group(1), f"{found.group(2)}.{found.group(3)}") if found else None


def own_link(link, cid, ts) -> bool:
    """Whether `link` is a Slack link to this very save."""
    return isinstance(link, str) and slack_save(link) == (cid, ts)


def plain(reason) -> str:
    """What a reason reads as on the page, whatever its raw text: the raw text is never shown, because it can be a
    Slack error code, a traceback or a sentence meant for another place."""
    text = str(reason or "").strip()
    if text in LEDGER:
        return WORDS[LEDGER[text]]
    if text.startswith(WORKER_PREFIX):
        return WORDS["worker"]
    if text == "ratelimited":
        return WORDS["limit"]
    if text in SIGN_IN_ERRORS or text.startswith(SIGN_IN):
        return WORDS["sign_in"]
    if text.startswith(("TimeoutError", "Error")) or len(text) > SILENT_AFTER:
        return WORDS["silent"]
    return WORDS["other"]


def well_formed(key, held) -> bool:
    """An entry this module wrote: it is kept under its own channel and message time, which fit what the ledger takes,
    its send time is text, and a save still waiting has a time to be tried."""
    return (isinstance(held, dict) and all(isinstance(held.get(f), str) for f in ("cid", "ts", "at"))
            and key == f"{held['cid']}:{held['ts']}"
            and effects.TARGET_KEYS["channel"].fullmatch(held["cid"]) is not None
            and effects.TARGET_KEYS["ts"].fullmatch(held["ts"]) is not None
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
        return [(k, h["cid"], h["ts"], h["at"], h["link"] if own_link(h.get("link"), h["cid"], h["ts"]) else "")
                for k, h in read().items() if well_formed(k, h) and not h.get("gave_up") and h["next"] <= at]


def drop(keep) -> None:
    """Forget every save `keep(key)` is false for: it was marked, or Slack no longer has it."""
    with locked():
        held = read()
        left = {k: h for k, h in held.items() if keep(k)}
        if left != held:
            write(left)


def line() -> dict:
    """The line the page shows, {"text", "links", "more"}, or {} when there is nothing to say. It covers the saves still
    unmarked after the last try, and at once any that need you to sign in, because trying again can't fix that. Each
    link says when its save was sent, because several links to Slack look alike."""
    shown = [h for k, h in read().items()
             if well_formed(k, h) and (h.get("gave_up") or plain(h.get("reason")) == WORDS["sign_in"])]
    if not shown:
        return {}
    why = list(dict.fromkeys(plain(h.get("reason")) for h in shown))
    if WORDS["sign_in"] in why:
        why.remove(WORDS["sign_in"])
        why.insert(0, WORDS["sign_in"])
    many = len(shown) > 1
    it, left = "them" if many else "it", len(why) - 2
    links = [{"url": h["link"], "at": h["at"]} for h in shown[:SHOWN] if own_link(h.get("link"), h["cid"], h["ts"])]
    if WORDS["sign_in"] in why:
        closing = f"Sign in to Slack again, then mark {it} done in Slack yourself."
    elif why == [WORDS["progress"]]:
        closing = f"Mark {it} done in Slack yourself if {'they are' if many else 'it is'} still open in a few minutes."
    else:
        closing = f"Mark {it} done in Slack yourself."
    text = (f"{len(shown)} save{'s' if many else ''} you sent to an agent couldn't be marked done in Slack: "
            + "; ".join(why[:2])
            + ("" if left < 1 else " and one other problem" if left == 1 else f" and {left} other problems")
            + ". " + closing + ("" if links else " They are still open in your Slack Later list."))
    return {"text": text, "links": links, "more": max(0, len(shown) - SHOWN) if links else 0}
