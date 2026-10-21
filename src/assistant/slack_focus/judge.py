"""Judge: one bounded model call that turns mechanical candidates into board rows.

It reads only the candidates triage surfaced, never the full thread set. For
each it keeps or sets aside the row, picks a lane, and writes a short title.
Verdicts are cached by content, so an unchanged thread is never re-judged.
If the model is unavailable, the raw mechanical rows stay, so the board
always renders.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Callable

from .settings import Settings
from .triage import LANE_ORDER, Resolver

LANES = ("Do first", "This week", "Promise", "Read", "Done")
EFFORTS = ("Quick", "Small", "Medium")
# llm(prompt) -> the model's text answer, or "" when it couldn't run.
LLM = Callable[[str], str]


def rubric(owner: str) -> str:
    return f"""You are triaging a focus dashboard for {owner}. Below is a JSON array of candidate items that
mechanical rules already surfaced from Slack. For EACH item decide if it is genuinely something {owner}
must act on - a direct question to them, a decision only they make, a review/approval they owe, or a
commitment they made and must deliver. Social chatter, FYIs, things owned by others, or already-answered
items are NOT actionable.

Most of these do NOT need a reply — the thread is complete, it's an FYI, an ack, or someone else
owns the next step. Be strict about what truly needs {owner} to act.

A request from someone for new work or what to pick up next (e.g. "any small features I can help
with?", "what should I work on?") IS a "Do first" ask — {owner} owes them an answer.

If a candidate is clearly ALREADY DONE by one of {owner}'s merged PRs listed below, mark its lane
"Done" (do not tell them to do it again).

Return ONLY a JSON array, one object per item you keep (omit pure noise/social chatter):
  {{"id": <id>, "lane": "Do first" | "This week" | "Promise" | "Read" | "Done", "title": "<=12-word action or summary>",
    "why": "<=15-word reason", "effort": "Quick" | "Small" | "Medium"}}
A direct request in a 1:1 DM (dm=true) — a question, a decision, or a personal ask like a promotion
recommendation — is "Do first": a specific person is waiting on {owner}. Do NOT demote a 1:1 DM ask to
"This week"; This week is for broader work with nobody blocked on an immediate reply.

Lanes: "Do first" = someone is waiting on {owner}'s reply or decision now. "This week" = needs them, not urgent.
"Promise" = they said they'd do something and it is NOT already done. "Read" = worth knowing but needs NO
response (thread complete, FYI, decision already made). "Done" = already completed per the merged PRs below.
When unsure between a response-lane and "Read", choose "Read". Keep titles concrete. No prose outside the array."""


def done_evidence(settings: Settings, resolver: Resolver, window_days: int, today: datetime) -> list[str]:
    """Your PRs merged in the window, so the judge can retire finished asks."""
    since = (today - timedelta(days=window_days + 2)).strftime("%Y-%m-%d")
    evidence = []
    for repo in settings.github_repos:
        out = resolver.gh(["pr", "list", "-R", repo, "--author", "@me", "--state", "merged",
                           "--search", f"merged:>={since}", "--json", "number,title", "--limit", "40"])
        try:
            evidence += [f'{repo.split("/")[-1]}#{p["number"]} {p["title"]}' for p in json.loads(out or "[]")]
        except (ValueError, KeyError, TypeError):
            continue
    return evidence


def candidates(focus: dict, threads: dict[int, dict]) -> list[dict]:
    out = []
    for i, item in enumerate(focus["active"]):
        t = threads.get(item.get("thread_id"))
        context = f'opener {t["first"]["author"]}: {t["first"]["text"][:160]}' if t else ""
        c = {"id": i, "lane": item["lane"], "channel": item["channel"], "from": item["who"],
             "dm": item["channel"].startswith("DM "), "text": item["text"][:240], "context": context,
             "link": item["link"]}
        key = json.dumps([c["link"], c["text"], c["lane"], c["channel"], c["from"], c["dm"], c["context"]],
                         sort_keys=True)
        c["fp"] = hashlib.sha256(key.encode()).hexdigest()
        out.append(c)
    return out


def usable_cache(saved, evidence: list[str], rubric_sig: str) -> dict:
    """Prior verdicts, or nothing when the merged-PR set or the rubric changed."""
    if not isinstance(saved, dict) or set(saved.get("done_evidence", [])) != set(evidence) \
            or saved.get("rubric") != rubric_sig or not isinstance(saved.get("verdicts"), dict):
        return {}
    return {k: v for k, v in saved["verdicts"].items()
            if v == "DROP" or (isinstance(v, dict) and isinstance(v.get("title"), str) and v["title"].strip())}


def parse(raw: str) -> dict[int, dict] | None:
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return {int(d["id"]): d for d in data
                if isinstance(d, dict) and "id" in d and isinstance(d.get("title"), str) and d["title"].strip()}
    except (ValueError, TypeError):
        return None


def judge(focus: dict, threads: dict[int, dict], evidence: list[str], saved_cache, llm: LLM) -> tuple[dict, dict]:
    """Return (focus with judged lanes, cache to save). Mutates nothing passed in."""
    focus = json.loads(json.dumps(focus))
    owner = focus.get("me_name") or "you"
    text = rubric(owner)
    rubric_sig = hashlib.sha256(text.encode()).hexdigest()
    cache = usable_cache(saved_cache, evidence, rubric_sig)
    cands = candidates(focus, threads)
    # Only new or changed candidates go to the model, renumbered 0..k so it
    # echoes dense ids.
    to_judge = [c for c in cands if c["fp"] not in cache]
    local = {c["fp"]: j for j, c in enumerate(to_judge)}
    payload = [{"id": j, "lane": c["lane"], "channel": c["channel"], "from": c["from"], "dm": c["dm"],
                "text": c["text"], "context": c["context"]} for j, c in enumerate(to_judge)]
    prompt = (f"{text}\n\nALREADY DONE — {owner}'s merged PRs this window:\n"
              f"{chr(10).join(evidence) or '(none found)'}\n\nCANDIDATES:\n{json.dumps(payload, indent=0)}\n")
    model = parse(llm(prompt)) if to_judge else {}

    if to_judge and not model:
        focus["judge"] = {"ran": False, "note": "The model didn't return usable rows, so these are the raw ones."}
        return focus, saved_cache if isinstance(saved_cache, dict) else {}

    judged, new_cache = {}, {}
    for c in cands:
        if c["fp"] in cache:
            v = cache[c["fp"]]
        else:
            mv = model.get(local[c["fp"]])
            v = {k: mv[k] for k in ("lane", "title", "why", "effort") if k in mv} if mv else "DROP"
        new_cache[c["fp"]] = v
        if v != "DROP":
            judged[c["id"]] = v

    kept, read, dropped = [], [], []
    for i, item in enumerate(focus["active"]):
        v = judged.get(i)
        if not v:
            dropped.append({"text": item["text"], "channel": item["channel"], "who": item.get("who", ""),
                            "link": item["link"], "why": "not something you need to act on"})
            continue
        item = dict(item)
        lane = v.get("lane") if v.get("lane") in LANES else item["lane"]
        why = v.get("why")
        item.update(lane=lane, text=v["title"], why=why if isinstance(why, str) else "",
                    effort=v["effort"] if v.get("effort") in EFFORTS else "")
        if lane == "Done":
            focus["handled"].append({"text": item["text"], "channel": item["channel"], "link": item["link"],
                                     "why": why if isinstance(why, str) else "already done"})
        else:
            (read if lane == "Read" else kept).append(item)
    kept.sort(key=lambda i: (LANE_ORDER.index(i["lane"]) if i["lane"] in LANE_ORDER else 9, -i.get("rank", 0)))
    read.sort(key=lambda i: -i.get("rank", 0))
    focus.update(active=kept, read=read, judge_dropped=dropped)
    focus["counts"]["handled"] = len(focus["handled"])
    focus["judge"] = {"ran": True, "respond": len(kept), "read": len(read), "dropped": len(dropped),
                      "cached": len(cands) - len(to_judge), "rejudged": len(to_judge)}
    return focus, {"done_evidence": sorted(evidence), "rubric": rubric_sig, "verdicts": new_cache}
