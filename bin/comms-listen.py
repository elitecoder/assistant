#!/usr/bin/env python3
"""comms-listen — event-driven assistant-comms daemon (Slack transport).

A single long-running process (KeepAlive LaunchAgent) with six concurrent jobs,
one blocking loop per thread joined under a shutdown Event:

  1. INBOUND (event) — REST-poll Slack (conversations.history via slack-poll.py)
     for inbound messages in the configured DM/channel. Each message is queued
     on disk and fed to the warm cmux session, which composes and sends a reply
     via slack-send.py. It leaves the queue only once the session's transcript
     shows it arrived; until then it's retried.

  2. WATCHDOG (timer) — every WATCHDOG_INTERVAL_SEC, ensure a live warm session
     exists (respawn if cmux says it's gone; leave it alone if cmux doesn't
     answer). Closes the gap where a warm workspace that died between inbound
     messages (cmux restart, crash, sleep) stayed dead until the next Slack
     message arrived.

  3. OUTBOUND PINGS (event) — watch actions-ledger.jsonl for appends. On new
     lines, skip housekeeping, format with comms_lib.fmt_action_line, and send
     at most LEDGER_MAX_PER_PASS plus one summary line. No LLM — mechanical,
     fires near-instantly (~2s stat-poll floor).

  4. INBOX (event) — watch ~/.assistant/inbox for cmux-watcher signals
     (workspace needs input / work complete) and ping within seconds, at most
     once per workspace per INBOX_COOLDOWN_SEC unless it's a real question.
     kqueue on macOS, stat-poll fallback elsewhere.

  5. PROPOSALS (timer) — watch ~/.assistant/proposals.jsonl (the durable queue
     the lesson-extractor writes). Deliver each new pending lesson proposal to
     the channel exactly once (id high-water-mark cursor, backlog skipped on
     first run), asking Mukul to confirm it. No LLM.

  6. HEARTBEAT PAGE (timer) — every 60s, check Assistant's heartbeat; if stale
     or status ∈ {frozen, stale_world, respawn-requested} for two checks in a
     row, send one templated urgent page, then one message when it recovers.
     No LLM.

All six reuse the tested CLIs and comms_lib. Durable memory stays in
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
import agent_session  # noqa: E402
import comms_lib  # noqa: E402
import comms_session  # noqa: E402


def _load_doctor():
    """The doctor lives at bin/assistant-doctor.py — a HYPHENATED filename that is
    not a valid module name, so a bare `import assistant_doctor` can never resolve
    (it silently sent the preflight down its except-and-continue path on every
    startup). Load it by file path, exactly as tests/test_doctor.py does, and
    register it under the importable name so the preflight can `import` it."""
    import importlib.util  # noqa: PLC0415
    if "assistant_doctor" in sys.modules:
        return sys.modules["assistant_doctor"]
    spec = importlib.util.spec_from_file_location(
        "assistant_doctor",
        str(Path(__file__).resolve().parent / "assistant-doctor.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["assistant_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod

HOME = Path(os.environ["HOME"])
REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
WARM_PROMPT = REPO / "prompts" / "prompt-assistant-comms-warm.md"

SLACK_POLL = BIN / "slack-poll.py"
SLACK_SEND = BIN / "slack-send.py"
CONVERSATION = BIN / "conversation.py"

# Slack has no server-side long-poll for message history; we REST-poll on a
# short interval (the same model discord-poll used).
SLACK_POLL_INTERVAL_SEC = int(os.environ.get("COMMS_SLACK_POLL_SEC", "3"))
LEDGER_POLL_SEC = float(os.environ.get("COMMS_LEDGER_POLL_SEC", "2"))
HEARTBEAT_CHECK_SEC = int(os.environ.get("COMMS_HEARTBEAT_CHECK_SEC", "60"))
HEARTBEAT_CONFIRM_CHECKS = 2

# Inbound messages wait on disk until the warm session confirms it received
# them. The slack cursor moves past a message as soon as it's polled, so before
# this queue a message that arrived while no session was up was lost for good
# (2026-09-27: two "are you alive?" messages). Undelivered messages are retried
# every PENDING_RETRY_SEC and given up after PENDING_MAX_AGE_SEC, when an
# answer would no longer help.
PENDING_RETRY_SEC = float(os.environ.get("COMMS_PENDING_RETRY_SEC", "30"))
PENDING_MAX_AGE_SEC = float(os.environ.get("COMMS_PENDING_MAX_AGE_SEC", str(3 * 3600)))
RESTART_NOTICE = ("My chat session isn't responding right now. I'll answer as soon "
                  "as it's back.")
RESTART_NOTICE_AFTER_SEC = float(os.environ.get("COMMS_RESTART_NOTICE_AFTER_SEC", "60"))

# At most this many action updates go out per ledger pass; the rest collapse
# into one summary line (2026-09-27: 129 posts in two minutes).
LEDGER_MAX_PER_PASS = int(os.environ.get("COMMS_LEDGER_MAX_PER_PASS", "5"))

# One workspace gets at most one ping per window, whatever the signal. A real
# question (AskUserQuestion) always goes through (2026-09-28: one workspace was
# pinged 21 times in six hours, a median of three minutes apart).
INBOX_COOLDOWN_SEC = float(os.environ.get("COMMS_INBOX_COOLDOWN_SEC", "900"))

# Proposals are a durable queue, not a live event, so we poll on a slow cadence
# (they're written at most a few times a day by the pulse-throttled extractor).
# Each drain delivers at most PROPOSALS_MAX_PER_DRAIN so one big extractor batch
# can't firehose the channel — the rest follow on later passes.
PROPOSALS_POLL_SEC = float(os.environ.get("COMMS_PROPOSALS_POLL_SEC", "30"))
PROPOSALS_MAX_PER_DRAIN = int(os.environ.get("COMMS_PROPOSALS_MAX_PER_DRAIN", "3"))

# Warm-session liveness watchdog. ensure_warm_session (the spawn/respawn logic)
# was originally called ONLY on daemon startup and per inbound Slack message, so
# a warm workspace that died during a quiet period (cmux restart, crash, machine
# sleep) stayed dead until the next inbound message — sometimes hours, with a
# stale session.json pointing at a ref cmux no longer knows. This loop closes
# that gap: it calls ensure_warm_session on a slow cadence so a dead/missing warm
# session self-heals within WATCHDOG_INTERVAL_SEC regardless of inbound traffic.
WATCHDOG_INTERVAL_SEC = int(os.environ.get("COMMS_WATCHDOG_INTERVAL_SEC", "60"))

# Exponential backoff for a warm session that keeps failing to come up. A single
# spawn now cleans up after itself (comms_session._abandon_failed_spawn), but a
# persistent boot failure at the fixed 60s cadence still means a new-workspace
# every minute — heavy cmux churn that helped kill cmux on 2026-09-14. On each
# non-alive tick the wait doubles from WATCHDOG_INTERVAL_SEC up to this cap; the
# next alive tick resets it. A healthy session never backs off.
WATCHDOG_BACKOFF_MAX_SEC = int(os.environ.get("COMMS_WATCHDOG_BACKOFF_MAX_SEC", "1800"))

# Serializes ensure_warm_session's respawn critical section. Without this the
# watchdog tick and an inbound message arriving at the same instant could BOTH
# see the session dead and BOTH spawn a fresh workspace — two warm sessions
# racing, one orphaned. Held only across the read→alive-check→respawn span, so
# it never blocks inbound replies for long.
_warm_session_lock = threading.Lock()

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

# Results of _warm_session, beyond the session record itself.
SESSION_ALIVE = "alive"
SESSION_SPAWNED = "spawned"
SESSION_UNREACHABLE = "unreachable"  # cmux isn't answering; the session is left alone
SESSION_NONE = "none"

# When cmux first stopped answering in the current episode, so the log gets one
# line per episode instead of one per check.
_cmux_silent_since: float | None = None


def _note_cmux_answer(answered: bool) -> None:
    global _cmux_silent_since
    now = time.time()
    if not answered and _cmux_silent_since is None:
        _cmux_silent_since = now
        log("cmux isn't answering — leaving the warm session alone until it does")
    elif answered and _cmux_silent_since is not None:
        log(f"cmux answering again after {int(now - _cmux_silent_since)}s")
        _cmux_silent_since = None


def _warm_session(paths: comms_lib.Paths, *, respawn_on_stale: bool = False,
                  force_respawn: bool = False) -> tuple[dict | None, str]:
    """The live warm-session record and how it was obtained, spawning one if
    none is alive.

    A session is replaced only when cmux says its workspace is gone, or when
    it's alive and the caller asks: respawn_on_stale (inbound path only) for a
    session whose model id no longer matches the current backend, or
    force_respawn for one that didn't accept a typed prompt. When cmux doesn't
    answer at all, the session is left alone — a refused or timed-out check
    says nothing about the workspace (2026-09-27: every such check used to
    close a healthy session and spawn another onto a stalled cmux).

    On respawn, close the prior warm workspace first so we never leak Claude
    processes. close_own_workspace is title-guarded — it only ever closes an
    'assistant-comms (warm)' workspace this daemon spawned, never user work
    (the narrow, allowlisted exception to the 2026-05-26 close-workspace ban).

    Serialized by _warm_session_lock: the watchdog tick and an inbound message
    can both call this concurrently, and without a guard both would see the
    session dead and double-spawn. The lock scopes only the respawn decision."""
    with _warm_session_lock:
        sess = comms_session.read_session(paths)
        if sess:
            state = comms_session.workspace_state(paths, sess["ws_ref"])
            _note_cmux_answer(state != comms_session.UNKNOWN)
            if state == comms_session.UNKNOWN:
                return sess, SESSION_UNREACHABLE
            if state == comms_session.ALIVE:
                # A stale-but-alive session still WORKS on its old backend, so
                # only the INBOUND path respawns it, BEFORE it feeds its own
                # message. The watchdog leaves a live session alone: closing it
                # there could race an active reply.
                if force_respawn:
                    why = "didn't accept the typed message"
                elif respawn_on_stale and not comms_session.warm_session_model_is_current(paths, sess):
                    why = f"model stale ({sess.get('model')!r} — backend changed since spawn)"
                else:
                    return sess, SESSION_ALIVE
            else:
                why = "gone"
            log(f"warm session {sess['ws_ref']} {why} — closing it and respawning")
            comms_session.close_own_workspace(paths, sess["ws_ref"], log=log)
            comms_session.clear_session_registry(paths)
        spawned = comms_session.spawn_session(paths, WARM_PROMPT, log=log)
        return spawned, (SESSION_SPAWNED if spawned else SESSION_NONE)


def ensure_warm_session(paths: comms_lib.Paths, *, respawn_on_stale: bool = False) -> dict | None:
    """The live warm-session record, spawning one if none is alive (see
    _warm_session)."""
    return _warm_session(paths, respawn_on_stale=respawn_on_stale)[0]


def feed_text(recs: list[dict], channel: str) -> str:
    """The user turn that hands inbound Slack message(s) to the warm session.

    The header carries the newest message's ts; its `msg_ts=<ts>` doubles as the
    marker that confirms the turn landed. Messages that piled up while the
    session was down go in one turn, so the session answers them together."""
    ts = recs[-1].get("msg_ts")
    header = f"[slack channel={channel} msg_ts={ts} send_cli={SLACK_SEND}]"
    if len(recs) == 1:
        return f"{header} {recs[0].get('text', '')}"
    parts = " ".join(f"({i}) {r.get('text', '')}" for i, r in enumerate(recs, 1))
    return (f"{header} {len(recs)} messages arrived while your session was down. "
            f"Answer them together in one reply. {parts}")


def reply_to_message(paths: comms_lib.Paths, sess: dict,
                     recs: list[dict]) -> tuple[bool, dict]:
    """Hand inbound message(s) to the warm session and confirm its transcript
    recorded them, then /clear if context >= 50%. Returns (delivered, the
    possibly refreshed session record).

    Confirmation looks for the message's marker in the bound transcript first,
    then in any transcript in the session's project folder, and rebinds the
    session to wherever it landed."""
    channel = str(recs[-1].get("channel"))
    marker = f"msg_ts={recs[-1].get('msg_ts')}"
    # Thread the session's provider through every transcript-root / context call:
    # a Droid session writes under ~/.factory/sessions with no usage block, so a
    # claude default here would read the wrong root and never clear (G3).
    agent = sess.get("agent") or agent_session.CLAUDE
    project_dir = comms_session.project_dir_for_cwd(sess["cwd"], agent)
    bound = sess.get("transcript_path")
    since = time.time() - 1
    found: list[str] = []

    def confirmed() -> bool:
        if bound and comms_session.transcript_has_submission(bound, marker):
            hit = bound
        else:
            hit = comms_session.find_submission(project_dir, marker, since)
        if hit:
            found.append(hit)
        return hit is not None

    t0 = time.time()
    delivered = comms_session.submit(paths, sess["surface_ref"], feed_text(recs, channel),
                                     marker, confirmed)
    log(f"reply channel={channel} msg={recs[-1].get('msg_ts')} n={len(recs)} "
        f"submitted={delivered} wall_ms={int((time.time() - t0) * 1000)}")
    if not delivered:
        return False, sess
    transcript = found[-1]

    # Context management: clear-and-resume at >= 50% (claude) or the size proxy
    # (droid). should_clear + clear_session are provider-aware; for droid a
    # "clear" is a lossless respawn since durable memory lives in conversation.jsonl.
    # clear_session owns the registry update and returns the refreshed record.
    if comms_session.should_clear(transcript, agent=agent):
        log(f"context threshold reached ({agent}) — clear-and-resume")
        return True, comms_session.clear_session(paths, sess, WARM_PROMPT, agent=agent, log=log)

    if transcript != bound:
        log(f"warm session transcript rebound to {transcript}")
        comms_session.write_session(paths, sess["ws_ref"], sess["surface_ref"],
                                    sess["cwd"], transcript)
        sess = comms_session.read_session(paths) or sess
    return True, sess


# --------------------------------------------------------------------------- pending inbound

_pending_lock = threading.Lock()


def _pending_path(paths: comms_lib.Paths) -> Path:
    return paths.comms_dir / "pending-inbound.json"


def read_pending(paths: comms_lib.Paths) -> list[dict]:
    try:
        data = json.loads(_pending_path(paths).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _write_pending(paths: comms_lib.Paths, recs: list[dict]) -> None:
    p = _pending_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(recs, indent=2))
    os.replace(tmp, p)


def add_pending(paths: comms_lib.Paths, rec: dict) -> None:
    """Queue an inbound message until the warm session confirms it. A message
    already queued (same msg_ts) isn't added twice."""
    with _pending_lock:
        recs = read_pending(paths)
        if any(r.get("msg_ts") == rec.get("msg_ts") for r in recs):
            return
        _write_pending(paths, [*recs, rec])


def remove_pending(paths: comms_lib.Paths, msg_tss: list) -> None:
    drop = set(msg_tss)
    with _pending_lock:
        _write_pending(paths, [r for r in read_pending(paths) if r.get("msg_ts") not in drop])


def message_age_sec(rec: dict, now: float) -> float:
    """Seconds since Slack received the message (its ts is epoch seconds)."""
    try:
        return now - float(rec.get("msg_ts"))
    except (TypeError, ValueError):
        return 0.0


def split_pending(recs: list[dict], channel: str, now: float,
                  max_age: float) -> tuple[list[dict], list[dict]]:
    """(still worth answering, too old to answer) for one channel's queued
    messages, oldest first."""
    mine = sorted((r for r in recs if str(r.get("channel") or "default") == channel),
                  key=lambda r: message_age_sec(r, now), reverse=True)
    fresh = [r for r in mine if message_age_sec(r, now) <= max_age]
    expired = [r for r in mine if message_age_sec(r, now) > max_age]
    return fresh, expired


def _record_inbound(rec: dict) -> None:
    """Append the inbound turn to conversation.jsonl as soon as it arrives, so
    it survives even if it's never delivered."""
    args = [str(CONVERSATION), "append", "--channel", str(rec.get("channel")),
            "--direction", "in", "--text", rec.get("text", "")]
    if rec.get("msg_ts") is not None:
        args += ["--msg-ts", str(rec["msg_ts"])]
    if rec.get("reply_to") is not None:
        args += ["--reply-to", str(rec["reply_to"])]
    cli(args, timeout=10)


def _restart_notice_path(paths: comms_lib.Paths) -> Path:
    return paths.comms_dir / "restart-notice.json"


def _notify_restart_once(paths: comms_lib.Paths, channel: str, env: dict | None) -> None:
    """Tell the user, once per outage, that their message is waiting. The marker
    is written before sending, so a failing send can't repeat every retry."""
    marker = _restart_notice_path(paths)
    if marker.exists():
        return
    marker.write_text(json.dumps({"ts": time.time(), "channel": channel}))
    rc, _out, err = cli(_send_args(RESTART_NOTICE, "reply", channel, None), timeout=30, env=env)
    log(f"restart notice sent to {channel}" if rc == 0
        else f"restart notice rc={rc} err={err.strip()[:160]}")


def _deliver_pending(paths: comms_lib.Paths, channel: str, env: dict | None) -> bool:
    """Try to hand this channel's queued messages to the warm session. Returns
    True when nothing is left waiting."""
    now = time.time()
    fresh, expired = split_pending(read_pending(paths), channel, now, PENDING_MAX_AGE_SEC)
    if expired:
        for r in expired:
            log(f"inbound msg={r.get('msg_ts')} still undelivered after "
                f"{comms_lib.fmt_age(int(message_age_sec(r, now)))} — giving up on it")
        remove_pending(paths, [r.get("msg_ts") for r in expired])
    if not fresh:
        return True
    sess, how = _warm_session(paths, respawn_on_stale=True)
    delivered = False
    if sess and how != SESSION_UNREACHABLE:
        delivered, _sess = reply_to_message(paths, sess, fresh)
        if not delivered:
            _warm_session(paths, force_respawn=True)
    if delivered:
        remove_pending(paths, [r.get("msg_ts") for r in fresh])
        _restart_notice_path(paths).unlink(missing_ok=True)
        return True
    log(f"inbound: {len(fresh)} message(s) waiting for the warm session "
        f"(session={how}); retrying in {int(PENDING_RETRY_SEC)}s")
    if message_age_sec(fresh[0], now) >= RESTART_NOTICE_AFTER_SEC:
        _notify_restart_once(paths, channel, env)
    return False


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


def _channel_worker(channel_id: str, ch_queue: queue.Queue, stop: threading.Event,
                    env: dict | None = None) -> None:
    """Per-channel worker: queues each inbound message on disk, delivers the
    queue, and retries every PENDING_RETRY_SEC until the warm session confirms
    it. Serializes replies for one channel while other channels run
    concurrently."""
    paths = comms_lib.Paths.from_env()
    waiting = bool(split_pending(read_pending(paths), channel_id, time.time(),
                                 PENDING_MAX_AGE_SEC)[0])
    last_try = 0.0
    while not stop.is_set():
        try:
            rec = ch_queue.get(timeout=1)
        except queue.Empty:
            rec = None
        if rec is not None:
            log(f"inbound channel={channel_id} msg={rec.get('msg_ts')} "
                f"text={rec.get('text', '')[:80]!r}")
            _record_inbound(rec)
            add_pending(paths, rec)
            waiting = True
        if waiting and (rec is not None or time.time() - last_try >= PENDING_RETRY_SEC):
            last_try = time.time()
            try:
                waiting = not _deliver_pending(paths, channel_id, env)
            except Exception as e:  # noqa: BLE001 — one bad pass must never kill the channel
                log(f"inbound delivery error (will retry): {type(e).__name__}: {e}")


def inbound_loop(stop: threading.Event, env: dict) -> None:
    paths = comms_lib.Paths.from_env()
    log("inbound loop started (slack, keyed-per-channel)")
    sess = ensure_warm_session(paths, respawn_on_stale=True)
    if sess:
        comms_session.reconcile_warm_workspaces(paths, keep=sess["ws_ref"], log=log)

    channel_workers: dict[str, tuple[queue.Queue, threading.Thread]] = {}

    def worker_for(channel_id: str) -> queue.Queue:
        if channel_id not in channel_workers:
            ch_q: queue.Queue = queue.Queue()
            t = threading.Thread(
                target=_channel_worker,
                args=(channel_id, ch_q, stop, env),
                name=f"inbound-{channel_id}",
                daemon=True,
            )
            t.start()
            channel_workers[channel_id] = (ch_q, t)
        return channel_workers[channel_id][0]

    # Messages still queued from before a restart get their workers right away.
    for channel_id in sorted({str(r.get("channel") or "default") for r in read_pending(paths)}):
        worker_for(channel_id)

    msg_queue: queue.Queue = queue.Queue()
    poller = threading.Thread(target=_poll_thread, args=(stop, env, msg_queue),
                              name="inbound-poller", daemon=True)
    poller.start()

    while not stop.is_set():
        try:
            rec = msg_queue.get(timeout=1)
        except queue.Empty:
            continue
        worker_for(str(rec.get("channel") or "default")).put(rec)


# --------------------------------------------------------------------------- warm-session liveness watchdog

def watchdog_tick(paths: comms_lib.Paths) -> str:
    """One watchdog pass: ensure a live warm session exists, respawning if the
    registered one is dead/missing. Returns a short status string for logging
    and tests. NEVER raises — a transient cmux error (cmux briefly down, a
    spawn timeout) must not kill the watchdog thread; it logs and retries on the
    next tick. Delegates the actual liveness check + respawn to
    _warm_session under _warm_session_lock, so this never races an
    inbound-driven respawn. "cmux-unresponsive" means the session was left
    alone because cmux didn't answer; it backs off like a failure so a long
    cmux stall isn't probed every minute."""
    try:
        sess, how = _warm_session(paths)
    except Exception as e:  # noqa: BLE001 — watchdog must survive any error
        return f"error:{type(e).__name__}"
    if how == SESSION_UNREACHABLE:
        return "cmux-unresponsive"
    return "alive" if sess else "no-session"


def watchdog_delay(fail_streak: int) -> int:
    """Seconds to wait before the next watchdog tick.

    fail_streak is the count of consecutive non-alive ticks (0 right after an
    alive tick). Delay doubles from WATCHDOG_INTERVAL_SEC per consecutive
    failure, capped at WATCHDOG_BACKOFF_MAX_SEC. Pure so the backoff curve is
    unit-tested without running the loop."""
    if fail_streak <= 0:
        return WATCHDOG_INTERVAL_SEC
    return min(WATCHDOG_INTERVAL_SEC * (2 ** fail_streak), WATCHDOG_BACKOFF_MAX_SEC)


def watchdog_loop(stop: threading.Event, env: dict) -> None:
    """Periodically call ensure_warm_session so a warm workspace that died
    between inbound messages (cmux restart, crash, machine sleep) self-heals
    within WATCHDOG_INTERVAL_SEC instead of waiting for the next Slack message.
    Slow cadence — the warm session is only load-bearing when a message arrives,
    and ensure_warm_session is already called per inbound message, so this is a
    safety net, not a hot path.

    A run of non-alive ticks (cmux down, a warm session that never boots) backs
    off exponentially via watchdog_delay so a persistent failure can't respawn a
    workspace every minute the way it did on 2026-09-14; an alive tick resets the
    cadence to WATCHDOG_INTERVAL_SEC."""
    paths = comms_lib.Paths.from_env()
    log(f"warm-session watchdog started (interval={WATCHDOG_INTERVAL_SEC}s)")
    fail_streak = 0
    while not stop.is_set():
        status = watchdog_tick(paths)
        if status == "alive":
            fail_streak = 0
        else:
            fail_streak += 1
            delay = watchdog_delay(fail_streak)
            log(f"watchdog: warm session {status} — retry in {delay}s "
                f"(consecutive failures={fail_streak})")
        stop.wait(watchdog_delay(fail_streak))


# --------------------------------------------------------------------------- outbound pings

HOUSEKEEPING_KINDS = ("decision-transition", "strategist-autopause", "stranded", "skipped")


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
    # Keel RECEIPT_KINDS (src/assistant/brief.py): silent, pull-only receipts the
    # brief shows quietly — a dropped noise event, an auto-done decision, a merge
    # dispatch. They must NOT push to Slack (comms branched before these existed;
    # without this an event-drop firehose hits the channel). Keep in sync with
    # brief.RECEIPT_KINDS and CommsSubsystem._broadcast_entry.
    if kind in ("event-drop", "decision-auto-done", "merge-dispatched"):
        return f"receipt kind={kind} (pull-only, shown in brief)"
    # Housekeeping the brief and dashboard already show: expiring stale
    # decisions, pausing the strategist, nudging a stalled workspace, and skip
    # records. Pushing them buried real updates (2026-09-27: 129 "decision
    # expired" posts in two minutes). Keep in sync with
    # CommsSubsystem._broadcast_entry.
    if kind in HOUSEKEEPING_KINDS:
        return f"housekeeping kind={kind} (pull-only, shown in brief)"
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


def plan_broadcast(entries: list[dict],
                   max_send: int = LEDGER_MAX_PER_PASS) -> tuple[list[dict], list[tuple[dict, str]], int]:
    """Split one ledger pass into (entries to post, suppressed entries with
    their reasons, how many more were held back past the per-pass cap)."""
    suppressed = []
    postable = []
    for entry in entries:
        reason = _suppress_reason(entry)
        if reason is None:
            postable.append(entry)
        else:
            suppressed.append((entry, reason))
    return postable[:max_send], suppressed, max(0, len(postable) - max_send)


def fmt_overflow(n: int) -> str:
    return (f"…and {n} more Assistant update{'s' if n != 1 else ''}. "
            f"They're on the dashboard at http://127.0.0.1:9876.")


def _mirror_sent(out: str, body: str) -> None:
    """Record each message slack-send posted as an out turn in
    conversation.jsonl."""
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


def ledger_loop(stop: threading.Event, env: dict) -> None:
    """Watch actions-ledger.jsonl; broadcast each new entry to the configured
    target, at most LEDGER_MAX_PER_PASS per pass plus one summary line for the
    rest. stat-poll (2s) — simple and dependency-free."""
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
        to_send, suppressed, overflow = plan_broadcast(entries, LEDGER_MAX_PER_PASS)
        for entry, reason in suppressed:
            log(f"suppressed broadcast key={entry.get('key', '')}: {reason}")
        if (to_send or overflow) and not target:
            log(f"no target configured — skipping {len(to_send) + overflow} broadcast(s)")
            to_send, overflow = [], 0
        for entry in to_send:
            key = entry.get("key", "")
            body = comms_lib.fmt_action_line(entry)
            rc, out, err = cli(_send_args(body, "action", target, key), timeout=30, env=env)
            if rc != 0:
                log(f"ledger broadcast rc={rc} key={key} err={err.strip()[:160]}")
                continue
            _mirror_sent(out, body)
            log(f"broadcast key={key}")
        if overflow:
            body = fmt_overflow(overflow)
            rc, out, err = cli(_send_args(body, "action", target, None), timeout=30, env=env)
            if rc == 0:
                _mirror_sent(out, body)
            log(f"broadcast overflow summary for {overflow} update(s) rc={rc}")
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


def inbox_should_ping(item: dict, last_ping_ts: float | None, now: float,
                      cooldown: float = INBOX_COOLDOWN_SEC) -> bool:
    """True if this workspace signal should reach Slack: a real question always
    does; anything else only when the workspace hasn't been pinged within the
    cooldown."""
    if item.get("pattern_matched") == "AskUserQuestion":
        return True
    return last_ping_ts is None or now - last_ping_ts >= cooldown


def _cooldown_path(paths: comms_lib.Paths) -> Path:
    return paths.comms_dir / "inbox-cooldown.json"


def _read_cooldown(paths: comms_lib.Paths) -> dict[str, float]:
    try:
        data = json.loads(_cooldown_path(paths).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def _write_cooldown(paths: comms_lib.Paths, last_ping: dict[str, float], now: float) -> None:
    """Persist per-workspace last-ping times, dropping ones past the cooldown so
    the file never grows without bound."""
    keep = {k: v for k, v in last_ping.items() if now - v < INBOX_COOLDOWN_SEC}
    p = _cooldown_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(keep))
    os.replace(tmp, p)


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
    last_ping = _read_cooldown(paths)
    n = 0
    stale = 0
    held = 0
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
        ws_key = str(item.get("ws_ref") or "ws")
        if not inbox_should_ping(item, last_ping.get(ws_key), now):
            try:
                p.unlink()
            except OSError:
                pass
            held += 1
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
        last_ping[ws_key] = now
        log(f"inbox: pinged {item.get('signal_type') or item.get('signal')} "
            f"ws={item.get('ws_ref')} ({p.name})")
    if n:
        _write_cooldown(paths, last_ping, now)
    if stale:
        log(f"inbox: dropped {stale} stale signal(s) older than {int(INBOX_MAX_AGE_SEC)}s (no ping)")
    if held:
        log(f"inbox: held back {held} signal(s) from workspaces pinged in the last "
            f"{int(INBOX_COOLDOWN_SEC)}s (no ping)")
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

def heartbeat_action(unhealthy_checks: int, paged: bool,
                     confirm: int = HEARTBEAT_CONFIRM_CHECKS) -> str | None:
    """"page" once Assistant's main loop has looked stopped for `confirm`
    checks in a row, "recover" at the first healthy check after a page, else
    None — one message per outage instead of one every half hour
    (2026-09-14→27: 621 identical pages). The confirmation keeps a pulse that
    runs a minute late from paging and recovering in back-to-back posts."""
    if unhealthy_checks >= confirm and not paged:
        return "page"
    if paged and unhealthy_checks == 0:
        return "recover"
    return None


def heartbeat_loop(stop: threading.Event, env: dict) -> None:
    paths = comms_lib.Paths.from_env()
    paged_last_ts: int | None = None  # the stale heartbeat's last pulse when we paged
    unhealthy_checks = 0
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
        if last_ts > 0 and target:
            age = int(time.time()) - last_ts
            unhealthy = (age > stale_sec
                         or hb.get("status") in {"frozen", "stale_world", "respawn-requested"})
            unhealthy_checks = unhealthy_checks + 1 if unhealthy else 0
            action = heartbeat_action(unhealthy_checks, paged_last_ts is not None)
            if action == "page":
                body = comms_lib.fmt_heartbeat_alert(hb, age)
                rc, _, _err = cli(_send_args(body, "urgent", target, None), timeout=30, env=env)
                paged_last_ts = last_ts
                log(f"heartbeat-stale page age={age}s rc={rc}")
            elif action == "recover":
                body = comms_lib.fmt_heartbeat_recovered(hb, max(0, last_ts - paged_last_ts))
                rc, _, _err = cli(_send_args(body, "action", target, None), timeout=30, env=env)
                paged_last_ts = None
                log(f"heartbeat recovered rc={rc}")
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


def _announce_misconfig_once(paths: comms_lib.Paths, key: str, summary: str, env: dict) -> None:
    """Under KeepAlive+ThrottleInterval=10s an unrecoverable misconfig would
    crash-loop; without a guard, a startup Slack send would spam the operator
    every ~10s. Dedup on a marker file keyed by `key` — a STABLE identifier (the
    set of failing check NAMES), NOT the human `summary` (which may embed
    variable network-error text that would defeat the dedup as it flaps).

    Fail CLOSED: if we cannot persist the marker, we do NOT send — a send we
    can't record would re-fire every 10s, the exact spam we're preventing. So
    write the marker FIRST; only send if that succeeded."""
    marker = paths.comms_dir / "misconfig-announced.txt"
    try:
        if marker.exists() and marker.read_text().strip() == key.strip():
            return  # already announced this failure class
    except OSError:
        pass
    # Persist the marker BEFORE sending (fail-closed). If we can't, skip the send.
    try:
        paths.comms_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(key.strip())
    except OSError:
        log("could not write misconfig marker — skipping announce to avoid crash-loop spam")
        return
    # Only send if we have a token + an allowlisted target (the gate in
    # slack-send enforces the target too). Best-effort; never raises.
    try:
        cfg = comms_lib.Config.load(paths.config)
        if comms_lib.bot_token() and cfg.target and cfg.is_allowed(cfg.target):
            cli(_send_args(f"⚠️ assistant-comms cannot start: {summary}", "urgent",
                           cfg.target, None), timeout=20, env=env)
    except Exception:  # noqa: BLE001 — announcing must never mask the real exit
        pass


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

    # Preflight: refuse to crash-loop silently. Run the doctor's slack+warm-
    # session checks; on a hard FAIL, log the specific remedy AND announce it
    # once to Slack (deduped) before exiting, so a misconfigured box says WHY.
    env0 = dict(os.environ)
    for k, v in comms_lib.load_bedrock_env().items():
        env0.setdefault(k, v)
    try:
        assistant_doctor = _load_doctor()  # hyphenated filename → load by path
        dchecks = assistant_doctor.run_checks(only="slack")
        failed = [c for c in dchecks if c.status == assistant_doctor.FAIL]
        if failed:
            summary = "; ".join(f"{c.name}: {c.detail}" for c in failed)
            # Stable dedup key = the SET of failing check names (sorted), free of
            # variable detail text — so a flapping network error doesn't re-page.
            key = "|".join(sorted(c.name for c in failed))
            log(f"preflight FAILED — {summary}")
            for c in failed:
                if c.remedy:
                    log(f"  fix {c.name}: {c.remedy}")
            _announce_misconfig_once(paths, key, summary, env0)
            return 1
        # cleared: drop any stale announce marker so the next real failure pages.
        try:
            (paths.comms_dir / "misconfig-announced.txt").unlink()
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001 — doctor must never itself block startup
        log(f"preflight doctor error (continuing): {e}")

    lock = acquire_singleton(paths)
    if lock is None:
        log("another comms-listen already holds the lock — exiting")
        return 0

    env = env0  # built above for preflight (os.environ + bedrock vars)

    stop = threading.Event()

    def handle_sig(signum, frame):  # noqa: ARG001
        log(f"signal {signum} — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    threads = _loop_threads(stop, env)
    log(f"comms-listen starting (pid={os.getpid()}, transport=slack) — "
        f"{len(threads)} loops")
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1)
    log("comms-listen stopped")
    return 0


def _loop_threads(stop: threading.Event, env: dict) -> list[threading.Thread]:
    """The daemon's worker threads — one per concurrent loop. Extracted from
    main() so a test can assert the watchdog is wired in without running the
    daemon (main() does preflight + singleton-lock + signal setup first).

    Order is stable: inbound first (so a message on startup is handled ASAP),
    then the watchdog (so a dead warm session self-heals within the first
    interval), then the broadcast/heartbeat loops."""
    return [
        threading.Thread(target=inbound_loop, args=(stop, env), name="inbound", daemon=True),
        threading.Thread(target=watchdog_loop, args=(stop, env), name="watchdog", daemon=True),
        threading.Thread(target=ledger_loop, args=(stop, env), name="ledger", daemon=True),
        threading.Thread(target=inbox_loop, args=(stop, env), name="inbox", daemon=True),
        threading.Thread(target=proposals_loop, args=(stop, env), name="proposals", daemon=True),
        threading.Thread(target=heartbeat_loop, args=(stop, env), name="heartbeat", daemon=True),
    ]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
