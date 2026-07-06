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


def fmt_action_line(entry: dict[str, Any]) -> str:
    """Render one actions-ledger entry for Slack. screen_read evidence is flagged
    because the Assistant itself rejects it — the flag travels with the message."""
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


def fmt_heartbeat_alert(hb: dict[str, Any], age_sec: int) -> str:
    return (
        f"*Assistant heartbeat stale*\n"
        f"ws={escape_mrkdwn(str(hb.get('ws_ref', '?')))} "
        f"status={escape_mrkdwn(str(hb.get('status', '?')))}\n"
        f"last pulse {fmt_age(age_sec)} ago "
        f"({escape_mrkdwn(str(hb.get('last_pulse_iso', '?')))})"
    )


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
