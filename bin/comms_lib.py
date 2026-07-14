"""comms_lib — pure helpers shared by every assistant-comms CLI tool.

Slack transport. No daemon, no asyncio, no third-party SDK — just functions and
dataclasses over stdlib urllib. Imported by slack-send.py, slack-poll.py,
conversation.py, link-msg.py, lookup-thread.py, and the test suite.

This is a faithful port of the Telegram/Discord comms_lib removed in 000b91d,
re-cut for Slack as the sole transport. The on-disk interfaces (conversation.jsonl,
threads.jsonl, the ledger cursor) are unchanged in shape so the daemon and warm
session behave exactly as before.
"""
from __future__ import annotations

import json
import os
import re as _re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- paths

@dataclass(frozen=True)
class Paths:
    """All filesystem paths the comms tools touch. Tests inject a tmp-rooted Paths
    via the COMMS_HOME / COMMS_ASSISTANT_DIR / COMMS_BIN_DIR env vars.

    Config lives at ~/.assistant/config.json (relocated out of the deleted comms/
    dir in f82097f); runtime state (conversation, cursors, threads, session) lives
    under ~/.assistant/comms/, recreated on first use."""
    home: Path
    assistant_dir: Path
    ledger: Path
    heartbeat: Path
    observer_report: Path
    comms_dir: Path
    config: Path
    cursor: Path                 # ledger byte offset (Claude's place in actions-ledger.jsonl)
    slack_cursor: Path           # Slack message-ts offset (the newest inbound ts we've seen)
    proposals: Path              # proposals.jsonl (lesson/pattern proposals the extractor mines)
    proposals_cursor: Path       # highest proposal id delivered to Slack (exactly-once high-water mark)
    daemon_hb: Path              # comms session's own heartbeat (the listen daemon writes this)
    threads: Path                # threads.jsonl (sent_msg_ts <-> ledger_key)
    conversation: Path           # conversation.jsonl (durable chat memory, both directions)
    free_text_log: Path          # any inbound Slack text Claude couldn't classify
    curator: Path
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
            config=Path(env.get("COMMS_CONFIG", str(assistant_dir / "config.json"))),
            cursor=comms_dir / "ledger.cursor",
            slack_cursor=comms_dir / "slack.cursor",
            proposals=assistant_dir / "proposals.jsonl",
            proposals_cursor=comms_dir / "proposals.cursor",
            daemon_hb=comms_dir / "heartbeat.json",
            threads=comms_dir / "threads.jsonl",
            conversation=comms_dir / "conversation.jsonl",
            free_text_log=comms_dir / "free-text.log",
            curator=bin_dir / "assistant-curator.py",
            cmux_bin=Path(env.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")),
        )


# --------------------------------------------------------------------------- config

# The Slack bot token is NEVER stored in config.json (per the operator's security
# rule). It comes from $SLACK_BOT_TOKEN in ~/.zprofile — the same source the
# slack-reactor daemon already uses. config.json holds only the routing target
# and the send-gate allowlist.
def bot_token(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    return env.get("SLACK_BOT_TOKEN", "")


@dataclass
class Config:
    """The routing + gate slice of config.json.

    target:          the default send/reply target — a Slack user id (U…, DMed)
                     or channel id (C…/D…). $SLACK_PING_TARGET overrides it.
    allowed_targets: the SEND-GATE allowlist. slack-send.py refuses (no API call)
                     any target not in this set. Setup writes it as exactly the
                     one private channel the bot was invited to (or the
                     operator's DM), confining the bot to that single
                     destination — defense-in-depth, not a rule requirement (a
                     bot posting to its own invited channel is not "sending on
                     the operator's behalf")."""
    target: str = ""
    allowed_targets: tuple[str, ...] = ()
    stale_heartbeat_sec: int = 1200
    mute_until_epoch: int = 0
    _path: Path | None = None

    @classmethod
    def load(cls, path: Path, env: dict[str, str] | None = None) -> "Config":
        if not path.exists():
            raise SystemExit(f"missing config at {path}; run assistant-comms-setup.sh first")
        raw = json.loads(path.read_text())
        sl = raw.get("slack", {}) if isinstance(raw.get("slack"), dict) else {}
        env = env if env is not None else os.environ
        target = env.get("SLACK_PING_TARGET") or str(sl.get("target", "") or "")
        allowed = tuple(str(t) for t in sl.get("allowed_targets", []) or [])
        return cls(
            target=target,
            allowed_targets=allowed,
            stale_heartbeat_sec=int(raw.get("stale_heartbeat_sec", 1200)),
            mute_until_epoch=int(raw.get("mute_until_epoch", 0)),
            _path=path,
        )

    def is_allowed(self, target: str) -> bool:
        """The gate. A target is sendable iff it is explicitly allowlisted."""
        return target in self.allowed_targets


# --------------------------------------------------------------------------- transport-aware send

def send_notification(text: str, config_path: Path, bin_dir: Path,
                      kind: str = "reply",
                      runner: Any = None) -> bool:
    """Send a notification via Slack. Returns True iff the send succeeded; never
    raises. config_path: ~/.assistant/config.json. bin_dir: the bin/ directory
    (for slack-send.py). The target is config.slack.target ($SLACK_PING_TARGET
    override); slack-send.py itself applies the send-gate."""
    import sys as _sys  # noqa: PLC0415
    try:
        cfg = Config.load(config_path)
    except SystemExit:
        return False
    if not cfg.target:
        return False
    _run = runner or (lambda argv: subprocess.run(argv, capture_output=True, timeout=15))
    cmd = [
        _sys.executable, str(bin_dir / "slack-send.py"),
        "--channel", cfg.target,
        "--text", text,
        "--kind", kind,
    ]
    result = _run(cmd)
    rc = result.returncode if hasattr(result, "returncode") else result[0]
    return rc == 0


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


# --------------------------------------------------------------------------- formatting (Slack mrkdwn)

_HTML_TAG_RE = _re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """Remove HTML tags and unescape entities — for the rare source that emits HTML."""
    s = _HTML_TAG_RE.sub("", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def escape_mrkdwn(s: str) -> str:
    """Slack mrkdwn only requires escaping the three link/entity chars & < >.
    Bold/italic/code markers are left intact — we build them ourselves."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_action_line(entry: dict[str, Any]) -> str:
    """Render one ledger entry for Slack. screen_read evidence is flagged
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
        f"*[{escape_mrkdwn(str(kind))}]* {outcome_marker} `{escape_mrkdwn(str(key))}`\n"
        f"ws={escape_mrkdwn(str(ws))} td={escape_mrkdwn(str(td))} pulse={pulse} "
        f"via={escape_mrkdwn(via_marker)}\n"
        f"_{escape_mrkdwn(evidence)}_"
    )


def fmt_heartbeat_alert(hb: dict[str, Any], age_sec: int) -> str:
    ws = str(hb.get('ws_ref', '?'))
    status = str(hb.get('status', '?'))
    last = str(hb.get('last_pulse_iso', '?'))
    age = fmt_age(age_sec)
    return (
        f"*Assistant heartbeat stale*\n"
        f"ws={escape_mrkdwn(ws)} status={escape_mrkdwn(status)}\n"
        f"last pulse {age} ago ({escape_mrkdwn(last)})"
    )


def fmt_workspace_signal(item: dict[str, Any]) -> str:
    """Render a cmux-watcher inbox item (written by bin/cmux-watcher.py) for
    Slack. The watcher drops these the instant cmux reports a workspace needs
    input or finished a notable turn — so the ping arrives in seconds.

    Lead with the outcome (what the workspace needs / did), then the workspace
    ref, then the screen snippet — the work first, the infra label second."""
    signal_type = item.get("signal_type") or item.get("signal") or "?"
    ws_ref = item.get("ws_ref") or "?"
    pattern = item.get("pattern_matched") or item.get("signal") or "?"
    snippet = (item.get("screen_snippet") or "").strip()
    headline = {
        "needs_input": "needs your input",
        "work_complete": "work looks complete",
        "pattern_match": "hit a watched signal",
    }.get(signal_type, signal_type)
    body = (
        f"*{escape_mrkdwn(str(ws_ref))} {escape_mrkdwn(str(headline))}*\n"
        f"signal=`{escape_mrkdwn(str(pattern))}`"
    )
    if snippet:
        body += f"\n_{escape_mrkdwn(snippet[:400])}_"
    return body


def fmt_lesson_proposal(entry: dict[str, Any]) -> str:
    """Render a pending lesson proposal (from proposals.jsonl) for Slack.

    The extractor mined a recurring pattern and drafted a rule; this asks Mukul
    to confirm it. The message carries the proposal id so the warm session can
    confirm THIS proposal (not the newest on disk) when Mukul replies `y` — the
    warm prompt reads the id back out of the conversation window."""
    trigger = (entry.get("trigger") or "").strip()
    rule = (entry.get("rule") or "").strip()
    target = entry.get("target") or "assistant"
    count = entry.get("pattern_count")
    pid = str(entry.get("id") or entry.get("ts") or "?")
    seen = f" (seen {count}×)" if count else ""
    head = f"*Lesson proposal*{seen} — reply `y` to add, `n` to drop"
    body = (
        f"{head}\n"
        f"*when* {escape_mrkdwn(trigger)}\n"
        f"*rule* {escape_mrkdwn(rule)}\n"
        f"_target={escape_mrkdwn(str(target))} · id=`{escape_mrkdwn(pid)}`_"
    )
    return body


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
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
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


# --------------------------------------------------------------------------- proposals cursor (delivery high-water mark)
#
# proposals.jsonl is a DURABLE queue, not an ephemeral signal stream: the
# lesson-extractor appends pending proposals, the confirm path rewrites the file
# in place (flipping status → confirmed). A byte-offset cursor like the ledger's
# would be corrupted by that in-place rewrite, so we track the highest proposal
# id we've DELIVERED instead. Proposal ids are ISO-8601-µs timestamps
# ("2026-07-08T15:42:01.693265Z") — lexicographically AND chronologically
# ordered — so "id > cursor" is a clean, rewrite-safe high-water mark.

def read_proposals_cursor(paths: "Paths") -> str:
    """The highest proposal id delivered so far, or "" if none (deliver-all)."""
    if not paths.proposals_cursor.exists():
        return ""
    try:
        return paths.proposals_cursor.read_text().strip()
    except OSError:
        return ""


def write_proposals_cursor(paths: "Paths", proposal_id: str) -> None:
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.proposals_cursor.write_text(str(proposal_id))


def initialize_proposals_cursor_if_missing(paths: "Paths") -> None:
    """First run: skip the existing backlog so the daemon never blasts a stale
    queue of proposals at the operator's phone (there can be hundreds — the
    pipeline sat dead for weeks before delivery was wired). We set the cursor to
    the highest id currently on disk; only proposals written AFTER this point
    deliver. Recoverable: delete proposals.cursor to replay the whole queue.

    Subsequent runs: no-op (resume from the stored high-water mark)."""
    if paths.proposals_cursor.exists():
        return
    highest = ""
    if paths.proposals.exists():
        for entry in read_all_proposals(paths):
            pid = str(entry.get("id") or entry.get("ts") or "")
            if pid > highest:
                highest = pid
    write_proposals_cursor(paths, highest)


def read_all_proposals(paths: "Paths") -> list[dict[str, Any]]:
    """Every proposal on disk, in file order. Malformed/blank lines dropped."""
    if not paths.proposals.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(paths.proposals) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def read_new_proposals(paths: "Paths", limit: int | None = None) -> list[dict[str, Any]]:
    """Return pending LESSON proposals with id > the delivery cursor, oldest
    first, WITHOUT advancing the cursor (the caller advances it only after a
    successful send, so a send failure retries next pass).

    Only `type == "lesson"` && `status == "pending"` entries are returned:
    those are the ones the warm session can actually confirm (pattern /
    lesson_audit proposals have no confirm path yet, so pinging them would be a
    dead-end). `limit` caps a single drain so one big extractor batch can't
    firehose the channel; the remainder delivers on the next pass."""
    cursor = read_proposals_cursor(paths)
    fresh: list[dict[str, Any]] = []
    for entry in read_all_proposals(paths):
        pid = str(entry.get("id") or entry.get("ts") or "")
        if not pid or pid <= cursor:
            continue
        if entry.get("type") != "lesson" or entry.get("status") != "pending":
            continue
        fresh.append(entry)
    fresh.sort(key=lambda e: str(e.get("id") or e.get("ts") or ""))
    if limit is not None:
        return fresh[:limit]
    return fresh


# --------------------------------------------------------------------------- Slack cursor (message-ts offset)
#
# Slack message timestamps are strings of the form "1699999999.000200" (seconds
# with a 6-digit sub-second suffix), lexicographically AND numerically ordered.
# We store the newest ts we've delivered; the poller fetches everything after it.

def read_slack_cursor(paths: Paths) -> str:
    if not paths.slack_cursor.exists():
        return "0"
    return paths.slack_cursor.read_text().strip() or "0"


def write_slack_cursor(paths: Paths, ts: str) -> None:
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.slack_cursor.write_text(str(ts))


def _ts_float(ts: Any) -> float:
    """Slack ts as a float for ordering. Non-numeric (only possible from a
    corrupted cursor file) sorts as oldest rather than crashing."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- threads.jsonl

def append_thread(paths: Paths, ledger_key: str | None, msg_ts: str, channel: str,
                  kind: str, clock=None) -> None:
    """Record a sent-message → ledger-entry link so inbound replies can be resolved."""
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": now_iso(clock),
        "ledger_key": ledger_key,
        "msg_ts": str(msg_ts),
        "channel": channel,
        "kind": kind,
    }
    with open(paths.threads, "a") as f:
        f.write(json.dumps(rec) + "\n")


def lookup_thread_by_msg_ts(paths: Paths, msg_ts: str) -> dict[str, Any] | None:
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
            if str(rec.get("msg_ts")) == str(msg_ts):
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

def append_conversation_turn(paths: Paths, channel: str, msg_ts: str | None,
                             direction: str, text: str,
                             reply_to: str | None = None,
                             kind: str | None = None, clock=None) -> None:
    """Append one turn (inbound or outbound) to conversation.jsonl.

    direction: "in" (from the user) or "out" (from comms).
    channel:   the Slack channel id (D… DM or C… channel) this turn belongs to.
    msg_ts:    the Slack message ts, or None if not yet known.
    reply_to:  the ts this turn was a reply to, if any.
    kind:      optional tag (e.g. action/urgent/reply/info for outbound)."""
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    epoch = clock() if clock else int(time.time())
    rec = {
        "ts": now_iso(clock),
        "epoch": epoch,
        "channel": channel,
        "msg_ts": str(msg_ts) if msg_ts is not None else None,
        "reply_to": str(reply_to) if reply_to is not None else None,
        "direction": direction,
        "text": text,
        "kind": kind,
    }
    with open(paths.conversation, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_conversation_window(paths: Paths, channel: str, max_turns: int = 20,
                             max_age_sec: int = 7200, now=None) -> list[dict[str, Any]]:
    """Return the recent conversation for one channel, oldest-first, bounded by
    BOTH max_turns AND max_age_sec (whichever is tighter wins).

    Rebuilt every turn to give the Claude session continuity without trusting
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
            if str(rec.get("channel")) != str(channel):
                continue
            if int(rec.get("epoch") or 0) < floor:
                continue
            rows.append(rec)
    # Tightest of the two bounds: keep the last `max_turns` of the age-filtered set.
    return rows[-max_turns:]


# --------------------------------------------------------------------------- context measurement
#
# The warm cmux Claude session /clears itself at 50% context. The only reliable
# size signal is the per-turn `usage` block Claude Code records in the session
# transcript JSONL. Live context = the last assistant turn's
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
            "AWS_PROFILE", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_PING_TARGET")
    pat = _re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
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


# --------------------------------------------------------------------------- comms heartbeat (the listen daemon writes this)

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
