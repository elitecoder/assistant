"""Feedback: one append-only record of what the owner told Assistant about the things it showed or did.

Each tap that means something (Done, Snooze, Drop, Send to Agent, Approve, Keep, Archive, a thumbs-down, a few typed
words) is one line in ~/.assistant/feedback/feedback.jsonl (folder 0700, file 0600, one JSON object per line). A later
change reads these lines to change what Assistant does; this file only keeps them, safely.

The rules are checks, not hopes:

  * On disk before it is acknowledged. `record()` returns only after the line is written and fsynced. When it can't
    be, it raises `Unrecorded`; the caller says so and carries on with the work, because feedback never blocks work.
    The file lock is taken without blocking, for at most LOCK_WAIT seconds, so a stuck holder can't hold up an answer.
    When a flush fails after the line was written, a line that cancels it is appended (when that works), so a retry
    doesn't leave the same thing twice.
  * Append-only. The file is opened for appending and nowhere else; no function rewrites or deletes a line. Taking
    feedback back is a new `retract` line that names the old one (`amends`).
  * Pointers and the person's own words, nothing else. What a record is about is a row pointer
    (`assistant.pointers.row_pointer`) or a task id, plus a trace id; each is matched against its shape, so a title, a
    sender's name, a link or message text can't be written. `words` is what the person typed: collapsed to one line,
    scrubbed with `assistant.redact`, then cut to WORDS_MAX characters. The context a later change needs to find similar
    items is the row's source and a keyed hash of its sender (`who_pointer`), never the sender's name.
  * A reason belongs to a standing thumbs-down, and a take-back to a thumbs-down, a tell or a used draft that hasn't been
    taken back, about the same item (the same `view_key`). What an action wrote (a Done, a Send) can't be taken back.
  * Every append runs under one flock on a dedicated lock file, because the Work queue server is threaded and other
    processes may write too. Paths are computed per call so tests that move HOME see fresh ones.

Reads never raise. The file is read once and then only the bytes added since (the way the effects ledger is), so what
stands is known for the whole file at no growing cost; `recent` looks at the newest RECENT_KEEP records.

Each record also writes one telemetry record (kind `feedback`): the signal, the reason, the surface and what kind of
thing it is about, with the subject and trace, never the words. A server passes `defer=True` and calls
`emit_pending()` once the answer has gone, so telemetry never delays the person.

Standard library plus `assistant.redact`, `assistant.pointers` and `assistant.telemetry`; it loads on the system
Python (3.9).
"""
from __future__ import annotations

import collections
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from assistant import pointers, telemetry
from assistant.redact import redact

SIGNALS = ("done", "drop", "snooze", "undo", "send", "send_handsoff", "approve", "keep", "archive", "mark_read",
           "used_draft", "kept_result", "thumbs_down", "tell", "retract")
REASONS = ("not_important", "wrong_order", "not_mine", "already_done", "bad_draft", "not_what_i_asked",
           "should_not_have_started", "should_not_have_pinged", "wrong_tone", "wrong_facts", "not_how_id_say",
           "didnt_finish")
KINDS = ("row", "start_here", "email", "candidate", "result", "draft", "page")
SURFACES = ("work-queue", "dashboard", "slack", "voice", "cli")
SOURCES = ("slack", "github", "gmail", "outlook", "calendar")
# The reasons the page offers for each kind of thing a person can mark wrong. `bad_draft` stays valid in the store: it
# is what a plain thumbs-down on a draft means.
REASONS_FOR = {
    "row": ("not_important", "wrong_order", "not_mine", "already_done"),
    "start_here": ("not_important", "wrong_order", "not_mine", "already_done"),
    "email": ("not_important", "wrong_order", "not_mine", "already_done"),
    "draft": ("wrong_tone", "wrong_facts", "not_how_id_say"),
    "result": ("not_what_i_asked", "didnt_finish", "should_not_have_started"),
    "candidate": (),
}
# The telemetry component each surface's records are filed under.
COMPONENTS = {"work-queue": "work-queue", "dashboard": "dashboard", "slack": "reactor", "voice": "comms", "cli": "cli"}
WORDS_MAX = 2000
WORDS_READ = 100000  # more than this is dropped before scrubbing, so a scrub never works on an unbounded paste
LOCK_WAIT = 0.5
RECENT_KEEP = 2000
READ_BLOCK = 4 * 1024 * 1024
TAIL_BYTES = 64
TELL_PREVIEW = 80
TAKE_BACK = ("thumbs_down", "tell", "used_draft")  # what a take-back may name: what the person gave, not what an action wrote
PENDING_MAX = 1000
KEY_MIN = 32  # bytes: a shorter sender key is as good as none

_ID = re.compile(r"fb-[0-9a-f]{16}")
_ROW = re.compile(r"row:[0-9a-f]{16}")
_TASK = re.compile(r"td-[0-9]{1,9}")
_WHO = re.compile(r"who:[0-9a-f]{16}")


class Unrecorded(Exception):
    """The line couldn't be written to disk and flushed, or the file stayed locked past the deadline."""


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
_monotonic = time.monotonic
_sleep = time.sleep


def lock_deadline() -> float:
    """When a wait for the file lock that begins now must give up (a `_monotonic()` value). A batch takes one."""
    return _monotonic() + LOCK_WAIT


@contextlib.contextmanager
def _locked(deadline: float):
    """The file lock, taken without blocking: Unrecorded when `deadline` (a `_monotonic()` value) passes first."""
    os.makedirs(str(feedback_dir()), mode=0o700, exist_ok=True)
    os.chmod(str(feedback_dir()), 0o700)
    fd = os.open(str(feedback_dir() / "feedback.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _monotonic() >= deadline:
                    raise Unrecorded("the feedback file is busy") from None
                _sleep(0.005)
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


# --- reading: the file as read so far -------------------------------------------------------------------------

class _State:
    """The file as read so far, in one process. It is only ever appended to, so it is read once and then only the bytes
    added since: `offset` is the end of the last whole line read. The file's first bytes, its last bytes read and its
    identity say whether it is still the file that was read; if not, it is read again from the start. Per record it
    keeps only what decides what stands; the newest RECENT_KEEP are kept whole."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.path, self.ident, self.head, self.tail, self.offset = None, None, b"", b"", 0
        self.recs: dict = {}      # id -> (subject, kind, signal, amends, reason, ts, told)
        self.by_subject: dict = {}  # subject -> ids, oldest first
        self.order: list = []     # every id, oldest first
        self.children: dict = {}  # id -> ids that amend it
        self.dead: set = set()    # ids taken back, and what amends them
        self.recent = collections.deque(maxlen=RECENT_KEEP)
        self.version = 0
        self.cached = (-1, {})

    def kill(self, rec_id: str) -> None:
        stack = [rec_id]
        while stack:
            found = stack.pop()
            self.dead.add(found)
            stack.extend(self.children.get(found, ()))

    def ingest(self, raw: bytes) -> None:
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):
            return
        if not (isinstance(rec, dict) and isinstance(rec.get("id"), str) and _ID.fullmatch(rec["id"])
                and isinstance(rec.get("signal"), str) and isinstance(rec.get("ts"), str)) or rec["id"] in self.recs:
            return
        rec_id, amends = rec["id"], rec.get("amends", "")
        words = rec.get("words", "") if isinstance(rec.get("words", ""), str) else ""
        told = words[:TELL_PREVIEW] + ("…" if len(words) > TELL_PREVIEW else "")
        subject = rec.get("subject", "") if isinstance(rec.get("subject", ""), str) else ""
        self.recs[rec_id] = (subject, rec.get("kind", ""), rec["signal"], amends if isinstance(amends, str) else "",
                             rec.get("reason", ""), rec["ts"], told)
        self.by_subject.setdefault(subject, []).append(rec_id)
        self.order.append(rec_id)
        self.recent.append(rec)
        if isinstance(amends, str) and amends:
            self.children.setdefault(amends, []).append(rec_id)
            if amends in self.dead:
                self.dead.add(rec_id)
            if rec["signal"] == "retract":
                self.kill(amends)
        self.version += 1


_STATE = _State()
_STATE_LOCK = threading.RLock()  # readers in other threads share the one state


def _sync() -> _State:
    """The state, brought up to date with the file. Call it holding _STATE_LOCK. A missing file is an empty record; any
    other failure to read raises OSError."""
    state = _STATE
    path = str(feedback_path())
    if state.path != path:
        state.reset()
        state.path = path
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        state.reset()
        state.path = path
        return state
    try:
        info = os.fstat(fd)
        ident = (info.st_dev, info.st_ino)
        if state.ident != ident or (state.offset and (
                os.pread(fd, len(state.head), 0) != state.head
                or os.pread(fd, len(state.tail), state.offset - len(state.tail)) != state.tail)):
            state.reset()  # another file, or this one shortened or rewritten in place
            state.path, state.ident = path, ident
        while info.st_size > state.offset:
            chunk = os.pread(fd, min(info.st_size - state.offset, READ_BLOCK), state.offset)
            cut = chunk.rfind(b"\n") + 1  # a line still being written isn't read until it is whole
            if not cut and state.offset + len(chunk) < info.st_size:
                chunk = os.pread(fd, info.st_size - state.offset, state.offset)  # one line longer than a block
                cut = chunk.rfind(b"\n") + 1
            if not cut:
                break
            if not state.offset:
                state.head = chunk[:min(64, cut)]
            for raw in chunk[:cut - 1].split(b"\n"):  # each ends in a newline
                state.ingest(raw)
            state.offset += cut
            state.tail = (state.tail + chunk[:cut])[-TAIL_BYTES:]
    finally:
        os.close(fd)
    return state


def forget() -> None:
    """Drop what was read, so the next call reads the whole file. For tests that compare the two."""
    with _STATE_LOCK:
        _STATE.reset()


def recent(subject=None, trace=None, signal=None, since=None, limit=200) -> list:
    """Records, newest first, at most `limit`, out of the newest RECENT_KEEP. `since` is a time in seconds.
    Taken-back records are included; use `latest_for` or `views` for what still stands. Never raises."""
    try:
        want = {"subject": subject, "trace": trace, "signal": signal}
        floor = _iso(float(since)) if since is not None else ""
        with _STATE_LOCK:
            _sync()
            found = list(_STATE.recent)
        out = []
        for r in reversed(found):
            if all(v is None or r.get(k, "") == v for k, v in want.items()) and r["ts"] >= floor:
                out.append(r)
                if len(out) >= limit:
                    break
        return out
    except Exception:  # noqa: BLE001 - reading feedback never breaks work
        return []


def readable() -> bool:
    """Whether the file can be read: True for one that doesn't exist yet (nothing is recorded), False for one that is
    there and can't be read. `recent()` gives `[]` for both. Never raises."""
    try:
        with _STATE_LOCK:
            _sync()
        return True
    except Exception:  # noqa: BLE001 - a read that can't tell is "no"
        return False


def _brief(rec_id: str, state: _State) -> dict:
    subject, kind, signal, amends, reason, ts, told = state.recs[rec_id]
    return {"id": rec_id, "ts": ts, "signal": signal, "kind": kind, "subject": subject, "amends": amends,
            "reason": reason, "told": told}


def standing(rec_id: str):
    """What is known of a record that hasn't been taken back (id, ts, signal, kind, subject, amends, reason), or None
    for one that is unknown or was taken back. Never raises."""
    try:
        with _STATE_LOCK:
            state = _sync()
            return _brief(rec_id, state) if rec_id in state.recs and rec_id not in state.dead else None
    except Exception:  # noqa: BLE001
        return None


def latest_for(subject):
    """The newest record about a subject that hasn't been taken back (a `retract` line itself never counts), as what
    `standing` gives, or None. Never raises."""
    try:
        with _STATE_LOCK:
            state = _sync()
            found = next((i for i in reversed(state.by_subject.get(subject, ()))
                          if i not in state.dead and state.recs[i][2] != "retract"), None)
            return _brief(found, state) if found else None
    except Exception:  # noqa: BLE001
        return None


def view_key(subject: str, kind: str) -> str:
    """The key `views` files what stands about an item under: the subject, except that a draft is its own item (it has
    its row's subject), and a note about the page is `page`. Two records are about the same item when these match."""
    return "page" if kind == "page" else "draft:" + subject if kind == "draft" else subject


def views() -> dict:
    """What the person has said about each item that still stands, for the page to show:
    {view_key: {"kind", "down": id of the thumbs-down or "", "reason": its newest reason or "", "told": the first words
    of what they told Assistant or "", "told_id"}}. Never raises."""
    try:
        with _STATE_LOCK:
            state = _sync()
            if state.cached[0] == state.version:
                return {k: dict(v) for k, v in state.cached[1].items()}
            out: dict = {}
            for rec_id in state.order:  # oldest first, so the newest of each kind wins
                subject, kind, signal, amends, reason, _ts, told = state.recs[rec_id]
                if rec_id in state.dead or signal not in ("thumbs_down", "tell"):
                    continue
                view = out.setdefault(view_key(subject, kind), {"kind": kind, "down": "", "reason": "", "told": "",
                                                                "told_id": ""})
                if signal == "tell":
                    view["told"], view["told_id"] = told, rec_id
                elif amends:
                    view["reason"] = reason
                else:
                    view["down"], view["reason"] = rec_id, ""
            state.cached = (state.version, out)
            return {k: dict(v) for k, v in out.items()}
    except Exception:  # noqa: BLE001
        return {}


# --- writing --------------------------------------------------------------------------------------------------

def _key() -> bytes:
    """This install's secret for sender pointers: 32 random bytes (64 hex characters) in the feedback folder, owner-only,
    made once. It is written to a temporary file and linked into place, so no reader ever sees it empty. b"" (no
    pointer is made) when what is there is shorter than KEY_MIN, which a half-made or emptied file would be."""
    path = feedback_dir() / "sender.key"
    try:
        found = path.read_bytes()
    except FileNotFoundError:
        os.makedirs(str(feedback_dir()), mode=0o700, exist_ok=True)
        tmp = feedback_dir() / f"sender.key.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_hex(32).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(str(tmp), str(path))
        except FileExistsError:
            pass  # another process made it first
        finally:
            os.unlink(str(tmp))
        found = path.read_bytes()
    return found if len(found) >= KEY_MIN else b""


def who_pointer(sender) -> str:
    """The pointer for a sender: `who:` and the first 16 hex characters of an HMAC-SHA-256 of the sender's text, lower-cased
    and trimmed, keyed by this install's secret. The same sender gives the same pointer here, and a list of names can't be
    matched against it. Deleting the secret makes old and new pointers stop matching. "" for no sender, and when the
    secret can't be had (never an error)."""
    sender = sender.strip().lower() if isinstance(sender, str) else ""
    if not sender:
        return ""
    try:
        key = _key()
    except OSError:
        return ""
    return "who:" + hmac.new(key, sender.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16] if key else ""


def text_pointer(kind: str, text: str) -> str:
    """16 hex characters that stand for `text` without holding it: an HMAC-SHA-256 of the text keyed by this install's secret
    (the one `who_pointer` uses, with `kind` mixed in so the two never match). The same text gives the same pointer here,
    and a list of guesses can't be matched against it. "" when the secret can't be had (never an error)."""
    try:
        key = _key()
    except OSError:
        return ""
    if not key:
        return ""
    return hmac.new(key, f"{kind}\0{text}".encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]


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
    words = " ".join(_text("words", words)[:WORDS_READ].split())
    if bool(words) != (signal == "tell"):
        raise ValueError("a tell has words, and nothing else does")
    return redact(words)[:WORDS_MAX]  # scrubbed whole, then cut, so a secret across the cut is never kept in part


def _check_amends(state: _State, signal: str, subject: str, kind: str, amends: str) -> None:
    """A reason or a take-back names a record about the same item that hasn't been taken back. A reason belongs to a
    thumbs-down. A take-back names only what the person gave (a thumbs-down, a tell, a used draft), never what an
    action wrote, a reason, or another take-back: a take-back is itself taken back from the moment it is written."""
    target = state.recs.get(amends)
    if target is None or view_key(target[0], target[1]) != view_key(subject, kind):
        raise ValueError("amends names no earlier record about this item")
    if amends in state.dead:
        raise ValueError("amends names a record that was already taken back")
    if signal == "retract":
        if target[2] not in TAKE_BACK or target[3]:
            raise ValueError("only a thumbs-down, a tell or a used draft can be taken back, and a reason can't be")
    elif not (target[2] == "thumbs_down" and not target[3]):
        raise ValueError("a reason belongs to a thumbs-down")


_PENDING = threading.local()


def emit_pending() -> None:
    """Send the telemetry records `record(..., defer=True)` held back in this thread, in one write. Call it after the
    answer has gone."""
    held, _PENDING.held = getattr(_PENDING, "held", []), []
    if held:
        telemetry.emit_many(held)


def record(signal, subject, kind, surface, trace="", reason="", words="", amends="", source="", who="",
           deadline=None, defer=False) -> str:
    """Write one record and return its id (`fb-` and 16 hex characters).

    `subject` is what it is about: a row pointer, or a task id for a `result`, or "" for the `page`. `trace`, `source`
    and `who` (a `who_pointer`) say which piece of work it was and what it looked like, never what it said. `amends`
    names the earlier record a reason or a take-back belongs to; it must be about the same item and not taken back.
    `words` is the person's own text, for `tell` only. `deadline` (a `lock_deadline()` value) is how long to wait for
    the file lock; a batch shares one. With `defer`, the telemetry record waits for `emit_pending()`.

    Raises ValueError for anything malformed and `Unrecorded` when the line couldn't be written and flushed."""
    words = _check(signal, subject, kind, surface, trace, reason, words, amends, source, who)
    rec_id = "fb-" + secrets.token_hex(8)
    rec = {"id": rec_id, "ts": _iso(_now()), "signal": signal, "kind": kind, "surface": surface, "subject": subject,
           "trace": trace, "reason": reason, "words": words, "amends": amends,
           "context": {k: v for k, v in (("source", source), ("who", who)) if v}}
    rec = {k: v for k, v in rec.items() if v or k in ("id", "ts", "signal", "kind", "surface")}
    line = (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8")
    cancel = (json.dumps({"id": "fb-" + secrets.token_hex(8), "ts": rec["ts"], "signal": "retract", "kind": kind,
                          "surface": surface, "subject": subject, "amends": rec_id}, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with _locked(deadline if deadline is not None else lock_deadline()):
            if amends:
                with _STATE_LOCK:
                    _check_amends(_sync(), signal, subject, kind, amends)
            try:
                _append(line)
            except OSError:
                with contextlib.suppress(OSError):
                    _append(cancel)  # the line may be on disk although the flush failed: cancel it, so a retry isn't a twin
                raise
    except OSError as exc:
        raise Unrecorded(f"couldn't write the feedback file: {type(exc).__name__}") from exc
    held = ("feedback", {"trace": trace, "subject": subject, "component": COMPONENTS[surface], "signal": signal,
                         "reason_code": reason or "none", "surface": surface, "subject_kind": kind})
    if defer:
        if len(getattr(_PENDING, "held", [])) < PENDING_MAX:
            _PENDING.held = getattr(_PENDING, "held", []) + [held]
    else:
        telemetry.emit_many([held])
    return rec_id
