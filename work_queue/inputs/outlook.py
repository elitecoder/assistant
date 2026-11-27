"""Outlook, read-only, through the Work IQ CLI (`npx -y @microsoft/workiq fetch`).

Work IQ signs in with your own Microsoft 365 account and only reads. The Work
queue never moves or deletes Outlook mail: approving rows builds an Outlook
search you paste into Outlook yourself. Rules sort each email:

- act      sent straight to you by a person, and you haven't replied in that conversation
- read     other mail from a person
- gh       GitHub notifications
- cal      meeting responses, cancellations, and out-of-office notes
- bulk     mail Outlook files under Other, and automated senders
- old      read mail you've kept for more than 30 days

gh, cal, bulk, and old can be approved for the delete search.

`events` reads today's Outlook calendar, in your local day, for the page's
Today tab. It skips cancelled events and marks the ones you declined.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

from work_queue import config, tools

DELETABLE = ("gh", "cal", "bulk", "old")
AUTOMATED = re.compile(r"(^|[._-])(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon)@")
CAL = re.compile(r"^(Accepted|Declined|Tentative|Canceled|Cancelled)\b|out of office|automatic reply", re.I)
REASONS = {
    "act": "Sent to you by a person, and you haven't replied in this conversation.",
    "read": "Mail from a person. Worth a look.",
    "gh": "A GitHub notification. GitHub keeps all of them.",
    "cal": "A meeting response, cancellation, or out-of-office note. Nothing to reply to.",
    "bulk": "A broadcast or an automated notice. Nothing to reply to.",
}
SELECT = "subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,bodyPreview,conversationId,inferenceClassification,webLink"
EVENT_SELECT = "subject,start,end,isAllDay,isCancelled,showAs,responseStatus,webLink"


def fetch(*paths: str) -> list[dict]:
    """One Work IQ call for up to four Graph paths. A failed path comes back as {"error": ...}."""
    cwd = config.data_dir() / "work/workiq"
    cwd.mkdir(parents=True, exist_ok=True)
    args = ["npx", "-y", "@microsoft/workiq", "fetch"]
    for p in paths:
        args += ["-u", p]
    text, docs, decoder, i = tools.run(args, timeout=600, cwd=cwd).strip(), [], json.JSONDecoder(), 0
    while i < len(text):
        obj, i = decoder.raw_decode(text, i)
        docs.append(obj)
        while i < len(text) and text[i].isspace():
            i += 1
    if len(docs) == 1 and "results" in docs[0]:
        docs = [x["data"] if x.get("statusCode") == 200 else {"error": x.get("error") or x} for x in docs[0]["results"]]
    if len(docs) != len(paths):
        raise tools.SourceError(f"Work IQ returned {len(docs)} answers for {len(paths)} requests")
    return docs


def one(path: str) -> dict:
    doc = fetch(path)[0]
    if "error" in doc:
        raise tools.SourceError(f"Work IQ couldn't read {path.split('?')[0]}: {str(doc['error'])[:200]}")
    return doc


def all_pages(base: str, field: str, since: str = "") -> list[dict]:
    out, before = [], ""
    while True:
        conds = [f"{field}%20ge%20{since}"] * bool(since) + [f"{field}%20lt%20{before}"] * bool(before)
        flt = f"&$filter={'%20and%20'.join(conds)}" if conds else ""
        page = one(f"{base}&$orderby={field}%20desc&$top=100{flt}")["value"]
        out += page
        if len(page) < 100:
            return out
        before = page[-1][field]


def text_of(body: dict) -> str:
    t = body.get("content") or ""
    if body.get("contentType") == "html":
        t = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", t)
        t = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", t)))
    return re.sub(r" *\n\s*", "\n", re.sub(r"[ \t\xa0]+", " ", t)).strip()[:4000]


def bodies(ids: list[str], sleep=time.sleep) -> dict[str, str]:
    prev = config.read_json(snapshot_path(), {}).get("bodies", {})
    out = {i: prev[i] for i in ids if i in prev}
    pending = [i for i in ids if i not in out]
    for attempt in range(4):
        retry = []
        for n in range(0, len(pending), 4):
            chunk = pending[n:n + 4]
            for mid, doc in zip(chunk, fetch(*[f"/me/messages/{mid}?$select=uniqueBody,body" for mid in chunk])):
                if "error" in doc:
                    retry.append(mid)
                else:
                    out[mid] = text_of(doc.get("uniqueBody") or doc.get("body") or {})
        pending = retry
        if not pending:
            return out
        sleep(2 + attempt * 2)
    raise tools.SourceError(f"Work IQ couldn't read {len(pending)} email bodies after retries")


def snapshot_path():
    return config.data_dir() / "work/outlook.json"


def group_of(m: dict, address: str, replied: set[str], now: datetime) -> str:
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
    to = {((r.get("emailAddress") or {}).get("address") or "").lower() for r in m.get("toRecipients") or []}
    received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
    if sender.endswith("@github.com"):
        return "gh"
    if CAL.search(m.get("subject") or ""):
        return "cal"
    if (m.get("inferenceClassification") or "").lower() == "other" or AUTOMATED.search(sender):
        return "bulk"
    if address.lower() in to and m.get("conversationId") not in replied:
        return "act"
    if m.get("isRead", True) and now - received > timedelta(days=30):
        return "old"
    return "read"


def names(recipients) -> list[str]:
    return [((r.get("emailAddress") or {}).get("name") or "") for r in recipients or []]


def rows(entry: dict, now: datetime | None = None, sleep=time.sleep) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    inbox = all_pages(f"/me/mailFolders/inbox/messages?$select={SELECT}", "receivedDateTime")
    since = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    replied = {s.get("conversationId") for s in all_pages(
        "/me/mailFolders/sentitems/messages?$select=conversationId,sentDateTime", "sentDateTime", since)}
    text = bodies([m["id"] for m in inbox], sleep)
    config.write_json(snapshot_path(), {"bodies": text})
    out = []
    for m in inbox:
        group = group_of(m, entry["address"], replied, now)
        received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).astimezone()
        sender = (m.get("from") or {}).get("emailAddress") or {}
        out.append({
            "key": "mail:" + hashlib.sha1(m["id"].encode()).hexdigest()[:12], "group": group,
            "title": m.get("subject") or "(no subject)", "subject": m.get("subject") or "(no subject)",
            "reason": REASONS.get(group) or f"You've kept this since {received:%b %-d, %Y}, and it doesn't ask for a reply.",
            "from": sender.get("name", ""), "addr": (sender.get("address") or "").lower(),
            "day": received.strftime("%b %-d"), "unread": not m.get("isRead", True),
            "to": names(m.get("toRecipients")), "cc": names(m.get("ccRecipients")),
            "body": text.get(m["id"]) or m.get("bodyPreview") or "", "link": m.get("webLink") or "",
        })
    return out


def local_day(now: datetime) -> tuple[str, str]:
    """The start and end of your local day around `now`, in UTC, as calendarView takes them."""
    day = now.astimezone().date()
    return tuple(datetime.combine(d, datetime.min.time()).astimezone().astimezone(timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ") for d in (day, day + timedelta(days=1)))


def events(entry: dict, now: datetime | None = None) -> list[dict]:
    """Today's Outlook events, in the same rows as gcal.rows. Graph gives start times in UTC."""
    start, end = local_day(now or datetime.now(timezone.utc))
    found = one(f"/me/calendarView?startDateTime={start}&endDateTime={end}&$select={EVENT_SELECT}"
                "&$orderby=start/dateTime&$top=200")
    return [{"account": entry["address"], "title": e.get("subject") or "(no title)",
             "when": "All day" if e.get("isAllDay") else datetime.fromisoformat(e["start"]["dateTime"])
             .replace(tzinfo=timezone.utc).astimezone().strftime("%-I:%M %p"),
             "link": e.get("webLink") or "", "declined": (e.get("responseStatus") or {}).get("response") == "declined"}
            for e in found["value"] if not e.get("isCancelled")]
