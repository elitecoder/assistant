"""slack — the daemon's Slack client. THE only place HTTP calls to Slack happen
inside the package.

Extracted from bin/slack-send.py + the formatting helpers in bin/comms_lib.py.
Self-contained on purpose: the daemon package must be importable as
`python -m assistant` without a sys.path hop into bin/, so the small HTTP-POST
and formatting logic is duplicated here rather than imported from comms_lib.

bin/slack-send.py is deliberately left untouched (the migration is additive —
the CLI keeps working for the scripts and skills that call it).

The send-gate is enforced HERE too: send() refuses any channel not in the
`allowed` set with a RuntimeError before any network egress, mirroring
slack-send.py. Both the CLI path and the in-process daemon path are gated so the
bot stays confined to its one comms channel (the private channel it was invited
to, or the operator's DM).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

API_BASE = "https://slack.com/api"

# A poster is (token, method, payload) -> response-dict. Injectable for tests so
# nothing here ever hits the network under unit test.
Poster = Callable[[str, str, dict], dict]


# ─── formatting (mrkdwn; verbatim behavior with comms_lib) ────────────────────

def escape_mrkdwn(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ref_footer(*refs: Any) -> str:
    """The trailing italic line: IDs and labels you only need to look something
    up, kept after the message itself. Empty and placeholder values are left out."""
    parts = [escape_mrkdwn(str(r)) for r in refs if r not in (None, "", "-", "?")]
    return f"_{' · '.join(parts)}_" if parts else ""


def _quote(text: str) -> str:
    """Slack blockquote, so the recorded evidence reads apart from the sentence.
    Blank lines are skipped, so empty text quotes to "", which the line join
    drops."""
    return "\n".join(f"> {ln}" for ln in text.splitlines() if ln.strip())


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# kind → (what Assistant did, as a past-tense phrase; what it set out to do).
# Covers the kinds that still reach Slack after the broadcast suppression
# (routine, receipt, and housekeeping kinds never post); anything else reads
# generically.
_ACTION_PHRASES: dict[str, tuple[str, str]] = {
    "ready_for_merge": ("asked a workspace to merge its PR", "ask a workspace to merge its PR"),
    "self-update": ("updated Assistant to the latest code", "update Assistant to the latest code"),
    "self-update-syntax-fail": ("updated Assistant to the latest code",
                                "update Assistant to the latest code"),
    "strategist-context": ("started researching a decision that's waiting on you",
                           "research a decision that's waiting on you"),
    "strategist-context-wrote": ("added background to your brief for a decision that's waiting on you",
                                 "add background to your brief for a decision that's waiting on you"),
    "strategist-autounpause": ("turned suggestion drafting back on",
                               "turn suggestion drafting back on"),
    "goal-edit": ("updated your goals", "update your goals"),
    "policy-bootstrap-upgrade": ("added new built-in rules for handling events",
                                 "add new built-in rules for handling events"),
}
_GENERIC_ACTION = ("took an automatic step", "take an automatic step")


def _action_sentence(kind: str, outcome: str) -> str:
    """One plain sentence: what Assistant did and whether it worked."""
    did, attempt = _ACTION_PHRASES.get(kind, _GENERIC_ACTION)
    if outcome == "verified":
        return f"I {did}."
    if outcome == "failed":
        return f"I tried to {attempt}, but it didn't work."
    if outcome == "rejected":
        return f"I tried to {attempt}, but it was turned down."
    if outcome == "skipped":
        return f"I didn't {attempt} this time."
    return f"I tried to {attempt} (result: {escape_mrkdwn(outcome)})."


def fmt_action_line(entry: dict[str, Any]) -> str:
    """Render one actions-ledger entry for Slack: a plain sentence about what
    Assistant did, then the refs. When something went wrong the recorded
    evidence is the news, so it's quoted under the sentence; otherwise it's
    machine detail and leads the footer. screen_read evidence is flagged because
    the Assistant itself rejects it as proof — the flag travels with the
    message."""
    kind = str(entry.get("kind") or "?")
    outcome = str(entry.get("outcome") or "?")
    evidence = _clip(entry.get("evidence") or "", 200)
    went_wrong = outcome in ("failed", "rejected")
    lines = [_action_sentence(kind, outcome)]
    if entry.get("verified_via") == "screen_read":
        lines.append("Heads up: I only confirmed this by reading the screen, "
                     "which isn't reliable proof.")
    if went_wrong:
        lines.append(_quote(escape_mrkdwn(evidence)))
    pulse = entry.get("pulse_idx")
    lines.append(_ref_footer(
        None if went_wrong else " ".join(evidence.split()),
        entry.get("ws_ref"), kind, entry.get("key"), entry.get("td"),
        f"pulse {pulse}" if pulse is not None else None))
    return "\n".join(ln for ln in lines if ln)


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


# Heartbeat statuses the pager alerts on even when the heartbeat is fresh.
_HEARTBEAT_STATUS_NOTES = {
    "frozen": "it reports that it's frozen",
    "stale_world": "it's working from an out-of-date view of your workspaces",
    "respawn-requested": "it asked to be restarted",
}


def fmt_heartbeat_alert(hb: dict[str, Any], age_sec: int) -> str:
    """Page when the pulse loop stops or reports a bad status. A bad status can
    arrive while runs are still recent, so that case names the status instead
    of claiming the loop stopped."""
    last = escape_mrkdwn(str(hb.get("last_pulse_iso") or "unknown"))
    age = fmt_age(age_sec)
    note = _HEARTBEAT_STATUS_NOTES.get(str(hb.get("status")))
    if note:
        return (f"*Assistant's main loop needs a look* — {note}. Last run {age} ago "
                f"({last}). I'll post again when it's back.")
    return (f"*Assistant's main loop has stopped* — no run for {age} (last run {last}). "
            f"I'll post again when it's back.")


def fmt_heartbeat_recovered(hb: dict[str, Any], down_sec: int) -> str:
    """The all-clear that follows a heartbeat page, so a page never dangles."""
    latest = hb.get("last_pulse_iso")
    since = f" (latest run {escape_mrkdwn(str(latest))})" if latest else ""
    return f"*Assistant's main loop is running again* after {fmt_age(down_sec)}{since}."


# ─── HTTP (the only network egress) ───────────────────────────────────────────

def _real_post(token: str, method: str, payload: dict) -> dict:
    url = f"{API_BASE}/{method}"
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Authorization": f"Bearer {token}",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"slack HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"slack URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"slack error: {data.get('error', data)}")
    return data


def resolve_channel(target: str, *, token: str, http: Poster | None = None) -> str:
    """A U… user id → its DM channel via conversations.open; a channel id passes
    through unchanged."""
    if target.startswith("U"):
        data = (http or _real_post)(token, "conversations.open", {"users": target})
        return data["channel"]["id"]
    return target


def send(text: str, target: str, *, token: str, allowed: Iterable[str],
         kind: str = "reply", reply_to: str | None = None,
         http: Poster | None = None) -> dict:
    """Send one message to `target` (U… user DMed, or C…/D… channel). Returns the
    parsed Slack response ({ok, channel, ts, …}) on success. Raises RuntimeError
    on a gate rejection or Slack API failure (the caller decides whether to
    swallow it).

    THE SEND-GATE: `target` must be in `allowed` or this raises before any
    network call — the same enforcement as bin/slack-send.py.

    `http` overrides the network poster — tests pass a fake so nothing leaves
    the box."""
    if target not in set(allowed):
        raise RuntimeError(f"send-gate: {target!r} not in allowed_targets")
    channel = resolve_channel(target, token=token, http=http)
    payload: dict = {
        "channel": channel,
        "text": text,
        "mrkdwn": "true",
        "unfurl_links": "false",
        "unfurl_media": "false",
    }
    if reply_to is not None:
        payload["thread_ts"] = str(reply_to)
    return (http or _real_post)(token, "chat.postMessage", payload)
