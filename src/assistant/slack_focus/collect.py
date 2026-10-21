"""Collect: pull the threads that may need you, and every message you sent.

Purely mechanical. Every thread lands in one bucket by rules, no model.
"""
from __future__ import annotations

import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import Client, iso
from .settings import Settings

SKIP_SUBTYPES = ("channel_join", "channel_leave")
Progress = Callable[[int, int], None]


def bucket(t: dict, settings: Settings) -> str:
    if t["last_author"] in settings.bot_authors or any(s in t["channel"] for s in settings.skip_channels):
        return "bot"
    if t["last_is_me"]:
        return "waiting_on_others"
    if t["i_posted_after_in_channel"] or t["i_reacted_to_last"]:
        return "replied_elsewhere"
    if t["kind"] in ("dm", "mpim"):
        return "ball_mine_dm"
    if t["mentioned_after_my_last_reply"] == "direct":
        return "ball_mine_mention"
    if t["i_participated"] and t["from_others_after_my_last"] > 0:
        return "thread_moved_on"
    if t["mentioned_after_my_last_reply"] == "group":
        return "group_mention"
    if t["unread"]:
        return "unread_watched" if t["watched"] else "unread_other"
    return "read_fyi"


def _messages(client: Client, method: str, params: dict) -> list[dict]:
    """Every page of a history or replies call, oldest first, each message once."""
    seen: dict[str, dict] = {}
    cursor = ""
    while True:
        r = client.call(method, params | ({"cursor": cursor} if cursor else {}))
        for m in r.get("messages", []):
            seen.setdefault(m["ts"], m)
        cursor = r.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return sorted(seen.values(), key=lambda m: float(m["ts"]))


def _shape(client: Client, m: dict) -> dict:
    author = client.user_name(m["user"]) if m.get("user") else m.get("username", "bot")
    return {"author": author, "at": iso(m["ts"])[:16], "text": client.clean(m.get("text", ""))[:400]}


def _summarize(client: Client, settings: Settings, cid: str, msgs: list[dict], thread_ts: str,
               parent_last_read: float | None = None) -> dict:
    ch = client.channel(cid)
    mine = [float(m["ts"]) for m in msgs if m.get("user") == client.me]
    my_last = max(mine) if mine else 0.0
    last = msgs[-1]
    after_me = [m for m in msgs if float(m["ts"]) > my_last and m.get("user") != client.me]
    kinds = {client.mentions_me(m.get("text", "")) for m in after_me}
    mention_after = "direct" if "direct" in kinds else "group" if "group" in kinds else None
    last_read = parent_last_read if parent_last_read is not None else ch["last_read"]
    return {
        "cid": cid, "root": msgs[0]["ts"], "channel": ch["name"], "kind": ch["kind"],
        "watched": cid in settings.watched_channels,
        "permalink": client.permalink(cid, last["ts"], thread_ts),
        "started": iso(msgs[0]["ts"]), "last_activity": iso(last["ts"]),
        "last_author": _shape(client, last)["author"], "last_is_me": last.get("user") == client.me,
        "i_participated": bool(mine), "messages": len(msgs), "from_others_after_my_last": len(after_me),
        "mentioned_after_my_last_reply": mention_after,
        "i_reacted_to_last": any(client.me in r.get("users", []) for r in last.get("reactions", [])),
        "unread": float(last["ts"]) > last_read and last.get("user") != client.me,
        "first": _shape(client, msgs[0]),
        "latest": [_shape(client, m) for m in msgs[-4:]] if len(msgs) > 1 else [],
    }


def collect(client: Client, settings: Settings, now: float | None = None,
            progress: Progress | None = None) -> tuple[dict, list[dict]]:
    """Return (threads document, your messages) for the last `settings.days`."""
    now = time.time() if now is None else now
    since = now - settings.days * 86400
    after = (datetime.fromtimestamp(now) - timedelta(days=settings.days + 1)).strftime("%Y-%m-%d")
    candidates: dict[tuple[str, str], None] = {}
    dm_channels: set[str] = set()

    for query, kind in ((f"<@{client.me}> after:{after}", "mention"), (f"is:dm after:{after}", "dm"),
                        (f"to:me after:{after}", "dm")):
        for m in client.search(query, max_pages=59):
            if float(m["ts"]) < since:
                continue
            link_query = urllib.parse.urlparse(m.get("permalink", "")).query
            thread_ts = urllib.parse.parse_qs(link_query).get("thread_ts", [m["ts"]])[0]
            if kind == "dm" and thread_ts == m["ts"]:
                dm_channels.add(m["channel"]["id"])
            else:
                candidates.setdefault((m["channel"]["id"], thread_ts), None)

    for cid in settings.watched_channels:
        for m in _messages(client, "conversations.history", {"channel": cid, "oldest": since - 14 * 86400, "limit": 200}):
            if float(m.get("latest_reply") or m["ts"]) >= since \
                    and m.get("subtype") not in ("channel_join", "bot_message"):
                candidates.setdefault((cid, m.get("thread_ts") or m["ts"]), None)

    threads = []
    # A DM surfaced any way gets its full in-window history, so a DM ask isn't
    # cut down to the one message search matched. That history covers threads
    # that start in the window; an older thread with a new reply keeps its own
    # fetch.
    dm_hits = set(dm_channels)
    for cid, thread_ts in list(candidates):
        if client.channel(cid)["kind"] in ("dm", "mpim"):
            dm_hits.add(cid)
            if float(thread_ts) >= since:
                del candidates[(cid, thread_ts)]
    for cid in sorted(dm_hits):
        h = _messages(client, "conversations.history", {"channel": cid, "oldest": since, "limit": 200})
        top = [m for m in h if not m.get("subtype")]
        if not top:
            continue
        for m in top:
            if m.get("reply_count"):
                candidates.setdefault((cid, m["ts"]), None)
        threads.append(_summarize(client, settings, cid, top, top[-1]["ts"]))

    total = len(candidates)
    for i, (cid, thread_ts) in enumerate(candidates):
        if progress and i % 10 == 0:
            progress(i + 1, total)
        msgs = [m for m in _messages(client, "conversations.replies", {"channel": cid, "ts": thread_ts, "limit": 200})
                if m.get("subtype") not in SKIP_SUBTYPES]
        if not msgs or float(msgs[-1]["ts"]) < since:
            continue
        parent = msgs[0]
        parent_read = float(parent.get("last_read") or 0) if parent.get("subscribed") else None
        threads.append(_summarize(client, settings, cid, msgs, thread_ts, parent_read))

    mine = [{"cid": m["channel"]["id"], "ts": m["ts"], "cname": m["channel"].get("name"),
             "text": client.clean(m.get("text", "")), "permalink": m.get("permalink", ""),
             "dm": bool(m["channel"].get("is_im") or m["channel"].get("is_mpim"))}
            for m in client.search(f"from:me after:{after}")]
    mine_ts = [(x["cid"], float(x["ts"]), x["permalink"], x["dm"]) for x in mine]
    for t in threads:
        last = datetime.fromisoformat(t["last_activity"]).timestamp()
        t["i_posted_after_in_channel"] = any(
            c == t["cid"] and ts > last and (dm or f"thread_ts={t['root']}" in link)
            for c, ts, link, dm in mine_ts)

    threads.sort(key=lambda t: t["last_activity"], reverse=True)
    for i, t in enumerate(threads):
        t["id"], t["bucket"] = i + 1, bucket(t, settings)
    print(f"collected {len(threads)} threads, {len(mine)} of your messages", file=sys.stderr)
    doc = {"generated": datetime.fromtimestamp(now, timezone.utc).isoformat(), "days": settings.days,
           "me": client.me, "me_name": client.me_name, "threads": threads}
    return doc, mine
