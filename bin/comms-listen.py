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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

HOME = Path(os.environ["HOME"])
REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
BOOT_PROMPT = REPO / "prompts" / "prompt-assistant-comms-agent.md"

TG_POLL = BIN / "tg-poll.py"
TG_SEND = BIN / "tg-send.py"
CONVERSATION = BIN / "conversation.py"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))
REPLY_MODEL = os.environ.get("COMMS_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
REPLY_TIMEOUT_SEC = int(os.environ.get("COMMS_REPLY_TIMEOUT_SEC", "180"))

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

def reply_to_message(rec: dict, env: dict) -> None:
    """Phase 1 reply engine: cold-spawn claude --print to answer one message.
    Records the inbound turn first (so even if claude dies, the message is in
    the conversation log), then the reply."""
    chat_id = rec["chat_id"]
    text = rec.get("text", "")
    msg_id = rec.get("msg_id")
    reply_to = rec.get("reply_to_msg_id")

    # Record inbound turn.
    in_args = [str(CONVERSATION), "append", "--chat", str(chat_id),
               "--direction", "in", "--text", text]
    if msg_id is not None:
        in_args += ["--msg-id", str(msg_id)]
    if reply_to is not None:
        in_args += ["--reply-to", str(reply_to)]
    cli(in_args, timeout=10)

    prompt = (
        f"You are assistant-comms answering ONE inbound Telegram message, "
        f"conversationally. Read {BOOT_PROMPT} for your role and tools. The "
        f"message (chat_id={chat_id}, msg_id={msg_id}, reply_to={reply_to}):\n\n"
        f"{text}\n\n"
        f"Reconstruct context with `{CONVERSATION} window --chat {chat_id}`. "
        f"If reply_to is set, resolve it with `{BIN}/lookup-thread.py --tg-msg "
        f"{reply_to} --include-ledger`. Then send your reply with "
        f"`{TG_SEND} --text \"...\" --chat {chat_id} --kind reply "
        f"--reply-to {msg_id}`, and record it with `{CONVERSATION} append "
        f"--chat {chat_id} --direction out --text \"...\" --kind reply "
        f"--reply-to {msg_id} --msg-id <the message_id tg-send printed>`. "
        f"Do exactly that, then stop."
    )
    cmd = [
        CLAUDE_BIN, "--model", REPLY_MODEL, "--dangerously-skip-permissions",
        "--print", "--add-dir", str(REPO), "--add-dir", str(HOME / ".assistant"),
        "--add-dir", str(HOME / ".claude"), "--add-dir", "/tmp",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             timeout=REPLY_TIMEOUT_SEC, env=env)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except Exception:  # noqa: BLE001
        rc = 1
    log(f"reply chat={chat_id} msg={msg_id} rc={rc} wall_ms={int((time.time()-t0)*1000)}")


def inbound_loop(stop: threading.Event, env: dict) -> None:
    log("inbound loop started (long-poll)")
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
            reply_to_message(rec, env)


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

def main() -> int:
    paths = comms_lib.Paths.from_env()
    if not paths.config.exists():
        log(f"no config at {paths.config} — run assistant-comms-setup.sh first")
        return 1
    if not BOOT_PROMPT.exists():
        log(f"missing boot prompt at {BOOT_PROMPT}")
        return 1

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
        threading.Thread(target=heartbeat_loop, args=(stop, env), name="heartbeat", daemon=True),
    ]
    log(f"comms-listen starting (pid={os.getpid()}) — 3 loops")
    for t in threads:
        t.start()
    # Block until a signal sets stop; daemon threads exit with the process.
    while not stop.is_set():
        stop.wait(1)
    log("comms-listen stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
