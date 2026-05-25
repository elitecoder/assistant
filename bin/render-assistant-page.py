#!/usr/bin/env python3
"""
render-assistant-page.py — single Renderer.

Reads ~/.claude/cache/world.json (Scanner output) and emits
~/.claude/assistant-dashboard.html with two tabs:
  [Decisions]  awaiting cards · activity feed · live sessions · direct talk
  [TODOs]      P0/P1 / P2/P3 / completed sections

Replaces render-decisions-dashboard.py + render-todo.py. One LaunchAgent.
Tab switch is client-side (URL hash). Auto-refresh meta-tag every 15s.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from html import escape as e
from pathlib import Path

HOME = Path(os.environ["HOME"])
WORLD_PATH = HOME / ".claude/cache/world.json"
ASSISTANT_STATE = HOME / ".claude/cache/assistant-state.json"
LEGACY_TRIAGE_STATE = HOME / ".claude/cache/triage-state.json"
DASHBOARD_HTML = HOME / ".claude/assistant-dashboard.html"
TODO_HTML = HOME / ".claude/assistant-todo.html"  # legacy redirect

ACTIVITY_HOURS = 24
FEED_LIMIT = 30  # was 80 — kept overflowing the page
NOISE_EVENT_KINDS = {"pulse-rollup", "heartbeat", "tick", "noop", "worker-heartbeat-stale"}
LIVE_SESSIONS_LIMIT = 20


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def age_str(seconds):
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 0:
        return "future"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def shorten_cwd(cwd, max_len=38):
    s = (cwd or "").replace("/Users/mukuls/", "~/")
    if len(s) > max_len:
        s = "…" + s[-(max_len - 1):]
    return s


def first_ws_ref(touches):
    for t in touches or []:
        if isinstance(t, str):
            ref = t
        elif isinstance(t, dict):
            ref = t.get("ref", "")
        else:
            ref = ""
        if isinstance(ref, str) and ref.startswith("workspace:"):
            return ref
    return None


def load_assistant_state():
    """Read the Assistant's pulse output. Falls back to the legacy
    `triage-state.json` path if the new file isn't there yet — until the live
    Assistant has run one pulse on the renamed prompt, the old path may still
    be the only one populated."""
    for path in (ASSISTANT_STATE, LEGACY_TRIAGE_STATE):
        try:
            return json.loads(path.read_text())
        except Exception:
            continue
    return {}


def render_awaiting(world):
    """Awaiting cards now come from Assistant's `awaiting_input[]` directly.
    No proposals dir, no Evaluator, no fuse. If the Assistant put it there, surface it.
    Sorted by confidence desc."""
    ts = load_assistant_state()
    awaiting = list(ts.get("awaiting_input") or [])
    awaiting.sort(key=lambda a: a.get("confidence") or 0, reverse=True)
    if not awaiting:
        return '<div class="empty">No decisions awaiting your input.</div>', 0
    cards = []
    for a in awaiting:
        tier = (a.get("tier") or "T3").upper()
        tier_lower = tier.lower()
        scope = ""
        for t in a.get("touches") or []:
            # tolerate {ref, name, type} dicts AND bare ref strings (newer
            # Assistant prompt sometimes emits the latter)
            if isinstance(t, str):
                scope = t
            elif isinstance(t, dict):
                scope = t.get("name") or t.get("ref") or ""
            if scope:
                break
        ws_ref = first_ws_ref(a.get("touches"))
        button = ""
        if ws_ref:
            button = (
                f'<div class="buttons">'
                f'<button class="btn" data-ws="{e(ws_ref)}" onclick="openWs(this)">'
                f'Open {e(ws_ref)}</button></div>'
            )
        alts = a.get("alt_actions") or []
        alts_html = ""
        if alts:
            alts_html = '<div class="alts">Alts: ' + " · ".join(
                f"<code>{e(s[:120])}</code>" for s in alts[:4]
            ) + "</div>"
        conf = a.get("confidence") or 0
        cards.append(f"""
<div class="card {tier_lower}">
  <div class="pills">
    <span class="pill {tier_lower}">{tier}</span>
    <span class="pill scope">{e(scope)}</span>
    <span class="age">conf {conf:.2f}</span>
  </div>
  <div class="action">{e(a.get('title', ''))}</div>
  <div class="reason">{e(a.get('detail', ''))}</div>
  {alts_html}
  {button}
</div>""")
    return "".join(cards), len(awaiting)


def render_decisions(world):
    """Decisions feed — read from the durable action ledger at
    ~/.assistant/actions-ledger.jsonl. Shows ONLY state-changing decisions
    (the Assistant did something, didn't just observe). Newest first.

    Bookkeeping kinds (heartbeat, observer-cache writes, no-op pulses) are
    filtered out. Each row is one decision: what was decided, against what,
    why (evidence), how it turned out.
    """
    LEDGER_PATH = HOME / ".assistant/actions-ledger.jsonl"
    DECISION_KINDS = {
        "dispatch", "status-flip", "cleanup", "close-workspace",
        "merge-pr", "nudge", "emit-card", "purge-awaiting",
    }
    # Bookkeeping/internal kinds we explicitly DON'T show
    SKIP_KINDS = {"test", "heartbeat", "observer-write", "summary-update"}

    rows = []
    if LEDGER_PATH.exists():
        # Read tail — last ~500 entries is plenty for a 24h window
        try:
            with open(LEDGER_PATH) as f:
                lines = f.readlines()[-500:]
        except Exception:
            lines = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ACTIVITY_HOURS)
        for line in lines:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = parse_iso(d.get("ts"))
            if not ts or ts < cutoff:
                continue
            kind = d.get("kind", "")
            if kind in SKIP_KINDS:
                continue
            if DECISION_KINDS and kind not in DECISION_KINDS:
                # Unknown kind — show it anyway in case it's a new decision
                # type, but tag T?. Cheap forward-compat.
                pass
            rows.append({
                "ts": ts,
                "kind": kind,
                "key": d.get("key", ""),
                "ws": d.get("ws_ref") or "",
                "td": d.get("td") or "",
                "evidence": (d.get("evidence") or "")[:240],
                "outcome": d.get("outcome", "verified"),
                "pulse_idx": d.get("pulse_idx", 0),
                "verdict": d.get("verdict") or {},
            })

    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows = rows[:FEED_LIMIT]

    if not rows:
        return f'<div class="empty">No decisions in the last {ACTIVITY_HOURS}h. (ledger: {LEDGER_PATH})</div>', 0

    # Render
    out = ['<div class="feed">']
    for r in rows:
        outcome_cls = {
            "verified": "ok",
            "failed": "fail",
            "skipped": "skip",
            "rejected": "fail",
        }.get(r["outcome"], "ok")
        outcome_glyph = {
            "verified": "✓",
            "failed": "✗",
            "skipped": "⊘",
            "rejected": "⊘",
        }.get(r["outcome"], "·")
        scope_parts = []
        if r["ws"]:
            scope_parts.append(r["ws"])
        if r["td"]:
            scope_parts.append(r["td"])
        scope = " · ".join(scope_parts)
        # The "decision" line is: kind on scope — outcome
        # Below: evidence (the WHY)
        applied = (r["verdict"] or {}).get("applied_lessons") or []
        applied_html = ""
        if applied:
            applied_html = f' <span class="lesson">📖 {e(", ".join(applied))}</span>'
        out.append(f"""
<div class="row decision outcome-{outcome_cls}">
  <span class="ts">{r['ts'].strftime('%H:%M:%S')}</span>
  <span class="kind kind-{e(r['kind'])}">{e(r['kind'])}</span>
  <span class="scope">{e(scope) or '—'}</span>
  <span class="outcome-glyph">{outcome_glyph}</span>
  <span class="evidence" title="{e(r['evidence'])}">{e(r['evidence'])}{applied_html}</span>
  <span class="pulse">#p{r['pulse_idx']}</span>
</div>""")
    out.append("</div>")
    return "".join(out), len(rows)


# Back-compat: callers still reference render_activity
render_activity = render_decisions


_ERROR_PATTERNS = (
    "api error:", "connectionrefused", "security token", "expired",
    "529", "overloaded_error", "context window",
)


def render_live_sessions(world):
    now = utc_now()
    sessions = []
    errored_hidden = 0
    for sess in world.get("live_sessions", []):
        if sess.get("is_cron"):
            continue
        la = sess.get("last_assistant") or {}
        lu = sess.get("last_user") or {}
        cands = []
        if lu.get("ts"):
            cands.append(("user", parse_iso(lu.get("ts")), lu.get("text", "")))
        if la.get("ts"):
            cands.append(("assistant", parse_iso(la.get("ts")), la.get("text", "")))
        if not cands:
            continue
        role, ts, text = max(cands, key=lambda c: c[1] or now)
        age_sec = int((now - ts).total_seconds()) if ts else None
        # Errored / dead-pending: API error in the last assistant text + idle >5min.
        # These already surface as awaiting cards via the Assistant; don't double-list.
        text_lower = (text or "").lower()
        is_errored = any(p in text_lower for p in _ERROR_PATTERNS) and (age_sec or 0) > 300
        if is_errored:
            errored_hidden += 1
            continue
        sessions.append({
            "ws_ref": sess.get("ws_ref"),
            "ws_title": sess.get("ws_title", ""),
            "cwd": sess.get("cwd", ""),
            "role": role, "ts": ts, "text": text or "",
            "age_sec": age_sec,
            "user_unanswered": sess.get("user_unanswered"),
            "queue_pending": sess.get("queue_pending", 0),
        })
    sessions.sort(key=lambda s: s["ts"] or now, reverse=True)
    sessions = sessions[:LIVE_SESSIONS_LIMIT]
    if not sessions:
        empty_msg = (
            f'<div class="empty">No active sessions. {errored_hidden} errored sessions hidden — see Awaiting cards above.</div>'
            if errored_hidden else
            '<div class="empty">No open sessions.</div>'
        )
        return empty_msg, 0
    rows = ['<div class="feed">']
    for s in sessions:
        age = s["age_sec"] or 999999
        if age < 300: pill = '<span class="pill t1">live</span>'
        elif age < 1800: pill = '<span class="pill t1">recent</span>'
        elif age < 7200: pill = '<span class="pill t2">idle</span>'
        else: pill = '<span class="pill scope">quiet</span>'
        unans = '<span class="pill held">unanswered</span>' if s["user_unanswered"] else ""
        queued = f'<span class="pill t2">queued {s["queue_pending"]}</span>' if s["queue_pending"] else ""
        role_pill = '<span class="pill t0">→ said</span>' if s["role"] == "assistant" else '<span class="pill held">← you</span>'
        ws = s["ws_ref"] or "—"
        # Pills must not wrap; keep label compact. Full cwd in the tooltip.
        scope_label = ws
        text = e(s["text"][:280])
        rows.append(f"""
<div class="row kind-proposal">
  <span class="ts">{age_str(age)} ago</span>
  <span class="pill scope" title="{e(s['cwd'])}">{e(scope_label)}</span>
  <span class="action-text" title="{text}">{role_pill} {text}</span>
  <span class="outcome">{pill} {unans} {queued}</span>
</div>""")
    rows.append("</div>")
    if errored_hidden:
        rows.append(
            f'<div class="meta" style="margin-top:8px;font-size:10px;">'
            f'{errored_hidden} errored / API-broken session(s) hidden — see Awaiting cards above.'
            f'</div>'
        )
    return "".join(rows), len(sessions)


def render_decisions_tab(world):
    awaiting_html, awaiting_n = render_awaiting(world)
    activity_html, activity_n = render_activity(world)
    live_html, live_n = render_live_sessions(world)
    counts = world.get("counts", {})
    triage = load_assistant_state()
    actions_24h = len(triage.get("actions_taken") or [])
    triage_meta = triage.get("_meta") or {}
    triage_age = "?"
    gen = parse_iso(triage_meta.get("generated_at"))
    if gen:
        triage_age = age_str((utc_now() - gen).total_seconds())
    return f"""
<div class="stats">
  <div class="stat"><div class="v">{awaiting_n}</div><div class="k">Awaiting input</div></div>
  <div class="stat"><div class="v">{actions_24h}</div><div class="k">Assistant actions</div></div>
  <div class="stat"><div class="v">{counts.get('truly_active_30m', 0)}</div><div class="k">Active sessions</div></div>
  <div class="stat"><div class="v">{counts.get('human_sessions', 0)}</div><div class="k">Live · {counts.get('cron_sessions', 0)} cron hidden</div></div>
  <div class="stat"><div class="v">{triage_age}</div><div class="k">Last Assistant pulse</div></div>
</div>

<div class="section">
  <h2>Awaiting your input <span class="count">{awaiting_n}</span></h2>
  {awaiting_html}
</div>

<div class="section">
  <h2>Open sessions <span class="count">{live_n} active</span></h2>
  {live_html}
</div>

<div class="section">
  <h2>Decisions <span class="count">{activity_n} · last {ACTIVITY_HOURS}h</span></h2>
  {activity_html}
</div>
""", awaiting_n


def render_todos_tab(world):
    # Read TODO state DIRECTLY from assistant-todo.json, not through the
    # world.json snapshot. world-scanner refreshes world.json every 30s, so
    # any flag flipped via the dashboard's todo-server endpoints (toggle /
    # remove / append-detail / dispatch-now) was invisible until the next
    # scanner tick. This caused the dashboard to look like flips were being
    # "reset" — the JSON was correct, world.json was stale, the renderer
    # echoed the stale value, and the auto-refresh re-displayed the stale
    # page. Reading the live JSON makes the dashboard reflect the truth on
    # the very next render call (which the server fires synchronously after
    # every mutation).
    TODO_PATH = HOME / ".claude/assistant-todo.json"
    todo = {}
    try:
        if TODO_PATH.exists():
            todo = json.loads(TODO_PATH.read_text())
    except Exception:
        todo = world.get("todo") or {}  # fall back to world.json snapshot
    if not todo:
        todo = world.get("todo") or {}
    items = todo.get("items") or []
    completed = todo.get("completed") or []
    # Group OPEN items by priority; closed items (done/deferred) shown separately.
    by_pri = {"P0": [], "P1": [], "P2": [], "P3": [], "P4": []}
    closed_in_items = []
    for it in items:
        st = it.get("status", "open")
        if st in {"done", "deferred"}:
            closed_in_items.append(it)
            continue
        p = it.get("priority", "P3")
        by_pri.setdefault(p, []).append(it)

    p0_p1 = len(by_pri["P0"]) + len(by_pri["P1"])
    sections = []
    for label, prios in [("P0 / P1 — top", ["P0", "P1"]), ("P2 / P3 — backlog", ["P2", "P3"]), ("P4 — someday", ["P4"])]:
        bucket = []
        for pr in prios:
            bucket.extend(by_pri.get(pr, []))
        if not bucket:
            continue
        # Order within bucket: in-progress first, blocked second, open third, stale last
        order = {"in-progress": 0, "blocked": 1, "open": 2, "stale": 3}
        bucket.sort(key=lambda i: order.get(i.get("status", "open"), 99))
        rows = []
        for it in bucket:
            src = it.get("source", "")
            url = it.get("url", "")
            url_html = f' <a href="{e(url)}" class="alt-link">[link]</a>' if url else ""
            status = it.get("status", "open")
            status_pill = f'<span class="pill status-{status}">{e(status)}</span>'
            td_id = it.get("id", "") or ""
            ad_flag = it.get("autoDispatch")
            # AUTO pill = "this should be auto-dispatched"; once dispatchedAt is set
            # the TODO has already been picked up by a workspace. Show DISPATCHED instead.
            if ad_flag:
                if it.get("dispatchedAt"):
                    ad = ' <span class="pill status-in-progress" title="dispatched at ' + e(it.get("dispatchedAt", "")) + ' to ' + e(it.get("dispatchedWs", "?")) + '">dispatched</span>'
                else:
                    ad = ' <span class="pill auto-dispatch">auto</span>'
            else:
                ad = ""
            # Per-row action toolbar — only on truly editable TODOs
            # (skip 'closed-in' historical entries shown in "Recently completed").
            if td_id:
                ad_state = "true" if ad_flag is True else ("false" if ad_flag is False else "null")
                detail_text = it.get("detail") or ""
                # Three explicit segmented-set buttons. The current state is
                # highlighted via .active. Each click is an absolute set, not
                # a cycle — eliminates the "did I click once or twice?" ambiguity.
                t_active = " active" if ad_state == "true" else ""
                f_active = " active" if ad_state == "false" else ""
                n_active = " active" if ad_state == "null" else ""
                tools_html = (
                    f'<div class="todo-tools">'
                    f'  <span class="tool-label">autoDispatch:</span>'
                    f'  <button class="tool-btn td-set td-set-true{t_active}" data-id="{e(td_id)}" data-value="true" title="Set autoDispatch=true (Assistant will spawn at next pulse)">on</button>'
                    f'  <button class="tool-btn td-set td-set-false{f_active}" data-id="{e(td_id)}" data-value="false" title="Set autoDispatch=false (manual dispatch only)">off</button>'
                    f'  <button class="tool-btn td-set td-set-null{n_active}" data-id="{e(td_id)}" data-value="null" title="Set autoDispatch=null (Bucket C — Assistant will surface a card asking what to do)">unset</button>'
                    f'  <span class="tool-spacer"></span>'
                    f'  <button class="tool-btn td-dispatch" data-id="{e(td_id)}" title="Force Bucket B at next Assistant pulse: sets autoDispatch=true and clears dispatchedAt">'
                    f'    Dispatch now'
                    f'  </button>'
                    f'  <button class="tool-btn td-context-toggle" data-id="{e(td_id)}" title="Append context to detail">'
                    f'    + context'
                    f'  </button>'
                    f'  <button class="tool-btn td-remove" data-id="{e(td_id)}" title="Remove this TODO (soft-delete: moved to removed[] in the JSON)">'
                    f'    remove'
                    f'  </button>'
                    f'</div>'
                    f'<div class="todo-context" data-id="{e(td_id)}" hidden>'
                    f'  <textarea class="td-context-text" data-id="{e(td_id)}" rows="3" placeholder="Add context (will be appended to detail with a [mukul ts] marker)"></textarea>'
                    f'  <button class="tool-btn td-context-save" data-id="{e(td_id)}">Append</button>'
                    f'</div>'
                )
            else:
                tools_html = ""
            rows.append(f"""
<div class="row todo-row" data-detail="{e(detail_text if td_id else '')}">
  <div class="todo-row-main" title="{e(detail_text)}">
    <span class="pill {it.get('priority', 'P3').lower()}">{e(it.get('priority', 'P3'))}</span>
    <span class="todo-id">{e(td_id or '—')}</span>
    <span class="action-text">{status_pill}{ad} {e(it.get('title', ''))}{url_html}</span>
    <span class="outcome">{e(src)}</span>
  </div>
  {tools_html}
</div>""")
        sections.append(f"""
<div class="section">
  <h2>{label} <span class="count">{len(bucket)}</span></h2>
  <div class="feed">{''.join(rows)}</div>
</div>""")
    if completed:
        recent_done = sorted(
            (c for c in completed if c.get("closedAt")),
            key=lambda c: c.get("closedAt", ""), reverse=True,
        )[:10]
        rows = []
        for c in recent_done:
            rows.append(f"""
<div class="row todo-row kind-event">
  <span class="pill done">DONE</span>
  <span class="todo-id">{e(c.get('id', '') or '—')}</span>
  <span class="action-text">{e(c.get('title', ''))}</span>
  <span class="outcome">{e((c.get('closedAt') or '')[:10])}</span>
</div>""")
        sections.append(f"""
<div class="section">
  <h2>Recently completed <span class="count">10 newest</span></h2>
  <div class="feed">{''.join(rows)}</div>
</div>""")
    return "".join(sections), p0_p1


def render():
    if not WORLD_PATH.exists():
        DASHBOARD_HTML.write_text("<h1>world.json not present yet — Scanner hasn't run.</h1>")
        return
    world = json.loads(WORLD_PATH.read_text())
    decisions_html, awaiting_n = render_decisions_tab(world)
    todos_html, p0_p1 = render_todos_tab(world)
    counts = world.get("counts", {})

    css = """
:root { --bg:#0f1115; --panel:#161a22; --line:#283044; --text:#e6e8ee; --muted:#8a93a6;
        --green:#5fd97a; --amber:#f0b330; --red:#e96b6b; --blue:#6bbef0; --purple:#b06bf0; }
html,body { background:var(--bg); color:var(--text); margin:0; padding:0; }
body { font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace; padding:20px 24px; }
h1 { font-size:18px; margin:0 0 4px; font-weight:600; }
.meta { color:var(--muted); font-size:11px; margin-bottom:14px; letter-spacing:0.04em; }
.tabs { display:flex; gap:6px; border-bottom:1px solid var(--line); margin-bottom:18px; }
.tab { padding:8px 14px; border:1px solid var(--line); border-bottom:none; border-radius:6px 6px 0 0; cursor:pointer; background:transparent; color:var(--muted); font:inherit; font-size:12px; letter-spacing:0.04em; }
.tab.active { background:var(--panel); color:var(--text); }
.tab-count { display:inline-block; margin-left:6px; padding:1px 7px; border-radius:9px; background:var(--line); color:var(--text); font-size:10px; font-weight:600; }
.tab.active .tab-count { background:#1c3a26; color:var(--green); }
.tab-panel { display:none; }
.tab-panel.active { display:block; }
.stats { display:flex; gap:10px; margin-bottom:22px; flex-wrap:wrap; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:8px 12px; min-width:90px; }
.stat .v { font-size:20px; font-weight:700; line-height:1; }
.stat .k { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:0.1em; margin-top:4px; }
.section { margin-bottom:32px; padding-top:8px; }
.section h2 { font-size:13px; color:var(--text); text-transform:none; letter-spacing:0.02em; margin:0 0 12px; font-weight:600; padding-bottom:6px; border-bottom:1px solid var(--line); }
.section h2 .count { color:var(--muted); font-weight:400; font-size:11px; margin-left:6px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin-bottom:10px; }
.card.t1 { border-left:3px solid var(--green); }
.card.t2 { border-left:3px solid var(--amber); }
.card.t3 { border-left:3px solid var(--red); }
.pills { display:flex; gap:6px; align-items:center; margin-bottom:8px; flex-wrap:wrap; }
.pill { display:inline-block; padding:2px 9px; border-radius:10px; background:var(--line); color:var(--text); letter-spacing:0.05em; font-size:10px; font-weight:600; text-transform:uppercase; white-space:nowrap; vertical-align:middle; text-align:center; box-sizing:border-box; width:auto; }
.pill.t0 { background:#1e2e44; color:var(--blue); }
.pill.t1 { background:#1c3a26; color:var(--green); }
.pill.t2 { background:#3a2d18; color:var(--amber); }
.pill.t3 { background:#3a1c1c; color:var(--red); }
.pill.scope { background:transparent; color:var(--muted); border:1px solid var(--line); }
.pill.held { background:#3a1c3a; color:#e96be9; }
.pill.p0 { background:#3a1c1c; color:var(--red); }
.pill.p1 { background:#3a2d18; color:var(--amber); }
.pill.p2 { background:#1e2e44; color:var(--blue); }
.pill.p3 { background:transparent; color:var(--muted); border:1px solid var(--line); }
.pill.p4 { background:transparent; color:var(--muted); border:1px solid var(--line); }
.pill.done { background:#1c3a26; color:var(--green); }
.pill.status-open { background:transparent; color:var(--muted); border:1px solid var(--line); }
.pill.status-in-progress { background:#1e2e44; color:var(--blue); }
.pill.status-blocked { background:#3a1c3a; color:#e96be9; }
.pill.status-done { background:#1c3a26; color:var(--green); }
.pill.status-deferred { background:transparent; color:var(--muted); border:1px solid var(--line); text-decoration:line-through; }
.pill.status-stale { background:transparent; color:var(--muted); border:1px dashed var(--line); }
.pill.auto-dispatch { background:#3a2d18; color:var(--amber); }
.age { color:var(--muted); margin-left:auto; font-size:10px; font-variant-numeric:tabular-nums; }
.action { font-size:14px; font-weight:500; margin:0 0 6px; }
.reason { color:var(--muted); font-size:12px; line-height:1.6; }
.alts { font-size:11px; color:var(--muted); margin-top:8px; }
.alts code { background:var(--bg); padding:2px 6px; border-radius:3px; font-size:10px; }
.feed { background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.row { padding:8px 14px; border-bottom:1px solid rgba(40,48,68,0.6); display:grid; grid-template-columns:78px max-content minmax(0,1fr) 240px; gap:14px; align-items:center; font-size:12px; min-height:32px; }
.row:last-child { border-bottom:none; }
.row .ts { color:var(--muted); font-size:10px; font-variant-numeric:tabular-nums; letter-spacing:0.04em; }
.row .action-text { white-space:normal; overflow:hidden; min-width:0; line-height:1.45; word-break:break-word; }
.row .outcome { color:var(--muted); font-size:10px; text-align:right; white-space:normal; overflow:hidden; min-width:0; line-height:1.4; word-break:break-word; }
/* TODO rows: stacked block (row-main on top, tools strip + context editor below) */
.row.todo-row { display:block; padding:8px 10px; }
.row.todo-row .todo-row-main { display:grid; grid-template-columns:max-content max-content minmax(0,1fr) 200px; gap:10px; align-items:baseline; }
.row.todo-row .action-text { font-size:13px; }
.row.todo-row .todo-id { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; letter-spacing:0.04em; user-select:all; cursor:text; }
.row.todo-row .todo-tools { display:flex; gap:6px; margin-top:4px; flex-wrap:wrap; }
.row.todo-row .todo-context { display:flex; gap:6px; margin-top:6px; }
.row.todo-row .todo-context[hidden] { display:none; }
.row.todo-row .td-context-text { flex:1; background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:6px 8px; font:inherit; font-size:12px; resize:vertical; min-height:60px; }
.tool-btn { background:transparent; color:var(--muted); border:1px solid var(--line); border-radius:6px; padding:3px 8px; font:inherit; font-size:10px; cursor:pointer; letter-spacing:0.03em; }
.tool-btn:hover { color:var(--text); background:var(--line); }
.tool-btn.busy { opacity:0.5; }
.tool-btn.ok { background:#1c3a26; color:var(--green); border-color:#1c3a26; }
.tool-btn.err { background:#3a1c1c; color:var(--red); border-color:#3a1c1c; }
.tool-label { color:var(--muted); font-size:10px; letter-spacing:0.04em; padding:3px 4px 3px 0; align-self:center; }
.tool-spacer { flex:0 0 8px; }
/* Segmented autoDispatch tri-state. The active button shows the CURRENT value;
   inactive buttons preview what clicking them WILL set. No cycle, no ambiguity. */
.tool-btn.td-set { padding:3px 10px; }
.tool-btn.td-set-true.active  { background:#3a2d18; color:var(--amber); border-color:var(--amber); }
.tool-btn.td-set-false.active { background:#2a2a2a; color:var(--muted); border-color:var(--muted); }
.tool-btn.td-set-null.active  { background:#1c2a3a; color:var(--blue);  border-color:var(--blue); }
.tool-btn.td-set:not(.active) { opacity:0.55; }
.tool-btn.td-dispatch { color:#a78bfa; border-color:#2c1f4a; }
.tool-btn.td-dispatch:hover { background:#2c1f4a; color:#c4b5fd; }
.tool-btn.td-remove { color:#e96b6b; border-color:#3a1c1c; opacity:0.7; }
.tool-btn.td-remove:hover { background:#3a1c1c; color:#ff9090; opacity:1; }
.row.kind-ledger { background:rgba(95,217,122,0.04); }
.row.kind-event { opacity:0.65; }
/* Decision rows: ts · kind · scope · ✓ · evidence · #pulse */
.row.decision { grid-template-columns:78px max-content max-content 16px minmax(0,1fr) 56px; gap:10px; }
.row.decision .kind { color:var(--text); font-weight:600; font-size:11px; letter-spacing:0.03em; text-transform:uppercase; }
.row.decision .kind-dispatch { color:#7ad9ff; }
.row.decision .kind-status-flip { color:#5fd97a; }
.row.decision .kind-cleanup { color:#5fd97a; }
.row.decision .kind-close-workspace { color:#5fd97a; }
.row.decision .kind-merge-pr { color:#a78bfa; }
.row.decision .kind-nudge { color:#fbbf77; }
.row.decision .kind-emit-card { color:var(--muted); }
.row.decision .kind-purge-awaiting { color:var(--muted); }
.row.decision .scope { color:var(--muted); font-family:var(--mono); font-size:11px; }
.row.decision .outcome-glyph { font-size:13px; text-align:center; }
.row.decision.outcome-ok .outcome-glyph { color:var(--green); }
.row.decision.outcome-fail .outcome-glyph { color:var(--red); }
.row.decision.outcome-skip .outcome-glyph { color:var(--muted); }
.row.decision.outcome-fail { background:rgba(255,107,107,0.06); }
.row.decision .evidence { color:var(--text); font-size:11px; line-height:1.45; word-break:break-word; opacity:0.85; }
.row.decision .evidence .lesson { color:#a78bfa; font-size:10px; margin-left:6px; }
.row.decision .pulse { color:var(--muted); font-size:10px; text-align:right; font-variant-numeric:tabular-nums; }
.empty { color:var(--muted); font-style:italic; padding:16px 18px; text-align:center; background:var(--panel); border:1px solid var(--line); border-radius:8px; }
.buttons { margin-top:10px; display:flex; gap:6px; flex-wrap:wrap; }
.btn { background:var(--line); color:var(--text); border:none; border-radius:6px; padding:5px 12px; font:inherit; font-size:11px; cursor:pointer; letter-spacing:0.04em; }
.btn:hover { background:#3a4868; }
.btn.busy { opacity:0.5; }
.btn.ok { background:#1c3a26; color:var(--green); }
.btn.err { background:#3a1c1c; color:var(--red); }
.alt-link { color:var(--blue); text-decoration:none; font-size:10px; margin-left:6px; }
.alt-link:hover { text-decoration:underline; }
.footer { margin-top:32px; color:var(--muted); font-size:10px; letter-spacing:0.06em; }
"""

    js = """
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
  if (window.location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
}
window.addEventListener('DOMContentLoaded', () => {
  const initial = (window.location.hash || '#decisions').replace('#', '');
  showTab(['decisions', 'todos'].includes(initial) ? initial : 'decisions');
  // Auto-refresh every 15s but preserve the current hash. We use location.reload()
  // (not <meta http-equiv="refresh">) because meta-refresh reloads from the original
  // href and drops the fragment on most browsers, snapping the user back to
  // the default Decisions tab even when they're reading TODOs.
  setInterval(() => { location.reload(); }, 15000);
});
async function openWs(btn) {
  const ws = btn.dataset.ws;
  btn.classList.add('busy');
  btn.textContent = 'Opening…';
  try {
    const r = await fetch('http://127.0.0.1:9876/focus/' + ws, {method: 'POST'});
    if (r.ok) {
      btn.classList.remove('busy');
      btn.classList.add('ok');
      btn.textContent = '✓ Focused ' + ws;
      setTimeout(() => { btn.classList.remove('ok'); btn.textContent = 'Open ' + ws; }, 1500);
    } else {
      const t = await r.text();
      btn.classList.remove('busy');
      btn.classList.add('err');
      btn.textContent = '✗ ' + (t || 'failed');
    }
  } catch (e) {
    btn.classList.remove('busy');
    btn.classList.add('err');
    btn.textContent = '✗ ' + (e.message || 'no server');
  }
}

// Delegated click handler for TODO row tools.
// Cycle: null → true → false → null (matches the dispatcher's three buckets).
async function handleTodoToolsClick(ev) {
  const btn = ev.target.closest('.tool-btn');
  if (!btn) return;
  const id = btn.dataset.id;
  if (!id) return;

  // ─── autoDispatch absolute-set (segmented tri-state) ───
  if (btn.classList.contains('td-set')) {
    if (btn.classList.contains('active')) return; // already at this value, no-op
    const value = btn.dataset.value; // 'true' | 'false' | 'null'
    const originalText = btn.textContent;
    btn.classList.add('busy');
    try {
      const r = await fetch(`http://127.0.0.1:9876/toggle/${id}?flag=autoDispatch&value=${value}`, {method: 'POST'});
      if (r.ok) {
        // Visually move .active to this button immediately so the user sees
        // the absolute-set semantics. The 15s auto-refresh will re-render
        // canonical state too.
        const group = btn.parentElement.querySelectorAll('.td-set');
        group.forEach(b => b.classList.remove('active'));
        btn.classList.remove('busy'); btn.classList.add('active', 'ok');
        btn.textContent = '✓ ' + originalText;
        setTimeout(() => { btn.classList.remove('ok'); btn.textContent = originalText; }, 1200);
      } else {
        const t = await r.text();
        btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (t || 'failed');
      }
    } catch (e) {
      btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (e.message || 'no server');
    }
    return;
  }

  // ─── Remove TODO (soft-delete via /remove/<id>) ───
  if (btn.classList.contains('td-remove')) {
    if (!confirm(`Remove ${id}?\n\nSoft-delete: TODO is moved into the 'removed[]' array of assistant-todo.json (recoverable, but disappears from the dashboard).`)) return;
    btn.classList.add('busy'); btn.textContent = 'Removing…';
    try {
      const r = await fetch(`http://127.0.0.1:9876/remove/${id}`, {method: 'POST'});
      const t = await r.text();
      if (r.ok) {
        // Hide the row immediately; auto-refresh will confirm
        const row = btn.closest('.row.todo-row');
        if (row) { row.style.opacity = '0.3'; row.style.transition = 'opacity 0.5s'; }
        btn.classList.remove('busy'); btn.classList.add('ok'); btn.textContent = '✓ removed';
      } else {
        btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (t || 'failed');
      }
    } catch (e) {
      btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (e.message || 'no server');
    }
    return;
  }

  // ─── Dispatch now: force Bucket B at next pulse ───
  if (btn.classList.contains('td-dispatch')) {
    if (!confirm(`Force ${id} to dispatch at next Assistant pulse?\n\nThis sets autoDispatch=true and clears dispatchedAt/dispatchedWs.\nIf the TODO was deferred/blocked/done, it will be reopened.`)) return;
    btn.classList.add('busy'); btn.textContent = 'Queueing…';
    try {
      const r = await fetch(`http://127.0.0.1:9876/dispatch-now/${id}`, {method: 'POST'});
      const t = await r.text();
      if (r.ok) {
        btn.classList.remove('busy'); btn.classList.add('ok'); btn.textContent = '✓ queued';
      } else {
        btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (t || 'failed');
      }
    } catch (e) {
      btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (e.message || 'no server');
    }
    return;
  }

  // ─── Toggle the inline context textarea ───
  if (btn.classList.contains('td-context-toggle')) {
    const box = document.querySelector(`.todo-context[data-id="${CSS.escape(id)}"]`);
    if (!box) return;
    const wasHidden = box.hasAttribute('hidden');
    if (wasHidden) {
      box.removeAttribute('hidden');
      const ta = box.querySelector('.td-context-text');
      if (ta) ta.focus();
    } else {
      box.setAttribute('hidden', '');
    }
    return;
  }

  // ─── Append context: POST textarea body to /append-detail/<id> ───
  if (btn.classList.contains('td-context-save')) {
    const ta = document.querySelector(`.td-context-text[data-id="${CSS.escape(id)}"]`);
    if (!ta) return;
    const body = (ta.value || '').trim();
    if (!body) { ta.focus(); return; }
    btn.classList.add('busy'); btn.textContent = 'Saving…';
    try {
      const r = await fetch(`http://127.0.0.1:9876/append-detail/${id}`, {
        method: 'POST',
        headers: {'Content-Type': 'text/plain; charset=utf-8'},
        body: body,
      });
      const t = await r.text();
      if (r.ok) {
        btn.classList.remove('busy'); btn.classList.add('ok'); btn.textContent = '✓ appended';
        ta.value = '';
        // Auto-collapse + let the 15s refresh redraw the new detail length.
        const box = document.querySelector(`.todo-context[data-id="${CSS.escape(id)}"]`);
        if (box) setTimeout(() => box.setAttribute('hidden', ''), 800);
      } else {
        btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (t || 'failed');
      }
    } catch (e) {
      btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ ' + (e.message || 'no server');
    }
    return;
  }
}
document.addEventListener('click', handleTodoToolsClick);
"""

    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Assistant</title>
<style>{css}</style>
<script>{js}</script>
</head><body>

<h1>Assistant</h1>
<div class="meta">{e(utc_now().strftime('%H:%M:%S UTC'))} · auto-refresh 15s · v3 (one Scanner, one Evaluator)</div>

<div class="tabs">
  <button class="tab" data-tab="decisions" onclick="showTab('decisions')">
    Decisions <span class="tab-count">{awaiting_n}</span>
  </button>
  <button class="tab" data-tab="todos" onclick="showTab('todos')">
    TODOs <span class="tab-count">{p0_p1}</span>
  </button>
</div>

<div class="tab-panel" data-panel="decisions">
{decisions_html}
</div>

<div class="tab-panel" data-panel="todos">
{todos_html}
</div>

<div class="footer">v3 · Scanner: ~/.claude/cache/world.json · Evaluator: ~/.architect/orchestrator-{{proposals,ledger}}/ · Lessons: ~/.claude/CLAUDE.md `## Lessons` · Undo: world-evaluator owns ledger</div>
</body></html>
"""
    DASHBOARD_HTML.write_text(body)
    # Legacy redirect for the old TODO bookmark
    TODO_HTML.write_text(
        '<!doctype html><meta http-equiv="refresh" content="0; url=assistant-dashboard.html#todos">'
    )
    print(f"wrote {len(body)} bytes (awaiting={awaiting_n}, todos_p0_p1={p0_p1})")


if __name__ == "__main__":
    render()
