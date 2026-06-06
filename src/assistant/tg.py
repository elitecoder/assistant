"""tg — the daemon's Telegram client. THE only place HTTP calls to Telegram
happen inside the package.

Extracted from bin/tg-send.py + the formatting helpers in bin/comms_lib.py.
Self-contained on purpose: the daemon package must be importable as
`python -m assistant` without a sys.path hop into bin/, so the small HTTP-POST
and formatting logic is duplicated here rather than imported from comms_lib.

bin/tg-send.py is deliberately left untouched (the migration is additive — the
existing CLI keeps working for the scripts and skills that call it).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# A poster is (token, method, payload) -> result-dict. Injectable for tests so
# nothing here ever hits the network under unit test.
Poster = Callable[[str, str, dict], dict]


# ─── formatting (from comms_lib.py, verbatim behavior) ────────────────────────

def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_action_line(entry: dict[str, Any]) -> str:
    """Render one actions-ledger entry for chat. screen_read evidence is flagged
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
        f"<b>[{escape_html(kind)}]</b> {outcome_marker} <code>{escape_html(key)}</code>\n"
        f"ws={escape_html(str(ws))} td={escape_html(str(td))} pulse={pulse} "
        f"via={escape_html(via_marker)}\n"
        f"<i>{escape_html(evidence)}</i>"
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
        f"<b>Assistant heartbeat stale</b>\n"
        f"ws={escape_html(str(hb.get('ws_ref', '?')))} "
        f"status={escape_html(str(hb.get('status', '?')))}\n"
        f"last pulse {fmt_age(age_sec)} ago "
        f"({escape_html(str(hb.get('last_pulse_iso', '?')))})"
    )


# ─── HTTP (the only network egress) ───────────────────────────────────────────

def _real_post(token: str, method: str, payload: dict) -> dict:
    url = API_BASE.format(token=token, method=method)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"telegram HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"telegram URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")
    return data["result"]


def send(text: str, chat_id: int, *, token: str, kind: str = "reply",
         reply_to: int | None = None, parse_mode: str | None = "HTML",
         silent: bool = False, http: Poster | None = None) -> dict:
    """Send one message to one chat. Returns the parsed Telegram `result` dict
    on success. Raises RuntimeError on a Telegram API failure (the caller
    decides whether to swallow it).

    `http` overrides the network poster — tests pass a fake so nothing leaves
    the box."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    if silent:
        payload["disable_notification"] = True
    poster = http or _real_post
    return poster(token, "sendMessage", payload)
