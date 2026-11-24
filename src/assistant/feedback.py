"""Feedback: one append-only record of what the owner told Assistant about the things it showed or did.

Each tap that means something (Done, Snooze, Drop, Send to Agent, Approve, Keep, Archive, a thumbs-down, a few typed
words) is one line in ~/.assistant/feedback/feedback.jsonl (folder 0700, file 0600, one JSON object per line). A later
change reads these lines to change what Assistant does; this file only keeps them, safely.

The rules are checks, not hopes:

  * On disk before it is acknowledged. `record()` returns only after the line is written and fsynced. When it can't
    be, it raises `Unrecorded`; the caller says so and carries on with the work, because feedback never blocks work.
  * Append-only. The file is opened for appending and nowhere else; no function rewrites or deletes a line. Taking
    feedback back is a new `retract` line that names the old one (`amends`).
  * Pointers and the person's own words, nothing else. What a record is about is a row pointer
    (`assistant.pointers.row_pointer`) or a task id, plus a trace id; each is matched against its shape, so a title, a
    sender's name, a link or message text can't be written. `words` is what the person typed: collapsed to one line,
    cut to WORDS_MAX characters and passed through `assistant.redact`. The context a later change needs to find similar
    items is the row's source and a hash of its sender (`who_pointer`), never the sender's name.
  * Every append runs under one flock on a dedicated lock file, because the Work queue server is threaded and other
    processes may write too. Paths are computed per call so tests that move HOME see fresh ones.

Reads never raise, skip a corrupt line, and look at the newest READ_BYTES of the file only.

Each record also writes one telemetry record (kind `feedback`): the signal, the reason, the surface and what kind of
thing it is about, with the subject and trace, never the words.

Standard library plus `assistant.redact`, `assistant.pointers` and `assistant.telemetry`; it loads on the system
Python (3.9).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from assistant import pointers, telemetry
from assistant.redact import redact

SIGNALS = ("done", "drop", "snooze", "undo", "send", "send_handsoff", "approve", "keep", "archive", "mark_read",
           "used_draft", "kept_result", "thumbs_down", "tell", "retract")
REASONS = ("not_important", "wrong_order", "not_mine", "already_done", "bad_draft", "not_what_i_asked",
           "should_not_have_started", "should_not_have_pinged")
KINDS = ("row", "start_here", "email", "candidate", "result", "draft", "page")
SURFACES = ("work-queue", "dashboard", "slack", "voice", "cli")
SOURCES = ("slack", "github", "gmail", "outlook", "calendar")
# The reasons that make sense for each kind of thing a person can mark wrong; the page offers only these.
REASONS_FOR = {
    "row": ("not_important", "wrong_order", "not_mine", "already_done"),
    "start_here": ("not_important", "wrong_order", "not_mine", "already_done"),
    "email": ("not_important", "wrong_order", "not_mine", "already_done"),
    "draft": ("bad_draft",),
    "result": ("not_what_i_asked", "should_not_have_started"),
}
# The telemetry component each surface's records are filed under.
COMPONENTS = {"work-queue": "work-queue", "dashboard": "dashboard", "slack": "reactor", "voice": "comms", "cli": "cli"}
WORDS_MAX = 2000
READ_BYTES = 4 * 1024 * 1024
TELL_PREVIEW = 80

_ID = re.compile(r"fb-[0-9a-f]{16}")
_ROW = re.compile(r"row:[0-9a-f]{16}")
_TASK = re.compile(r"td-[0-9]{1,9}")
_WHO = re.compile(r"who:[0-9a-f]{16}")


class Unrecorded(Exception):
    """The line couldn't be written to disk and flushed."""


# --- paths and the lock ---------------------------------------------------------------------------------------

def feedback_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".assistant" / "feedback"


def feedback_path() -> Path:
    return feedback_dir() / "feedback.jsonl"


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Seams, so a test can make a write or a flush fail or take a while.
_write = os.write
_fsync = os.fsync


@contextlib.contextmanager
def _locked():
    os.makedirs(str(feedback_dir()), mode=0o700, exist_ok=True)
    os.chmod(str(feedback_dir()), 0o700)
    fd = os.open(str(feedback_dir() / "feedback.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _append(line: bytes) -> None:
    """Add one line to the end of the file and flush it to disk. The only place the file is opened for writing."""
    fd = os.open(str(feedback_path()), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line  # a crash left a torn last line: don't glue this one onto it
        view = memoryview(line)
        while view:
            view = view[_write(fd, view):]
        _fsync(fd)
    finally:
        os.close(fd)


# --- reading --------------------------------------------------------------------------------------------------

def _load() -> list:
    """The valid records in the newest READ_BYTES of the file, oldest first. A corrupt line is skipped. Raises OSError
    when the file exists and can't be read."""
    try:
        with open(str(feedback_path()), "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - READ_BYTES))
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.split(b"\n")
    if size > READ_BYTES:
        lines = lines[1:]  # the first line is probably cut in half
    found = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str) and _ID.fullmatch(rec["id"]) \
                and isinstance(rec.get("signal"), str) and isinstance(rec.get("ts"), str):
            found.append(rec)
    return found


def _taken_back(records: list) -> set:
    """The ids of the records taken back: those a `retract` names, and those that amend one of them. `records` is
    oldest first, and a record only amends an earlier one."""
    retracted = {r.get("amends") for r in records if r["signal"] == "retract"}
    dead: set = set()
    for r in records:
        if r["id"] in retracted or r.get("amends") in dead:
            dead.add(r["id"])
    return dead


def recent(subject=None, trace=None, signal=None, since=None, limit=200) -> list:
    """Records, newest first, at most `limit`. `since` is a time in seconds. Taken-back records are included; use
    `latest_for` or `views` for what still stands. Never raises."""
    try:
        want = {"subject": subject, "trace": trace, "signal": signal}
        floor = _iso(float(since)) if since is not None else ""
        out = []
        for r in reversed(_load()):
            if all(v is None or r.get(k, "") == v for k, v in want.items()) and r["ts"] >= floor:
                out.append(r)
                if len(out) >= limit:
                    break
        return out
    except Exception:  # noqa: BLE001 - reading feedback never breaks work
        return []


def latest_for(subject):
    """The newest record about a subject that hasn't been taken back (a `retract` line itself never counts), or None.
    Never raises."""
    try:
        records = _load()
        dead = _taken_back(records)
        return next((r for r in reversed(records)
                     if r.get("subject", "") == subject and r["signal"] != "retract" and r["id"] not in dead), None)
    except Exception:  # noqa: BLE001
        return None


def view_key(subject: str, kind: str) -> str:
    """The key `views` files what stands about an item under: the subject, except that a draft is its own item (it has
    its row's subject), and a note about the page is `page`."""
    return "page" if kind == "page" else "draft:" + subject if kind == "draft" else subject


def views() -> dict:
    """What the person has said about each subject that still stands, for the page to show:
    {subject: {"kind", "down": id of the thumbs-down or "", "reason": its reason or "", "told": the first words of what
    they told Assistant or ""}}, by `view_key`. Never raises."""
    try:
        records = _load()
        dead = _taken_back(records)
        out: dict = {}
        for r in records:  # oldest first, so the newest of each kind wins
            if r["id"] in dead or r["signal"] not in ("thumbs_down", "tell"):
                continue
            view = out.setdefault(view_key(r.get("subject", ""), r.get("kind", "")), {"kind": r.get("kind", ""), "down": "", "reason": "",
                                                               "told": ""})
            if r["signal"] == "tell":
                words = r.get("words", "")
                view["told"] = words[:TELL_PREVIEW] + ("…" if len(words) > TELL_PREVIEW else "")
            elif r.get("amends"):
                view["reason"] = r.get("reason", "")
            else:
                view["down"], view["reason"] = r["id"], ""
        return out
    except Exception:  # noqa: BLE001
        return {}


# --- writing --------------------------------------------------------------------------------------------------

def who_pointer(sender) -> str:
    """The pointer for a sender: `who:` and the first 16 hex characters of the SHA-256 of the sender's text, lower-cased
    and trimmed. Never the sender's name. "" for no sender."""
    sender = sender.strip().lower() if isinstance(sender, str) else ""
    return "who:" + hashlib.sha256(sender.encode("utf-8", "replace")).hexdigest()[:16] if sender else ""


def _text(name: str, value) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


def _check(signal, subject, kind, surface, trace, reason, words, amends, source, who) -> str:
    """Refuse anything malformed; return the words, cleaned."""
    for name, value, allowed in (("signal", signal, SIGNALS), ("kind", kind, KINDS), ("surface", surface, SURFACES)):
        if _text(name, value) not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    if _text("reason", reason) and reason not in REASONS:
        raise ValueError("reason must be empty or one of: " + ", ".join(REASONS))
    if _text("source", source) and source not in SOURCES:
        raise ValueError("source must be empty or one of: " + ", ".join(SOURCES))
    if _text("who", who) and not _WHO.fullmatch(who):
        raise ValueError("who must be a pointer made by who_pointer")
    if kind == "page":
        if _text("subject", subject):
            raise ValueError("a note about the page has no subject")
    elif not (_TASK if kind == "result" else _ROW).fullmatch(_text("subject", subject)):
        raise ValueError("subject must be " + ("a task id" if kind == "result" else "a row pointer"))
    if _text("trace", trace) and not pointers.is_trace_id(trace):
        raise ValueError("trace must be empty or a trace id")
    if _text("amends", amends) and not _ID.fullmatch(amends):
        raise ValueError("amends must be empty or the id of an earlier record")
    if bool(amends) != (signal == "retract" or bool(reason)):
        raise ValueError("a take-back and a reason each name the record they belong to, and nothing else does")
    if reason and signal != "thumbs_down":
        raise ValueError("only a thumbs-down has a reason")
    words = " ".join(_text("words", words).split())[:WORDS_MAX]
    if bool(words) != (signal == "tell"):
        raise ValueError("a tell has words, and nothing else does")
    return redact(words)


def record(signal, subject, kind, surface, trace="", reason="", words="", amends="", source="", who="") -> str:
    """Write one record and return its id (`fb-` and 16 hex characters).

    `subject` is what it is about: a row pointer, or a task id for a `result`, or "" for the `page`. `trace`, `source`
    and `who` (a `who_pointer`) say which piece of work it was and what it looked like, never what it said. `amends`
    names the earlier record a reason or a take-back belongs to; it must be about the same subject. `words` is the
    person's own text, for `tell` only.

    Raises ValueError for anything malformed and `Unrecorded` when the line couldn't be written and flushed."""
    words = _check(signal, subject, kind, surface, trace, reason, words, amends, source, who)
    rec_id = "fb-" + secrets.token_hex(8)
    rec = {"id": rec_id, "ts": _iso(_now()), "signal": signal, "kind": kind, "surface": surface, "subject": subject,
           "trace": trace, "reason": reason, "words": words, "amends": amends,
           "context": {k: v for k, v in (("source", source), ("who", who)) if v}}
    rec = {k: v for k, v in rec.items() if v or k in ("id", "ts", "signal", "kind", "surface")}
    try:
        with _locked():
            if amends and not any(r["id"] == amends and r.get("subject", "") == subject for r in _load()):
                raise ValueError("amends names no earlier record about this subject")
            _append((json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError as exc:
        raise Unrecorded(f"couldn't write the feedback file: {type(exc).__name__}") from exc
    telemetry.emit("feedback", trace, subject, COMPONENTS[surface], signal=signal, reason_code=reason or "none",
                   surface=surface, subject_kind=kind)
    return rec_id
