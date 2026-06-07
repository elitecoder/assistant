#!/usr/bin/env python3
"""comms-listen — event-driven assistant-comms daemon (Phase 1).

Replaces the 300s timer pulse. A single long-running process (KeepAlive
LaunchAgent) with three concurrent jobs:

  1. INBOUND (event) — long-poll Telegram getUpdates(timeout=25). Telegram
     holds the connection and returns the instant Mukul messages, so there
     is no 5-minute queue wait. On a message: cold-spawn `claude --print` to
     compose a reply (Phase 1 reply engine; Phase 2 swaps this for a warm
     cmux session). Reply latency = claude boot+reason (~30-90s), not 5 min.

  2. OUTBOUND PINGS (event) — watch actions-ledger.jsonl for appends. On new
     lines, format with comms_lib.fmt_action_line and tg-send. No LLM — these
     are mechanical, so they fire near-instantly.

  3. HEARTBEAT PAGE (timer) — every 60s, check Assistant's heartbeat; if stale
     or status∈{frozen,stale_world,respawn-requested}, send a templated urgent
     page (30-min dedup). No LLM.

All three reuse the tested CLIs (tg-send.py / tg-poll.py / conversation.py)
and comms_lib. Durable memory stays in conversation.jsonl, so a crash +
KeepAlive respawn loses nothing.

Threads, not asyncio: three blocking loops, one per job, joined under a
shutdown Event. Telegram long-poll is the only thing that blocks meaningfully,
and it self-cancels on the next 25s boundary.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402
import comms_session  # noqa: E402

HOME = Path(os.environ["HOME"])
REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
WARM_PROMPT = REPO / "prompts" / "prompt-assistant-comms-warm.md"

REPLY_WAIT_SEC = int(os.environ.get("COMMS_REPLY_WAIT_SEC", "120"))

TG_POLL = BIN / "tg-poll.py"
TG_SEND = BIN / "tg-send.py"
CONVERSATION = BIN / "conversation.py"

LONGPOLL_TIMEOUT = int(os.environ.get("COMMS_LONGPOLL_SEC", "25"))
LEDGER_POLL_SEC = float(os.environ.get("COMMS_LEDGER_POLL_SEC", "2"))
HEARTBEAT_CHECK_SEC = int(os.environ.get("COMMS_HEARTBEAT_CHECK_SEC", "60"))
HEARTBEAT_DEDUP_SEC = 1800

PYTHON = sys.executable  # use the same interpreter that launched us for the CLIs


def utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    paths = comms_lib.Paths.from_env()
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_iso()}] {msg}"
    with open(paths.comms_dir / "comms-listen.log", "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr, flush=True)


def cli(argv: list[str], timeout: int = 30, env: dict | None = None) -> tuple[int, str, str]:
    """Run one of our CLIs with the daemon's interpreter."""
    try:
        p = subprocess.run([PYTHON, *argv], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


# --------------------------------------------------------------------------- inbound

# The warm session record (ws_ref/surface_ref/cwd/transcript) lives on disk in
# session.json; the inbound loop holds it in memory and re-ensures it as needed.

def ensure_warm_session(paths: comms_lib.Paths) -> dict | None:
    """Return a live warm-session record, spawning one if none is alive.

    On respawn, close the prior warm workspace first so we never leak Claude
    processes — without this, daemon restarts / session deaths piled up 6 live
    warm workspaces during 2026-06-05 testing. close_own_workspace only touches
    a workspace this daemon spawned (title-verified), never an arbitrary one."""
    sess = comms_session.read_session(paths)
    if sess and comms_session.cmux_alive(paths, sess["ws_ref"]):
        return sess
    if sess:
        log(f"warm session {sess['ws_ref']} gone — closing it and respawning")
        comms_session.close_own_workspace(paths, sess["ws_ref"], log=log)
        comms_session.clear_session_registry(paths)
    return comms_session.spawn_session(paths, WARM_PROMPT, log=log)


def reply_to_message(paths: comms_lib.Paths, sess: dict, rec: dict) -> dict:
    """Warm reply: record inbound, feed the message to the warm session, wait
    for its reply turn in the transcript, then /clear if context >= 50%.
    Returns the (possibly refreshed) session record."""
    chat_id = rec["chat_id"]
    text = rec.get("text", "")
    msg_id = rec.get("msg_id")
    reply_to = rec.get("reply_to_msg_id")

    # Record the inbound turn first — survives even if the session stalls.
    in_args = [str(CONVERSATION), "append", "--chat", str(chat_id),
               "--direction", "in", "--text", text]
    if msg_id is not None:
        in_args += ["--msg-id", str(msg_id)]
    if reply_to is not None:
        in_args += ["--reply-to", str(reply_to)]
    cli(in_args, timeout=10)

    transcript = sess.get("transcript_path") or comms_session.newest_transcript(sess["cwd"])
    before_lines = comms_session.transcript_line_count(transcript) if transcript else 0

    # Feed the message as a user turn. The warm session's boot prompt tells it
    # how to reconstruct context, reply via tg-send, and record the out turn.
    # A photo message has no text of its own — tg-poll fills `text` with the
    # caption (or "[photo]"). Prefix with [Photo attached] so the warm session
    # knows an image was sent even when there is no caption, rather than seeing
    # what looks like an empty turn.
    body = f"[Photo attached] {text}" if rec.get("has_photo") else text
    feed_text = (
        f"[telegram chat_id={chat_id} msg_id={msg_id} reply_to={reply_to}] {body}"
    )
    t0 = time.time()
    comms_session.feed(paths, sess["surface_ref"], feed_text)

    # Wait for the session to produce a new assistant turn (transcript grows).
    grew = False
    while time.time() - t0 < REPLY_WAIT_SEC:
        time.sleep(2)
        if transcript and comms_session.transcript_line_count(transcript) > before_lines:
            grew = True
            break
        if not transcript:
            transcript = comms_session.newest_transcript(sess["cwd"])
    log(f"reply chat={chat_id} msg={msg_id} grew={grew} wall_ms={int((time.time()-t0)*1000)}")

    # Context management: clear-and-resume at >= 50% so the next reply stays
    # fast. The session re-reads its boot prompt (identity) and reconstructs
    # the thread from conversation.jsonl (memory) — lossless.
    if transcript and comms_session.should_clear(transcript):
        log(f"context >= {int(comms_session.CLEAR_THRESHOLD*100)}% — clear-and-resume")
        comms_session.clear_session(paths, sess["surface_ref"], WARM_PROMPT)
        # After /clear the transcript resets — re-resolve so we don't measure
        # the cleared session against the old (now-stale) transcript path.
        new_t = comms_session.newest_transcript(sess["cwd"])
        if new_t:
            comms_session.write_session(paths, sess["ws_ref"], sess["surface_ref"],
                                        sess["cwd"], new_t)
            sess = comms_session.read_session(paths) or sess
            return sess

    if transcript and transcript != sess.get("transcript_path"):
        comms_session.write_session(paths, sess["ws_ref"], sess["surface_ref"],
                                    sess["cwd"], transcript)
        sess = comms_session.read_session(paths) or sess
    return sess


def inbound_loop(stop: threading.Event, env: dict) -> None:
    log("inbound loop started (long-poll, warm session)")
    paths = comms_lib.Paths.from_env()
    sess = ensure_warm_session(paths)
    # Startup sweep: close any warm workspaces that aren't the one we just
    # ensured. Covers the case where a prior daemon died leaving live orphans
    # (the singleton lock stops two daemons, but not stale workspaces).
    if sess:
        comms_session.reconcile_warm_workspaces(paths, keep=sess["ws_ref"], log=log)
    while not stop.is_set():
        rc, out, err = cli([str(TG_POLL), "--timeout", str(LONGPOLL_TIMEOUT)],
                           timeout=LONGPOLL_TIMEOUT + 15, env=env)
        if rc != 0:
            log(f"tg-poll rc={rc} err={err.strip()[:200]}")
            stop.wait(5)
            continue
        try:
            msgs = json.loads(out.strip() or "[]")
        except json.JSONDecodeError:
            log(f"tg-poll bad json: {out[:200]}")
            continue
        for rec in msgs:
            if stop.is_set():
                break
            log(f"inbound chat={rec.get('chat_id')} msg={rec.get('msg_id')} "
                f"text={rec.get('text','')[:80]!r}")
            sess = ensure_warm_session(paths)
            if not sess:
                log("no warm session available — cannot reply this message")
                continue
            sess = reply_to_message(paths, sess, rec)


# --------------------------------------------------------------------------- outbound pings

def ledger_loop(stop: threading.Event, env: dict) -> None:
    """Watch actions-ledger.jsonl; broadcast each new entry. stat-poll (2s) —
    simple and dependency-free; the latency floor is the poll interval, ~2s."""
    paths = comms_lib.Paths.from_env()
    comms_lib.initialize_cursor_if_missing(paths)
    log("ledger loop started")
    while not stop.is_set():
        try:
            entries = comms_lib.read_new_ledger_lines(paths)
        except Exception as e:  # noqa: BLE001
            log(f"ledger read error: {e}")
            entries = []
        for entry in entries:
            outcome = entry.get("outcome")
            if outcome == "skipped":
                continue  # no work happened
            key = entry.get("key", "")
            # Only broadcast urgent events — routine noops and emit-cards are
            # surfaced by the warm session when asked. This keeps the channel
            # owned by the warm responder, not the daemon.
            kind = entry.get("kind", "")
            if kind in ("noop", "emit-card"):
                log(f"suppressed routine broadcast kind={kind} key={key}")
                continue
            if kind == "self-update" and "skip" in key:
                log(f"suppressed self-update-skip broadcast key={key}")
                continue
            if kind in ("lesson-proposal", "lesson_proposal") or key.startswith("lesson-proposal"):
                log(f"suppressed lesson-proposal broadcast key={key} (delivered via warm session)")
                continue
            body = comms_lib.fmt_action_line(entry)
            rc, out, err = cli(
                [str(TG_SEND), "--text", body, "--kind", "action",
                 "--ledger-key", key],
                timeout=30, env=env)
            if rc != 0:
                log(f"ledger broadcast rc={rc} key={key} err={err.strip()[:160]}")
                continue
            # Mirror each sent broadcast into conversation.jsonl as an out turn,
            # per chat, so it's part of the thread for later replies.
            for line in out.strip().splitlines():
                try:
                    sent = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sent.get("muted") or not sent.get("message_id"):
                    continue
                cli([str(CONVERSATION), "append", "--chat", str(sent["chat_id"]),
                     "--direction", "out", "--text", body, "--kind", "action",
                     "--msg-id", str(sent["message_id"])], timeout=10)
            log(f"broadcast key={key}")
        stop.wait(LEDGER_POLL_SEC)


# --------------------------------------------------------------------------- inbox watcher (cmux-watcher signals)

INBOX_DIR = HOME / ".assistant" / "inbox"
# Cmux-watcher (bin/cmux-watcher.py) drops cmux-*.json signals here the instant
# a workspace needs input or finishes a notable turn. We ping the phone within
# seconds instead of waiting for the next pulse. pulse-*.json belongs to the
# mechanical pulse and is NOT ours — we only consume cmux-*.json.
INBOX_GLOB = "cmux-*.json"
INBOX_POLL_FALLBACK_SEC = float(os.environ.get("COMMS_INBOX_POLL_SEC", "2"))


def _drain_inbox_once(env: dict) -> int:
    """Read every cmux-*.json in the inbox, ping the phone, delete it. Returns
    the number of items processed. A malformed file is logged and removed so it
    never wedges the loop. Atomic-write on the producer side (tmp+rename) means
    we never read a half-written file."""
    if not INBOX_DIR.exists():
        return 0
    n = 0
    for p in sorted(INBOX_DIR.glob(INBOX_GLOB)):
        try:
            raw = p.read_text()
        except OSError:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            log(f"inbox: dropping malformed {p.name}")
            try:
                p.unlink()
            except OSError:
                pass
            continue
        body = comms_lib.fmt_workspace_signal(item)
        ledger_key = f"{item.get('ws_ref') or 'ws'}:{item.get('signal_type') or 'signal'}"
        rc, _out, err = cli(
            [str(TG_SEND), "--text", body, "--kind", "action",
             "--ledger-key", ledger_key],
            timeout=30, env=env)
        if rc != 0:
            log(f"inbox: tg-send rc={rc} for {p.name} err={err.strip()[:160]}")
            # Leave the file in place so the next pass retries rather than
            # silently losing the signal.
            continue
        try:
            p.unlink()
        except OSError:
            pass
        n += 1
        log(f"inbox: pinged {item.get('signal_type')} ws={item.get('ws_ref')} "
            f"({p.name})")
    return n


def inbox_loop(stop: threading.Event, env: dict) -> None:
    """Watch ~/.assistant/inbox for cmux-watcher signals and ping the phone.

    Event-driven on macOS via select.kqueue (NOTE_WRITE/NOTE_EXTEND on the inbox
    directory) — instant wake on a new file, no 2s poll latency. Linux (no
    kqueue) falls back to a short stat-poll. We always drain once on entry to
    catch anything dropped before we started watching, and re-drain on every
    wake. The kqueue timeout doubles as a safety net so a missed vnode event
    can never strand a signal."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    log("inbox loop started (cmux-watcher signals)")
    # Drain whatever is already queued before we start blocking.
    _drain_inbox_once(env)

    kq = getattr(select, "kqueue", None)
    if kq is None:
        # Linux / no kqueue: stat-poll fallback.
        log("inbox loop: kqueue unavailable — stat-poll fallback")
        while not stop.is_set():
            _drain_inbox_once(env)
            stop.wait(INBOX_POLL_FALLBACK_SEC)
        return

    inbox_fd = os.open(str(INBOX_DIR), os.O_RDONLY)
    try:
        kqueue = select.kqueue()
        kevent = select.kevent(
            inbox_fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
        )
        kqueue.control([kevent], 0)
        while not stop.is_set():
            # Block up to 5s for a vnode change; the timeout is the safety-net
            # re-drain so a missed event never strands a file.
            events = kqueue.control(None, 1, 5)
            if stop.is_set():
                break
            if events:
                # Coalesce a burst of writes into one drain.
                time.sleep(0.05)
            _drain_inbox_once(env)
    finally:
        try:
            os.close(inbox_fd)
        except OSError:
            pass


# --------------------------------------------------------------------------- heartbeat page

def heartbeat_loop(stop: threading.Event, env: dict) -> None:
    paths = comms_lib.Paths.from_env()
    cfg = comms_lib.Config.load(paths.config)
    last_alert = 0
    log("heartbeat loop started")
    while not stop.is_set():
        try:
            hb_raw = paths.heartbeat.read_text() if paths.heartbeat.exists() else ""
            hb = json.loads(hb_raw) if hb_raw else {}
        except json.JSONDecodeError:
            hb = {}
        last_ts = int(hb.get("last_pulse_ts") or 0)
        if last_ts > 0:
            age = int(time.time()) - last_ts
            stale = age > cfg.stale_heartbeat_sec
            bad = hb.get("status") in {"frozen", "stale_world", "respawn-requested"}
            now = int(time.time())
            if (stale or bad) and now - last_alert >= HEARTBEAT_DEDUP_SEC:
                body = comms_lib.fmt_heartbeat_alert(hb, age)
                rc, _, err = cli([str(TG_SEND), "--text", body, "--kind", "urgent"],
                                timeout=30, env=env)
                last_alert = now
                log(f"heartbeat-stale page age={age}s rc={rc}")
            elif not (stale or bad):
                last_alert = 0  # healthy → re-arm
        comms_lib.write_comms_heartbeat(paths, status="active", pulse_idx=0,
                                       note="listen-daemon")
        stop.wait(HEARTBEAT_CHECK_SEC)


# --------------------------------------------------------------------------- main

def acquire_singleton(paths: comms_lib.Paths):
    """flock a pidfile so only ONE daemon runs. Two long-pollers against the
    same bot token collide with Telegram 409 'terminated by other getUpdates'
    and neither works (observed 2026-06-05). flock auto-releases when the
    holder dies, so a crash never leaves a stuck lock. Returns the open file
    handle (keep it alive for the process lifetime) or None if held."""
    import fcntl
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    lockfile = paths.comms_dir / "comms-listen.pid"
    fh = open(lockfile, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> int:
    paths = comms_lib.Paths.from_env()
    if not paths.config.exists():
        log(f"no config at {paths.config} — run assistant-comms-setup.sh first")
        return 1
    if not WARM_PROMPT.exists():
        log(f"missing warm responder prompt at {WARM_PROMPT}")
        return 1

    lock = acquire_singleton(paths)
    if lock is None:
        log("another comms-listen already holds the lock — exiting")
        return 0

    env = dict(os.environ)
    for k, v in comms_lib.load_bedrock_env().items():
        env.setdefault(k, v)

    stop = threading.Event()

    def handle_sig(signum, frame):  # noqa: ARG001
        log(f"signal {signum} — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    threads = [
        threading.Thread(target=inbound_loop, args=(stop, env), name="inbound", daemon=True),
        threading.Thread(target=ledger_loop, args=(stop, env), name="ledger", daemon=True),
        threading.Thread(target=inbox_loop, args=(stop, env), name="inbox", daemon=True),
        threading.Thread(target=heartbeat_loop, args=(stop, env), name="heartbeat", daemon=True),
    ]
    log(f"comms-listen starting (pid={os.getpid()}) — 4 loops")
    for t in threads:
        t.start()
    # Block until a signal sets stop; daemon threads exit with the process.
    while not stop.is_set():
        stop.wait(1)
    log("comms-listen stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
