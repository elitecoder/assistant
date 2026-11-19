"""Gmail inboxes, read through the `gog` CLI with each account's own sign-in.

A refresh reads every inbox conversation plus unread mail outside the inbox,
and reuses a conversation from the last snapshot when its messages didn't
change. Rules sort each conversation by Gmail's own labels and headers:

- act       a person wrote to you in Primary, and it's unread or starred
- unsorted  other Primary mail from a person; it stays with you
- self      a note you emailed yourself
- news      a mailing list (it has a List-Unsubscribe header)
- shop      an order, shipping, or receipt update
- note      an automated notice, a promotion, or a social notification
- unread    news, shop, note, or self mail that you never opened

Only news, shop, note, self, and unread can be approved for Trash.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from work_queue import config, tools

DELETABLE = ("unread", "news", "shop", "note", "self")
AUTOMATED = re.compile(r"(^|[._-])(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|updates?)@")
SHOP = re.compile(r"\b(order|shipped|shipping|delivered|delivery|receipt|your purchase|refund)\b", re.I)
REASONS = {
    "act": "A person wrote to you, and you haven't opened it or you starred it.",
    "unsorted": "Mail from a person. It stays with you.",
    "self": "A note you emailed yourself. Delete it if you're done with it.",
    "news": "A mailing list or newsletter.",
    "shop": "An order, shipping, or receipt update. The store keeps your order history.",
    "note": "An automated notice or a promotion. Nothing to reply to.",
}


def gog(account: str, *args: str):
    return tools.run_json(["gog", *args, "-a", account, "-j"], timeout=120)


def header(msg: dict, name: str) -> str:
    return next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == name), "")


def body_text(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if part.get("mimeType") == "text/plain" and data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
    for p in part.get("parts") or []:
        text = body_text(p)
        if text:
            return text
    if part.get("mimeType") == "text/html" and data:
        h = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        h = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", h)
        return html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li)>", "\n", h)))
    return ""


def snapshot_path(account: str):
    return config.data_dir() / "work/gmail" / (hashlib.sha1(account.encode()).hexdigest()[:12] + ".json")


def search(account: str, query: str) -> list[dict]:
    found = gog(account, "gmail", "messages", "search", query, "--max", "1000")
    if found.get("has_more"):
        raise tools.SourceError(f"{account}: more than 1000 messages match {query!r}; archive some and refresh")
    return found.get("messages") or []


def thread(account: str, tid: str, msg_ids: list[str], archived: bool) -> dict:
    ms = gog(account, "gmail", "thread", "get", tid)["thread"]["messages"]
    last, frm = ms[-1], header(ms[-1], "from")
    labels = {label for m in ms for label in m.get("labelIds", [])}
    category = next((c[9:].title() for c in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
                                             "CATEGORY_FORUMS") if c in labels), "Primary")
    unsubscribe = header(last, "list-unsubscribe")
    return {
        "tid": tid, "msg_ids": msg_ids, "count": len(ms), "subject": header(ms[0], "subject"), "from": frm,
        "addr": (re.search(r"<([^>]+)>", frm) or re.match(r"(\S*)", frm)).group(1).lower(),
        "to": header(last, "to"), "date": last["internalDate"], "labels": sorted(labels), "category": category,
        "snippet": html.unescape(last.get("snippet", "")),
        "body": re.sub(r"\n\s*\n+", "\n", body_text(last["payload"])).strip()[:4000], "archived": archived,
        "list": bool(unsubscribe),
        "unsub_url": next(iter(re.findall(r"<(https://[^>]+)>", unsubscribe)), "")
        if "one-click" in header(last, "list-unsubscribe-post").lower() else "",
    }


def fetch(account: str) -> list[dict]:
    inbox, outside = search(account, "in:inbox"), search(account, "is:unread -in:inbox")
    by_thread: dict[str, list[str]] = {}
    for m in inbox:
        by_thread.setdefault(m["threadId"], []).append(m["id"])
    archived = {m["threadId"] for m in outside if m["threadId"] not in by_thread}
    for m in outside:
        if m["threadId"] in archived:
            by_thread.setdefault(m["threadId"], []).append(m["id"])
    labels = {m["id"]: set(m.get("labels") or []) for m in inbox + outside}
    prev = {t["tid"]: t for t in config.read_json(snapshot_path(account), {}).get("threads", [])}

    def one(tid: str) -> dict:
        kept = prev.get(tid)
        if kept and sorted(kept["msg_ids"]) == sorted(by_thread[tid]) and kept["archived"] == (tid in archived):
            return {**kept, "labels": sorted(set().union(*(labels[i] for i in by_thread[tid])))}
        return thread(account, tid, by_thread[tid], tid in archived)

    with ThreadPoolExecutor(6) as pool:
        threads = sorted(pool.map(one, by_thread), key=lambda t: -int(t["date"]))
    config.write_json(snapshot_path(account), {"threads": threads})
    return threads


def group_of(t: dict, account: str) -> str:
    unread, a = "UNREAD" in t["labels"], t["addr"]
    if a == account.lower():
        group = "self"
    elif t["list"]:
        group = "news"
    elif SHOP.search(t["subject"]):
        group = "shop"
    elif AUTOMATED.search(a) or t["category"] != "Primary":
        group = "note"
    else:
        return "act" if unread or "STARRED" in t["labels"] else "unsorted"
    return "unread" if unread else group


def day(ms) -> str:
    dt = datetime.fromtimestamp(int(ms) / 1000)
    return dt.strftime("%b %-d" if dt.year == datetime.now().year else "%b %-d, %Y")


def rows(account: str) -> list[dict]:
    out = []
    for t in fetch(account):
        group = group_of(t, account)
        reason = REASONS.get(group) or "You never opened it, and it doesn't need you."
        place = "all" if t["archived"] else "inbox"
        out.append({
            "key": "gmail:" + hashlib.sha1(f"{account}:{t['tid']}".encode()).hexdigest()[:12], "account": account,
            "msg_ids": t["msg_ids"], "group": group, "title": t["subject"] or "(no subject)", "reason": reason,
            "subject": t["subject"] or "(no subject)", "from": re.sub(r"\s*<[^>]+>", "", t["from"]).strip('" ') or t["addr"],
            "addr": t["addr"], "day": day(t["date"]), "unread": "UNREAD" in t["labels"], "to": [t["to"]] if t["to"] else [],
            "cc": [], "body": t["body"] or t["snippet"], "unsub_url": t["unsub_url"],
            "link": f"https://mail.google.com/mail/?authuser={account}#{place}/{t['tid']}",
        })
    return out
