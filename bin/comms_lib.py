"""comms_lib — pure helpers shared by every assistant-comms CLI tool.

No daemon, no asyncio. Just functions and dataclasses. Imported by
tg-send.py, tg-poll.py, link-msg.py, lookup-thread.py, and the test suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- paths

@dataclass(frozen=True)
class Paths:
    """All filesystem paths the comms tools touch. Tests inject a tmp-rooted Paths
    via the COMMS_HOME / COMMS_ASSISTANT_DIR / COMMS_BIN_DIR env vars."""
    home: Path
    assistant_dir: Path
    ledger: Path
    heartbeat: Path
    observer_report: Path
    comms_dir: Path
    config: Path
    cursor: Path                 # ledger byte offset (Claude's place in actions-ledger.jsonl)
    tg_cursor: Path              # Telegram update_id offset
    daemon_hb: Path              # comms session's own heartbeat (Claude writes this)
    threads: Path                # threads.jsonl (sent_msg_id <-> ledger_key)
    conversation: Path           # conversation.jsonl (durable chat memory, both directions)
    free_text_log: Path          # any inbound TG text Claude couldn't classify
    terminal_tab: Path           # records osascript tab id of the comms Terminal window
    curator: Path
    heartbeat_write: Path
    spawn_assistant: Path
    cmux_bin: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Paths":
        env = env if env is not None else dict(os.environ)
        home = Path(env.get("COMMS_HOME", env["HOME"]))
        assistant_dir = Path(env.get("COMMS_ASSISTANT_DIR", str(home / ".assistant")))
        bin_dir = Path(env.get("COMMS_BIN_DIR", str(home / "dev/assistant/bin")))
        comms_dir = assistant_dir / "comms"
        return cls(
            home=home,
            assistant_dir=assistant_dir,
            ledger=assistant_dir / "actions-ledger.jsonl",
            heartbeat=assistant_dir / "heartbeat.json",
            observer_report=assistant_dir / "observer-latest-report.json",
            comms_dir=comms_dir,
            config=comms_dir / "config.json",
            cursor=comms_dir / "ledger.cursor",
            tg_cursor=comms_dir / "tg.cursor",
            daemon_hb=comms_dir / "heartbeat.json",
            threads=comms_dir / "threads.jsonl",
            conversation=comms_dir / "conversation.jsonl",
            free_text_log=comms_dir / "free-text.log",
            terminal_tab=comms_dir / "terminal-tab.txt",
            curator=bin_dir / "assistant-curator.py",
            heartbeat_write=bin_dir / "heartbeat-write.py",
            spawn_assistant=bin_dir / "spawn-assistant.sh",
            cmux_bin=Path(env.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")),
        )


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    bot_token: str
    chat_ids: set[int]
    stale_heartbeat_sec: int = 1200
    mute_until_epoch: int = 0
    _path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(f"missing config at {path}; run assistant-comms-setup.sh first")
        raw = json.loads(path.read_text())
        tg = raw.get("telegram", {})
        return cls(
            bot_token=tg["bot_token"],
            chat_ids={int(x) for x in tg.get("chat_ids", [])},
            stale_heartbeat_sec=int(raw.get("stale_heartbeat_sec", 1200)),
            mute_until_epoch=int(raw.get("mute_until_epoch", 0)),
            _path=path,
        )

    def save(self) -> None:
        if self._path is None:
            raise RuntimeError("Config.save called without a path (use Config.load)")
        raw = {
            "telegram": {"bot_token": self.bot_token, "chat_ids": sorted(self.chat_ids)},
            "stale_heartbeat_sec": self.stale_heartbeat_sec,
            "mute_until_epoch": self.mute_until_epoch,
        }
        self._path.write_text(json.dumps(raw, indent=2))
        os.chmod(self._path, 0o600)


# --------------------------------------------------------------------------- time

def now_iso(clock=None) -> str:
    if clock is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_age(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"


def parse_duration(s: str) -> int | None:
    s = s.strip().lower()
    if not s or len(s) < 2:
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    if n < 0:
        return None
    unit = s[-1]
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return None


# --------------------------------------------------------------------------- formatting

def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_action_line(entry: dict[str, Any]) -> str:
    """Render one ledger entry for chat. screen_read evidence is flagged
    because Assistant itself rejects it — the flag travels with the message."""
    kind = entry.get("kind", "?")
    key = entry.get("key", "?")
    ws = entry.get("ws_ref") or "-"
    td = entry.get("td") or "-"
    outcome = entry.get("outcome", "?")
    via = entry.get("verified_via") or "?"
    pulse = entry.get("pulse_idx", "?")
    evidence = (entry.get("evidence") or "")[:200]
    via_marker = "(!)screen_read" if via == "screen_read" else via
    outcome_marker = {
        "verified": "ok", "failed": "fail", "skipped": "skip", "rejected": "rej",
    }.get(outcome, outcome)
    return (
        f"<b>[{escape_html(kind)}]</b> {outcome_marker} <code>{escape_html(key)}</code>\n"
        f"ws={escape_html(str(ws))} td={escape_html(str(td))} pulse={pulse} via={escape_html(via_marker)}\n"
        f"<i>{escape_html(evidence)}</i>"
    )


def fmt_heartbeat_alert(hb: dict[str, Any], age_sec: int) -> str:
    return (
        f"<b>Assistant heartbeat stale</b>\n"
        f"ws={escape_html(str(hb.get('ws_ref', '?')))} "
        f"status={escape_html(str(hb.get('status', '?')))}\n"
        f"last pulse {fmt_age(age_sec)} ago "
        f"({escape_html(str(hb.get('last_pulse_iso', '?')))})"
    )


# --------------------------------------------------------------------------- cursor (ledger byte offset)

def read_ledger_cursor(paths: Paths) -> int:
    if not paths.cursor.exists():
        return 0
    try:
        return int(paths.cursor.read_text().strip() or "0")
    except ValueError:
        return 0


def write_ledger_cursor(paths: Paths, offset: int) -> None:
    paths.cursor.write_text(str(offset))


def initialize_cursor_if_missing(paths: Paths) -> None:
    """First run: skip the backlog. Subsequent runs: resume."""
    if paths.cursor.exists():
        return
    if paths.ledger.exists():
        write_ledger_cursor(paths, paths.ledger.stat().st_size)
    else:
        write_ledger_cursor(paths, 0)


def read_new_ledger_lines(paths: Paths) -> list[dict[str, Any]]:
    """Read every ledger line written since the last cursor and advance the cursor.
    Returns parsed entries (malformed lines are dropped). Handles ledger rotation
    by detecting size < cursor."""
    if not paths.ledger.exists():
        return []
    cur = read_ledger_cursor(paths)
    size = paths.ledger.stat().st_size
    if size < cur:
        cur = 0
        write_ledger_cursor(paths, 0)
    if size == cur:
        return []
    with open(paths.ledger, "rb") as f:
        f.seek(cur)
        chunk = f.read(size - cur)
    write_ledger_cursor(paths, size)
    out: list[dict[str, Any]] = []
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------- TG cursor (update_id offset)

def read_tg_cursor(paths: Paths) -> int:
    if not paths.tg_cursor.exists():
        return 0
    try:
        return int(paths.tg_cursor.read_text().strip() or "0")
    except ValueError:
        return 0


def write_tg_cursor(paths: Paths, offset: int) -> None:
    paths.tg_cursor.write_text(str(offset))


# --------------------------------------------------------------------------- threads.jsonl

def append_thread(paths: Paths, ledger_key: str | None, tg_msg_id: int, chat_id: int,
                  kind: str, clock=None) -> None:
    """Record a sent-message → ledger-entry link so inbound replies can be resolved."""
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": now_iso(clock),
        "ledger_key": ledger_key,
        "tg_msg_id": tg_msg_id,
        "chat_id": chat_id,
        "kind": kind,
    }
    with open(paths.threads, "a") as f:
        f.write(json.dumps(rec) + "\n")


def lookup_thread_by_msg_id(paths: Paths, tg_msg_id: int) -> dict[str, Any] | None:
    if not paths.threads.exists():
        return None
    last: dict[str, Any] | None = None
    with open(paths.threads) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("tg_msg_id") == tg_msg_id:
                last = rec  # keep last in case of dupes
    return last


def lookup_thread_by_ledger_key(paths: Paths, ledger_key: str) -> list[dict[str, Any]]:
    if not paths.threads.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(paths.threads) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ledger_key") == ledger_key:
                out.append(rec)
    return out


# --------------------------------------------------------------------------- conversation.jsonl
#
# Durable chat memory. The comms Claude session treats its context window as
# disposable scratch — every turn it reconstructs the recent thread from this
# file. So a crash, /clear, or auto-compact loses nothing: the conversation
# picks up exactly where it left off. One JSONL row per turn, both directions.

def append_conversation_turn(paths: Paths, chat_id: int, msg_id: int | None,
                             direction: str, text: str,
                             reply_to: int | None = None,
                             kind: str | None = None, clock=None) -> None:
    """Append one turn (inbound or outbound) to conversation.jsonl.

    direction: "in" (from the user) or "out" (from comms).
    msg_id:    the Telegram message_id, or None if not yet known.
    reply_to:  the message_id this turn was a reply to, if any.
    kind:      optional tag (e.g. action/urgent/reply/info for outbound).
    """
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    epoch = clock() if clock else int(time.time())
    rec = {
        "ts": now_iso(clock),
        "epoch": epoch,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "reply_to": reply_to,
        "direction": direction,
        "text": text,
        "kind": kind,
    }
    with open(paths.conversation, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_conversation_window(paths: Paths, chat_id: int, max_turns: int = 20,
                             max_age_sec: int = 7200, now=None) -> list[dict[str, Any]]:
    """Return the recent conversation for one chat, oldest-first, bounded by
    BOTH max_turns AND max_age_sec (whichever is tighter wins).

    Rebuilt every pulse to give the Claude session continuity without trusting
    its context window. Malformed / blank lines are skipped."""
    if not paths.conversation.exists():
        return []
    now_epoch = now() if now else int(time.time())
    floor = now_epoch - max_age_sec
    rows: list[dict[str, Any]] = []
    with open(paths.conversation) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("chat_id") != chat_id:
                continue
            if int(rec.get("epoch") or 0) < floor:
                continue
            rows.append(rec)
    # Tightest of the two bounds: keep the last `max_turns` of the age-filtered set.
    return rows[-max_turns:]


# --------------------------------------------------------------------------- context measurement
#
# Phase 2 keeps a warm cmux Claude session and /clears it at 50% context. The
# only reliable size signal is the per-turn `usage` block Claude Code records
# in the session transcript JSONL. The live context = the last assistant turn's
#   input_tokens + cache_creation_input_tokens + cache_read_input_tokens
# (cache_read is the bulk — the prompt-cached conversation so far).

CONTEXT_WINDOW_TOKENS = 1_000_000


def read_context_tokens(transcript_path: str | Path) -> int | None:
    """Return the live context size in tokens from the last assistant turn's
    usage block, or None if the transcript has no usage data yet."""
    p = Path(transcript_path)
    if not p.exists():
        return None
    last: dict[str, Any] | None = None
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                last = usage
    if last is None:
        return None
    return (int(last.get("input_tokens") or 0)
            + int(last.get("cache_creation_input_tokens") or 0)
            + int(last.get("cache_read_input_tokens") or 0))


def context_fraction(tokens: int | None, window: int = CONTEXT_WINDOW_TOKENS) -> float:
    """Fraction of the context window in use (0.0–…). None tokens → 0.0."""
    if not tokens or window <= 0:
        return 0.0
    return tokens / window


# --------------------------------------------------------------------------- bedrock env

def load_bedrock_env(home: Path | None = None) -> dict[str, str]:
    """Parse the Bedrock auth vars out of ~/.zprofile. launchd does not source
    it, so a headless `claude --print` spawned from a LaunchAgent would 403
    against AWS STS without these. Same approach as bin/pulse.py."""
    home = home or Path(os.environ["HOME"])
    zprofile = home / ".zprofile"
    if not zprofile.exists():
        return {}
    keys = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE", "ANTHROPIC_API_KEY")
    import re
    pat = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
    out: dict[str, str] = {}
    for line in zprofile.read_text().splitlines():
        m = pat.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k not in keys:
            continue
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


# --------------------------------------------------------------------------- subprocess

def run_cmd(argv: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, return (rc, stdout, stderr). Never raises on non-zero."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)


def cmux_read_screen(paths: Paths, workspace_ref: str, lines: int = 50) -> str:
    rc, out, err = run_cmd(
        [str(paths.cmux_bin), "read-screen", "--workspace", workspace_ref], timeout=15)
    if rc != 0:
        return f"(cmux read-screen failed rc={rc}: {err.strip()})"
    tail = "\n".join(out.splitlines()[-lines:])
    return tail or "(empty)"


# --------------------------------------------------------------------------- comms heartbeat (the Claude session writes this every pulse)

def write_comms_heartbeat(paths: Paths, status: str = "active",
                          pulse_idx: int = 0, note: str = "", clock=None) -> None:
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    epoch = clock() if clock else int(time.time())
    payload: dict[str, Any] = {
        "ts": now_iso(clock),
        "epoch": epoch,
        "pid": os.getpid(),
        "status": status,
        "pulse_idx": pulse_idx,
    }
    if note:
        payload["note"] = note
    tmp = paths.daemon_hb.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, paths.daemon_hb)


def read_comms_heartbeat(paths: Paths) -> dict[str, Any] | None:
    if not paths.daemon_hb.exists():
        return None
    try:
        return json.loads(paths.daemon_hb.read_text())
    except json.JSONDecodeError:
        return None
