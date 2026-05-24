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
        ref = t.get("ref", "")
        if ref.startswith("workspace:"):
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


def render_activity(world):
    """Activity feed = Assistant's actions_taken[] + recent ledger entries.
    Newest first. The actions are already verified by the Assistant on the
    same pulse, so we display them as ✓ / ✗ based on the verification flag."""
    triage = load_assistant_state()
    actions = triage.get("actions_taken") or []
    ledger = world.get("ledger_recent", [])
    events = world.get("inbox_events_recent", [])
    activity = []
    for a in actions:
        ts = parse_iso(a.get("ts"))
        if not ts:
            continue
        verified = a.get("verified", False)
        outcome = "✓ done" if verified else "✗ unverified"
        scope = ""
        for t in [a.get("target")] if a.get("target") else []:
            scope = t.get("name") or t.get("ref") or ""
        activity.append({
            "ts": ts, "kind": "ledger", "tier": "T1",
            "action": (a.get("evidence") or a.get("kind") or "")[:140],
            "outcome": outcome, "scope": scope,
        })
    for l in ledger:
        ts = parse_iso(l.get("ts"))
        if not ts:
            continue
        if l.get("undone"):
            outcome = "↶ undone"
        elif l.get("result", {}).get("ok"):
            outcome = f"✓ {l.get('execute_via', '')}"
        else:
            outcome = f"✗ failed · {l.get('execute_via', '')}"
        scope = ""
        for t in l.get("touches") or []:
            scope = t.get("name") or t.get("ref") or ""
            if scope:
                break
        activity.append({
            "ts": ts, "kind": "ledger", "tier": (l.get("tier") or "T?").upper(),
            "action": l.get("action", "")[:180], "outcome": outcome, "scope": scope,
        })
    for ev in events:
        kind = ev.get("kind", "")
        if kind in {"executor-fired", "executor-fire-failed", "hermes-fired", "hermes-fire-failed"}:
            continue
        if kind in NOISE_EVENT_KINDS and ev.get("severity") != "urgent":
            continue
        ts = parse_iso(ev.get("ts"))
        if not ts:
            continue
        activity.append({
            "ts": ts, "kind": "event", "tier": "T0",
            "action": ev.get("summary", "")[:180],
            "outcome": f"{ev.get('worker', '')} · {ev.get('severity', '')}",
            "scope": ev.get("worker", ""),
        })
    activity.sort(key=lambda a: a["ts"], reverse=True)
    activity = activity[:FEED_LIMIT]
    if not activity:
        return '<div class="empty">No activity in the last 24h.</div>', 0
    rows = ['<div class="feed">']
    for a in activity:
        tier_lower = a["tier"].lower() if a["tier"] in {"T0", "T1", "T2", "T3"} else "t0"
        rows.append(f"""
<div class="row kind-{a['kind']}">
  <span class="ts">{a['ts'].strftime('%H:%M:%S')}</span>
  <span class="pill {tier_lower}">{a['tier']}</span>
  <span class="action-text" title="{e(a['action'])}">{e(a['action'])}</span>
  <span class="outcome">{e(a['scope'])} · {e(a['outcome'])}</span>
</div>""")
    rows.append("</div>")
    return "".join(rows), len(activity)


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
  <h2>Recent activity <span class="count">{activity_n} · last {ACTIVITY_HOURS}h</span></h2>
  {activity_html}
</div>
""", awaiting_n


def render_todos_tab(world):
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
            # AUTO pill = "this should be auto-dispatched"; once dispatchedAt is set
            # the TODO has already been picked up by a workspace. Show DISPATCHED instead.
            if it.get("autoDispatch"):
                if it.get("dispatchedAt"):
                    ad = ' <span class="pill status-in-progress" title="dispatched at ' + e(it.get("dispatchedAt", "")) + ' to ' + e(it.get("dispatchedWs", "?")) + '">dispatched</span>'
                else:
                    ad = ' <span class="pill auto-dispatch">auto</span>'
            else:
                ad = ""
            rows.append(f"""
<div class="row todo-row" title="{e(it.get('detail', ''))}">
  <span class="pill {it.get('priority', 'P3').lower()}">{e(it.get('priority', 'P3'))}</span>
  <span class="todo-id">{e(it.get('id', '') or '—')}</span>
  <span class="action-text">{status_pill}{ad} {e(it.get('title', ''))}{url_html}</span>
  <span class="outcome">{e(src)}</span>
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
/* TODO rows: priority pill · ID · title · source */
.row.todo-row { grid-template-columns:max-content max-content minmax(0,1fr) 200px; }
.row.todo-row .action-text { font-size:13px; }
.row.todo-row .todo-id { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; letter-spacing:0.04em; user-select:all; cursor:text; }
.row.kind-ledger { background:rgba(95,217,122,0.04); }
.row.kind-event { opacity:0.65; }
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

<div class="footer">v3 · Scanner: ~/.claude/cache/world.json · Evaluator: ~/.architect/orchestrator-{{proposals,ledger}}/ · Lessons: ~/.assistant/lessons/index.md · Undo: world-evaluator owns ledger</div>
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
