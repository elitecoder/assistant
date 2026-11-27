"""Render the Work queue page from one refresh's rows.

Everything on the page comes from your linked sources, the rules in
work_queue/inputs/, and, in Pro mode, the model's triage (work_queue/triage.py):
an action title, a why, a disposition, a lane, an effort, and a draft on each
open row, the Start here list, and the Since last night box. The Open list
groups rows by the model's lane, then the rows it hasn't sorted by source.
Every open row has Done, Snooze, Drop, and Send to Agent, which hands the row
to an agent for a first pass; the Send to
agents tab sends many at once, and can send them hands-off. A row the model
called `agent` says "Agent can finish". A sent row leaves the page, and Ready
for you shows its TODO once it's done or blocked, until you mark it Done or a
refresh finds its follow-up already happened (work_queue/ready.py). Send to
Agent opens a note box first. The Email tab holds Outlook, each Gmail account,
and Unsubscribe, with Send to Agent, Snooze, Archive, and Mark read on each
email. In Pro mode every
item also has a thumbs-down and a Tell Assistant link, which record what you said (assistant/feedback.py). Every piece of
model text is escaped. Drafts are text to copy: nothing on the page sends them.
"""
from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime

from assistant import feedback, pointers
from work_queue.inputs import gmail, outlook

e = html.escape
DISP = {"you": "You", "agent": "Agent", "handoff": "Hand off", "drop": "Drop"}
AGENT_HINT = '<span class="disp d-agent">Agent can finish</span>'
NEWTAB = ' target="_blank" rel="noopener noreferrer"'
EMAIL_MARK = "https://work-queue.local/email/"
READY = ('<div class="ready" id="ready" hidden><div class="cut-top"><b>Ready for you</b><span class="meta">Agents you sent '
         'work to from this page finished these or are blocked. Read the result, do what it leaves for you, then select '
         'Done. Work whose pull requests merged, or that you already followed up on, clears on its own.</span>'
         '<button class="chip ready-show" onclick="toggleCleared()" hidden>Show cleared</button></div>'
         '<ol class="ready-list"></ol><ul class="ready-cleared"></ul></div>'
         '<div class="ready working" id="working" hidden><div class="cut-top"><b>With agents</b><span class="meta">Work you sent '
         'that isn\'t finished yet. The steps show only what Assistant recorded: Started means it launched a session, not '
         'that the session is still working.</span></div><ol class="ready-list working-list"></ol></div>')
LANES = [
    ("do_first", "Do first", "Someone is waiting on your reply or decision."),
    ("promise", "Promise", "You said you would. Deliver it, hand it off, or say you're dropping it."),
    ("week", "This week", "Needs you, but not first thing."),
    ("optional", "Optional", "Skim if you have time."),
]
UNSORTED = ('<div class="sf-group"><h3>Not sorted yet</h3><p class="meta">The model hasn\'t put these in a lane yet, '
            'so they\'re grouped by where they came from.</p></div>')
SLACK_GROUPS = [
    ("last", "You had the last word", "You replied, and nobody asked anything after."),
    ("ownnote", "Your own messages", "Messages of yours you saved. Nobody replied."),
    ("fyi", "Read-only posts", "Channel posts that don't mention you. You never replied."),
    ("moved", "The thread moved on without you", "You replied, and later messages didn't need you."),
    ("stale", "Old asks nobody chased", "Someone asked, you didn't answer in Slack, and nobody followed up."),
    ("promise", "Your old promises nobody chased", "Closing these drops the promise. Check them more carefully."),
    ("question", "Threads that ended with a question", "The last message asks something, but nobody followed up."),
]
OUTLOOK_GROUPS = [
    ("gh", "GitHub notifications", "Bot comments, CI results, and PR updates. GitHub keeps all of them."),
    ("cal", "Calendar notices", "Meeting responses, cancellations, and out-of-office notes."),
    ("bulk", "Broadcasts and automated notices", "Sent to many people, or by a robot."),
    ("old", "Old mail you kept", "Read mail older than 30 days. Delete only the ones you're done with."),
]
GMAIL_GROUPS = [
    ("unread", "Never opened", "Mail you never opened that doesn't need you, including unread mail outside your inbox."),
    ("news", "Newsletters and mailing lists", "Most are on the web too."),
    ("shop", "Shopping and receipts", "The store keeps your order history."),
    ("note", "Notifications and promotions", "Automated notices, promotions, and social updates."),
    ("self", "Notes you sent yourself", "Delete the ones you're done with."),
]


def day(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%b %-d")


def cx(m: dict, cls: str, label: str = "") -> str:
    text = e(m["text"]) if m.get("text") else "<i>(no text)</i>"
    return (f'<div class="cx {cls}"><span class="cx-who">{e(m["who"])} · {day(m["ts"])}{label}</span>'
            f'<span class="cx-t">{text}</span></div>')


def ctx_html(rec: dict) -> str:
    """The saved Slack message with the start of its thread and the latest replies."""
    if rec.get("error"):
        return '<div class="ap-ctx"><div class="cx cx-gap">Slack can\'t show this message anymore.</div></div>'
    parts = [cx(rec["root"], "cx-root", " · started the thread")] if rec["root"] else []
    parts.append(cx(rec["msg"], "cx-saved", " · the message you saved"))
    after = rec["after"]
    hidden = rec["after_n"] - len(after)
    if hidden > 0:
        parts.append(f'<div class="cx cx-gap">{hidden} more {"reply" if hidden == 1 else "replies"} in between</div>')
    parts += [cx(m, "cx-after" + ("" if i == len(after) - 1 else " cx-more"), " · latest" if i == len(after) - 1 else "")
              for i, m in enumerate(after)]
    if not after:
        parts.append('<div class="cx cx-gap">No replies.</div>')
    more = '<button class="cx-toggle" onclick="this.parentElement.classList.toggle(\'open\')"></button>'
    return f'<div class="ap-ctx">{"".join(parts)}{more if len(after) > 1 else ""}</div>'


def pill(v: dict) -> str:
    """The model's call on a row: "Agent can finish" for `agent`, "Model suggests: ..." otherwise."""
    if not v:
        return ""
    if v["disposition"] == "agent":
        return AGENT_HINT
    return f'<span class="disp d-{e(v["disposition"])}">Model suggests: {e(DISP[v["disposition"]])}</span>'


def fb_span(on: bool, kind: str, key: str) -> str:
    """Where the page puts the small feedback controls for one item, or nothing when feedback is off (it is on only
    where the model's triage is, which is Pro mode). The page's script fills it in. `key` is what the server is
    told the item by; `data-fp` is the item's pointer, which the page looks its saved feedback up by."""
    return (f'<span class="fb" data-fk="{kind}" data-fp="{e(feedback.view_key(pointers.row_pointer(key), kind))}" data-fkey="{e(key)}"></span>'
            if on else "")


def open_row(title: str, meta: str, link: str, extra: str = "", verdict: dict | None = None, mk: str = "",
             source: str = "", feedback: bool = False) -> str:
    """One Open list row, with the model's verdict when there is one, and Send to Agent whatever the verdict. The
    model's action title is the headline, with the row's own text under it. A row in one of the model's lanes names
    its `source`. An email row also carries its stable mark key, so hiding or sending it from the Email tab hides it
    here too."""
    v = verdict or {}
    head = e(v.get("title") or title)
    orig = f'<span class="sf-orig">{e(title)}</span>' if v.get("title") else ""
    src = f'<span class="pr-pill src">{e(source)}</span> ' if v.get("lane") else ""
    effort = f'<span class="sf-effort sf-{e(v["effort"].lower())}">{e(v["effort"])}</span>' if v else ""
    why = f'<span class="sf-why">{e(v["why"])}</span>' if v else ""
    draft = (f'<div class="draft"><textarea readonly rows="{min(6, 2 + len(v["draft"]) // 90)}" aria-label="Draft reply">'
             f'{e(v["draft"])}</textarea><button class="btn" onclick="copyDraft(this)">Copy draft</button></div>'
             + (f'<div class="draft-fb">{fb_span(True, "draft", link)}</div>' if feedback else "")) if v.get("draft") else ""
    marked = f' data-mk="{e(mk)}"' if mk else ""
    return (f'<div class="sf-row" data-key="{e(link)}"{marked} data-q-title="{e(title)}" data-q-detail="{e(meta)}">'
            f'<div class="sf-main"><a class="sf-link" href="{e(link)}"{NEWTAB}>'
            f'<span class="sf-title">{pill(v)}{head}</span>{orig}<span class="sf-meta">{src}{e(meta)}</span>{why}</a>{effort}'
            '<button class="btn sf-mark" onclick="setState(this,\'done\')">Done</button>'
            '<button class="btn ghost" onclick="setState(this,\'snoozed\')" title="Hide for 3 days">Snooze</button>'
            '<button class="btn ghost" onclick="setState(this,\'dropped\')">Drop</button>'
            '<button class="btn sf-send" onclick="compose(this,queueTodo)" title="An agent takes a first pass and leaves drafts '
            f'for you">Send to Agent</button>{fb_span(feedback, "email" if mk else "row", mk or link)}</div>{draft}{extra}</div>')


def agents_section(rows: list[tuple[str, str, str, str, bool]]) -> str:
    """The Send to agents tab: every open row, the ones the model said an agent can finish first, to send many at once."""
    picks = "".join(f'<label class="ag-row" data-key="{e(link)}" data-q-title="{e(title)}" data-q-detail="{e(meta)}">'
                    '<input type="checkbox" class="ag-pick" onchange="agCount()">'
                    f'<span class="ag-text"><span class="sf-title">{AGENT_HINT if agent else ""}{e(title)}</span>'
                    f'<span class="sf-meta">{e(source)} · {e(meta)}</span></span></label>'
                    for title, meta, link, source, agent in sorted(rows, key=lambda r: not r[4]))
    controls = ('<div class="ag-bar"><label class="ag-opt"><input type="checkbox" id="ag-all" onchange="agAll(this)"> '
                'Select all</label><label class="ag-opt" title="The agent files tickets, posts Slack replies, and opens '
                'PRs without waiting for you"><input type="checkbox" id="ag-hands"> Hands-off</label>'
                '<button class="btn sf-send" id="ag-go" disabled onclick="agSend()">Send 0 to agents</button>'
                '<span class="meta" id="ag-msg"></span></div>')
    body = f'<div class="sf-list" id="ag-list">{picks}</div>' if picks else '<p class="meta">Nothing open right now.</p>'
    return section("agents", "Send to agents", "Every open item, with the ones marked Agent can finish first. Each one "
                   "you send becomes an Assistant TODO. Its status and result show on the Tasks tab of the Assistant "
                   "dashboard, and its session on the Sessions tab once it starts. Assistant starts up to 2 TODOs every "
                   "5 minutes, and only while fewer than 5 sessions are busy. Sessions you are using count, so an item "
                   "you send can wait; the Tasks tab says when starts are on hold. An agent takes a first "
                   "pass and leaves drafts for you; turn on Hands-off to let it finish and post on its own.",
                   controls + body)


def buttons(undo: str = "ap-undo", ok: str = "Approve", feedback: str = "") -> str:
    pick = "this.closest('.ap-row').dataset.key"
    return (f'<div class="ap-btns"><button class="btn ap-ok" onclick="decide([{pick}],\'approve\')">{ok}</button>'
            f'<button class="btn ghost ap-keep" onclick="decide([{pick}],\'keep\')">Keep</button>'
            + (f'<button class="btn ghost {undo}" onclick="decide([{pick}],\'{"clear" if undo == "ap-clear" else "undo"}\')">Undo</button>'
               if undo else "") + feedback + '</div>')


def ap_row(key: str, title: str, link: str, ctx: str, why: str, kind: str, undo: str = "ap-undo", ev: str = "",
           m: dict | None = None, feedback: bool = False) -> str:
    """One row you can approve. An email row (`m`) also has the email actions."""
    evidence = f'<span class="ap-ev">Evidence: {e(ev)}</span>' if ev else ""
    return (f'<div class="ap-row{" em-row" if m else ""}" data-key="{e(key)}" data-kind="{kind}"{em_data(m) if m else ""}>'
            f'<div class="ap-text"><a href="{e(link)}"{NEWTAB} class="sf-title">{e(title)}</a>{ctx}'
            f'<span class="ap-why"><b>Why it can go:</b> {e(why)}</span>{evidence}<span class="ap-status"></span>'
            f'{em_acts(m) if m else ""}</div>{buttons(undo, feedback=fb_span(feedback, "candidate", key))}</div>')


def email_mark(m: dict) -> str:
    """The mark key of an email: its own id, which stays the same across refreshes, unlike a Gmail link."""
    return EMAIL_MARK + m["key"]


def em_data(m: dict) -> str:
    """What an email row's actions need: its mark key, link, the TODO title and detail, and for Gmail its key."""
    detail = (f'Email from {m["from"]} on {m["day"]}, subject "{m["subject"]}". Why it\'s on the list: {m["reason"]} '
              f'It says: {m["body"][:700]}')
    gmail_key = f' data-gkey="{e(m["key"])}"' if m["key"].startswith("gmail:") else ""
    return (f' data-mk="{e(email_mark(m))}" data-link="{e(m["link"])}" data-q-title="{e("Email: " + m["title"])}"'
            f' data-q-detail="{e(detail)}"{gmail_key}')


def em_acts(m: dict, feedback: bool = False) -> str:
    """Send to Agent and Snooze on every email; Archive (while it's in the inbox) and Mark read (while it's unread) on
    Gmail only, since the Work queue only reads Outlook. Undo shows on a hidden email."""
    gmail_row = m["key"].startswith("gmail:")
    return ('<div class="em-acts"><span class="em-status"></span>'
            '<button class="btn sf-send" onclick="compose(this,sendMail)" title="An agent takes a first pass and leaves '
            'drafts for you">Send to Agent</button>'
            '<button class="btn ghost" onclick="snoozeMail(this)" title="Hide for 3 days">Snooze</button>'
            + ('<button class="btn ghost em-archive" onclick="gmailAct(this,\'archive\')">Archive</button>'
               if gmail_row and not m.get("archived") else "")
            + ('<button class="btn ghost em-readbtn" onclick="gmailAct(this,\'read\')">Mark read</button>'
               if gmail_row and m["unread"] else "")
            + '<button class="btn em-undo" onclick="undoMail(this)" hidden>Undo</button>' + fb_span(feedback, "email", email_mark(m)) + '</div>')


def emails(snap: dict) -> dict:
    """The Gmail conversation behind each Gmail row's key, for the server's Archive and Mark read."""
    return {m["key"]: {"account": account, "tid": m["tid"]} for account, rows in (snap.get("gmail") or {}).items()
            for m in rows if m.get("tid")}


def row_context(snap: dict) -> dict:
    """What a feedback record may say about each row, by every key the page names it by: where it came from, and a hash
    of who sent it (never the name). Saved by the refresh, because a request must never say it. GitHub rows carry no
    sender of their own."""
    out: dict = {}

    def add(keys, source: str, sender: str = "") -> None:
        found = {"source": source, **({"who": feedback.who_pointer(sender)} if sender.strip() else {})}
        out.update({k: found for k in keys if isinstance(k, str) and k})

    for r in snap.get("slack") or []:
        add([r["key"], r["link"]], "slack", r.get("who") or "")
    for p in snap.get("github") or []:
        add([p["key"], p["link"]], "github")
    for source, rows in (("outlook", snap.get("outlook") or []),
                         *(("gmail", found) for found in (snap.get("gmail") or {}).values())):
        for m in rows:
            add([m["key"], email_mark(m), m["link"]], source, m.get("addr") or m.get("from") or "")
    return out


def ap_group(group: str, title: str, help_text: str, rows: list[str], is_open: bool = False) -> str:
    return (f'<details class="ap-group" data-group="{group}"{" open" * is_open}><summary><span class="ap-gt">{title}</span> '
            f'<span class="count">{len(rows)}</span><button class="btn ap-all" onclick="event.preventDefault();approveGroup(this)">'
            f'Approve all {len(rows)}</button></summary><p class="meta">{help_text}</p><div class="sf-list">{"".join(rows)}</div></details>')


def mail_ctx(m: dict) -> str:
    to = ", ".join(m["to"][:4]) + (f" and {len(m['to']) - 4} more" if len(m["to"]) > 4 else "")
    cc = f"; cc {', '.join(m['cc'][:3])}" if m["cc"] else ""
    head = f'{m["from"]} · {m["day"]}' + (f" · to {to}{cc}" if to else "")
    more = '<button class="cx-toggle" onclick="this.parentElement.classList.toggle(\'open\')"></button>' if len(m["body"]) > 260 else ""
    return (f'<div class="ap-ctx"><div class="cx cx-saved"><span class="cx-who">{e(head)}</span>'
            f'<span class="cx-t">{e(m["body"][:3000])}</span></div>{more}</div>')


def mail_info(m: dict, need: bool = False, feedback: bool = False) -> str:
    """An email that needs you or is worth reading, with the email actions."""
    flag = '<span class="pr-pill amber">Unread</span>' if m["unread"] else ""
    return (f'<div class="pr-row em-row{" em-need" if need else ""}"{em_data(m)}><a class="sf-title" href="{e(m["link"])}"'
            f'{NEWTAB}>{e(m["title"])}</a><div class="pr-meta">{flag}</div>{mail_ctx(m)}<span class="ap-why"><b>Why:</b> '
            f'{e(m["reason"])}</span>{em_acts(m, feedback)}</div>')


def mail_list(title: str, rows: list[dict], need: bool = False, feedback: bool = False) -> str:
    return (f'<h4 class="mail-h">{title} <span class="count">{len(rows)}</span></h4>'
            f'<div class="sf-list">{"".join(mail_info(m, need, feedback) for m in rows)}</div>')


def mail_section(sid: str, title: str, note: str, body: str, is_open: bool) -> str:
    """One collapsible part of the Email tab. It starts open when it has email that needs you."""
    return (f'<details class="mb-sec" id="{sid}"{" open" * is_open}><summary><span class="mb-t">{title}</span> '
            f'<span class="meta">{note}</span></summary>{body}</details>')


def section(sid: str, title: str, meta: str, body: str) -> str:
    return f'<div class="ap" id="{sid}"><div class="cut-top"><b>{title}</b><span class="meta">{meta}</span></div>{body}</div>'


def lane(title: str, help_text: str, rows: list[str], key: str = "") -> str:
    """One group of Open list rows: one of the model's lanes (`key`), or one source, whose rows in a lane come as ""."""
    named = f' data-lane="{key}"' if key else ""
    rows = [r for r in rows if r]
    return (f'<div class="sf-lane"{named}><h3>{title} <span class="count">{len(rows)}</span></h3><p class="meta">{help_text}</p>'
            f'<div class="sf-list">{"".join(rows)}</div></div>')


def counted(d: dict) -> str:
    return ", ".join(f"{d[k]} {k}" for k in ("new", "gone", "changed") if d[k])


START_SEND = ('<button class="btn sf-send" onclick="compose(this,startSend)" title="An agent takes a first pass and leaves '
              'drafts for you">Send to Agent</button>')


def start_section(tri: dict, note: str) -> str:
    """The Start here tab: what changed since the baseline, then where to start. Marked items hide in the page."""
    box = ""
    if tri["changes"]:
        lines = "".join(f'<li><b>{e(d["source"])}</b>: {counted(d)}.{" " + e(d["text"]) if d.get("text") else ""}</li>'
                        for d in tri["changes"]["sources"]) or "<li>Nothing changed.</li>"
        box = (f'<div class="changes"><b>Since last night</b> <span class="meta">Compared with the refresh on '
               f'{e(tri["changes"]["built"])}. The counts come from your rows; the sentences come from the model.</span>'
               f'<ul>{lines}</ul></div>')
    heads = {s["key"]: tri["rows"].get(s["key"], {}).get("title") for s in tri["start"]}
    items = "".join(f'<li data-key="{e(s["key"])}" data-src="{e(s["source"].lower())}"'
                    f'{" data-blocked" if s["blocked"] else ""}><div>'
                    f'<a class="sf-title" href="{e(s["key"])}"{NEWTAB}>{e(heads[s["key"]] or s["title"])}</a>'
                    f' <span class="pr-pill src">{e(s["source"])}</span>'
                    + (f' <span class="sf-effort sf-{e(s["effort"].lower())}">{e(s["effort"])}</span>' if s["effort"] else "")
                    + '<button class="btn sf-mark" onclick="setState(this,\'done\')">Done</button>'
                    + ("" if s["blocked"] else START_SEND) + fb_span(True, "start_here", s["key"]) + '</div>'
                    + (f'<span class="sf-orig">{e(s["title"])}</span>' if heads[s["key"]] else "") +
                    f'<span class="ap-why">{e(s["reason"])}</span></li>' for s in tri["start"])
    body = f'<ol class="start-list">{items}</ol>' if items else '<p class="meta">No Start here list this time.</p>'
    return section("start", "Start here", "In this order, ranked by the model on who's waiting, deadlines, effort, and age. "
                   "Done hides an item here and marks its row done. Send to Agent hands it to an agent for a first pass, "
                   "and the item and its row leave the page.", READY + note + box + body)


def render(snap: dict) -> tuple[str, dict]:
    """The page and the approval candidates the server acts on."""
    cands: dict[str, dict] = {}
    lanes, sections, tabs = [], [], []
    tri = snap.get("triage")
    pro = tri is not None  # triage runs only in Pro mode, and so do the feedback controls
    verdicts = (tri or {}).get("rows", {})
    note = (f'<p class="triage-note">Triage unavailable: {e(tri["note"])}. Rows without a model verdict still show, '
            'grouped by the rules, and offer Send to Agent.</p>'
            if tri and tri["note"] else "")
    if tri and tri["failed"]:
        note += (f'<p class="triage-note">Part of triage didn\'t finish. {e(". ".join(tri["failed"]))}. Each part is '
                 'asked again tomorrow, or sooner when its rows change.</p>')
    if tri and tri["waiting"]:
        note += f'<p class="meta">{tri["waiting"]} rows wait for triage until the next refresh.</p>'
    if tri and tri["skipped"]:
        note += f'<p class="meta">The model didn\'t answer about {tri["skipped"]} rows. They\'re asked again tomorrow.</p>'

    slack_rows = snap.get("slack") or []
    agent_rows: list[tuple[str, str, str, str, bool]] = []
    laned: dict[str, list[str]] = {key: [] for key, _, _ in LANES}

    def row(source: str, title: str, meta: str, link: str, extra: str = "", mk: str = "") -> str:
        """The row's HTML for its source's group, or "" when it goes in the model's lane instead."""
        v = verdicts.get(link)
        agent_rows.append((title, meta, link, source, bool(v) and v["disposition"] == "agent"))
        html_row = open_row(title, meta, link, extra, v, mk, source, pro)
        if v and v.get("lane") in laned:
            laned[v["lane"]].append(html_row)
            return ""
        return html_row

    open_slack = [row("Slack", r["title"], " · ".join(x for x in (r["who"], r["when"]) if x), r["link"], ctx_html(r["ctx"]))
                  for r in slack_rows if r["group"] == "open"]
    if snap.get("slack") is not None:
        lanes.append(lane("Slack", "Later saves whose thread moved recently.", open_slack))
    if snap.get("github") is not None:
        lanes.append(lane("Pull requests", "Open pull requests that ask for your review.",
                          [row("GitHub", p["title"], p["meta"] + (" · draft" if p["draft"] else ""), p["link"])
                           for p in snap["github"]]))
    acts = [m for m in snap.get("outlook") or [] if m["group"] == "act"]
    if snap.get("outlook") is not None:
        lanes.append(lane("Outlook", "Mail sent to you by a person that you haven't replied to.",
                          [row("Outlook", m["title"], f'{m["from"]} · {m["day"]}', m["link"], mail_ctx(m), email_mark(m))
                           for m in acts]))
    gmail_by = snap.get("gmail") or {}
    for account, rows in gmail_by.items():
        lanes.append(lane(f"Gmail: {e(account)}", "Mail from a person that you haven't opened, or that you starred.",
                          [row("Gmail", m["title"], f'{m["from"]} · {m["day"]}', m["link"], mail_ctx(m), email_mark(m))
                           for m in rows if m["group"] == "act"]))
    if any(laned.values()):
        rest = [body for body in lanes if 'class="sf-row"' in body]
        lanes = [lane(t, h, laned[key], key) for key, t, h in LANES if laned[key]] + ([UNSORTED, *rest] if rest else [])
    total = sum(body.count('class="sf-row"') for body in lanes)
    starts = bool(tri and (tri["start"] or tri["changes"] or tri["note"] or tri["failed"]))
    ready = "" if starts else READY
    sections.append(f'<div id="open-list" class="tab-sec"><div class="cut"><div class="cut-top"><b>Open list</b>'
                    f'<span class="meta"><span id="open-total">{total}</span> open items. Mark each Done, Snooze it for 3 days, or Drop it. '
                    'Send to Agent hands a row to an agent for a first pass, and it leaves drafts for you. To let agents '
                    'finish on their own, use the Send to agents tab with Hands-off.</span></div>'
                    '<button class="chip" id="showhidden">Show snoozed and dropped</button></div>'
                    f'{ready}{note}{"".join(lanes) or "<p class=meta>No sources are linked yet.</p>"}</div>')
    tabs.append(("open-list", "Open list", total, "var(--c-cut)"))
    if lanes:
        sections.append(agents_section(agent_rows))
        tabs.append(("agents", "Send to agents", len(agent_rows), "var(--c-unsub)"))
    if starts:
        at = 0 if tri["start"] else len(sections)
        sections.insert(at, start_section(tri, note))
        tabs.insert(at, ("start", "Start here", len(tri["start"]), "var(--c-start)"))

    if snap.get("calendar"):
        cal_rows = "".join(f'<div class="pr-row"><a class="sf-title" href="{e(c["link"])}"{NEWTAB}>{e(c["when"])} · {e(c["title"])}</a>'
                           f'<span class="meta">{e(c["account"])}{" · you declined" if c["declined"] else ""}</span></div>'
                           for c in snap["calendar"])
        sections.append(section("calendar", "Today", f'{len(snap["calendar"])} events on your calendars today.',
                                f'<div class="sf-list">{cal_rows}</div>'))
        tabs.append(("calendar", "Today", len(snap["calendar"]), "var(--c-start)"))

    mail = snap.get("outlook") or []
    parts, need = [], 0
    if snap.get("outlook") is not None:
        by = {g: [m for m in mail if m["group"] == g and m["body"]] for g in outlook.DELETABLE}
        for rows in by.values():
            cands.update({m["key"]: {"kind": "mail", "link": m["link"], "mk": email_mark(m)} for m in rows})
        groups = "".join(ap_group(f"mail-{g}", t, h, [ap_row(m["key"], m["subject"], m["link"], mail_ctx(m), m["reason"], "mail",
                                                             "ap-clear", m=m, feedback=pro) for m in by[g]]) for g, t, h in OUTLOOK_GROUPS if by[g])
        reads = [m for m in mail if m["group"] == "read"]
        need += len(acts)
        parts.append(mail_section("email", "Outlook", f"{len(mail)} emails in your inbox. {len(acts)} need you, "
                                  f"{len(reads)} are worth reading, and {sum(map(len, by.values()))} can go.",
                                  mail_list("Needs you", acts, True, pro) + mail_list("Worth reading", reads, False, pro) +
                                  '<h4 class="mail-h">Ready to delete</h4><p class="meta">The Work queue only reads Outlook. '
                                  'Approve what can go, and this search updates to find exactly those emails. Paste it into '
                                  'Outlook search with the scope set to Inbox, select all, and delete.</p>'
                                  '<div class="mq"><code id="mq-q">Approve emails below to build your search.</code>'
                                  '<button class="btn" id="mq-copy" disabled onclick="copySearch(this)">Copy search</button></div>'
                                  f'<p class="meta" id="mq-note"></p>{groups}', bool(acts)))

    unsub: dict[tuple, list[dict]] = {}
    for n, (account, rows) in enumerate(gmail_by.items(), 1):
        keep = {m["addr"] for m in rows if m["group"] not in gmail.DELETABLE}
        for m in rows:
            if m["group"] in gmail.DELETABLE and m["addr"] not in keep and m["addr"] != account.lower():
                unsub.setdefault((account, m["addr"], m["from"]), []).append(m)
        by = {g: [m for m in rows if m["group"] == g] for g in gmail.DELETABLE}
        for g_rows in by.values():
            cands.update({m["key"]: {"kind": "gmail", "account": account, "msg_ids": m["msg_ids"], "link": m["link"],
                                     "mk": email_mark(m)} for m in g_rows})
        groups = "".join(ap_group(f"gmail-{g}", t, h, [ap_row(m["key"], m["subject"], m["link"], mail_ctx(m), m["reason"], "gmail",
                                                              m=m, feedback=pro) for m in by[g]]) for g, t, h in GMAIL_GROUPS if by[g])
        looks = [m for m in rows if m["group"] == "unsorted"]
        yours = [m for m in rows if m["group"] == "act"]
        n_del = sum(map(len, by.values()))
        need += len(yours)
        parts.append(mail_section(f"gmail-{n}", f"Gmail: {e(account)}", f"{len(rows)} conversations. {len(yours)} need you, "
                                  f"{len(looks)} are worth a look, and {n_del} can go.",
                                  mail_list("Needs you", yours, True, pro) + mail_list("Worth a look", looks, False, pro) +
                                  '<h4 class="mail-h">Ready to delete</h4><p class="meta">Approve what can go, then select Apply approvals. '
                                  'Each conversation moves to Gmail\'s Trash, which keeps it for 30 days. Undo moves it back to your inbox.</p>'
                                  f'{groups}', bool(yours)))

    subs = []
    for (account, addr, name), ms in unsub.items():
        url = next((m["unsub_url"] for m in ms if m["unsub_url"].startswith("https://")), "")
        if url:
            key = "unsub:" + hashlib.sha1(f"{account}:{addr}:{name}".encode()).hexdigest()[:12]
            cands[key] = {"kind": "unsub", "url": url, "link": ms[0]["link"]}
            subs.append(f'<div class="ap-row" data-key="{e(key)}" data-kind="unsub"><div class="ap-text">'
                        f'<a href="{e(ms[0]["link"])}"{NEWTAB} class="sf-title">{e(name)}</a>'
                        f'<span class="meta">{e(addr)} · {e(account)}</span><span class="ap-why">{len(ms)} of their emails '
                        f'can go. Latest: {e(ms[0]["subject"][:80])}</span><span class="ap-status"></span></div>{buttons("", "Unsubscribe", fb_span(pro, "candidate", key))}</div>')
    if subs:
        parts.append(mail_section("unsubscribe", "Unsubscribe", f"{len(subs)} senders support one-click unsubscribe. Approve a "
                                  "sender, then select Apply approvals, and the Work queue sends that sender's unsubscribe request. "
                                  "You can't undo this here. Senders that also send mail you keep are left out.",
                                  ap_group("unsub", "Senders", "", subs, True), False))
    if parts:
        sections.append(section("mailbox", "Email", "Each part opens and closes. A part with email that needs you starts open. "
                                "Snooze hides an email for 3 days, Archive and Mark read change it in Gmail and check that "
                                "Gmail did, and Send to Agent hands it to an agent for a first pass.",
                                '<button class="chip" id="em-showhidden" onclick="toggleMailHidden()">Show hidden email'
                                '</button>' + "".join(parts)))
        tabs.append(("mailbox", "Email", need, "var(--c-outlook)"))

    if snap.get("slack") is not None:
        by = {g: [r for r in slack_rows if r["group"] == g] for g, _, _ in SLACK_GROUPS}
        for rows in by.values():
            cands.update({r["key"]: {"kind": "slack", "cid": r["cid"], "ts": r["ts"], "link": r["link"]} for r in rows})
        unread = [r for r in slack_rows if r["group"] == "unread"]
        groups = "".join(ap_group(g, t, h, [ap_row(r["key"], r["title"], r["link"], ctx_html(r["ctx"]), r["reason"], "slack",
                                                   ev=r["evidence"], feedback=pro) for r in by[g]]) for g, t, h in SLACK_GROUPS if by[g])
        unread_html = "".join(f'<div class="ap-urow"><a href="{e(r["link"])}"{NEWTAB}>Open in Slack</a> '
                              f'<span class="meta">{e(r["where"])}</span></div>' for r in unread)
        closable = sum(map(len, by.values()))
        sections.append(section("slack-saves", "Slack saves ready to mark done",
                                f"{closable} of your {len(slack_rows)} open Later saves. Each row says why it looks finished, "
                                "with the last message as evidence. Approve, then select Apply approvals. Undo reopens it.",
                                groups + (f'<details class="ap-unread"><summary><span class="ap-gt">Saves that can\'t be read, '
                                          f'so they aren\'t judged</span> <span class="count">{len(unread)}</span></summary>'
                                          f'<div class="sf-list">{unread_html}</div></details>' if unread else "")))
        tabs.append(("slack-saves", "Slack saves", closable, "var(--c-slack)"))
    tabs.append(("all", "All", "", "var(--muted)"))

    status = "".join(f'<li class="src-{e(s["state"])}"><b>{e(s["name"])}</b>: {e(s["detail"])}</li>' for s in snap["status"])
    tabs_html = ('<nav class="tabs" id="tabs" aria-label="Sections">'
                 + "".join(f'<a class="tab" href="#{t}" data-t="{t}" style="--acc:{c}">{e(n)}{f" <span>{k}</span>" if k != "" else ""}</a>'
                           for t, n, k, c in tabs) + '</nav>')
    maildata = json.dumps([{"k": m["key"], "a": m["addr"], "s": m["subject"]} for m in mail]).replace("</", "<\\/")
    bar = ('<div class="ap-bar" id="ap-bar"><span id="ap-count"></span><button class="btn ap-apply" id="ap-apply" '
           'onclick="applyNow()">Apply approvals</button><span class="meta" id="ap-msg"></span></div>') if cands else ""
    doc = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>Work queue</title><style>{CSS}</style><body><div class="section">'
           f'<div class="sf-head"><h2>Work queue</h2><p class="meta">Last refreshed <span id="built" data-iso="{e(snap.get("built_iso", ""))}">{e(snap["built"])}</span> from your linked sources. '
           + ("Rules sort every row, and a model explains each open row and picks where to start. Nothing is sent "
              "without you.</p>" if tri else "Rules sort every row; nothing is sent without you.</p>") +
           '<button class="btn" id="refresh" onclick="refreshNow()">Refresh</button><span class="meta" id="refresh-msg"></span><p class="meta" id="held-msg"></p>' + (PAGE_FB if pro else "") + '</div>'
           f'<ul class="sources">{status}</ul>{tabs_html}{SEARCH}{"".join(sections)}{bar}'
           f'</div><script>const MAILDATA={maildata};</script><script>{JS}</script></body></html>')
    return doc, cands


SEARCH = ('<div class="tab-search" id="tab-search-bar"><label for="tab-search">Search this tab <kbd aria-hidden="true">/</kbd>'
          '</label><input id="tab-search" type="search" placeholder="Words on this tab" aria-keyshortcuts="/" '
          'oninput="filterTab(this.value)" onkeydown="tabSearchKey(event)"><span class="meta" id="tab-search-empty" '
          'role="status" hidden>Nothing on this tab matches. Press Escape in the box to clear the search.</span></div>')


PAGE_FB = ('<span class="fb" data-fk="page" data-fp="page" data-fkey=""></span><span class="meta fb-promise">Assistant keeps what '
           'you tell it. It doesn\'t use it yet; that\'s planned for the next update.</span>')


def placeholder(message: str) -> str:
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Work queue</title><style>{CSS}</style><body><div class="section"><div class="sf-head"><h2>Work queue</h2>'
            f'<p class="meta">{e(message)}</p><button class="btn" id="refresh" onclick="refreshNow()">Refresh</button>'
            f'<span class="meta" id="refresh-msg"></span><p class="meta" id="held-msg"></p></div></div><script>const MAILDATA=[];</script><script>{JS}</script></body></html>')


CSS = """
:root{--bg:#0a0a0d;--panel:#13141a;--line:rgba(255,255,255,0.06);--line-strong:rgba(255,255,255,0.10);--hover:rgba(255,255,255,0.025);
--text:#ededf0;--text-2:#c9cad1;--muted:#7c7e8a;--quiet:#9fa1ad;--green:#5fd97a;--green-bg:rgba(95,217,122,0.12);--amber:#f0b330;--amber-bg:rgba(240,179,48,0.12);
--blue:#6bbef0;--blue-bg:rgba(107,190,240,0.12);--purple:#b06bf0;--purple-bg:rgba(176,107,240,0.14);--red:#e96b6b;--red-bg:rgba(233,107,107,0.12);
--c-unsub:#4fd1c5;--c-start:#f0b330;--c-outlook:#6bbef0;--c-gmail:#f0846b;--c-slack:#b06bf0;--c-cut:#e96be9;
--sans:-apple-system,BlinkMacSystemFont,"Inter","SF Pro Text",system-ui,"Segoe UI",Roboto,sans-serif;
--mono:"Geist Mono","JetBrains Mono","SF Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:light){:root{--bg:#fafafb;--panel:#ffffff;--line:rgba(0,0,0,0.08);--line-strong:rgba(0,0,0,0.14);--hover:rgba(0,0,0,0.03);
--text:#16171c;--text-2:#3a3c45;--muted:#6b6d78;--quiet:#585a66}}
*{box-sizing:border-box}html,body{background:var(--bg);color:var(--text);margin:0;padding:0}
body{font:13px/1.55 var(--sans);padding:28px 32px 48px;-webkit-font-smoothing:antialiased;max-width:1040px;margin:0 auto}
.meta{font:11px/1.5 var(--mono);color:var(--muted)}
.section h2{font:600 12px/1.4 var(--sans);color:var(--text-2);letter-spacing:.02em;text-transform:uppercase;margin:0}
.count{color:var(--muted);font:500 11px/1 var(--mono);margin-left:6px;text-transform:none}
.btn{background:rgba(127,127,127,0.10);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:500 11px/1.2 var(--mono);cursor:pointer;flex:none}
.btn:hover{border-color:var(--line-strong)}.btn.ghost{color:var(--muted)}.btn:disabled{opacity:.45;cursor:default}
.sf-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.sf-head .meta{margin:0;max-width:640px}
.sources{margin:0 0 10px;padding-left:18px;color:var(--muted);font-size:12px}.sources b{color:var(--text-2)}.sources .src-error b{color:var(--red)}
.cut{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--c-cut);border-radius:10px;padding:12px 14px;margin-bottom:22px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.cut-top{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline}.cut-top b{font-size:13px}
.chip{background:transparent;color:var(--text-2);border:1px solid var(--line-strong);border-radius:99px;padding:4px 10px;font:500 11px/1.2 var(--mono);cursor:pointer}
.sf-lane{margin-bottom:24px}.sf-lane h3{font:600 13px/1.4 var(--sans);margin:0 0 2px}.sf-lane .meta{margin:0 0 8px}
.sf-list{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.sf-row{padding:10px 14px;border-top:1px solid var(--line)}.sf-row:first-child{border-top:0}.sf-row:hover{background:var(--hover)}
.sf-main{display:flex;align-items:center;gap:8px}
.sf-link{flex:1;min-width:0;color:inherit;text-decoration:none;display:flex;flex-direction:column;gap:2px}
.sf-title{color:var(--text);overflow-wrap:anywhere}.sf-orig{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.start-list .sf-orig{display:block}.sf-meta .pr-pill.src{font-size:10px}
.sf-group{margin:4px 0 12px;padding-top:12px;border-top:1px solid var(--line)}
.sf-group h3{font:600 13px/1.4 var(--sans);margin:0 0 2px;color:var(--text-2)}.sf-group .meta{margin:0}
.sf-lane[data-lane="do_first"] .sf-list{border-left:3px solid var(--green)}.sf-lane[data-lane="promise"] .sf-list{border-left:3px solid var(--c-cut)}
.sf-lane[data-lane="week"] .sf-list{border-left:3px solid var(--blue)}.sf-lane[data-lane="optional"] .sf-list{border-left:3px solid var(--purple)}.sf-meta{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.sf-row.is-done .sf-title{text-decoration:line-through;opacity:.7}
.sf-row.is-hidden{display:none}body.show-hidden .sf-row.is-hidden{display:block;opacity:.55}
.ap{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--acc,var(--line-strong));border-radius:10px;padding:12px 14px;margin-bottom:22px}
.ap .cut-top{margin-bottom:8px}.ap .cut-top b{color:var(--acc,var(--text))}
#calendar{--acc:var(--c-start)}#email{--acc:var(--c-outlook)}[id^="gmail-"]{--acc:var(--c-gmail)}#slack-saves{--acc:var(--c-slack)}#unsubscribe{--acc:var(--c-unsub)}
.ap-group{border-top:1px solid var(--line);padding:6px 0}
.ap-group summary{cursor:pointer;display:flex;align-items:center;gap:8px;font:600 13px/1.4 var(--sans);color:var(--text-2);padding:4px 0}
.ap-group summary .ap-all{margin-left:auto}.ap-group .meta{margin:2px 0 8px}
.ap-row{display:flex;gap:10px;align-items:flex-start;padding:10px 14px;border-top:1px solid var(--line)}.ap-row:first-child{border-top:0}
.ap-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}.ap-text a{text-decoration:none}
.ap-why{color:var(--text-2);font-size:12px}.ap-why b{color:var(--acc,var(--text));font-weight:600}.ap-ev{color:var(--muted);font-size:12px;font-style:italic;overflow-wrap:anywhere}
.ap-status{font:600 10px/1.4 var(--mono)}.ap-btns{display:flex;gap:6px;flex:none}
.ap-row .ap-undo,.ap-row .ap-clear{display:none}.ap-row.approved .ap-clear,.ap-row.kept .ap-clear{display:inline-block}
.ap-row.approved{box-shadow:inset 3px 0 0 var(--green)}.ap-row.approved .ap-ok{background:var(--green-bg);color:var(--green)}
.ap-row.kept{opacity:.6}.ap-row.applied .ap-ok,.ap-row.applied .ap-keep{display:none}.ap-row.applied .ap-undo{display:inline-block}
.ap-row.applied .sf-title{text-decoration:line-through;opacity:.7}.ap-row.failed .ap-status{color:var(--red)}.ap-row.applied .ap-status{color:var(--green)}
.ap-bar{position:sticky;bottom:0;display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--panel);border:1px solid var(--line-strong);border-radius:10px;padding:10px 14px;margin:0 0 22px;z-index:5}
.ap-apply{background:var(--green-bg);color:var(--green)}
.ap-ctx{border-left:2px solid var(--line-strong);padding:2px 0 2px 10px;margin:6px 0 4px;display:flex;flex-direction:column;gap:6px;max-width:760px}
.cx{display:flex;flex-direction:column;gap:1px;font:12px/1.45 var(--sans)}.cx-who{color:var(--muted);font:600 11px/1.4 var(--sans)}
.cx-t{color:var(--text-2);white-space:pre-line;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.cx-saved{background:var(--hover);border:1px solid var(--line);border-radius:6px;padding:5px 8px}.cx-saved .cx-t{color:var(--text);-webkit-line-clamp:4}
.cx-gap{color:var(--muted);font:500 11px/1.4 var(--mono)}.cx-more{display:none}
.ap-ctx.open .cx-more{display:flex}.ap-ctx.open .cx-t{-webkit-line-clamp:unset;display:block}
.cx-toggle{align-self:flex-start;background:none;border:0;padding:0;color:var(--blue);font:600 11px/1.4 var(--sans);cursor:pointer}
.cx-toggle::after{content:"Show more"}.ap-ctx.open .cx-toggle::after{content:"Show less"}
.tabs{position:sticky;top:0;z-index:8;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:10px 0;margin:0 0 14px;background:var(--bg);border-bottom:1px solid var(--line)}
.tab{flex:none;display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:999px;border:1px solid var(--line-strong);color:var(--text-2);text-decoration:none;font:600 12px/1.2 var(--sans);white-space:nowrap}
.tab span{font:600 10px/1 var(--mono);padding:2px 6px;border-radius:999px;color:var(--acc)}
.tab.on{border-color:var(--acc);color:var(--text)}.tab-hidden{display:none!important}
.ap,#open-list{scroll-margin-top:64px}
.ag-bar{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin:0 0 10px}.ag-opt{display:inline-flex;gap:6px;align-items:center;font-size:13px;color:var(--text-2)}
.ag-row{display:flex;gap:10px;align-items:flex-start;padding:10px 14px;border-top:1px solid var(--line);cursor:pointer}.ag-row:first-child{border-top:0}.ag-row[hidden]{display:none}
.ag-text{display:flex;flex-direction:column;gap:2px;min-width:0;overflow-wrap:anywhere}#ag-msg{overflow-wrap:anywhere}
.fb{position:relative;display:inline-flex;gap:6px;align-items:center;flex:none}.fb-what{font:11px/1.2 var(--mono);color:var(--quiet)}
.fb-down{position:relative;margin:0 3px;padding:7px;line-height:0;color:var(--quiet)}.fb-down::after{content:"";position:absolute;inset:-8.5px}.fb-down svg{display:block}
.fb-down[aria-pressed="true"]{color:var(--text);border-color:var(--red);background:var(--red-bg)}
.btn.fb-open,.btn.why-link{border-color:transparent;background:none;color:var(--quiet);text-decoration:underline;text-underline-offset:2px}
.fb-pop{position:fixed;z-index:30;display:flex;flex-wrap:wrap;gap:6px;width:min(280px,calc(100vw - 16px));padding:8px;
background:var(--panel);border:1px solid var(--line-strong);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.35)}.fb-pop[hidden]{display:none}
.fb-pop .btn{text-align:left}.fb-pop .btn.ghost{color:var(--quiet)}.fb-why{flex-basis:100%}.fb-r[aria-pressed="true"]{color:var(--text);border-color:var(--text-2)}
.fb-note,.fb-why{font:10px/12px var(--mono);color:var(--quiet)}.fb-part{display:block}
.fb-link{background:none;border:0;padding:0 2px;color:var(--text-2);font:inherit;text-decoration:underline;cursor:pointer}
.sf-main .fb-note{position:absolute;right:0;top:100%;margin-top:2px;white-space:nowrap;text-align:right}
.fb-extra{display:block}.fb-line{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.fb-line:not(:empty){margin-top:9px}
.fb-tell{display:flex;gap:6px;align-items:flex-start;margin-top:6px}
.fb-tell textarea{flex:1;min-width:0;min-height:36px;resize:vertical;font:13px/1.5 var(--sans);padding:6px 8px;background:var(--hover);color:var(--text);border:1px solid var(--line-strong);border-radius:6px}
.draft-fb{margin-top:9px}.fb-promise{margin:0}
.why-box{flex:1 0 100%;width:100%;margin:8px 0 4px;padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--hover);font-size:13px;line-height:1.5;color:var(--text-2)}
.ap-row:has(> .why-box){flex-wrap:wrap}.why-steps{margin:0 0 6px;padding:0;list-style:none}.why-steps li{display:flex;gap:10px;padding:2px 0}
.why-steps time{flex:none;width:9.5em;font:11px/18px var(--mono);color:var(--quiet)}.why-box p{margin:0 0 6px}.why-foot{font:11px/16px var(--mono);color:var(--quiet)}
.btn.why-close{margin-top:4px;color:var(--text-2)}.btn.why-link{position:relative;padding:6px 0;margin-left:4px}.btn.why-link::after{content:"";position:absolute;inset:-4px -6px}.why-head{margin:0 0 4px;font:600 13px/1.4 var(--sans);color:var(--text)}.why-head:focus{outline:none}
@media (max-width:620px){.why-steps li{flex-direction:column;gap:0}.why-steps time{width:auto}}
.toast.err{border-color:var(--red)}.toast{position:fixed;right:16px;bottom:70px;max-width:min(360px,calc(100vw - 32px));padding:10px 14px;border-radius:8px;background:var(--panel);border:1px solid var(--green);font-size:13px;z-index:50}
.mq{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}.mq code{font:11px/1.5 var(--mono);color:var(--text-2);background:var(--hover);border:1px solid var(--line);border-radius:6px;padding:4px 8px;overflow-wrap:anywhere;min-width:0;flex:1}
.mail-h{font:600 13px/1.4 var(--sans);color:var(--text-2);margin:14px 0 4px}
.pr-row{display:flex;flex-direction:column;gap:4px;padding:12px 14px;border-top:1px solid var(--line)}.pr-row:first-child{border-top:0}.pr-row>a{text-decoration:none}
.pr-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.pr-pill{font:600 10px/1 var(--mono);padding:3px 6px;border-radius:4px}
.pr-pill.amber{color:var(--amber);background:var(--amber-bg)}
.ap-unread{border-top:1px solid var(--line);padding:6px 0}.ap-unread summary{cursor:pointer;font:600 13px/1.4 var(--sans);color:var(--text-2);padding:4px 0}
.ap-urow{padding:6px 14px;border-top:1px solid var(--line)}.ap-urow a{color:var(--blue);text-decoration:none}
.disp{font:600 10px/1 var(--mono);border-radius:4px;padding:2px 6px;margin-right:6px;white-space:nowrap}
.d-you{color:var(--green);background:var(--green-bg)}.d-agent{color:var(--blue);background:var(--blue-bg)}
.d-handoff{color:var(--purple);background:var(--purple-bg)}.d-drop{color:var(--red);background:var(--red-bg)}
.sf-effort{flex:none;font:500 10px/1 var(--mono);border-radius:4px;padding:3px 6px;color:var(--blue);background:var(--blue-bg)}
.sf-medium,.sf-large{color:var(--amber);background:var(--amber-bg)}
.sf-why{color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
.sf-row.is-done{opacity:.6}
.draft{display:flex;gap:8px;align-items:flex-start;margin-top:8px}
.draft textarea{flex:1;min-width:0;background:var(--hover);color:var(--text-2);border:1px solid var(--line-strong);border-radius:6px;padding:6px 8px;font:12px/1.5 var(--sans);resize:vertical}
.triage-note{margin:0 0 14px;padding:8px 12px;border-left:3px solid var(--amber);border-radius:4px;background:var(--amber-bg);color:var(--text);overflow-wrap:anywhere}
#start{--acc:var(--c-start)}
.changes{border:1px solid var(--line);border-left:3px solid var(--c-outlook);border-radius:8px;padding:10px 14px;margin:0 0 14px;overflow-wrap:anywhere}
.changes ul{margin:6px 0 0;padding-left:18px}.changes li{margin:3px 0}
.start-list{margin:6px 0 0;padding-left:22px;display:flex;flex-direction:column;gap:10px}.start-list li{color:var(--muted)}
.start-list li[hidden]{display:none}.start-list li>div{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.start-list a{text-decoration:none;overflow-wrap:anywhere}.start-list .sf-mark{margin-left:auto}
.pr-pill.src{color:var(--text-2);background:var(--hover);border:1px solid var(--line)}
#mailbox{--acc:var(--c-outlook)}#mailbox>.chip{margin:0 0 8px}
.mb-sec{border-top:1px solid var(--line);padding:4px 0;scroll-margin-top:64px}
.mb-sec>summary{cursor:pointer;padding:8px 0;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.mb-t{font:600 14px/1.4 var(--sans);color:var(--acc,var(--text))}
.em-acts{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin-top:6px}
.em-status{font:600 11px/1.4 var(--mono);color:var(--amber)}.em-status:empty{display:none}
.em-gone{display:none!important}body.show-em-hidden .em-gone{display:flex!important;opacity:.6}
.em-read .em-readbtn,.em-read .pr-pill.amber{display:none}
.compose{flex:1 0 100%;width:100%;margin:8px 0 4px;display:flex;flex-direction:column;gap:6px}
.ap-row:has(> .compose){flex-wrap:wrap}
.compose textarea{width:100%;min-height:58px;max-height:240px;resize:none;overflow-y:auto;font:13px/1.5 var(--sans);padding:8px;
border-radius:6px;border:1px solid var(--line-strong);background:var(--hover);color:var(--text)}
.compose-btns{display:flex;gap:6px}
.task-progress{display:flex;flex-wrap:wrap;gap:4px 14px;list-style:none;margin:6px 0 0;padding:0;font-size:12px;color:var(--text-2)}
.task-step::before{content:"○ " / ""}.task-step.reached::before{content:"● " / "";color:var(--green)}
.task-step.current{color:var(--blue);font-weight:600}.task-step.current::before{content:"◉ " / ""}.task-step a{color:inherit}
.tab-search{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 14px;color:var(--text-2);font-size:12px}
.tab-search input{width:260px;max-width:100%;background:var(--panel);color:var(--text);border:1px solid var(--line-strong);border-radius:8px;padding:8px 12px;font:inherit}
.tab-search kbd{border:1px solid var(--line-strong);border-radius:4px;padding:0 5px}.q-miss{display:none!important}
.ready{border:1px solid var(--line);border-left:3px solid var(--green);border-radius:8px;padding:10px 14px;margin:0 0 14px;overflow-wrap:anywhere}
.working{border-left-color:var(--blue)}
.ready-list{margin:6px 0 0;padding-left:22px;display:flex;flex-direction:column;gap:10px}
.ready-list li>div{display:flex;flex-wrap:wrap;gap:9px;align-items:center}
.ready-result{display:block;margin-top:9px;color:var(--text-2);font-size:12px;white-space:pre-line}
.ready-result.blocked{color:var(--amber)}.ready-result a{color:var(--blue)}
.ready-check{display:block;margin-top:4px;font-size:12px;color:var(--text-2)}.ready-check.note{color:var(--green)}
.ready-cleared{margin:10px 0 0;padding-left:22px;display:flex;flex-direction:column;gap:8px;opacity:.75}
.ready-cleared:empty{display:none}.ready-cleared li>div{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
@media (max-width:860px){li .btn.why-link,.em-row .btn.why-link{margin-left:6px}li .btn.fb-open,.em-row .btn.fb-open{position:relative;padding:6px 0 6px 4px}li .btn.fb-open::after,.em-row .btn.fb-open::after{content:"";position:absolute;inset:-6px 0}.sf-main{flex-wrap:wrap}.sf-main>.fb{display:contents}.sf-main .fb-down{margin:.5px 0 .5px .5px;padding:6.75px}.sf-main .fb-open,.sf-main .why-link{position:relative;padding:0;margin:-4px 0 0 .5px}.sf-main .why-link{margin-left:4.5px}.sf-main .fb-open::after,.sf-main .why-link::after{content:"";position:absolute;inset:-8px 0}.fb{flex-wrap:wrap}.sf-main .fb-note{position:static;flex-basis:100%;white-space:normal;text-align:left}.sf-link{flex-basis:100%}}
@media (max-width:620px){.fb-pop{width:min(224px,calc(100vw - 16px))}.fb-pop{gap:4px}.fb-pop .btn{padding:5px 7px;font-size:10px}body{padding:20px 16px 40px}.ap-row{flex-direction:column}.ap-group summary{flex-wrap:wrap}
.draft{flex-direction:column}.draft textarea{width:100%}}
"""

JS = r"""
const DAY=864e5;
let MK={}, ST={decisions:{},applied:{},status:{},sent:[],marks:{},refresh:{},slack_held:{}}, API=true, polling=false, sending=false;
ST.queued={};ST.mail={};ST.checks={};ST.feedback={};let GONE=new Set(),qTimer=0;
const CLEARED_FOR=7*DAY,READY_MARK='https://work-queue.local/ready/',MARKED={done:'Marked done',dropped:'Dropped',snoozed:'Snoozed'};
const $=id=>document.getElementById(id);
async function api(path,opts){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts||{}));if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);const o=await r.json();
 if(o&&o.feedback&&o.feedback.recorded===false)LOST=JSON.parse(opts.body).state==='done'?'that':'it';return o}
// An action that worked but whose note couldn't be saved: the next message says so as well as what it says itself, or says it alone.
let LOST='';
function flushLost(){if(LOST)toast('')}
function toast(msg,bad){const lost=LOST;LOST='';if(lost){const said='Assistant couldn’t record '+lost+' for later.';
  msg=msg?msg+(/[.!?…]$/.test(msg)?'':'.')+' '+said:(lost==='that'?'Done. ':'That worked. ')+said}
 document.querySelectorAll('.toast').forEach(t=>t.remove());const t=document.createElement('div');t.className=bad?'toast err':'toast';t.textContent=msg;document.body.append(t);setTimeout(()=>t.remove(),12000)}
function paint(){const now=Date.now(),sent=new Set(ST.sent||[]);let hidden=0;
 document.querySelectorAll('.sf-lane .sf-row,.ag-row,.start-list li:not([data-blocked])').forEach(r=>{if(sent.has(r.dataset.key))r.remove()});
 document.querySelectorAll('.sf-lane .sf-row').forEach(r=>{const v=MK[r.dataset.key],done=!!v&&v.state==='done';
  const hide=!!v&&(v.state==='dropped'||(v.state==='snoozed'&&v.until>now));
  r.classList.toggle('is-done',done);r.classList.toggle('is-hidden',hide);if(hide)hidden++;
  r.querySelector('.sf-mark').textContent=done?'Undo':'Done'});
 hidden+=paintMail(now,sent);
 const away=k=>{const v=MK[k];return !!v&&(v.state!=='snoozed'||v.until>now)};
 document.querySelectorAll('.start-list li').forEach(li=>{li.hidden=!('blocked' in li.dataset)&&away(li.dataset.key)});
 document.querySelectorAll('.ag-row').forEach(r=>{r.hidden=away(r.dataset.key);if(r.hidden)r.querySelector('.ag-pick').checked=false});
 agCount();
 const open=document.querySelectorAll('.sf-lane .sf-row:not(.is-done):not(.is-hidden)').length;
 for(const [s,n] of [['#open-total',open],['#tabs .tab[data-t="open-list"] span',open],
  ['#tabs .tab[data-t="agents"] span',document.querySelectorAll('.ag-row:not([hidden])').length],
  ['#tabs .tab[data-t="start"] span',document.querySelectorAll('.start-list li:not([hidden])').length]]){
  const el=document.querySelector(s);if(el)el.textContent=n}
 document.querySelectorAll('.sf-lane').forEach(l=>{l.querySelector('h3 .count').textContent=l.querySelectorAll('.sf-row:not(.is-done):not(.is-hidden)').length});
 const b=$('showhidden');if(b)b.textContent=(document.body.classList.contains('show-hidden')?'Hide':'Show')+' snoozed and dropped ('+hidden+')'
 renderReady();renderWorking();fbPaint();schedulePoll();const q=$('tab-search');if(q)filterTab(q.value)}
function hides(v,now){return !!v&&(v.state!=='snoozed'||v.until>now)}
function mailWhy(r,now){const ks=[r.dataset.mk,r.dataset.link],q=ks.map(k=>ST.queued[k]).find(Boolean);
 if(q)return 'Sent to an agent as '+q.id;
 if((ST.mail[r.dataset.gkey]||{}).archived)return 'Archived in Gmail';
 const v=ks.map(k=>MK[k]).find(m=>hides(m,now));if(!v)return '';
 return MARKED[v.state]+(v.state==='snoozed'?' until '+new Date(v.until).toLocaleDateString([],{month:'short',day:'numeric'}):'')}
function paintMail(now,sent){let extra=0,gone=0;GONE=new Set();
 document.querySelectorAll('.sf-lane .sf-row[data-mk]').forEach(r=>{const k=r.dataset.mk;if(sent.has(k)){r.remove();return}
  if(hides(MK[k],now)&&!r.classList.contains('is-hidden')){r.classList.add('is-hidden');extra++}});
 document.querySelectorAll('.em-row').forEach(r=>{const why=mailWhy(r,now);r.classList.toggle('em-gone',!!why);
  if(why){GONE.add(r.dataset.key);gone++}
  r.querySelector('.em-status').textContent=why;r.querySelector('.em-undo').hidden=!why||why.startsWith('Sent');
  r.classList.toggle('em-read',!!(ST.mail[r.dataset.gkey]||{}).read)});
 const t=document.querySelector('#tabs .tab[data-t="mailbox"] span'),b=$('em-showhidden');
 if(t)t.textContent=document.querySelectorAll('.em-need:not(.em-gone)').length;
 if(b)b.textContent=(document.body.classList.contains('show-em-hidden')?'Hide':'Show')+' hidden email ('+gone+')';
 return extra}
function toggleMailHidden(){document.body.classList.toggle('show-em-hidden');paint()}
function readyKey(id){return READY_MARK+id}
function renderReady(){const box=$('ready');if(!box)return;const ol=box.querySelector('.ready-list'),cl=box.querySelector('.ready-cleared');
 FBKEEP=fbKeep();ol.textContent='';cl.textContent='';
 const ready=Object.values(ST.queued).filter(q=>(q.status==='done'||q.status==='blocked')&&!MK[readyKey(q.id)]);
 const auto=Object.values(ST.queued).filter(q=>{const m=MK[readyKey(q.id)];return !!m&&!!m.auto&&Date.now()-new Date(m.at)<CLEARED_FOR});
 ready.sort((a,b)=>b.finished.localeCompare(a.finished)).forEach(q=>ol.append(readyItem(q)));
 const open=box.classList.contains('show-cleared'),chip=box.querySelector('.ready-show');
 chip.hidden=!auto.length;chip.textContent=(open?'Hide':'Show')+' cleared ('+auto.length+')';
 if(open)auto.forEach(q=>cl.append(clearedItem(q)));box.hidden=!ready.length&&!auto.length;
 document.querySelectorAll('.start-list li').forEach(li=>{const q=ST.queued[li.dataset.key];
  if(q&&!li.querySelector('.sf-send,.sf-sent')){const b=document.createElement('button');b.className='btn sf-sent';b.disabled=true;
   b.textContent='Sent '+q.id;b.title='Already sent to an agent';li.firstElementChild.append(b)}})}
function readyItem(q){const li=document.createElement('li'),top=document.createElement('div'),t=document.createElement('span'),s=document.createElement('span');
 t.className='sf-title';t.textContent=q.title+' ('+q.id+')';top.append(t,button('Done','btn sf-mark',b=>doneReady(b,q.id)));
 if(q.workspace)top.append(button('Open workspace','btn',b=>openWs(b,q.id)));
 if(FB){const f=document.createElement('span');f.className='fb';f.dataset.fk='result';f.dataset.fp=q.id;f.dataset.fkey=readyKey(q.id);top.append(f)}
 s.className='ready-result '+q.status;li.dataset.id=q.id;
 if(q.status==='blocked')s.textContent='Blocked: '+(q.reason||'the agent didn’t say why.');
 else linkify(s,q.result||'Done, but the agent left no written result. Open '+q.id+' on the Tasks tab of the Assistant dashboard to see what happened.');
 li.append(top,s);const c=(ST.checks||{})[q.id]||{},say=q.status==='blocked'?c.note:c.pending;
 if(say){const n=document.createElement('span');n.className='ready-check'+(c.note?' note':'');n.textContent=say;li.append(n)}
 return li}
function clearedItem(q){const li=document.createElement('li'),top=document.createElement('div'),t=document.createElement('span'),s=document.createElement('span');
 t.className='sf-title';t.textContent=q.title+' ('+q.id+')';top.append(t,button('Undo','btn sf-mark',b=>undoCleared(b,q.id)));
 s.className='ready-check';s.textContent='Cleared on its own: '+MK[readyKey(q.id)].why;li.dataset.id=q.id;li.append(top,s);return li}
function toggleCleared(){$('ready').classList.toggle('show-cleared');renderReady()}
async function undoCleared(b,id){b.disabled=true;
 try{MK=(await api('/api/mark',{method:'POST',body:JSON.stringify({key:readyKey(id),state:'clear'})})).marks;paint();
  toast('It’s back on Ready for you, and it won’t clear on its own again.')}
 catch(e){b.disabled=false;toast('Not saved: '+e.message)}}
function button(label,cls,run){const b=document.createElement('button');b.className=cls;b.textContent=label;b.onclick=()=>run(b);return b}
async function doneReady(b,id){b.disabled=true;
 try{MK=(await api('/api/mark',{method:'POST',body:JSON.stringify({key:readyKey(id),state:'done'})})).marks;paint();flushLost()}
 catch(e){b.disabled=false;toast('Not saved: '+e.message)}}
async function openWs(b,id){b.disabled=true;
 try{await api('/api/focus',{method:'POST',body:JSON.stringify({id})});toast('Brought the workspace for '+id+' to the front.')}
 catch(e){toast('Couldn’t open the workspace: '+e.message)}
 b.disabled=false}
const LINK=/((?:https?|file):\/\/[^\s<>"')]+)/;
function linkify(el,text){el.textContent='';text.split(LINK).forEach((part,i)=>{if(i%2===0){el.append(part);return}
 const m=/^(.*?)([.,;:!?]*)$/.exec(part),a=document.createElement('a');
 a.href=m[1].startsWith('file:')?'/files'+new URL(m[1]).pathname:m[1];a.textContent=m[1];a.target='_blank';a.rel='noopener noreferrer';
 el.append(a,m[2])})}
function schedulePoll(){clearTimeout(qTimer);
 if(!polling&&Object.values(ST.queued).some(q=>!((q.status==='done'||q.status==='blocked')&&MK[readyKey(q.id)])))qTimer=setTimeout(refresh,20000)}
const noteKeys=(go,no)=>ev=>{if(ev.key==='Enter'&&(ev.metaKey||ev.ctrlKey))go.click();else if(ev.key==='Escape')no.click()};
function compose(b,send){const host=b.closest('.em-row,.sf-row,li'),open=host.querySelector(':scope > .compose');
 if(open)return open.querySelector('textarea').focus();
 const f=document.createElement('div'),ta=document.createElement('textarea'),row=document.createElement('div');
 const go=button('Send','btn cp-go',async()=>{go.disabled=true;if(await send(b,ta.value.trim()))f.remove();else go.disabled=false});
 const no=button('Cancel','btn ghost cp-no',()=>f.remove());
 f.className='compose';row.className='compose-btns';ta.rows=2;ta.setAttribute('aria-label','Note for the agent');
 ta.placeholder='Note for the agent (optional). For example: draft a reply, or check the logs first. Cmd+Enter sends, Esc cancels.';
 ta.oninput=()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight+2,240)+'px'};
 ta.onkeydown=noteKeys(go,no);
 row.append(go,no);f.append(ta,row);host.append(f);ta.focus()}
function sentMsg(o,note){if(!o.existing)return 'Sent to an agent as '+o.id+' for a first pass. '+FOLLOW;
 return 'Already sent as '+o.id+', so nothing new was sent.'+(note?' Your note wasn’t added to it. Add it in that agent’s workspace once it starts.':'')+' '+FOLLOW}
function noteSent(k,o,title){ST.queued=Object.assign({},ST.queued,{[k]:{id:o.id,title,status:'open',result:'',reason:'',finished:'',workspace:''}})}
async function sendMail(b,note){const r=b.closest('.em-row'),k=r.dataset.mk;b.disabled=true;
 try{const o=await api('/api/todo',{method:'POST',body:JSON.stringify({key:k,title:r.dataset.qTitle,detail:r.dataset.qDetail,url:r.dataset.link,note})});
  MK[k]={state:'done',until:0};ST.sent=[...new Set(ST.sent),k];noteSent(k,o,r.dataset.qTitle);paint();renderAp();toast(sentMsg(o,note));refresh();return true}
 catch(e){b.disabled=false;toast('Not sent: '+e.message);return false}}
async function markMail(r,marks,msg){
 try{MK=(await api('/api/mark',{method:'POST',body:JSON.stringify({marks})})).marks;paint();renderAp();toast(msg)}
 catch(e){toast('Not saved: '+e.message)}}
function snoozeMail(b){const r=b.closest('.em-row');
 return markMail(r,{[r.dataset.mk]:{state:'snoozed',until:Date.now()+3*DAY}},'Snoozed for 3 days. Show hidden email lists it, with Undo.')}
function undoMail(b){const r=b.closest('.em-row');if((ST.mail[r.dataset.gkey]||{}).archived)return gmailAct(b,'unarchive');
 const marks={};[r.dataset.mk,r.dataset.link].filter(k=>MK[k]).forEach(k=>marks[k]={state:'clear'});return markMail(r,marks,'It’s back on the list.')}
const DID={archive:'Archived in Gmail. Gmail confirmed that no message in the conversation is in your inbox.',
 read:'Marked read in Gmail. Gmail confirmed that every message in the conversation is read.',unarchive:'Moved back to your Gmail inbox.'};
async function gmailAct(b,action){const r=b.closest('.em-row');b.disabled=true;
 try{const o=await api('/api/gmail',{method:'POST',body:JSON.stringify({key:r.dataset.gkey,action})});MK=o.marks;ST.mail=o.mail;paint();renderAp();toast(DID[action])}
 catch(e){toast('Not done: '+e.message+'. Nothing changed on the page.')}
 b.disabled=false}
async function setState(b,state){const k=b.closest('[data-key]').dataset.key,cur=MK[k]&&MK[k].state;
 const body=cur===state?{key:k,state:'clear'}:{key:k,state,until:state==='snoozed'?Date.now()+3*DAY:0};
 try{MK=(await api('/api/mark',{method:'POST',body:JSON.stringify(body)})).marks}catch(e){toast('Not saved: '+e.message)}paint();flushLost()}
if($('showhidden'))$('showhidden').onclick=()=>{document.body.classList.toggle('show-hidden');paint()};
function mailSearch(){const appr=new Set(MAILDATA.filter(m=>(ST.decisions[m.k]||{}).decision==='approve'&&!GONE.has(m.k)).map(m=>m.k));
 if(!appr.size)return{q:'',n:0,manual:[]};const terms=[];
 for(const a of new Set(MAILDATA.map(m=>m.a).filter(Boolean)))terms.push({t:'from:'+a,hit:MAILDATA.filter(m=>m.a===a).map(m=>m.k)});
 for(const s of new Set(MAILDATA.map(m=>m.s.replace(/"/g,'')).filter(Boolean))){const low=s.toLowerCase();terms.push({t:'subject:"'+s+'"',hit:MAILDATA.filter(m=>m.s.replace(/"/g,'').toLowerCase().includes(low)).map(m=>m.k)})}
 const ok=terms.filter(x=>x.hit.every(k=>appr.has(k))),cov=new Set(),pick=[];
 for(;;){let best=null,gain=0;for(const x of ok){const g=x.hit.filter(k=>!cov.has(k)).length;if(g>gain||(g&&g===gain&&x.t.length<best.t.length)){best=x;gain=g}}if(!gain)break;pick.push(best.t);best.hit.forEach(k=>cov.add(k))}
 return{q:pick.join(' OR '),n:cov.size,manual:[...appr].filter(k=>!cov.has(k))}}
function renderMq(MS){const q=$('mq-q');if(!q)return;q.textContent=MS.q||'Approve emails below to build your search.';$('mq-copy').disabled=!MS.q;
 $('mq-note').textContent=MS.q?('Finds the '+MS.n+' emails you approved and nothing else in your inbox as last read. Newer mail from the same sender or with the same subject matches too, so check the results before you delete.'+(MS.manual.length?' '+MS.manual.length+' approved emails share a sender and subject with ones you kept, so delete those by hand.':'')):''}
function copySearch(b){navigator.clipboard.writeText($('mq-q').textContent).then(()=>b.textContent='Copied')}
function copyDraft(b){navigator.clipboard.writeText(b.previousElementSibling.value).then(()=>{b.textContent='Copied';fbDraft(b)})}
function apCounts(){document.querySelectorAll('.ap-group').forEach(g=>{const left=g.querySelectorAll('.ap-row:not(.applied):not(.approved):not(.kept):not(.em-gone):not(.q-miss)').length;const b=g.querySelector('.ap-all');b.textContent=left?'Approve all '+left:'All decided';b.disabled=!left});}
function renderAp(){let approved=0;const MS=mailSearch();
 document.querySelectorAll('.ap-row').forEach(r=>{const k=r.dataset.key,d=(ST.decisions[k]||{}).decision,a=ST.applied[k]||{},kind=r.dataset.kind;
  const done=a.status==='done',settled=done||a.status==='gone';r.classList.toggle('applied',settled);r.classList.toggle('approved',d==='approve'&&!settled);r.classList.toggle('kept',d==='keep');r.classList.toggle('failed',a.status==='error');
  let s='';if(kind==='mail')s=d==='approve'?(MS.manual.includes(k)?'Approved. No search can single this one out, so delete it by hand':'Approved: in your Outlook search'):d==='keep'?'Kept':'';
  else if(done)s=(kind==='unsub'?(a.verified?'Unsubscribed':'Sent, but the sender didn’t confirm'):(kind==='gmail'?'In Gmail Trash':'Marked done in Slack')+(a.verified?', verified':', not verified')+(kind==='gmail'&&a.error?' · Undo not done: '+a.error:'')+(kind==='slack'&&a.undo_error?' · Undo not done: '+a.undo_error:''));
  else if(a.status==='gone')s='Slack no longer has this save';
  else if(a.status==='error')s='Failed: '+a.error;else if(a.status==='undone')s=kind==='gmail'?'Back in your inbox':'Reopened in Slack';
  else if(d==='keep')s='Kept';else if(d==='undo')s=kind==='gmail'?'Will restore':'Will reopen';
  r.querySelector('.ap-status').textContent=s;if(d==='approve'&&!settled&&kind!=='mail'&&!GONE.has(k))approved++});
 renderMq(MS);
 apCounts();
 const undo=Object.entries(ST.decisions).filter(([k,v])=>v.decision==='undo'&&(ST.applied[k]||{}).status==='done').length,run=!!ST.status.running,bar=$('ap-apply');
 if(!bar)return;$('ap-count').textContent=API?(approved+' approved'+(undo?', '+undo+' to reopen':'')):'Approvals need the Work queue server';
 bar.disabled=!API||run||!(approved+undo);bar.textContent=run?'Working…':'Apply approvals';
 const st=ST.status;$('ap-msg').textContent=run?'Working. Slack changes run in your Slack browser session; Gmail changes run in the background.':st.finished?('Last run: '+(st.verified||0)+' of '+((st.slack||0)+(st.gmail||0)+(st.unsub||0))+' verified'+(st.error?' · '+st.error:'')):''}
const FOLLOW='Its status and result show on the Tasks tab of the Assistant dashboard, and its session on the Sessions tab once it starts.';
async function sendRow(r,mode,note){const k=r.dataset.key;
 const o=await api('/api/todo',{method:'POST',body:JSON.stringify({key:k,title:r.dataset.qTitle,detail:r.dataset.qDetail,url:k,mode,note})});
 noteSent(k,o,r.dataset.qTitle);MK[k]={state:'done',until:0};ST.sent=[...(ST.sent||[]),k];paint();return o}
async function sendFrom(b,r,note){b.disabled=true;b.textContent='Sending...';
 try{const o=await sendRow(r,'assist',note);toast(sentMsg(o,note));return true}
 catch(e){b.disabled=false;b.textContent='Send to Agent';toast('Not sent: '+e.message);return false}}
function queueTodo(b,note){return sendFrom(b,b.closest('.sf-row'),note)}
function startSend(b,note){const k=b.closest('li').dataset.key;
 return sendFrom(b,[...document.querySelectorAll('.sf-lane .sf-row')].find(r=>r.dataset.key===k),note)}
function agPicks(){return [...document.querySelectorAll('.ag-row:not([hidden]):not(.q-miss) .ag-pick:checked')].map(c=>c.closest('.ag-row'))}
function agCount(clear){const b=$('ag-go');if(!b)return;const n=agPicks().length;b.textContent='Send '+n+' to agents';b.disabled=!API||!n||sending;if(clear)$('ag-all').checked=false}
function agAll(c){document.querySelectorAll('.ag-pick').forEach(p=>p.checked=c.checked&&!p.closest('.q-miss'));agCount()}
async function agSend(){const rows=agPicks(),n=rows.length,hands=$('ag-hands').checked,msg=$('ag-msg');if(!n)return;
 if(!confirm('Start '+n+' agent'+(n>1?'s':'')+'? '+(hands?'Hands-off: each one finishes the work and posts, files, and opens PRs without asking you.':'Each one takes a first pass and leaves drafts for you to send.')))return;
 sending=true;agCount();const bad=[];let ok=0;
 for(const [i,r] of rows.entries()){msg.textContent='Sending '+(i+1)+' of '+n+'...';
  try{await sendRow(r,hands?'handsoff':'assist');ok++}catch(e){bad.push(r.dataset.qTitle+': '+e.message)}}
 sending=false;
 msg.textContent=[ok?'Sent '+ok+' to agents'+(hands?', hands-off.':' for a first pass.')+' Their status and results show on the Tasks tab of the Assistant dashboard, and each session on the Sessions tab once it starts.':'',
  bad.length?'Not sent: '+bad.join('; ')+'.':''].filter(Boolean).join(' ');agCount()}
function localTime(iso){const d=new Date(iso);return isNaN(d)?'':d.toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function renderRefresh(){const r=ST.refresh||{},b=$('refresh');b.disabled=!!r.running;b.textContent=r.running?'Refreshing…':'Refresh';
 $('refresh-msg').textContent=r.running?'Reading your sources. This page reloads when it finishes.':r.error?'Last refresh failed: '+r.error:'';
 const h=ST.slack_held,el=$('held-msg'),L=h.links||[];el.textContent=h.text||'';
 L.forEach((l,i)=>{const t=localTime(l.at),a=document.createElement('a');a.href=l.url;a.target='_blank';a.rel='noopener';
  a.textContent='Open in Slack'+(t?' (sent '+t+')':'');a.setAttribute('aria-label','Open save '+(i+1)+' of '+L.length+' in Slack'+(t?', sent '+t:''));el.append(i?' · ':' ',a)});
 if(L.length)el.append(h.more?' and '+h.more+' more.':'.')}
async function refresh(){try{ST=Object.assign(ST,await api('/api/state'));API=true;MK=ST.marks||{}}catch(_){API=false}
 const wasRunning=polling;paint();renderAp();renderRefresh();
 if(ST.status.running||(ST.refresh||{}).running){polling=true;setTimeout(refresh,2500)}else{polling=false;if(wasRunning&&(ST.refresh||{}).finished)reloadIfIdle()}}
function reloadIfIdle(){if(Object.keys(WHY).length)return toast('The refresh finished. Close the explanation under “Why is this here?”, then reload to see the new rows.');
 if([...document.querySelectorAll('.compose textarea')].some(t=>t.value.trim()))
  return toast('The refresh finished. Send or cancel your note, then reload the page to see the new rows.');
 const asked=Object.values(FBUI);
 if(asked.some(u=>u.pop))return toast('The refresh finished. Pick a reason or Skip, then reload to see the new rows.');
 if(asked.some(u=>u.tell&&(u.words||'').trim()))return toast('The refresh finished. Send or cancel what you are telling Assistant, then reload to see the new rows.');
 location.reload()}
async function decide(keys,decision){try{ST.decisions=(await api('/api/decide',{method:'POST',body:JSON.stringify({keys,decision})})).decisions;renderAp();flushLost()}catch(e){$('ap-msg').textContent='Not saved: '+e.message}}
function approveGroup(b){const keys=[...b.closest('.ap-group').querySelectorAll('.ap-row:not(.applied):not(.approved):not(.kept):not(.em-gone):not(.q-miss)')].map(r=>r.dataset.key);decide(keys,'approve')}
async function applyNow(){try{await api('/api/apply',{method:'POST',body:'{}'});ST.status={running:true};renderAp();setTimeout(refresh,1500)}catch(e){$('ap-msg').textContent='Not started: '+e.message}}
async function refreshNow(){try{await api('/api/refresh',{method:'POST',body:'{}'});ST.refresh={running:true};renderRefresh();polling=true;setTimeout(refresh,2500)}catch(e){$('refresh-msg').textContent='Not started: '+e.message}}
const FB=!!document.querySelector('.fb[data-fk="page"]'),FBX=new Map(),FBR={row:[['not_important','Not important'],['wrong_order','Wrong order'],['not_mine','Not mine'],['already_done','Already done']],
 draft:[['wrong_tone','Wrong tone'],['wrong_facts','Wrong facts'],['not_how_id_say','Not how I’d say it']],
 result:[['not_what_i_asked','Not what I asked'],['didnt_finish','Didn’t finish'],['should_not_have_started','Should not have been started']],candidate:[]};
FBR.start_here=FBR.email=FBR.row;let FBUI={},FBKEEP=null;
const SAID={not_important:'not important',wrong_order:'wrong order',not_mine:'not mine',already_done:'already done',wrong_tone:'wrong tone',wrong_facts:'wrong facts',
 not_how_id_say:'not how I’d say it',not_what_i_asked:'not what I asked',didnt_finish:'it didn’t finish',should_not_have_started:'should not have been started',bad_draft:'this draft is bad'};
const WHAT={draft:'this draft',result:'this result'};
const THUMB='<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.7a2 2 0 0 0-2 1.7l-1.4 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/></svg>';
function fbBtn(label,id,run){const b=button(label,'btn ghost',run);b.type='button';b.dataset.fb=id;return b}
const PROMISE="Assistant keeps what you tell it. It doesn't use it yet; that's planned for the next update.";
function fbNoteEl(s,v){const k=s.dataset.fk,parts=[];
 if(v.down){const acts=[];if(FBR[k].length)acts.push(v.reason?['Change','change',()=>fbOpen(s)]:['Add a reason','add',()=>fbOpen(s)]);
  acts.push(['Undo','undo',()=>fbUndo(s,v.down)]);parts.push(['You said: '+(SAID[v.reason]||(WHAT[k]||'this')+' is wrong')+'.',acts])}
 if(v.told)parts.push(['You told Assistant: “'+v.told+'”'+(/[.!?…]$/.test(v.told)?'':'.'),[['Undo','undo-told',()=>fbUndo(s,v.told_id)]]]);
 if(!parts.length)return null;
 const n=document.createElement('span');n.className='fb-note';n.title=PROMISE;
 parts.forEach(([t,acts])=>{const p=document.createElement('span');p.className='fb-part';p.append(t+' ');
  acts.forEach(([l,id,f],i)=>{if(i)p.append(' · ');const b=button(l,'fb-link',f);b.type='button';b.dataset.fb=id;p.append(b)});n.append(p)});return n}
function fbBuild(s){const k=s.dataset.fk,h={},x=document.createElement('span'),line=document.createElement('span');
 if(k==='draft'){const w=document.createElement('span');w.className='fb-what';w.textContent='Draft';s.append(w)}
 if(k!=='page'){h.down=fbBtn('','down',()=>fbThumb(s));h.down.classList.add('fb-down');h.down.innerHTML=THUMB;h.what=k==='draft'?'This draft is wrong':k==='result'?'This result is wrong':'This is wrong';s.append(h.down);
  h.pop=document.createElement('div');h.pop.className='fb-pop';h.pop.setAttribute('role','group');h.pop.setAttribute('aria-label','Why is this wrong?');h.pop.hidden=true;
  h.pop.onkeydown=ev=>{if(ev.key==='Escape'){FBUI[s.dataset.fp].pop=false;fbPaint();h.down.focus()}}}
 h.open=fbBtn(k==='page'?'Tell Assistant about this page':k==='draft'?'Tell Assistant about this draft':'Tell Assistant','open',()=>fbTell(s));h.open.classList.add('fb-open');
 s.append(h.open);if(k!=='page'&&k!=='draft'){h.why=fbBtn('Why?','why',()=>whyToggle(s));h.why.classList.remove('ghost');h.why.classList.add('why-link');h.why.setAttribute('aria-expanded','false');
  h.why.setAttribute('aria-label','Why is this here?');h.why.title='Why is this here?';s.append(h.why)}
 if(h.pop)s.append(h.pop);x.className='fb-extra';x.dataset.fp=s.dataset.fp;line.className='fb-line';x.append(line);h.x=x;FBX.set(s,h);const at=s.closest('.ap-row');if(at)at.querySelector('.ap-text').append(x);else s.parentElement.after(x)}
function fbPlace(s){const h=FBX.get(s),p=h.pop,{width:w,height:t}=p.getBoundingClientRect(),r=h.down.getBoundingClientRect(),
 own=s.closest('.sf-row,.ap-row,li')||s.parentElement,reach=Math.max(60,own.getBoundingClientRect().height),
 mine=s.querySelector('.fb-note')||h.x.querySelector('.fb-note'),n=(mine||h.down).getBoundingClientRect(),
 card=(s.closest('.section')||document.body).getBoundingClientRect(),lo=Math.max(8,card.left),hi=Math.min(innerWidth-8,card.right),
 rects=(q,keep)=>[...document.querySelectorAll(q)].filter(keep).map(b=>b.getBoundingClientRect()).filter(b=>b.width&&b.bottom>0&&b.top<innerHeight),
 must=rects('.fb-down,.fb-open',b=>b!==h.open).concat([n]),link=h.open.getBoundingClientRect(),notes=rects('.fb-note',b=>b!==mine),
 btns=rects('.sf-main > .btn,.why-link',()=>true),clamp=x=>Math.max(lo,Math.min(x,hi-w)),
 xs=[...new Set([n.left-8-w,r.left-8-w,r.right-w,lo,hi-w,...btns.filter(b=>Math.abs(b.top-r.top)<160).map(b=>b.left-8-w),
   ...Array.from({length:Math.ceil((hi-lo-w)/24)+1},(_,k)=>lo+24*k)].map(clamp))],
 gap=(x,y)=>Math.hypot(Math.max(r.left-x-w,x-r.right,0),Math.max(r.top-y-t,y-r.bottom,0)),
 clear=(b,x,y,m)=>x+w+m<=b.left||x>=b.right+m||y+t+m<=b.top||y>=b.bottom+m,
 area=(b,x,y)=>Math.max(0,Math.min(x+w,b.right)-Math.max(x,b.left))*Math.max(0,Math.min(y+t,b.bottom)-Math.max(y,b.top)),
 // never over another thumb, another Tell Assistant or its own note; as far as it can be, not over its own link, another note or a button;
 // and always within one row's height of its thumb
 cost=(x,y)=>{const g=gap(x,y),c=Math.hypot(x+w/2-r.left-r.width/2,y+t/2-r.top-r.height/2);return g+(g>reach?1000:0)+c/20+(clear(link,x,y,4)?0:400)+100*notes.filter(b=>!clear(b,x,y,0)).length+btns.reduce((a,b)=>a+area(b,x,y),0)/400};
 let pick=null,low=1e9;for(const x of xs)for(let y=8;y<=innerHeight-t-8;y+=6)if(must.every(b=>clear(b,x,y,4))){const c=cost(x,y);if(c<low){low=c;pick=[x,y]}}
 pick=pick||[clamp(r.right-w),Math.max(8,Math.min(r.bottom+4,innerHeight-t-8))];
 p.style.left=pick[0]+'px';p.style.top=pick[1]+'px'}
function fbPopFill(s,h,v,u){const R=FBR[s.dataset.fk];h.pop.textContent='';
 if(R.length){const t=document.createElement('span');t.className='fb-why';t.textContent='Why is this wrong? Optional';h.pop.append(t)}
 R.forEach(([c,l])=>{const b=fbBtn(l,'r:'+c,()=>fbReason(s,c));b.classList.add('fb-r');b.setAttribute('aria-pressed',v.reason===c);h.pop.append(b)});
 h.pop.append(fbBtn('Skip','skip',()=>{u.pop=false;fbPaint();h.down.focus()}))}
function fbSync(s){const fp=s.dataset.fp,u=FBUI[fp]=FBUI[fp]||{},v=ST.feedback[fp]||{},h=FBX.get(s),open=!!(h.pop&&u.pop===fbWhere(s)&&v.down);
 if(h.down){h.down.setAttribute('aria-pressed',!!v.down);h.down.setAttribute('aria-label',v.down?h.what+'. Press to take it back':h.what);h.down.title=v.down?'Press to take it back':'Tell Assistant this is wrong';h.pop.hidden=!open;
  if(open){const sig=[v.down,v.reason].join('|');if(h.psig!==sig){h.psig=sig;fbPopFill(s,h,v,u)}fbPlace(s)}}
 const sig=JSON.stringify([v.down,v.reason,v.told,v.told_id]);
 if(h.sig!==sig){h.sig=sig;const host=s.parentElement.classList.contains('sf-main')?s:h.x.firstChild,old=host.querySelector(':scope > .fb-note'),n=fbNoteEl(s,v);
  if(old)old.remove();if(n)host.append(n)}
 let box=h.x.querySelector('.fb-tell');
 if(!u.tell){if(box)box.remove();return}
 if(box)return;
 box=document.createElement('div');box.className='fb-tell';const ta=document.createElement('textarea'),go=button('Send','btn fb-go',()=>fbSendTell(s)),
  no=button('Cancel','btn ghost fb-no',()=>{u.tell=false;u.words='';fbPaint()});
 ta.rows=2;ta.value=u.words||'';ta.dataset.fb='text';ta.setAttribute('aria-label','Tell Assistant');ta.placeholder='What should Assistant know about this? Cmd+Enter sends, Esc cancels.';
 ta.oninput=()=>{u.words=ta.value};ta.onkeydown=noteKeys(go,no);
 go.dataset.fb='send';no.dataset.fb='cancel';box.append(ta,go,no);h.x.append(box)}
function fbPaint(){if(!FB)return;document.querySelectorAll('.fb').forEach(s=>{if(!FBX.has(s))fbBuild(s);
  const bar=s.parentElement;if(bar.lastElementChild!==s)bar.append(s);fbSync(s)});whyPaint();
 if(FBKEEP){const k=FBKEEP,el=document.querySelector('#ready [data-fp="'+k.fp+'"] [data-fb="'+k.id+'"]');FBKEEP=null;
  if(el){el.focus();if(k.s!=null)el.setSelectionRange(k.s,k.e)}}}
function fbKeep(){const a=document.activeElement,h=a.closest('#ready [data-fp]');return h?{fp:h.dataset.fp,id:a.dataset.fb,s:a.selectionStart,e:a.selectionEnd}:null}
function fbWhere(s){return(s.closest('[id]')||{}).id||'-'}  // the same item shows in two tabs: the choices open in one
function fbOpen(s){Object.values(FBUI).forEach(u=>u.pop=false);FBUI[s.dataset.fp].pop=fbWhere(s);fbPaint();FBX.get(s).pop.querySelector('button').focus()}
function fbThumb(s){const v=ST.feedback[s.dataset.fp]||{};
 return v.down?fbUndo(s,v.down):fbSend(s,{key:s.dataset.fkey,kind:s.dataset.fk,signal:'thumbs_down'},()=>({pop:false}))}
document.addEventListener('click',ev=>{if(!ev.target.closest('.fb-pop')&&Object.values(FBUI).some(u=>u.pop)){Object.values(FBUI).forEach(u=>u.pop=false);fbPaint()}},true);
for(const e of ['scroll','resize'])window.addEventListener(e,()=>FBX.forEach((h,s)=>{if(h.pop&&!h.pop.hidden)fbPlace(s)}),true);
let WHY={},whyN=0;
function whyTime(sec){const d=new Date(sec*1000);if(isNaN(d))return '';
 const t=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}).replace(/ ([AP])M$/,(m,x)=>' '+x.toLowerCase()+'m');
 return (d.toDateString()===new Date().toDateString()?'Today':d.toLocaleDateString([],{month:'short',day:'numeric'})+',')+' '+t}
function whyToggle(s){const p=s.dataset.fp,u=WHY[p];if(u&&u.where===fbWhere(s))return whyClose(s);
 const mine=WHY[p]={where:fbWhere(s),status:'loading',n:++whyN};whyPaint();FBX.get(s).x.nextElementSibling.querySelector('.why-head').focus();
 api('/api/why?key='+encodeURIComponent(s.dataset.fkey)).then(o=>{if(WHY[p]===mine)Object.assign(mine,{status:'done',data:o})},()=>{if(WHY[p]===mine)mine.status='failed'}).then(whyPaint)}
function whyClose(s){delete WHY[s.dataset.fp];whyPaint();FBX.get(s).why.focus()}
function whyCloseAll(){if(Object.keys(WHY).length){WHY={};whyPaint()}}
function whyFill(body,u){body.textContent='';const add=(tag,text,cls)=>{const e=document.createElement(tag);if(cls)e.className=cls;e.textContent=text;body.append(e);return e};
 if(u.status==='loading')add('p','Loading');
 else if(u.status==='failed')add('p','Assistant couldn’t load this. Close it and try again.');
 else{const o=u.data;if(o.message)add('p',o.message);
  if(o.steps.length){const ol=add('ol','','why-steps');o.steps.forEach(st=>{const li=document.createElement('li'),t=document.createElement('time'),at=whyTime(st.at),last=st.count>1?whyTime(st.last):'';
   if(at){t.dateTime=new Date(st.at*1000).toISOString();t.textContent=at}
   li.append(t,st.text+(last?' Last time: '+last+'.':''));ol.append(li)})}
  if(o.left_out)add('p',o.left_out);if(o.note)add('p',o.note);add('p',o.footer,'why-foot')}}
function whyPaint(){document.querySelectorAll('.fb').forEach(s=>{const h=FBX.get(s);if(!h||!h.why)return;
 const u=WHY[s.dataset.fp],open=!!u&&u.where===fbWhere(s);let box=h.x.nextElementSibling;box=box&&box.classList.contains('why-box')?box:null;h.why.setAttribute('aria-expanded',open);
 if(!open){if(box)box.remove();return}
 if(!box){box=document.createElement('div');box.className='why-box';box.setAttribute('role','region');box.setAttribute('aria-label','Why is this here?');
  const head=document.createElement('h4'),body=document.createElement('div'),c=button('Close','btn ghost why-close',()=>whyClose(s));
  head.className='why-head';head.tabIndex=-1;head.textContent='Why is this here?';c.type='button';box.append(head,body,c);h.x.after(box)}
 const sig=JSON.stringify([u.status,u.data]);if(box.dataset.sig!==sig){box.dataset.sig=sig;whyFill(box.children[1],u)}})}
document.addEventListener('keydown',ev=>{if(ev.key!=='Escape'||!Object.keys(WHY).length||ev.target.id==='tab-search'&&ev.target.value)return;  // Escape in a search with words clears only the search
 if(document.querySelector('.compose,.fb-tell')||Object.values(FBUI).some(u=>u.pop||u.tell))return;  // a note box or the reasons close first
 const s=[...document.querySelectorAll('.fb')].filter(f=>{const u=WHY[f.dataset.fp];return u&&u.where===fbWhere(f)}).sort((a,b)=>WHY[b.dataset.fp].n-WHY[a.dataset.fp].n)[0];
 if(s)whyClose(s)},true);
function fbPost(body){return api('/api/feedback',{method:'POST',body:JSON.stringify(body)})}
async function fbSend(s,body,after){const u=FBUI[s.dataset.fp];if(u.busy)return;u.busy=true;
 let ok=false;try{const o=await fbPost(body);ST.feedback=Object.assign({},ST.feedback,{[s.dataset.fp]:o.view});Object.assign(u,after(o.view));fbPaint();ok=true}
 catch(e){toast('Assistant couldn’t save that. Try again.',true)}
 u.busy=false;return ok}
function fbUndo(s,id){return fbSend(s,{key:s.dataset.fkey,kind:s.dataset.fk,signal:'retract',amends:id},()=>({pop:false}))}
function fbChange(s){const h=FBX.get(s),b=(s.querySelector('.fb-note')||h.x.querySelector('.fb-note')||s).querySelector('[data-fb="change"]');(b||h.down).focus()}  // the panel is gone: focus goes to the note's Change link
function fbReason(s,reason){const v=ST.feedback[s.dataset.fp];
 if(v.reason===reason){FBUI[s.dataset.fp].pop=false;fbPaint();return fbChange(s)}
 return fbSend(s,{key:s.dataset.fkey,kind:s.dataset.fk,signal:'thumbs_down',reason,amends:v.down},()=>({pop:false})).then(ok=>ok&&fbChange(s))}
function fbTell(s){const u=FBUI[s.dataset.fp];u.tell=true;fbPaint();FBX.get(s).x.querySelector('textarea').focus()}
function fbSendTell(s){const u=FBUI[s.dataset.fp],words=(u.words||'').trim();if(!words)return toast('Write something to tell Assistant first.');
 return fbSend(s,{key:s.dataset.fkey,kind:s.dataset.fk,signal:'tell',words},()=>({tell:false,words:''}))}
async function fbDraft(b){const s=b.closest('.sf-row').querySelector('.draft-fb .fb');
 try{await fbPost({key:s.dataset.fkey,kind:'draft',signal:'used_draft'})}catch(e){toast('Copied. Assistant couldn’t record that you used the draft.')}}
function renderWorking(){const box=$('working');if(!box)return;const ol=box.querySelector('.working-list');ol.textContent='';
 Object.values(ST.queued).filter(q=>q.status==='open'||q.status==='in-progress').sort((a,b)=>b.id.localeCompare(a.id,undefined,{numeric:true}))
  .forEach(q=>{const li=document.createElement('li'),top=document.createElement('div'),t=document.createElement('span');
   t.className='sf-title';t.textContent=q.title+' ('+q.id+')';top.append(t);if(q.workspace)top.append(button('Open workspace','btn',b=>openWs(b,q.id)));
   li.dataset.id=q.id;li.append(top,steps(q));ol.append(li)});
 box.hidden=!ol.children.length}
function steps(q){const ol=document.createElement('ol'),d=new Date(q.started),known=!!q.started&&!isNaN(d),pr=/^https?:\/\//.test(q.pr||'');
 const reached=pr?2:q.started||q.workspace?1:0,day=known?' '+d.toLocaleDateString([],{month:'short',day:'numeric'}):'';
 ol.className='task-progress';ol.setAttribute('aria-label','Progress');
 [['Queued','Assistant starts it at its next check.'],
  ['Started'+day,reached?'Assistant started it'+(known?' '+localTime(q.started):'')+(q.workspace?' in '+q.workspace:'')+'. That doesn’t show the session is still working.':''],
  ['PR open',pr?q.pr:''],['Done','The agent marks it done when it records its result.']].forEach(([label,title],i)=>{
  const li=document.createElement('li');li.className='task-step'+(i===reached?' current':i<reached?' reached':'');
  if(i===reached)li.setAttribute('aria-current','step');if(title)li.title=title;
  if(i===2&&pr){const a=document.createElement('a');a.href=q.pr;a.target='_blank';a.rel='noopener noreferrer';a.textContent=label;li.append(a)}
  else li.textContent=label;ol.append(li)});
 return ol}
const Q_ITEMS='.start-list > li, .ready-list > li, .ready-cleared > li, .sf-row, .pr-row, .ap-row, .ag-row',Q_HIT=Q_ITEMS.split(', ').map(x=>x+':not(.q-miss)').join(', ');
function searchText(el){const c=el.cloneNode(true);c.querySelectorAll('button, textarea, input, .compose, .task-progress, .fb, .fb-extra, .why-box').forEach(x=>x.remove());return c.textContent.toLowerCase()}
const hits=c=>[...c.querySelectorAll(Q_HIT)].some(el=>getComputedStyle(el).display!=='none');  // a snoozed or hidden match doesn't count
function filterTab(v){const q=v.trim().toLowerCase();
 document.querySelectorAll(Q_ITEMS).forEach(el=>el.classList.toggle('q-miss',!!q&&!searchText(el).includes(q)));
 const groups=[...document.querySelectorAll('.sf-lane, .sf-group, .ready, details')],hit=new Set(q?groups.filter(hits):[]);
 groups.forEach(c=>{if(!c.matches('details')||c.matches('.ap-group, .mb-sec'))c.classList.toggle('q-miss',!!q&&!hit.has(c))});
 document.querySelectorAll('details').forEach(d=>{if(hit.has(d)){if(!('qWas' in d.dataset))d.dataset.qWas=d.open?'1':'';d.open=true}
  else if('qWas' in d.dataset){d.open=d.dataset.qWas==='1';delete d.dataset.qWas}});
 $('tab-search-empty').hidden=!q||[...document.querySelectorAll(Q_HIT)].some(el=>el.offsetParent!==null);agCount(true);apCounts()}
function clearSearch(){const s=$('tab-search');if(s&&s.value){s.value='';filterTab('')}}
function tabSearchKey(ev){if(ev.key==='Escape'){clearSearch();ev.target.blur()}}
document.addEventListener('keydown',ev=>{const s=$('tab-search');if(ev.key!=='/'||ev.metaKey||ev.ctrlKey||ev.altKey||!s)return;
 if(ev.target.closest('input, textarea, select, [contenteditable]'))return;ev.preventDefault();s.focus()});
const TAB_IDS=[...document.querySelectorAll('#tabs .tab')].map(a=>a.dataset.t).filter(t=>t!=='all');
function tabOf(id){if(TAB_IDS.includes(id)||id==='all')return id;const el=id?$(id):null,sec=el&&TAB_IDS.find(s=>$(s).contains(el));
 if(!sec)return TAB_IDS[0];for(let d=el;d;d=d.parentElement)if(d.tagName==='DETAILS')d.open=true;
 setTimeout(()=>el.scrollIntoView({block:'start'}));return sec}
function showTab(id){const t=tabOf(id);whyCloseAll();clearSearch();TAB_IDS.forEach(s=>$(s).classList.toggle('tab-hidden',t!=='all'&&s!==t));
 document.querySelectorAll('#tabs .tab').forEach(a=>a.classList.toggle('on',a.dataset.t===t));try{localStorage.setItem('wq-tab',t)}catch(_){}}
window.addEventListener('hashchange',()=>showTab(decodeURIComponent(location.hash.slice(1))));
if(TAB_IDS.length){let saved=null;try{saved=localStorage.getItem('wq-tab')}catch(_){}showTab(decodeURIComponent(location.hash.slice(1))||saved||TAB_IDS[0])}
const BUILT=$('built');if(BUILT&&!isNaN(new Date(BUILT.dataset.iso||'x')))BUILT.textContent=new Date(BUILT.dataset.iso)
 .toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
refresh();
"""
