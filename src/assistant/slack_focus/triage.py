"""Triage: turn bucketed threads and your messages into board lanes. No model.

  - ball-in-your-court buckets map to lanes (Do first, This week, Optional)
  - a thread resolves to "Auto-resolved" when every linked PR is merged or
    closed, or every linked Jira ticket is done
  - your own messages that read like a commitment become Promises
  - nothing is dropped here; low-signal threads only carry a hint for the judge
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .settings import Settings
from .store import signature

LANE = {"ball_mine_dm": "Do first", "ball_mine_mention": "Do first",
        "thread_moved_on": "This week", "group_mention": "This week", "unread_watched": "Optional"}
LANE_ORDER = ["Do first", "Promise", "This week", "Optional"]
DIGEST_RE = re.compile(
    r"standup|TL;DR|KEY (DECISIONS|TOPICS|OUTCOMES)|:clipboard:|weekly sync|e-?scrum|:pushpin:", re.I)
PROMISE_RE = re.compile(
    r"\b(i'?ll|i will|let me|i can (?:get|take|look|help|make|add|fix|do|share|send)|"
    r"i'?m (?:on|looking|working)|will (?:do|send|share|fix|add|look|get|create|remove|update|report|sync|merge)|"
    r"on it|going to|plan to|by (?:mon|tue|wed|thu|fri|tomorrow|eod|eow|next week)|tomorrow|next week|"
    r"report back)\b", re.I)
DELIVERABLE_RE = re.compile(
    r"\b(PR|ticket|review|fix|doc|docs|test|tests|channel|meeting|canvas|data|slide|plan|demo|epic|branch|"
    r"upgrader|pipeline|flag|patent|rule|skill|dashboard|wiki|list|invite|agent|eval)s?\b", re.I)
Runner = Callable[..., subprocess.CompletedProcess]


def pr_regex(hosts) -> re.Pattern:
    host = "|".join(re.escape(h) for h in hosts)
    return re.compile(rf"(?:https?://)?({host})/([\w.-]+/[\w.-]+)/pull/(\d+)")


def jira_regex(keys) -> re.Pattern | None:
    if not keys:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")-\d+\b")


class Resolver:
    """Answers "is this PR or ticket closed?" with gh and the Jira REST API,
    caching each answer for the run. Any failure counts as still open."""

    def __init__(self, settings: Settings, run: Runner = subprocess.run, env: dict | None = None,
                 urlopen=urllib.request.urlopen):
        self.settings = settings
        self._run = run
        self._env = os.environ if env is None else env
        self._urlopen = urlopen
        self._pr: dict[tuple, bool] = {}
        self._jira: dict[str, bool] = {}

    def gh(self, args: list[str], timeout: int = 40) -> str | None:
        try:
            out = self._run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    def pr_closed(self, host: str, repo: str, number: str) -> bool:
        key = (host, repo, number)
        if key not in self._pr:
            target = repo if host == "github.com" else f"{host}/{repo}"
            out = self.gh(["pr", "view", number, "-R", target, "--json", "state"], timeout=30)
            try:
                self._pr[key] = out is not None and json.loads(out)["state"] in ("MERGED", "CLOSED")
            except (ValueError, KeyError, TypeError):
                self._pr[key] = False
        return self._pr[key]

    def jira_done(self, key: str) -> bool:
        jira = self.settings.jira
        if key not in self._jira:
            base = jira.base_url or self._env.get("JIRA_BASE_URL", "").rstrip("/")
            token = self._env.get(jira.token_env, "")
            email = self._env.get(jira.email_env, "") if jira.email_env else ""
            done = False
            if base.startswith("https://") and token:
                auth = ("Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()) if email \
                    else "Bearer " + token
                req = urllib.request.Request(f"{base}/rest/api/2/issue/{key}?fields=status",
                                             headers={"Authorization": auth})
                try:
                    fields = json.loads(self._urlopen(req, timeout=20).read())["fields"]
                    done = fields["status"]["statusCategory"]["key"] == "done"
                except Exception:  # noqa: BLE001 — an unreachable ticket stays open
                    done = False
            self._jira[key] = done
        return self._jira[key]


def _recent_text(t: dict) -> str:
    return " ".join(m["text"] for m in (t.get("latest") or [])) + " " + t["first"]["text"]


def resolution(t: dict, resolver: Resolver, prs_re: re.Pattern, keys_re: re.Pattern | None) -> str | None:
    blob = _recent_text(t)
    prs = set(prs_re.findall(blob))
    if prs and all(resolver.pr_closed(*p) for p in prs):
        return "linked PR merged or closed"
    keys = set(keys_re.findall(blob)) if keys_re and resolver.settings.jira.enabled else set()
    if keys and all(resolver.jira_done(k) for k in keys):
        return "linked ticket done"
    return None


def noise(t: dict, me_name: str) -> str | None:
    """A hint for the judge that a thread looks low-signal. It never drops."""
    if DIGEST_RE.search(t["first"]["text"]):
        return "digest or standup post"
    recent = t.get("latest") or []
    if t["bucket"] == "thread_moved_on" and "?" not in " ".join(m["text"] for m in recent):
        return "discussion, no open question"
    if recent and len(recent[-1]["text"]) < 40 and recent[-1]["author"] != me_name:
        return "trailed off (short ack)"
    return None


def rank(t: dict) -> float:
    score = 0.0
    if t["mentioned_after_my_last_reply"] == "direct" or t.get("kind") == "dm":
        score += 100
    if "?" in " ".join(m["text"] for m in (t.get("latest") or [t["first"]])):
        score += 20
    return score + datetime.fromisoformat(t["last_activity"]).timestamp() / 1e9


def triage(threads_doc: dict, mine: list[dict], settings: Settings, resolver: Resolver) -> dict:
    me_name = threads_doc.get("me_name") or ""
    prs_re = pr_regex(settings.github_hosts)
    keys_re = jira_regex(settings.jira.project_keys)
    active, handled = [], []
    for t in threads_doc["threads"]:
        lane = LANE.get(t["bucket"])
        if not lane:
            continue
        why = resolution(t, resolver, prs_re, keys_re)
        if why:
            handled.append({"text": t["first"]["text"][:140], "channel": t["channel"], "link": t["permalink"],
                            "why": why})
            continue
        ask = next((m for m in reversed(t.get("latest") or []) if m["author"] != me_name), None) or t["first"]
        # A 1:1 DM is your ask. A group DM that trails off with no question and
        # no @-mention of you waits for this week instead.
        if t["bucket"] == "ball_mine_dm" and t.get("kind") == "mpim" and "?" not in (ask["text"] or "") \
                and not t["mentioned_after_my_last_reply"]:
            lane = "This week"
        active.append({
            "low_signal": noise(t, me_name), "lane": lane, "channel": t["channel"], "who": t["last_author"],
            "text": (ask["text"] or t["first"]["text"])[:160], "link": t["permalink"],
            "mention": t["mentioned_after_my_last_reply"] == "direct",
            "rank": rank(t), "last": t["last_activity"][:16], "thread_id": t["id"], "sig": signature(t),
        })

    seen_prs = set()
    for m in mine:
        text = m["text"]
        if not (PROMISE_RE.search(text) and DELIVERABLE_RE.search(text) and len(text) > 30
                and not text.rstrip().endswith("?")):
            continue
        prs = set(prs_re.findall(text))
        key = tuple(sorted(prs))
        if key and key in seen_prs:
            continue
        seen_prs.add(key)
        cname = m.get("cname") or ""
        channel = ("group DM" if cname.startswith("mpdm-") else "DM") if m.get("dm") else "#" + cname
        if prs and all(resolver.pr_closed(*p) for p in prs):
            handled.append({"text": text[:140], "channel": channel, "link": m["permalink"],
                            "why": "linked PR merged or closed"})
            continue
        active.append({"lane": "Promise", "channel": channel, "who": "you", "text": text[:200],
                       "link": m["permalink"], "mention": False, "rank": float(m["ts"]), "last": ""})

    active.sort(key=lambda i: (LANE_ORDER.index(i["lane"]), -i["rank"]))
    return {
        "generated": datetime.now(timezone.utc).isoformat(), "window_days": threads_doc["days"],
        "me_name": me_name, "active": active, "handled": handled,
        "counts": {"active": len(active), "handled": len(handled), "threads": len(threads_doc["threads"])},
    }
