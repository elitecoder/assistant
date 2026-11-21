"""Slack Later saves, with the conversation around each one.

The open Later list comes from your signed-in browser session
(work_queue/slack_browser.py). The context and the reply checks come from the
read-only focus-board client with your user token ($SLACK_USER_TOKEN), so every
refresh re-reads each thread and sees new replies. Rules, not judgment, decide
which saves look finished:

- open      the thread moved in the last `close_after_days` days, so it stays with you
- last      you wrote the last message, and nobody asked anything since
- ownnote   you saved your own message, and nobody replied
- promise   your last message said you'd do something, and nobody chased it
- moved     you replied, and the thread carried on without you
- question  you replied, and the thread ended on a question nobody followed up
- fyi       a channel post that doesn't mention you, and you never replied
- stale     someone asked, you didn't answer in Slack, and nobody followed up
- unread    the save can't be read, so it isn't judged
"""
from __future__ import annotations

import contextlib
import fcntl
import re
import time
from datetime import datetime

from assistant.slack_focus.client import Client

from work_queue import config, tools

WAIT = 90
BUSY = ("The Slack browser session is busy with another job: a refresh, Apply, or marking sent saves done. Try again "
        "in a few minutes.")
PROMISE = re.compile(r"\b(I'?ll|I will|let me|I'?m going to)\b", re.I)
TWO_DAYS = 172800


def client_url(entry: dict) -> str:
    return f"https://app.slack.com/client/{entry['team_id']}/later"


def api_url(entry: dict) -> str:
    return entry["workspace_url"].rstrip("/") + "/api/"


class Busy(tools.SourceError):
    """Another job held the Slack browser profile for longer than the caller waits."""


@contextlib.contextmanager
def session_lock(wait: float | None = None):
    """Hold the Work queue's Slack browser profile: Chromium opens a profile in one process at a time, so the refresh,
    Apply, and the sent-save worker take turns. With `wait`, give up after that many seconds and raise Busy; without
    it, wait as long as it takes, which only the background worker does."""
    path = config.data_dir() / "slack-browser.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        end = None if wait is None else time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if end is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Busy(BUSY) from None
                time.sleep(0.2)
        yield


def browser(action: str, entry: dict, *extra: str) -> list[str]:
    return tools.playwright_cmd("slack_browser.py", action, "--client-url", client_url(entry),
                                "--api-url", api_url(entry), "--profile", str(config.browser_profile()), *extra)


def later(entry: dict) -> list[dict]:
    with session_lock(WAIT):
        out = tools.run_json(browser("list", entry, "--headless"), timeout=300)
    if not out.get("ok"):
        raise tools.SourceError(out.get("error") or "Slack Later list failed")
    return [i for i in out["items"] if i.get("item_type") == "message"]


def shape(client: Client, m: dict) -> dict:
    text = m.get("text") or ""
    return {"who": client.user_name(m.get("user", "")) or m.get("username") or "bot", "user": m.get("user", ""),
            "ts": m["ts"], "text": client.clean(text)[:900], "to_me": f"<@{client.me}>" in text}


def context(client: Client, cid: str, ts: str) -> dict:
    ch = client.channel(cid)
    rec = {"where": ch["name"], "kind": ch["kind"]}
    msgs = client.call("conversations.replies", {"channel": cid, "ts": ts, "limit": 200}).get("messages") or []
    if not msgs:
        msgs = client.call("conversations.history", {"channel": cid, "latest": ts, "inclusive": "true",
                                                     "limit": 1}).get("messages") or []
    target = next((m for m in msgs if m["ts"] == ts), None)
    if target is None:
        return {**rec, "error": "Slack can't show this message anymore"}
    root = target.get("thread_ts") or ts
    if root != ts:
        msgs = client.call("conversations.replies", {"channel": cid, "ts": root, "limit": 200}).get("messages") or msgs
    threaded = root != ts or bool(target.get("reply_count"))
    if threaded:
        after = [m for m in msgs if float(m["ts"]) > float(ts)]
    elif ch["kind"] in ("dm", "mpim"):
        window = client.call("conversations.history", {"channel": cid, "oldest": ts, "limit": 20,
                                                       "latest": f"{float(ts) + TWO_DAYS:.6f}"})
        after = sorted((m for m in window.get("messages") or [] if m["ts"] != ts), key=lambda m: float(m["ts"]))
    else:
        after = []
    return {**rec, "thread_ts": root, "msg": shape(client, target),
            "root": shape(client, msgs[0]) if root != ts else None,
            "after": [shape(client, m) for m in after[-4:]], "after_n": len(after),
            "me_after": any(m.get("user") == client.me for m in after)}


def classify(rec: dict, me: str, now: float, close_after_days: int) -> tuple[str, str]:
    if rec.get("error"):
        return "unread", rec["error"]
    last = (rec["after"] or [rec["msg"]])[-1]
    idle = int((now - float(last["ts"])) // 86400)
    if idle < close_after_days:
        return "open", ""
    quiet = f"Quiet for {idle} days."
    if last["user"] == me:
        if PROMISE.search(last["text"]):
            return "promise", f"Your last message promised something, and nobody followed up. {quiet} Closing it drops the promise."
        if rec["after"]:
            return "last", f"You had the last word, and nobody asked anything since. {quiet}"
        return "ownnote", f"You saved your own message, and nobody replied. {quiet}"
    if rec["me_after"]:
        if "?" in last["text"] or last["to_me"]:
            return "question", f"The thread ended on a question, but nobody followed up. {quiet} Check it before you close it."
        return "moved", f"You replied, and the thread carried on without needing you. {quiet}"
    if rec["kind"] == "channel" and not rec["msg"]["to_me"]:
        return "fyi", f"A channel post that doesn't mention you, and you never replied. {quiet}"
    return "stale", f"You didn't answer in Slack, and nobody followed up. {quiet}"


def day(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%b %-d")


def short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def rows(entry: dict, client: Client, now: float) -> list[dict]:
    out = []
    for item in later(entry):
        cid, ts = item["item_id"], item["ts"]
        rec = context(client, cid, ts)
        group, reason = classify(rec, client.me, now, int(entry.get("close_after_days") or 14))
        link = client.permalink(cid, ts, rec.get("thread_ts") or ts)
        row = {"key": f"{cid}:{ts}", "cid": cid, "ts": ts, "group": group, "reason": reason, "link": link,
               "where": rec["where"], "ctx": rec, "title": f"{rec['where']}: saved message"}
        if group != "unread":
            last = (rec["after"] or [rec["msg"]])[-1]
            topic = (rec["root"] or rec["msg"])["text"].split("\n")[0]
            row.update(title=f"{rec['where']}: {short(topic, 100) or '(no text)'}", topic=topic, who=rec["msg"]["who"],
                       when=day(ts), evidence=f"{last['who']}, {day(last['ts'])}: “{short(last['text'], 150)}”")
        out.append(row)
    return out
