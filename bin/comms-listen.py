#!/usr/bin/env python3
"""comms-listen — event-driven assistant-comms daemon (Slack transport).

A single long-running process (KeepAlive LaunchAgent) with five concurrent jobs,
one blocking loop per thread joined under a shutdown Event:

  1. INBOUND (event) — REST-poll Slack (conversations.history via slack-poll.py)
     for inbound messages in the configured DM/channel. On a message: feeds the
     warm cmux session, which composes and sends a reply via slack-send.py.

  2. OUTBOUND PINGS (event) — watch actions-ledger.jsonl for appends. On new
     lines, format with comms_lib.fmt_action_line and send. No LLM — mechanical,
     fires near-instantly (~2s stat-poll floor).

  3. INBOX (event) — watch ~/.assistant/inbox for cmux-watcher signals
     (workspace needs input / work complete) and ping within seconds. kqueue on
     macOS, stat-poll fallback elsewhere.

  4. PROPOSALS (timer) — watch ~/.assistant/proposals.jsonl (the durable queue
     the lesson-extractor writes). Deliver each new pending lesson proposal to
     the channel exactly once (id high-water-mark cursor, backlog skipped on
     first run), asking Mukul to confirm it. No LLM.

  5. HEARTBEAT PAGE (timer) — every 60s, check Assistant's heartbeat; if stale
     or status ∈ {frozen, stale_world, respawn-requested}, send a templated
     urgent page (30-min dedup). No LLM.

All five reuse the tested CLIs and comms_lib. Durable memory stays in
conversation.jsonl, so a crash + KeepAlive respawn loses nothing.

Slack is the sole transport. The bot token comes from $SLACK_BOT_TOKEN; the
routing target + send-gate allowlist come from ~/.assistant/config.json.
slack-send.py itself enforces the send-gate, so even this daemon cannot page a
non-allowlisted target.
"""
from __future__ import annotations

import json
import os
import queue
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

SLACK_POLL = BIN / "slack-poll.py"
SLACK_SEND = BIN / "slack-send.py"
CONVERSATION = BIN / "conversation.py"

# Slack has no server-side long-poll for message history; we REST-poll on a
# short interval (the same model discord-poll used).
SLACK_POLL_INTERVAL_SEC = int(os.environ.get("COMMS_SLACK_POLL_SEC", "3"))
LEDGER_POLL_SEC = float(os.environ.get("COMMS_LEDGER_POLL_SEC", "2"))
HEARTBEAT_CHECK_SEC = int(os.environ.get("COMMS_HEARTBEAT_CHECK_SEC", "60"))
HEARTBEAT_DEDUP_SEC = 1800

# Proposals are a durable queue, not a live event, so we poll on a slow cadence
# (they're written at most a few times a day by the pulse-throttled extractor).
# Each drain delivers at most PROPOSALS_MAX_PER_DRAIN so one big extractor batch
# can't firehose the channel — the rest follow on later passes.
PROPOSALS_POLL_SEC = float(os.environ.get("COMMS_PROPOSALS_POLL_SEC", "30"))
PROPOSALS_MAX_PER_DRAIN = int(os.environ.get("COMMS_PROPOSALS_MAX_PER_DRAIN", "3"))

PYTHON = sys.executable  # use the same interpreter that launched us for the CLIs


def _send_args(text: str, kind: str, channel: str,
               ledger_key: str | None, reply_to: str | None = None) -> list[str]:
    """Build the argv for a slack-send.py call."""
    base = [str(SLACK_SEND), "--text", text, "--kind", kind, "--channel", channel]
    if ledger_key:
        base += ["--ledger-key", ledger_key]
    if reply_to:
        base += ["--reply-to", reply_to]
    return base


def _target(paths: comms_lib.Paths) -> str:
    """The default send target (config.slack.target, $SLACK_PING_TARGET override)."""
    try:
        return comms_lib.Config.load(paths.config).target
    except SystemExit:
        return ""


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

def ensure_warm_session(paths: comms_lib.Paths) -> dict | None:
    """Return a live warm-session record, spawning one if none is alive.

    On respawn, close the prior warm workspace first so we never leak Claude
    processes. close_own_workspace is title-guarded — it only ever closes an
    'assistant-comms (warm)' workspace this daemon spawned, never user work
    (the narrow, allowlisted exception to the 2026-05-26 close-workspace ban)."""
    sess = comms_session.read_session(paths)
    if sess and comms_session.cmux_alive(paths, sess["ws_ref"]):
        return sess
    if sess:
        log(f"warm session {sess['ws_ref']} gone — closing it and respawning")
        comms_session.close_own_workspace(paths, sess["ws_ref"], log=log)
        comms_session.clear_session_registry(paths)
    return comms_session.spawn_session(paths, WARM_PROMPT, log=log)


def reply_to_message(paths: comms_lib.Paths, sess: dict, rec: dict) -> dict:
    """Warm reply: record inbound, feed the message to the warm session, wait for
    its reply turn in the transcript, then /clear if context >= 50%. Returns the
    (possibly refreshed) session record."""
    channel = rec.get("channel")
    text = rec.get("text", "")
    msg_ts = rec.get("msg_ts")
    reply_to = rec.get("reply_to")

    # Record the inbound turn first — survives even if the session stalls.
    in_args = [str(CONVERSATION), "append", "--channel", str(channel),
               "--direction", "in", "--text", text]
    if msg_ts is not None:
        in_args += ["--msg-ts", str(msg_ts)]
    if reply_to is not None:
        in_args += ["--reply-to", str(reply_to)]
    cli(in_args, timeout=10)

    transcript = sess.get("transcript_path") or comms_session.newest_transcript(sess["cwd"])
    before_lines = comms_session.transcript_line_count(transcript) if transcript else 0

    # Feed the message as a user turn. The warm session's boot prompt tells it
    # how to reconstruct context, reply via slack-send.py, and record the out
    # turn. This is a 1:1 channel — the session replies at TOP LEVEL (no
    # threading), so the header only needs the channel + send CLI.
    feed_text = (
        f"[slack channel={channel} msg_ts={msg_ts} send_cli={SLACK_SEND}] {text}"
    )
    t0 = time.time()
    comms_session.feed(paths, sess["surface_ref"], feed_text)

    grew = False
    while time.time() - t0 < REPLY_WAIT_SEC:
        time.sleep(2)
        if transcript and comms_session.transcript_line_count(transcript) > before_lines:
            grew = True
            break
        if not transcript:
            transcript = comms_session.newest_transcript(sess["cwd"])
    log(f"reply channel={channel} msg={msg_ts} grew={grew} wall_ms={int((time.time()-t0)*1000)}")

    # Context management: clear-and-resume at >= 50%.
    if transcript and comms_session.should_clear(transcript):
        log(f"context >= {int(comms_session.CLEAR_THRESHOLD*100)}% — clear-and-resume")
        comms_session.clear_session(paths, sess["surface_ref"], WARM_PROMPT)
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


def _poll_thread(stop: threading.Event, env: dict, msg_queue: queue.Queue) -> None:
    """Continuously poll Slack for inbound messages and enqueue them.

    Runs independently of the consumer so messages are never dropped while a
    warm-session reply is in flight."""
    while not stop.is_set():
        rc, out, err = cli([str(SLACK_POLL)], timeout=35, env=env)
        if rc != 0:
            log(f"slack-poll rc={rc} err={err.strip()[:200]}")
            stop.wait(5)
            continue
        try:
            msgs = json.loads(out.strip() or "[]")
        except json.JSONDecodeError:
            log(f"slack-poll bad json: {out[:200]}")
            stop.wait(SLACK_POLL_INTERVAL_SEC)
            continue
        if isinstance(msgs, dict):  # an {"error": …} object
            log(f"slack-poll error: {msgs.get('error')}")
            stop.wait(5)
            continue
        for rec in msgs:
            msg_queue.put(rec)
        if not stop.is_set():
            stop.wait(SLACK_POLL_INTERVAL_SEC)


def _channel_worker(channel_id: str, ch_queue: queue.Queue, stop: threading.Event) -> None:
    """Per-channel worker: serializes replies for one channel while other
    channels run concurrently."""
    paths = comms_lib.Paths.from_env()
    sess = ensure_warm_session(paths)
    while not stop.is_set():
        try:
            rec = ch_queue.get(timeout=1)
        except queue.Empty:
            continue
        log(f"inbound channel={channel_id} msg={rec.get('msg_ts')} "
            f"text={rec.get('text','')[:80]!r}")
        sess = ensure_warm_session(paths)
        if not sess:
            log(f"no warm session — skipping msg={rec.get('msg_ts')}")
            continue
        sess = reply_to_message(paths, sess, rec)


def inbound_loop(stop: threading.Event, env: dict) -> None:
    paths = comms_lib.Paths.from_env()
    log("inbound loop started (slack, keyed-per-channel)")
    sess = ensure_warm_session(paths)
    if sess:
        comms_session.reconcile_warm_workspaces(paths, keep=sess["ws_ref"], log=log)

    channel_workers: dict[str, tuple[queue.Queue, threading.Thread]] = {}
    msg_queue: queue.Queue = queue.Queue()
    poller = threading.Thread(target=_poll_thread, args=(stop, env, msg_queue),
                              name="inbound-poller", daemon=True)
    poller.start()

    while not stop.is_set():
        try:
            rec = msg_queue.get(timeout=1)
        except queue.Empty:
            continue
        channel_id = str(rec.get("channel") or "default")
        if channel_id not in channel_workers:
            ch_q: queue.Queue = queue.Queue()
            t = threading.Thread(
                target=_channel_worker,
                args=(channel_id, ch_q, stop),
                name=f"inbound-{channel_id}",
                daemon=True,
            )
            t.start()
            channel_workers[channel_id] = (ch_q, t)
        channel_workers[channel_id][0].put(rec)


# --------------------------------------------------------------------------- outbound pings

def _suppress_reason(entry: dict) -> str | None:
    """Return a reason string if this ledger entry should NOT be broadcast to
    Slack, or None if it should. Pure decision logic (no I/O) — the daemon owns
    only urgent/actionable events; routine churn is surfaced by the warm session
    when asked, keeping the channel from becoming a firehose.

    Mirrors CommsSubsystem._broadcast_entry's suppression set exactly. NOTE:
    self-update FAILURES are intentionally NOT suppressed (only self-update
    'skip' keys are) — a real recurring fetch failure SHOULD surface. The fix
    for such a failure is to make it stop failing, not to mute it."""
    if entry.get("outcome") == "skipped":
        return "skipped (no work happened)"
    kind = entry.get("kind", "")
    key = entry.get("key", "")
    if kind in ("noop", "emit-card"):
        return f"routine kind={kind}"
    if kind == "self-update" and "skip" in key:
        return "self-update-skip"
    if kind in ("lesson-proposal", "lesson_proposal") or key.startswith("lesson-proposal"):
        # Lesson proposals are delivered by proposals_loop straight from
        # proposals.jsonl (the durable queue), never as an action-ledger entry.
        # This branch stays as defense-in-depth: if anything ever writes a
        # lesson-proposal ledger kind, it must not double-fire through the
        # ledger broadcast.
        return "lesson-proposal (delivered via proposals_loop)"
    return None


def ledger_loop(stop: threading.Event, env: dict) -> None:
    """Watch actions-ledger.jsonl; broadcast each new entry to the configured
    target. stat-poll (2s) — simple and dependency-free."""
    paths = comms_lib.Paths.from_env()
    comms_lib.initialize_cursor_if_missing(paths)
    log("ledger loop started (slack)")
    while not stop.is_set():
        target = _target(paths)
        try:
            entries = comms_lib.read_new_ledger_lines(paths)
        except Exception as e:  # noqa: BLE001
            log(f"ledger read error: {e}")
            entries = []
        for entry in entries:
            key = entry.get("key", "")
            reason = _suppress_reason(entry)
            if reason is not None:
                log(f"suppressed broadcast key={key}: {reason}")
                continue
            if not target:
                log(f"no target configured — skipping broadcast key={key}")
                continue
            body = comms_lib.fmt_action_line(entry)
            send_argv = _send_args(body, "action", target, key)
            rc, out, err = cli(send_argv, timeout=30, env=env)
            if rc != 0:
                log(f"ledger broadcast rc={rc} key={key} err={err.strip()[:160]}")
                continue
            # Mirror each sent broadcast into conversation.jsonl as an out turn.
            for line in out.strip().splitlines():
                try:
                    sent = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sent.get("muted") or not sent.get("message_id"):
                    continue
                convo_id = sent.get("channel")
                if convo_id:
                    cli([str(CONVERSATION), "append", "--channel", str(convo_id),
                         "--direction", "out", "--text", body, "--kind", "action",
                         "--msg-ts", str(sent["message_id"])], timeout=10)
            log(f"broadcast key={key}")
        stop.wait(LEDGER_POLL_SEC)


# --------------------------------------------------------------------------- lesson-proposal delivery

def _drain_proposals_once(env: dict, paths: comms_lib.Paths | None = None) -> int:
    """Deliver each new pending lesson proposal to Slack exactly once, advancing
    the delivery high-water mark only after a successful send. Returns the number
    PINGED this pass.

    Mirrors the ledger loop's discipline: read fresh entries (id > cursor)
    without mutating the cursor, send, then advance. A send failure leaves the
    cursor untouched so the proposal retries on the next pass — no proposal is
    ever silently lost. Capped at PROPOSALS_MAX_PER_DRAIN per pass so a burst
    can't firehose the channel. Each delivery is mirrored into conversation.jsonl
    as an out turn so the warm session can resolve `y`/`n` after a /clear."""
    paths = paths or comms_lib.Paths.from_env()
    target = _target(paths)
    fresh = comms_lib.read_new_proposals(paths, limit=PROPOSALS_MAX_PER_DRAIN)
    if not fresh:
        return 0
    if not target:
        log(f"proposals: no target configured — leaving {len(fresh)} for retry")
        return 0
    n = 0
    for entry in fresh:
        pid = str(entry.get("id") or entry.get("ts") or "")
        body = comms_lib.fmt_lesson_proposal(entry)
        send_argv = _send_args(body, "action", target, f"proposal:{pid}")
        rc, out, err = cli(send_argv, timeout=30, env=env)
        if rc != 0:
            # Halt on first failure: advancing past pid would skip it forever.
            # Everything already delivered kept its cursor; this one retries.
            log(f"proposals: send rc={rc} id={pid} err={err.strip()[:160]} — halting drain")
            break
        # Mirror the sent proposal into conversation.jsonl so the warm session
        # can find the id when Mukul replies `y` after a /clear.
        for line in out.strip().splitlines():
            try:
                sent = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sent.get("muted") or not sent.get("message_id"):
                continue
            convo_id = sent.get("channel")
            if convo_id:
                cli([str(CONVERSATION), "append", "--channel", str(convo_id),
                     "--direction", "out", "--text", body, "--kind", "action",
                     "--msg-ts", str(sent["message_id"])], timeout=10)
        comms_lib.write_proposals_cursor(paths, pid)
        n += 1
        log(f"proposals: pinged lesson proposal id={pid}")
    return n


def proposals_loop(stop: threading.Event, env: dict) -> None:
    """Watch proposals.jsonl; deliver each new pending lesson proposal to the
    configured target exactly once. slow stat-poll (30s) — proposals are a
    durable queue written a few times a day, not a live stream."""
    paths = comms_lib.Paths.from_env()
    comms_lib.initialize_proposals_cursor_if_missing(paths)
    backlog = len(comms_lib.read_all_proposals(paths))
    log(f"proposals loop started (slack) — cursor={comms_lib.read_proposals_cursor(paths)!r} "
        f"backlog={backlog} skipped (deliver only new; rm proposals.cursor to replay)")
    while not stop.is_set():
        try:
            _drain_proposals_once(env, paths)
        except Exception as e:  # noqa: BLE001
            log(f"proposals drain error: {e}")
        stop.wait(PROPOSALS_POLL_SEC)


# --------------------------------------------------------------------------- inbox watcher (cmux-watcher signals)

INBOX_DIR = HOME / ".assistant" / "inbox"
# cmux-watcher (bin/cmux-watcher.py) drops cmux-*.json signals here the instant a
# workspace needs input or finishes a notable turn. We ping within seconds
# instead of waiting for the next pulse. pulse-*.json belongs to the mechanical
# pulse and is NOT ours — we only consume cmux-*.json.
INBOX_GLOB = "cmux-*.json"
INBOX_POLL_FALLBACK_SEC = float(os.environ.get("COMMS_INBOX_POLL_SEC", "2"))
# A workspace signal is only actionable while it's fresh — a "needs input" from
# an hour ago (let alone weeks) is noise, not a page. cmux-watcher keeps writing
# these whether or not comms is running, so on startup we can face a large stale
# backlog; anything older than this is dropped WITHOUT a ping. Live signals
# arrive within ~2s, far inside the window.
INBOX_MAX_AGE_SEC = float(os.environ.get("COMMS_INBOX_MAX_AGE_SEC", "300"))


def _signal_age_sec(item: dict, path: Path, now: float) -> float:
    """Age of a signal in seconds. Prefer the ISO `ts` cmux-watcher stamps;
    fall back to the file mtime if it's missing/unparseable."""
    ts = item.get("ts")
    if isinstance(ts, str) and ts:
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now - dt.timestamp()
        except ValueError:
            pass
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def _drain_inbox_once(env: dict) -> int:
    """Read every cmux-*.json in the inbox, ping, delete it. Returns the number
    of items PINGED. Stale signals (older than INBOX_MAX_AGE_SEC) are deleted
    without a ping. A malformed file is logged and removed so it never wedges the
    loop. Atomic-write on the producer side means we never read a half-written
    file. A failed send leaves the file in place so the next pass retries."""
    if not INBOX_DIR.exists():
        return 0
    paths = comms_lib.Paths.from_env()
    target = _target(paths)
    now = time.time()
    n = 0
    stale = 0
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
        # Freshness gate: never ping a stale signal — delete it silently.
        age = _signal_age_sec(item, p, now)
        if age > INBOX_MAX_AGE_SEC:
            try:
                p.unlink()
            except OSError:
                pass
            stale += 1
            continue
        if not target:
            log(f"inbox: no target configured — leaving {p.name} for retry")
            continue
        body = comms_lib.fmt_workspace_signal(item)
        ledger_key = f"{item.get('ws_ref') or 'ws'}:{item.get('signal_type') or item.get('signal') or 'signal'}"
        send_argv = _send_args(body, "action", target, ledger_key)
        rc, _out, err = cli(send_argv, timeout=30, env=env)
        if rc != 0:
            log(f"inbox: send rc={rc} for {p.name} err={err.strip()[:160]}")
            continue
        try:
            p.unlink()
        except OSError:
            pass
        n += 1
        log(f"inbox: pinged {item.get('signal_type') or item.get('signal')} "
            f"ws={item.get('ws_ref')} ({p.name})")
    if stale:
        log(f"inbox: dropped {stale} stale signal(s) older than {int(INBOX_MAX_AGE_SEC)}s (no ping)")
    return n


def inbox_loop(stop: threading.Event, env: dict) -> None:
    """Watch ~/.assistant/inbox for cmux-watcher signals and ping.

    Event-driven on macOS via select.kqueue (NOTE_WRITE/NOTE_EXTEND on the inbox
    directory) — instant wake on a new file. Linux (no kqueue) falls back to a
    short stat-poll. We always drain once on entry and re-drain on every wake;
    the kqueue timeout doubles as a safety net so a missed vnode event can never
    strand a signal."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    log("inbox loop started (cmux-watcher signals, slack)")
    _drain_inbox_once(env)

    kq = getattr(select, "kqueue", None)
    if kq is None:
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
            events = kqueue.control(None, 1, 5)
            if stop.is_set():
                break
            if events:
                time.sleep(0.05)  # coalesce a burst of writes into one drain
            _drain_inbox_once(env)
    finally:
        try:
            os.close(inbox_fd)
        except OSError:
            pass


# --------------------------------------------------------------------------- heartbeat page

def heartbeat_loop(stop: threading.Event, env: dict) -> None:
    paths = comms_lib.Paths.from_env()
    last_alert = 0
    log("heartbeat loop started (slack)")
    while not stop.is_set():
        try:
            cfg = comms_lib.Config.load(paths.config)
            stale_sec = cfg.stale_heartbeat_sec
            target = cfg.target
        except SystemExit:
            stale_sec, target = 1200, ""
        try:
            hb_raw = paths.heartbeat.read_text() if paths.heartbeat.exists() else ""
            hb = json.loads(hb_raw) if hb_raw else {}
        except json.JSONDecodeError:
            hb = {}
        last_ts = int(hb.get("last_pulse_ts") or 0)
        if last_ts > 0:
            age = int(time.time()) - last_ts
            stale = age > stale_sec
            bad = hb.get("status") in {"frozen", "stale_world", "respawn-requested"}
            now = int(time.time())
            if (stale or bad) and now - last_alert >= HEARTBEAT_DEDUP_SEC and target:
                body = comms_lib.fmt_heartbeat_alert(hb, age)
                send_argv = _send_args(body, "urgent", target, None)
                rc, _, err = cli(send_argv, timeout=30, env=env)
                last_alert = now
                log(f"heartbeat-stale page age={age}s rc={rc}")
            elif not (stale or bad):
                last_alert = 0  # healthy → re-arm
        comms_lib.write_comms_heartbeat(paths, status="active", pulse_idx=0,
                                        note="listen-daemon")
        stop.wait(HEARTBEAT_CHECK_SEC)


# --------------------------------------------------------------------------- main

def acquire_singleton(paths: comms_lib.Paths):
    """flock a pidfile so only ONE daemon runs. flock auto-releases when the
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
    if not comms_lib.bot_token():
        log("SLACK_BOT_TOKEN not set in the environment — cannot start")
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
        threading.Thread(target=proposals_loop, args=(stop, env), name="proposals", daemon=True),
        threading.Thread(target=heartbeat_loop, args=(stop, env), name="heartbeat", daemon=True),
    ]
    log(f"comms-listen starting (pid={os.getpid()}, transport=slack) — 5 loops")
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1)
    log("comms-listen stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
