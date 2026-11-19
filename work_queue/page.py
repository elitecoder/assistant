"""Render the Work queue page from one refresh's rows.

Everything on the page comes from your linked sources and the rules in
work_queue/inputs/. Nothing is hand-written: there's no ranked "start here"
list, no drafted replies, and no per-row advice. Every open row has Done,
Snooze, Drop, and Send to Assistant, and you decide which ones to send.
"""
from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime

from work_queue.inputs import gmail, outlook

e = html.escape
NEWTAB = ' target="_blank" rel="noopener noreferrer"'
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


def open_row(title: str, meta: str, link: str, extra: str = "") -> str:
    return (f'<div class="sf-row" data-key="{e(link)}" data-q-title="{e(title)}" data-q-detail="{e(meta + ". Source: " + link)}">'
            f'<div class="sf-main"><a class="sf-link" href="{e(link)}"{NEWTAB}>'
            f'<span class="sf-title">{e(title)}</span><span class="sf-meta">{e(meta)}</span></a>'
            '<button class="btn sf-mark" onclick="setState(this,\'done\')">Done</button>'
            '<button class="btn ghost" onclick="setState(this,\'snoozed\')" title="Hide for 3 days">Snooze</button>'
            '<button class="btn ghost" onclick="setState(this,\'dropped\')">Drop</button>'
            '<button class="btn sf-send" onclick="queueTodo(this)" title="Create an Assistant TODO that starts on its own">Send to Assistant</button>'
            f'</div><div class="q-status" hidden></div>{extra}</div>')


def buttons(undo: str = "ap-undo", ok: str = "Approve") -> str:
    pick = "this.closest('.ap-row').dataset.key"
    return (f'<div class="ap-btns"><button class="btn ap-ok" onclick="decide([{pick}],\'approve\')">{ok}</button>'
            f'<button class="btn ghost ap-keep" onclick="decide([{pick}],\'keep\')">Keep</button>'
            + (f'<button class="btn ghost {undo}" onclick="decide([{pick}],\'{"clear" if undo == "ap-clear" else "undo"}\')">Undo</button>'
               if undo else "") + '</div>')


def ap_row(key: str, title: str, link: str, ctx: str, why: str, kind: str, undo: str = "ap-undo", ev: str = "") -> str:
    evidence = f'<span class="ap-ev">Evidence: {e(ev)}</span>' if ev else ""
    return (f'<div class="ap-row" data-key="{e(key)}" data-kind="{kind}"><div class="ap-text">'
            f'<a href="{e(link)}"{NEWTAB} class="sf-title">{e(title)}</a>{ctx}'
            f'<span class="ap-why"><b>Why it can go:</b> {e(why)}</span>{evidence}<span class="ap-status"></span></div>'
            f'{buttons(undo)}</div>')


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


def mail_info(m: dict) -> str:
    flag = '<span class="pr-pill amber">Unread</span>' if m["unread"] else ""
    return (f'<div class="pr-row"><a class="sf-title" href="{e(m["link"])}"{NEWTAB}>{e(m["title"])}</a>'
            f'<div class="pr-meta">{flag}</div>{mail_ctx(m)}<span class="ap-why"><b>Why:</b> {e(m["reason"])}</span></div>')


def section(sid: str, title: str, meta: str, body: str) -> str:
    return f'<div class="ap" id="{sid}"><div class="cut-top"><b>{title}</b><span class="meta">{meta}</span></div>{body}</div>'


def lane(title: str, help_text: str, rows: list[str]) -> str:
    return (f'<div class="sf-lane"><h3>{title} <span class="count">{len(rows)}</span></h3><p class="meta">{help_text}</p>'
            f'<div class="sf-list">{"".join(rows)}</div></div>')


def render(snap: dict) -> tuple[str, dict]:
    """The page and the approval candidates the server acts on."""
    cands: dict[str, dict] = {}
    lanes, sections, tabs = [], [], []

    slack_rows = snap.get("slack") or []
    open_slack = [open_row(r["title"], " · ".join(x for x in (r["who"], r["when"]) if x), r["link"], ctx_html(r["ctx"]))
                  for r in slack_rows if r["group"] == "open"]
    if snap.get("slack") is not None:
        lanes.append(lane("Slack", "Later saves whose thread moved recently.", open_slack))
    if snap.get("github") is not None:
        lanes.append(lane("Pull requests", "Open pull requests that ask for your review.",
                          [open_row(p["title"], p["meta"] + (" · draft" if p["draft"] else ""), p["link"]) for p in snap["github"]]))
    acts = [m for m in snap.get("outlook") or [] if m["group"] == "act"]
    if snap.get("outlook") is not None:
        lanes.append(lane("Outlook", "Mail sent to you by a person that you haven't replied to.",
                          [open_row(m["title"], f'{m["from"]} · {m["day"]}', m["link"], mail_ctx(m)) for m in acts]))
    gmail_by = snap.get("gmail") or {}
    for account, rows in gmail_by.items():
        lanes.append(lane(f"Gmail: {e(account)}", "Mail from a person that you haven't opened, or that you starred.",
                          [open_row(m["title"], f'{m["from"]} · {m["day"]}', m["link"], mail_ctx(m))
                           for m in rows if m["group"] == "act"]))
    total = sum(body.count('class="sf-row"') for body in lanes)
    sections.append(f'<div id="open-list" class="tab-sec"><div class="cut"><div class="cut-top"><b>Open list</b>'
                    f'<span class="meta">{total} open items. Mark each Done, Snooze it for 3 days, Drop it, '
                    'or send it to Assistant to finish on its own.</span></div>'
                    '<button class="chip" id="showhidden">Show snoozed and dropped</button></div>'
                    f'{"".join(lanes) or "<p class=meta>No sources are linked yet.</p>"}</div>')
    tabs.append(("open-list", "Open list", total, "var(--c-cut)"))

    if snap.get("calendar"):
        cal_rows = "".join(f'<div class="pr-row"><a class="sf-title" href="{e(c["link"])}"{NEWTAB}>{e(c["when"])} · {e(c["title"])}</a>'
                           f'<span class="meta">{e(c["account"])}{" · you declined" if c["declined"] else ""}</span></div>'
                           for c in snap["calendar"])
        sections.append(section("calendar", "Today", f'{len(snap["calendar"])} events on your calendars today.',
                                f'<div class="sf-list">{cal_rows}</div>'))
        tabs.append(("calendar", "Today", len(snap["calendar"]), "var(--c-start)"))

    mail = snap.get("outlook") or []
    if snap.get("outlook") is not None:
        by = {g: [m for m in mail if m["group"] == g and m["body"]] for g in outlook.DELETABLE}
        for rows in by.values():
            cands.update({m["key"]: {"kind": "mail", "link": m["link"]} for m in rows})
        groups = "".join(ap_group(f"mail-{g}", t, h, [ap_row(m["key"], m["subject"], m["link"], mail_ctx(m), m["reason"], "mail", "ap-clear")
                                                      for m in by[g]]) for g, t, h in OUTLOOK_GROUPS if by[g])
        reads = [m for m in mail if m["group"] == "read"]
        sections.append(section("email", "Outlook", f"{len(mail)} emails in your inbox. {len(acts)} need you, "
                                f"{len(reads)} are worth reading, and {sum(map(len, by.values()))} can go.",
                                f'<h4 class="mail-h">Worth reading <span class="count">{len(reads)}</span></h4>'
                                f'<div class="sf-list">{"".join(mail_info(m) for m in reads)}</div>'
                                '<h4 class="mail-h">Ready to delete</h4><p class="meta">The Work queue only reads Outlook. '
                                'Approve what can go, and this search updates to find exactly those emails. Paste it into '
                                'Outlook search with the scope set to Inbox, select all, and delete.</p>'
                                '<div class="mq"><code id="mq-q">Approve emails below to build your search.</code>'
                                '<button class="btn" id="mq-copy" disabled onclick="copySearch(this)">Copy search</button></div>'
                                f'<p class="meta" id="mq-note"></p>{groups}'))
        tabs.append(("email", "Outlook", len(acts), "var(--c-outlook)"))

    unsub: dict[tuple, list[dict]] = {}
    for n, (account, rows) in enumerate(gmail_by.items(), 1):
        keep = {m["addr"] for m in rows if m["group"] not in gmail.DELETABLE}
        for m in rows:
            if m["group"] in gmail.DELETABLE and m["addr"] not in keep and m["addr"] != account.lower():
                unsub.setdefault((account, m["addr"], m["from"]), []).append(m)
        by = {g: [m for m in rows if m["group"] == g] for g in gmail.DELETABLE}
        for g_rows in by.values():
            cands.update({m["key"]: {"kind": "gmail", "account": account, "msg_ids": m["msg_ids"], "link": m["link"]} for m in g_rows})
        groups = "".join(ap_group(f"gmail-{g}", t, h, [ap_row(m["key"], m["subject"], m["link"], mail_ctx(m), m["reason"], "gmail")
                                                       for m in by[g]]) for g, t, h in GMAIL_GROUPS if by[g])
        looks = [m for m in rows if m["group"] == "unsorted"]
        n_del = sum(map(len, by.values()))
        sections.append(section(f"gmail-{n}", f"Gmail: {e(account)}", f"{len(rows)} conversations. {len(looks)} are worth a look, "
                                f"and {n_del} can go.",
                                f'<h4 class="mail-h">Worth a look <span class="count">{len(looks)}</span></h4>'
                                f'<div class="sf-list">{"".join(mail_info(m) for m in looks)}</div>'
                                '<h4 class="mail-h">Ready to delete</h4><p class="meta">Approve what can go, then select Apply approvals. '
                                'Each conversation moves to Gmail\'s Trash, which keeps it for 30 days. Undo moves it back to your inbox.</p>'
                                f'{groups}'))
        tabs.append((f"gmail-{n}", f"Gmail: {account.split('@')[0]}", sum(m["group"] == "act" for m in rows), "var(--c-gmail)"))

    subs = []
    for (account, addr, name), ms in unsub.items():
        url = next((m["unsub_url"] for m in ms if m["unsub_url"].startswith("https://")), "")
        if url:
            key = "unsub:" + hashlib.sha1(f"{account}:{addr}:{name}".encode()).hexdigest()[:12]
            cands[key] = {"kind": "unsub", "url": url, "link": ms[0]["link"]}
            subs.append(f'<div class="ap-row" data-key="{e(key)}" data-kind="unsub"><div class="ap-text">'
                        f'<a href="{e(ms[0]["link"])}"{NEWTAB} class="sf-title">{e(name)}</a>'
                        f'<span class="meta">{e(addr)} · {e(account)}</span><span class="ap-why">{len(ms)} of their emails '
                        f'can go. Latest: {e(ms[0]["subject"][:80])}</span><span class="ap-status"></span></div>{buttons("", "Unsubscribe")}</div>')
    if subs:
        sections.append(section("unsubscribe", "Unsubscribe", f"{len(subs)} senders support one-click unsubscribe. Approve a "
                                "sender, then select Apply approvals, and the Work queue sends that sender's unsubscribe request. "
                                "You can't undo this here. Senders that also send mail you keep are left out.",
                                ap_group("unsub", "Senders", "", subs, True)))
        tabs.append(("unsubscribe", "Unsubscribe", len(subs), "var(--c-unsub)"))

    if snap.get("slack") is not None:
        by = {g: [r for r in slack_rows if r["group"] == g] for g, _, _ in SLACK_GROUPS}
        for rows in by.values():
            cands.update({r["key"]: {"kind": "slack", "cid": r["cid"], "ts": r["ts"], "link": r["link"]} for r in rows})
        unread = [r for r in slack_rows if r["group"] == "unread"]
        groups = "".join(ap_group(g, t, h, [ap_row(r["key"], r["title"], r["link"], ctx_html(r["ctx"]), r["reason"], "slack",
                                                   ev=r["evidence"]) for r in by[g]]) for g, t, h in SLACK_GROUPS if by[g])
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
           f'<div class="sf-head"><h2>Work queue</h2><p class="meta">Built {e(snap["built"])} from your linked sources. '
           'Rules sort every row; nothing is sent without you.</p>'
           '<button class="btn" id="refresh" onclick="refreshNow()">Refresh</button><span class="meta" id="refresh-msg"></span></div>'
           f'<ul class="sources">{status}</ul>{tabs_html}{"".join(sections)}{bar}'
           f'</div><script>const MAILDATA={maildata};{JS}</script></body></html>')
    return doc, cands


def placeholder(message: str) -> str:
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Work queue</title><style>{CSS}</style><body><div class="section"><div class="sf-head"><h2>Work queue</h2>'
            f'<p class="meta">{e(message)}</p><button class="btn" id="refresh" onclick="refreshNow()">Refresh</button>'
            f'<span class="meta" id="refresh-msg"></span></div></div><script>const MAILDATA=[];{JS}</script></body></html>')


CSS = """
:root{--bg:#0a0a0d;--panel:#13141a;--line:rgba(255,255,255,0.06);--line-strong:rgba(255,255,255,0.10);--hover:rgba(255,255,255,0.025);
--text:#ededf0;--text-2:#c9cad1;--muted:#7c7e8a;--green:#5fd97a;--green-bg:rgba(95,217,122,0.12);--amber:#f0b330;--amber-bg:rgba(240,179,48,0.12);
--blue:#6bbef0;--red:#e96b6b;--red-bg:rgba(233,107,107,0.12);
--c-unsub:#4fd1c5;--c-start:#f0b330;--c-outlook:#6bbef0;--c-gmail:#f0846b;--c-slack:#b06bf0;--c-cut:#e96be9;
--sans:-apple-system,BlinkMacSystemFont,"Inter","SF Pro Text",system-ui,"Segoe UI",Roboto,sans-serif;
--mono:"Geist Mono","JetBrains Mono","SF Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:light){:root{--bg:#fafafb;--panel:#ffffff;--line:rgba(0,0,0,0.08);--line-strong:rgba(0,0,0,0.14);--hover:rgba(0,0,0,0.03);
--text:#16171c;--text-2:#3a3c45;--muted:#6b6d78}}
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
.sf-title{color:var(--text);overflow-wrap:anywhere}.sf-meta{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
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
.q-status{margin:6px 0 2px;padding:6px 10px;border-left:3px solid var(--c-unsub);border-radius:4px;font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
.q-status[hidden]{display:none}.q-status.done{border-color:var(--green);color:var(--text)}.q-status.blocked{border-color:var(--c-gmail)}.q-status a{color:inherit;text-decoration:underline}
.toast{position:fixed;right:16px;bottom:70px;max-width:min(360px,calc(100vw - 32px));padding:10px 14px;border-radius:8px;background:var(--panel);border:1px solid var(--green);font-size:13px;z-index:50}
.mq{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}.mq code{font:11px/1.5 var(--mono);color:var(--text-2);background:var(--hover);border:1px solid var(--line);border-radius:6px;padding:4px 8px;overflow-wrap:anywhere;min-width:0;flex:1}
.mail-h{font:600 13px/1.4 var(--sans);color:var(--text-2);margin:14px 0 4px}
.pr-row{display:flex;flex-direction:column;gap:4px;padding:12px 14px;border-top:1px solid var(--line)}.pr-row:first-child{border-top:0}.pr-row>a{text-decoration:none}
.pr-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.pr-pill{font:600 10px/1 var(--mono);padding:3px 6px;border-radius:4px}
.pr-pill.amber{color:var(--amber);background:var(--amber-bg)}
.ap-unread{border-top:1px solid var(--line);padding:6px 0}.ap-unread summary{cursor:pointer;font:600 13px/1.4 var(--sans);color:var(--text-2);padding:4px 0}
.ap-urow{padding:6px 14px;border-top:1px solid var(--line)}.ap-urow a{color:var(--blue);text-decoration:none}
@media (max-width:620px){body{padding:20px 16px 40px}.sf-main{flex-wrap:wrap}.sf-link{flex-basis:100%}.ap-row{flex-direction:column}.ap-group summary{flex-wrap:wrap}}
"""

JS = r"""
const DAY=864e5;
let MK={}, ST={decisions:{},applied:{},status:{},queued:{},marks:{},refresh:{}}, API=true, QPREV=null, qTimer=null, polling=false;
const $=id=>document.getElementById(id);
async function api(path,opts){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts||{}));if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);return r.json()}
function toast(msg){const t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.append(t);setTimeout(()=>t.remove(),12000)}
function paint(){const now=Date.now();let hidden=0;
 document.querySelectorAll('.sf-lane .sf-row').forEach(r=>{const v=MK[r.dataset.key];
  const done=!!v&&v.state==='done', hide=!!v&&(v.state==='dropped'||(v.state==='snoozed'&&v.until>now));
  r.classList.toggle('is-done',done);r.classList.toggle('is-hidden',hide);if(hide)hidden++;
  r.querySelector('.sf-mark').textContent=done?'Undo':'Done'});
 document.querySelectorAll('.sf-lane').forEach(l=>{l.querySelector('h3 .count').textContent=l.querySelectorAll('.sf-row:not(.is-done):not(.is-hidden)').length});
 const b=$('showhidden');if(b)b.textContent=(document.body.classList.contains('show-hidden')?'Hide':'Show')+' snoozed and dropped ('+hidden+')'}
async function setState(b,state){const k=b.closest('.sf-row').dataset.key,cur=MK[k]&&MK[k].state;
 const body=cur===state?{key:k,state:'clear'}:{key:k,state,until:state==='snoozed'?Date.now()+3*DAY:0};
 try{MK=(await api('/api/mark',{method:'POST',body:JSON.stringify(body)})).marks}catch(e){toast('Not saved: '+e.message)}paint()}
if($('showhidden'))$('showhidden').onclick=()=>{document.body.classList.toggle('show-hidden');paint()};
function mailSearch(){const appr=new Set(MAILDATA.filter(m=>(ST.decisions[m.k]||{}).decision==='approve').map(m=>m.k));
 if(!appr.size)return{q:'',n:0,manual:[]};const terms=[];
 for(const a of new Set(MAILDATA.map(m=>m.a).filter(Boolean)))terms.push({t:'from:'+a,hit:MAILDATA.filter(m=>m.a===a).map(m=>m.k)});
 for(const s of new Set(MAILDATA.map(m=>m.s.replace(/"/g,'')).filter(Boolean))){const low=s.toLowerCase();terms.push({t:'subject:"'+s+'"',hit:MAILDATA.filter(m=>m.s.replace(/"/g,'').toLowerCase().includes(low)).map(m=>m.k)})}
 const ok=terms.filter(x=>x.hit.every(k=>appr.has(k))),cov=new Set(),pick=[];
 for(;;){let best=null,gain=0;for(const x of ok){const g=x.hit.filter(k=>!cov.has(k)).length;if(g>gain||(g&&g===gain&&x.t.length<best.t.length)){best=x;gain=g}}if(!gain)break;pick.push(best.t);best.hit.forEach(k=>cov.add(k))}
 return{q:pick.join(' OR '),n:cov.size,manual:[...appr].filter(k=>!cov.has(k))}}
function renderMq(MS){const q=$('mq-q');if(!q)return;q.textContent=MS.q||'Approve emails below to build your search.';$('mq-copy').disabled=!MS.q;
 $('mq-note').textContent=MS.q?('Finds the '+MS.n+' emails you approved and nothing else in your inbox as last read. Newer mail from the same sender or with the same subject matches too, so check the results before you delete.'+(MS.manual.length?' '+MS.manual.length+' approved emails share a sender and subject with ones you kept, so delete those by hand.':'')):''}
function copySearch(b){navigator.clipboard.writeText($('mq-q').textContent).then(()=>b.textContent='Copied')}
function renderAp(){let approved=0;const MS=mailSearch();
 document.querySelectorAll('.ap-row').forEach(r=>{const k=r.dataset.key,d=(ST.decisions[k]||{}).decision,a=ST.applied[k]||{},kind=r.dataset.kind;
  const done=a.status==='done';r.classList.toggle('applied',done);r.classList.toggle('approved',d==='approve'&&!done);r.classList.toggle('kept',d==='keep');r.classList.toggle('failed',a.status==='error');
  let s='';if(kind==='mail')s=d==='approve'?(MS.manual.includes(k)?'Approved. No search can single this one out, so delete it by hand':'Approved: in your Outlook search'):d==='keep'?'Kept':'';
  else if(done)s=(kind==='unsub'?(a.verified?'Unsubscribed':'Sent, but the sender didn’t confirm'):(kind==='gmail'?'In Gmail Trash':'Marked done in Slack')+(a.verified?', verified':', not verified'));
  else if(a.status==='error')s='Failed: '+a.error;else if(a.status==='undone')s=kind==='gmail'?'Back in your inbox':'Reopened in Slack';
  else if(d==='keep')s='Kept';else if(d==='undo')s=kind==='gmail'?'Will restore':'Will reopen';
  r.querySelector('.ap-status').textContent=s;if(d==='approve'&&!done&&kind!=='mail')approved++});
 renderMq(MS);
 document.querySelectorAll('.ap-group').forEach(g=>{const left=g.querySelectorAll('.ap-row:not(.applied):not(.approved):not(.kept)').length;const b=g.querySelector('.ap-all');b.textContent=left?'Approve all '+left:'All decided';b.disabled=!left});
 const undo=Object.entries(ST.decisions).filter(([k,v])=>v.decision==='undo'&&(ST.applied[k]||{}).status==='done').length,run=!!ST.status.running,bar=$('ap-apply');
 if(!bar)return;$('ap-count').textContent=API?(approved+' approved'+(undo?', '+undo+' to reopen':'')):'Approvals need the Work queue server';
 bar.disabled=!API||run||!(approved+undo);bar.textContent=run?'Working…':'Apply approvals';
 const st=ST.status;$('ap-msg').textContent=run?'Working. Slack changes run in your Slack browser session; Gmail changes run in the background.':st.finished?('Last run: '+(st.verified||0)+' of '+((st.slack||0)+(st.gmail||0)+(st.unsub||0))+' verified'+(st.error?' · '+st.error:'')):''}
function linkify(el,text){el.textContent='';text.split(/(https?:\/\/[^\s)]+)/).forEach(p=>{if(/^https?:\/\//.test(p)){const a=document.createElement('a');a.href=p;a.textContent=p;a.target='_blank';a.rel='noopener noreferrer';el.append(a)}else el.append(p)})}
function renderQueued(){const Q=ST.queued||{},seen={};
 document.querySelectorAll('.sf-row[data-q-title]').forEach(r=>{const q=Q[r.dataset.key],b=r.querySelector('.sf-send'),s=r.querySelector('.q-status');
  if(!q||q.status==='removed'){s.hidden=!q;if(q)s.textContent='The TODO '+q.id+' was removed. Select Send to Assistant to queue it again.';s.className='q-status';b.disabled=false;b.textContent='Send to Assistant';return}
  seen[r.dataset.key]=q.status;b.disabled=true;b.textContent='Sent '+q.id;s.hidden=false;
  const done=q.status==='done';s.className='q-status '+(done?'done':q.status==='blocked'?'blocked':'');
  if(done)linkify(s,q.result?'Done. '+q.result:'Done, but the agent left no written result. Open '+q.id+' on the Assistant dashboard to see what happened.');
  else if(q.status==='blocked')s.textContent='Blocked'+(q.reason?': '+q.reason:'.');
  else if(q.status==='in-progress'||q.dispatched)s.textContent='Working on it ('+q.id+').';
  else s.textContent='Queued as '+q.id+'. Assistant picks it up within about 5 minutes.';
  if(QPREV&&QPREV[r.dataset.key]&&QPREV[r.dataset.key]!=='done'&&done)toast('Finished: '+r.querySelector('.sf-title').textContent)});
 QPREV=seen;clearTimeout(qTimer);if(Object.values(seen).some(v=>v!=='done'))qTimer=setTimeout(refresh,20000)}
async function queueTodo(b){const r=b.closest('.sf-row');b.disabled=true;b.textContent='Sending...';
 try{const o=await api('/api/todo',{method:'POST',body:JSON.stringify({key:r.dataset.key,title:r.dataset.qTitle,detail:r.dataset.qDetail,url:r.dataset.key})});ST.queued=Object.assign({},ST.queued,{[r.dataset.key]:{id:o.id,status:'open'}});renderQueued()}
 catch(e){b.disabled=false;b.textContent='Send to Assistant';const s=r.querySelector('.q-status');s.hidden=false;s.className='q-status blocked';s.textContent='Not sent: '+e.message}}
function renderRefresh(){const r=ST.refresh||{},b=$('refresh');b.disabled=!!r.running;b.textContent=r.running?'Refreshing…':'Refresh';
 $('refresh-msg').textContent=r.running?'Reading your sources. This page reloads when it finishes.':r.error?'Last refresh failed: '+r.error:''}
async function refresh(){try{ST=Object.assign(ST,await api('/api/state'));API=true;MK=ST.marks||{}}catch(_){API=false}
 const wasRunning=polling;paint();renderAp();renderQueued();renderRefresh();
 if(ST.status.running||(ST.refresh||{}).running){polling=true;setTimeout(refresh,2500)}else{polling=false;if(wasRunning&&(ST.refresh||{}).finished)location.reload()}}
async function decide(keys,decision){try{ST.decisions=(await api('/api/decide',{method:'POST',body:JSON.stringify({keys,decision})})).decisions;renderAp()}catch(e){$('ap-msg').textContent='Not saved: '+e.message}}
function approveGroup(b){const keys=[...b.closest('.ap-group').querySelectorAll('.ap-row:not(.applied):not(.approved):not(.kept)')].map(r=>r.dataset.key);decide(keys,'approve')}
async function applyNow(){try{await api('/api/apply',{method:'POST',body:'{}'});ST.status={running:true};renderAp();setTimeout(refresh,1500)}catch(e){$('ap-msg').textContent='Not started: '+e.message}}
async function refreshNow(){try{await api('/api/refresh',{method:'POST',body:'{}'});ST.refresh={running:true};renderRefresh();polling=true;setTimeout(refresh,2500)}catch(e){$('refresh-msg').textContent='Not started: '+e.message}}
const TAB_IDS=[...document.querySelectorAll('#tabs .tab')].map(a=>a.dataset.t).filter(t=>t!=='all');
function showTab(id){const t=TAB_IDS.includes(id)||id==='all'?id:'open-list';TAB_IDS.forEach(s=>$(s).classList.toggle('tab-hidden',t!=='all'&&s!==t));
 document.querySelectorAll('#tabs .tab').forEach(a=>a.classList.toggle('on',a.dataset.t===t));try{localStorage.setItem('wq-tab',t)}catch(_){}}
window.addEventListener('hashchange',()=>showTab(decodeURIComponent(location.hash.slice(1))));
if(TAB_IDS.length){let saved=null;try{saved=localStorage.getItem('wq-tab')}catch(_){}showTab(decodeURIComponent(location.hash.slice(1))||saved||'open-list')}
refresh();
"""
