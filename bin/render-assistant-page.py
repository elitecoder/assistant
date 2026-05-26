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


def render_workspaces_tab():
    """Read ~/.assistant/observer-summaries/<ws>.json (one file per workspace,
    written each pulse by the Assistant) and render a row per workspace with
    its current summary + verdict.

    Filtered to workspaces currently open in cmux (via world.json snapshot).
    Stale summaries for closed workspaces are skipped. The summary is
    written by the Observer and saved at end-of-pulse — no extra LLM calls
    happen here."""
    summaries_dir = HOME / ".assistant/observer-summaries"
    if not summaries_dir.exists():
        return '<div class="muted">No observer summaries yet — waiting for first pulse.</div>', 0

    # Pull live signals from world.json: open workspaces + per-workspace
    # last_turn_age_sec / agent_status / cwd / pr_refs.
    open_ws: set[str] = set()
    ws_meta: dict[str, dict] = {}
    try:
        world_data = json.loads(WORLD_PATH.read_text())
        for w in world_data.get("workspaces", []) or []:
            ref = w.get("ws_ref") or w.get("ref") or w.get("workspace_ref")
            if ref:
                open_ws.add(ref)
                ws_meta[ref] = w

        # Index live_sessions by tab_id so we can correlate workspace → session.
        sess_by_tab: dict[str, dict] = {}
        for s in world_data.get("live_sessions", []) or []:
            tab = s.get("tab_id")
            if tab:
                sess_by_tab[tab] = s
        # Stitch session signals into workspace meta.
        for ref, w in ws_meta.items():
            for surf in (w.get("surfaces") or []):
                # In cmux, surface.ref maps to a tab_id elsewhere; we only
                # have ref on surfaces. The ws_meta panel_id mapping isn't
                # exposed here. Best we can do: match by cwd of any session
                # whose tab_id appears in panel_ids — fall back to None.
                pass
            # Look up session by matching cwd: world.live_sessions has a cwd
            # field; ws_meta doesn't carry cwd directly — but the summary file
            # for this workspace does. We attach session info per-row below.
    except Exception:
        world_data = {}

    def session_for_ws(ws_cwd: str) -> dict | None:
        """Find live_sessions entry whose cwd matches the workspace cwd. Best-effort."""
        if not ws_cwd:
            return None
        for s in world_data.get("live_sessions", []) or []:
            if s.get("cwd") == ws_cwd:
                return s
        return None

    rows = []
    now = utc_now().timestamp()
    for jf in sorted(summaries_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text())
        except Exception:
            continue
        ws_ref = data.get("ws_ref") or jf.stem
        if open_ws and ws_ref not in open_ws:
            continue
        title = data.get("title") or ""
        cwd = data.get("cwd") or ""

        # Verdict: new schema field. Fall back to legacy `classification`.
        verdict = data.get("verdict")
        if not verdict:
            classification = (data.get("classification") or "").upper()
            verdict = {
                "ACTIVE":        "active",
                "DONE":          "ready_for_cleanup",
                "STRANDED":      "stranded",
                "AWAITING_USER": "needs_user",
                "BROKEN":        "needs_user",
                "UNKNOWN":       "unknown",
            }.get(classification, "unknown")

        summary = data.get("summary") or data.get("summary_for_next_pulse") or "(no summary recorded yet)"
        ts = data.get("ts") or data.get("last_updated_ts") or 0
        age_sec = max(0, int(now - ts)) if ts else None

        # Live signals from the workspace's session (if found).
        sess = session_for_ws(cwd)
        last_turn_age = sess.get("last_turn_age_sec") if sess else None
        # Working = last assistant turn was a tool_use (still in flight).
        last_assistant_text = (sess or {}).get("last_assistant", {}).get("text", "") or ""
        agent_status = "working" if last_assistant_text.startswith("[tool_use:") else "idle"
        if not sess:
            agent_status = None  # unknown

        pr_refs = data.get("pr_refs") or []

        rows.append({
            "ws_ref": ws_ref, "title": title, "cwd": cwd, "verdict": verdict,
            "summary": summary, "age_sec": age_sec, "ts": ts,
            "agent_status": agent_status, "last_turn_age_sec": last_turn_age,
            "pr_refs": pr_refs,
        })

    rows.sort(key=lambda r: -(r.get("ts") or 0))

    if not rows:
        return '<div class="muted">No observer summaries yet — waiting for first pulse.</div>', 0

    def fmt_age(s):
        if s is None:
            return "—"
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"

    # Title-prefix → category tag (gives every row a contextual chip).
    def category_tag(title: str) -> tuple[str, str] | None:
        t = (title or "")
        if t.startswith("Auto:"):       return ("auto",     "category-auto")
        if t.startswith("Resumed:"):    return ("resumed",  "category-resumed")
        if "deflake" in t.lower():      return ("deflake",  "category-deflake")
        if "audit" in t.lower():        return ("audit",    "category-audit")
        if "phonebook" in t.lower():    return ("phonebook","category-audit")
        if "Architect" in t:            return ("arch",     "category-arch")
        if "Assistant" in t:            return ("internal", "category-internal")
        if "probe" in t.lower():        return ("probe",    "category-internal")
        if "combine" in t.lower():      return ("combine",  "category-arch")
        return None

    # icon · short label · CSS class suffix (drives tinted bg + glow)
    VERDICT_PILL = {
        "active":            ('▶', 'active',   'active'),
        "ready_for_merge":   ('✓', 'merge',    'merge'),
        "ready_for_cleanup": ('✦', 'cleanup',  'cleanup'),
        "stranded":          ('⚠', 'stranded', 'stranded'),
        "needs_user":        ('ⓘ', 'user',     'user'),
        "unknown":           ('—', 'unknown',  'unknown'),
    }

    html_rows = []
    for r in rows:
        icon, label, vcls = VERDICT_PILL.get(r["verdict"], VERDICT_PILL["unknown"])
        stale = (r["age_sec"] or 0) > 300
        age_class = " stale" if stale else ""

        # Status dot: green pulse when working, dim circle when idle, hollow if unknown.
        if r["agent_status"] == "working":
            status_dot = '<span class="status-dot working" title="Agent working — tool_use in flight"></span>'
        elif r["agent_status"] == "idle":
            status_dot = '<span class="status-dot idle" title="Agent idle"></span>'
        else:
            status_dot = '<span class="status-dot unknown" title="No live session detected"></span>'

        # Category chip
        cat = category_tag(r["title"])
        cat_chip = f'<span class="ws-cat {cat[1]}">{cat[0]}</span>' if cat else ''

        # PR chips — small monospace pills.
        pr_chips = ""
        if r["pr_refs"]:
            chips = "".join(
                f'<span class="ws-pr-chip">PR #{p}</span>'
                for p in r["pr_refs"][:3]
            )
            pr_chips = f'<span class="ws-pr-chips">{chips}</span>'

        # Last-turn age chip — separate from summary-age. Only show if known.
        live_age_chip = ""
        if r["last_turn_age_sec"] is not None:
            la = r["last_turn_age_sec"]
            la_str = fmt_age(la)
            la_class = "fresh" if la < 600 else ("warm" if la < 1800 else "cold")
            live_age_chip = f'<span class="ws-live-age {la_class}" title="Last turn {la_str} ago">⟳ {la_str}</span>'

        html_rows.append(
            f'<div class="ws-row verdict-{vcls}-row">'
            f'  <div class="ws-row-head">'
            f'    {status_dot}'
            f'    <button class="ws-ref-btn" data-ws="{e(r["ws_ref"])}" onclick="openWs(this)" title="Focus this workspace in cmux">{e(r["ws_ref"])}</button>'
            f'    <span class="ws-title">{e(r["title"])}</span>'
            f'    {cat_chip}'
            f'    {pr_chips}'
            f'    {live_age_chip}'
            f'    <span class="verdict-pill verdict-{vcls}"><span class="vp-icon">{icon}</span>{e(label)}</span>'
            f'    <span class="ws-age{age_class}" title="Summary written {fmt_age(r["age_sec"])} ago">{fmt_age(r["age_sec"])}</span>'
            f'  </div>'
            f'  <div class="ws-summary">{e(r["summary"])}</div>'
            f'</div>'
        )

    return f'<div class="ws-list">{"".join(html_rows)}</div>', len(rows)


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
    workspaces_html, ws_n = render_workspaces_tab()
    counts = world.get("counts", {})

    css = """
/* ────────────────────────────────────────────────────────────────────
   Linear/Vercel-style refined dark UI. System sans for body, monospace
   for code-y bits (ws_ref, td-IDs, timestamps). Generous whitespace,
   subtle borders, softly-glowing verdict pills.
   ──────────────────────────────────────────────────────────────────── */
:root {
  --bg:           #0a0a0d;
  --panel:        #13141a;
  --panel-2:      #181922;
  --line:         rgba(255,255,255,0.06);
  --line-strong:  rgba(255,255,255,0.10);
  --hover:        rgba(255,255,255,0.025);
  --text:         #ededf0;
  --text-2:       #c9cad1;
  --muted:        #7c7e8a;
  --muted-2:      #5a5c66;

  --green:        #5fd97a;
  --green-bg:     rgba(95,217,122,0.12);
  --green-glow:   rgba(95,217,122,0.18);

  --amber:        #f0b330;
  --amber-bg:     rgba(240,179,48,0.12);
  --amber-glow:   rgba(240,179,48,0.18);

  --red:          #e96b6b;
  --red-bg:       rgba(233,107,107,0.12);
  --red-glow:     rgba(233,107,107,0.18);

  --blue:         #6bbef0;
  --blue-bg:      rgba(107,190,240,0.12);
  --blue-glow:    rgba(107,190,240,0.18);

  --purple:       #b06bf0;
  --purple-bg:    rgba(176,107,240,0.12);
  --purple-glow:  rgba(176,107,240,0.20);

  --pink:         #e96be9;

  --sans:         -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text",
                  system-ui, "Segoe UI", Roboto, sans-serif;
  --mono:         "Geist Mono", "JetBrains Mono", "SF Mono", ui-monospace,
                  Menlo, Consolas, monospace;
}

* { box-sizing:border-box; }
html, body { background:var(--bg); color:var(--text); margin:0; padding:0; }
body {
  font: 13px/1.55 var(--sans);
  padding: 28px 32px 48px;
  font-feature-settings: "ss01", "cv11";
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

h1 {
  font: 600 18px/1.2 var(--sans);
  letter-spacing: -0.01em;
  margin: 0 0 4px;
}
.meta {
  font: 11px/1.5 var(--mono);
  color: var(--muted);
  margin-bottom: 22px;
  letter-spacing: 0;
}

/* ─── Tab nav: Linear-style underline ─── */
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 24px;
  padding: 0;
}
.tab {
  position: relative;
  padding: 10px 14px 12px;
  border: none;
  background: transparent;
  color: var(--muted);
  font: 500 13px/1 var(--sans);
  letter-spacing: -0.005em;
  cursor: pointer;
  transition: color 0.12s ease;
  margin-bottom: -1px; /* overlap the tabs border so the underline sits on it */
}
.tab:hover { color: var(--text-2); }
.tab.active { color: var(--text); }
.tab.active::after {
  content: "";
  position: absolute;
  left: 10px; right: 10px; bottom: -1px;
  height: 2px;
  background: var(--text);
  border-radius: 2px 2px 0 0;
}
.tab-count {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--line-strong);
  color: var(--text-2);
  font: 600 10px/1.5 var(--mono);
  letter-spacing: 0;
  vertical-align: middle;
}
.tab.active .tab-count {
  background: var(--green-bg);
  color: var(--green);
}
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ─── Stats strip ─── */
.stats {
  display: flex;
  gap: 8px;
  margin-bottom: 28px;
  flex-wrap: wrap;
}
.stat {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 16px;
  min-width: 110px;
}
.stat .v {
  font: 600 22px/1 var(--sans);
  letter-spacing: -0.02em;
  color: var(--text);
}
.stat .k {
  font: 500 10px/1.4 var(--sans);
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 6px;
}

/* ─── Sections ─── */
.section { margin-bottom: 36px; }
.section h2 {
  font: 600 12px/1.4 var(--sans);
  color: var(--text-2);
  letter-spacing: 0.02em;
  text-transform: uppercase;
  margin: 0 0 14px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
.section h2 .count {
  color: var(--muted);
  font: 500 11px/1 var(--mono);
  letter-spacing: 0;
  margin-left: 8px;
  text-transform: none;
}

/* ─── Awaiting cards (Decisions tab) ─── */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 10px;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.card:hover { border-color: var(--line-strong); }
.card.t1 { box-shadow: inset 3px 0 0 0 var(--green); }
.card.t2 { box-shadow: inset 3px 0 0 0 var(--amber); }
.card.t3 { box-shadow: inset 3px 0 0 0 var(--red); }
.pills { display: flex; gap: 6px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
.action { font: 500 14px/1.45 var(--sans); margin: 0 0 6px; color: var(--text); letter-spacing: -0.005em; }
.reason { color: var(--text-2); font-size: 12px; line-height: 1.6; }
.alts { font: 11px/1.5 var(--sans); color: var(--muted); margin-top: 10px; }
.alts code { background: rgba(255,255,255,0.04); padding: 2px 6px; border-radius: 4px; font: 10px/1 var(--mono); color: var(--text-2); }

/* ─── Generic pill ─── */
.pill {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--line-strong);
  color: var(--text-2);
  font: 600 10px/1.4 var(--mono);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
  vertical-align: middle;
}
.pill.t0 { background: var(--blue-bg);  color: var(--blue); }
.pill.t1 { background: var(--green-bg); color: var(--green); }
.pill.t2 { background: var(--amber-bg); color: var(--amber); }
.pill.t3 { background: var(--red-bg);   color: var(--red); }
.pill.scope { background: transparent; color: var(--muted); border: 1px solid var(--line); }
.pill.held { background: rgba(233,107,233,0.12); color: var(--pink); }
.pill.p0 { background: var(--red-bg);   color: var(--red); }
.pill.p1 { background: var(--amber-bg); color: var(--amber); }
.pill.p2 { background: var(--blue-bg);  color: var(--blue); }
.pill.p3 { background: transparent; color: var(--muted); border: 1px solid var(--line); }
.pill.p4 { background: transparent; color: var(--muted-2); border: 1px solid var(--line); }
.pill.done { background: var(--green-bg); color: var(--green); }
.pill.status-open        { background: transparent; color: var(--muted); border: 1px solid var(--line); }
.pill.status-in-progress { background: var(--blue-bg); color: var(--blue); }
.pill.status-blocked     { background: rgba(233,107,233,0.12); color: var(--pink); }
.pill.status-done        { background: var(--green-bg); color: var(--green); }
.pill.status-deferred    { background: transparent; color: var(--muted); border: 1px solid var(--line); text-decoration: line-through; }
.pill.status-stale       { background: transparent; color: var(--muted); border: 1px dashed var(--line); }
.pill.auto-dispatch { background: var(--amber-bg); color: var(--amber); }

/* ─── Verdict pills (Workspaces tab) — icon + soft glow ─── */
.verdict-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px 3px 8px;
  border-radius: 999px;
  font: 600 10px/1.4 var(--mono);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}
.verdict-pill .vp-icon {
  font-size: 9px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  margin-right: 1px;
}
.verdict-active {
  background: var(--blue-bg);
  color: var(--blue);
  box-shadow: 0 0 0 1px rgba(107,190,240,0.18), 0 0 12px -2px var(--blue-glow);
}
.verdict-merge {
  background: var(--green-bg);
  color: var(--green);
  box-shadow: 0 0 0 1px rgba(95,217,122,0.18), 0 0 12px -2px var(--green-glow);
}
.verdict-cleanup {
  background: var(--purple-bg);
  color: var(--purple);
  box-shadow: 0 0 0 1px rgba(176,107,240,0.20), 0 0 12px -2px var(--purple-glow);
}
.verdict-stranded {
  background: var(--amber-bg);
  color: var(--amber);
  box-shadow: 0 0 0 1px rgba(240,179,48,0.18), 0 0 12px -2px var(--amber-glow);
}
.verdict-user {
  background: var(--red-bg);
  color: var(--red);
  box-shadow: 0 0 0 1px rgba(233,107,107,0.18), 0 0 12px -2px var(--red-glow);
}
.verdict-unknown {
  background: rgba(255,255,255,0.04);
  color: var(--muted);
}

/* ─── Workspaces tab — divided list, not boxed rows ─── */
.ws-list {
  border-top: 1px solid var(--line);
}
.ws-row {
  padding: 14px 4px 16px;
  border-bottom: 1px solid var(--line);
  transition: background 0.12s ease;
}
.ws-row:hover { background: var(--hover); }
.ws-row-head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 8px;
}
.ws-row-head .ws-title {
  flex: 1 1 220px;
  min-width: 180px;
}
.ws-row-head .ws-age { margin-left: auto; }
/* status dot — pulsing green when working, dim when idle */
.status-dot {
  width: 7px; height: 7px; border-radius: 50%;
  flex-shrink: 0; display: inline-block;
}
.status-dot.working {
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(95,217,122,0.5);
  animation: pulse-dot 1.6s ease-in-out infinite;
}
.status-dot.idle    { background: var(--muted); opacity: 0.4; }
.status-dot.unknown { background: transparent; border: 1px solid var(--line-strong); }
@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(95,217,122,0.45); }
  50%      { box-shadow: 0 0 0 4px rgba(95,217,122,0);   }
}
/* category chip */
.ws-cat {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font: 500 9.5px/1.4 var(--mono);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  background: rgba(255,255,255,0.04);
  color: var(--text-2);
  border: 1px solid var(--line);
}
.category-auto     { color: var(--amber);  background: var(--amber-bg);  border-color: rgba(240,179,48,0.18); }
.category-resumed  { color: var(--purple); background: var(--purple-bg); border-color: rgba(176,107,240,0.20); }
.category-deflake  { color: var(--blue);   background: var(--blue-bg);   border-color: rgba(107,190,240,0.18); }
.category-audit    { color: #f0b3d5;       background: rgba(240,179,213,0.08); border-color: rgba(240,179,213,0.18); }
.category-arch     { color: #6be0c8;       background: rgba(107,224,200,0.08); border-color: rgba(107,224,200,0.18); }
.category-internal { color: var(--muted); }
/* PR chip group */
.ws-pr-chips { display: inline-flex; gap: 4px; }
.ws-pr-chip {
  font: 500 10px/1.2 var(--mono);
  padding: 2px 7px;
  border-radius: 4px;
  background: rgba(107,190,240,0.08);
  color: var(--blue);
  border: 1px solid rgba(107,190,240,0.16);
}
/* live-age chip — last turn age (separate from summary age) */
.ws-live-age {
  font: 500 10px/1.2 var(--mono);
  padding: 2px 7px;
  border-radius: 4px;
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--line);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0;
}
.ws-live-age.fresh { color: var(--green); background: var(--green-bg); border-color: rgba(95,217,122,0.18); }
.ws-live-age.warm  { color: var(--amber); background: var(--amber-bg); border-color: rgba(240,179,48,0.18); }
.ws-live-age.cold  { color: var(--muted); }
/* faint left border on row tinted to verdict */
.ws-row { position: relative; }
.ws-row::before {
  content: ""; position: absolute; left: -8px; top: 14px; bottom: 16px;
  width: 2px; border-radius: 1px; opacity: 0;
  transition: opacity 0.18s ease;
}
.ws-row.verdict-active-row::before   { background: var(--blue);   opacity: 0.5; }
.ws-row.verdict-merge-row::before    { background: var(--green);  opacity: 0.6; }
.ws-row.verdict-cleanup-row::before  { background: var(--purple); opacity: 0.55; }
.ws-row.verdict-stranded-row::before { background: var(--amber);  opacity: 0.6; }
.ws-row.verdict-user-row::before     { background: var(--red);    opacity: 0.6; }
.ws-row.verdict-unknown-row::before  { background: var(--muted);  opacity: 0.18; }
.ws-row:hover::before { opacity: 0.85; }
.ws-ref-btn {
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text-2);
  font: 500 11px/1 var(--mono);
  letter-spacing: 0;
  padding: 5px 10px;
  cursor: pointer;
  transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease;
}
.ws-ref-btn:hover {
  border-color: var(--line-strong);
  color: var(--text);
  background: rgba(255,255,255,0.03);
}
.ws-ref-btn.busy { opacity: 0.5; }
.ws-ref-btn.ok   { background: var(--green-bg); color: var(--green); border-color: rgba(95,217,122,0.25); }
.ws-ref-btn.err  { background: var(--red-bg); color: var(--red); border-color: rgba(233,107,107,0.25); }
.ws-title {
  font: 500 13px/1.4 var(--sans);
  color: var(--text);
  letter-spacing: -0.005em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-age {
  font: 500 11px/1 var(--mono);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
  min-width: 36px;
}
.ws-age.stale { color: var(--amber); }
.ws-summary {
  color: var(--text-2);
  font: 13px/1.55 var(--sans);
  padding-left: 4px;
  margin-top: 4px;
  opacity: 0.86;
}

/* ─── Generic feed (used by Decisions, sessions, TODOs) ─── */
.feed {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
}
.row {
  padding: 11px 16px;
  border-bottom: 1px solid var(--line);
  display: grid;
  grid-template-columns: 78px max-content minmax(0,1fr) 240px;
  gap: 14px;
  align-items: center;
  font-size: 12px;
  min-height: 36px;
  transition: background 0.12s ease;
}
.row:last-child { border-bottom: none; }
.row:hover { background: var(--hover); }
.row .ts {
  color: var(--muted);
  font: 500 11px/1.3 var(--mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0;
}
.row .action-text {
  color: var(--text-2);
  white-space: normal;
  overflow: hidden;
  min-width: 0;
  line-height: 1.5;
  word-break: break-word;
}
.row .outcome {
  color: var(--muted);
  font: 500 10px/1.4 var(--mono);
  text-align: right;
  white-space: normal;
  overflow: hidden;
  min-width: 0;
  word-break: break-word;
}
.age {
  color: var(--muted);
  margin-left: auto;
  font: 500 11px/1 var(--mono);
  font-variant-numeric: tabular-nums;
}
.age.stale { color: var(--amber); }

/* ─── Decision rows ─── */
.row.decision {
  grid-template-columns: 78px max-content max-content 16px minmax(0,1fr) 56px;
  gap: 12px;
}
.row.decision .kind {
  font: 600 10px/1.4 var(--mono);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-2);
}
.row.decision .kind-dispatch       { color: var(--blue); }
.row.decision .kind-status-flip    { color: var(--green); }
.row.decision .kind-cleanup        { color: var(--green); }
.row.decision .kind-close-workspace{ color: var(--green); }
.row.decision .kind-merge-pr       { color: var(--purple); }
.row.decision .kind-nudge          { color: var(--amber); }
.row.decision .kind-emit-card      { color: var(--muted); }
.row.decision .kind-purge-awaiting { color: var(--muted); }
.row.decision .scope {
  color: var(--muted);
  font: 500 11px/1.3 var(--mono);
}
.row.decision .outcome-glyph {
  font-size: 13px;
  text-align: center;
  line-height: 1;
}
.row.decision.outcome-ok   .outcome-glyph { color: var(--green); }
.row.decision.outcome-fail .outcome-glyph { color: var(--red); }
.row.decision.outcome-skip .outcome-glyph { color: var(--muted); }
.row.decision.outcome-fail { background: rgba(233,107,107,0.04); }
.row.decision .evidence {
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.5;
  word-break: break-word;
}
.row.decision .evidence .lesson { color: var(--purple); font-size: 10px; margin-left: 6px; }
.row.decision .pulse {
  color: var(--muted);
  font: 500 10px/1 var(--mono);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.row.kind-event { opacity: 0.55; }

/* ─── TODO rows ─── */
.row.todo-row { display: block; padding: 12px 16px; }
.row.todo-row .todo-row-main {
  display: grid;
  grid-template-columns: max-content max-content minmax(0,1fr) 200px;
  gap: 12px;
  align-items: baseline;
}
.row.todo-row .action-text { font: 13px/1.5 var(--sans); color: var(--text); }
.row.todo-row .todo-id {
  color: var(--muted);
  font: 500 11px/1.3 var(--mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0;
  user-select: all;
  cursor: text;
}
.row.todo-row .todo-tools {
  display: flex;
  gap: 6px;
  margin-top: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.row.todo-row .todo-context { display: flex; gap: 6px; margin-top: 8px; }
.row.todo-row .todo-context[hidden] { display: none; }
.row.todo-row .td-context-text {
  flex: 1;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  font: 12px/1.5 var(--sans);
  resize: vertical;
  min-height: 60px;
}
.row.todo-row .td-context-text:focus { outline: none; border-color: var(--line-strong); }

/* ─── Tool buttons ─── */
.tool-btn {
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 4px 9px;
  font: 500 10px/1.3 var(--mono);
  letter-spacing: 0.02em;
  cursor: pointer;
  transition: color 0.12s ease, background 0.12s ease, border-color 0.12s ease;
}
.tool-btn:hover { color: var(--text); border-color: var(--line-strong); background: rgba(255,255,255,0.03); }
.tool-btn.busy { opacity: 0.5; }
.tool-btn.ok   { background: var(--green-bg); color: var(--green); border-color: rgba(95,217,122,0.25); }
.tool-btn.err  { background: var(--red-bg);   color: var(--red);   border-color: rgba(233,107,107,0.25); }
.tool-label {
  color: var(--muted);
  font: 500 10px/1 var(--mono);
  letter-spacing: 0.02em;
  padding: 3px 4px 3px 0;
  align-self: center;
}
.tool-spacer { flex: 0 0 8px; }
.tool-btn.td-set { padding: 4px 11px; }
.tool-btn.td-set-true.active  { background: var(--amber-bg); color: var(--amber); border-color: rgba(240,179,48,0.30); }
.tool-btn.td-set-false.active { background: rgba(255,255,255,0.05); color: var(--text-2); border-color: var(--line-strong); }
.tool-btn.td-set-null.active  { background: var(--blue-bg); color: var(--blue); border-color: rgba(107,190,240,0.30); }
.tool-btn.td-set:not(.active) { opacity: 0.5; }
.tool-btn.td-dispatch { color: var(--purple); border-color: rgba(176,107,240,0.20); }
.tool-btn.td-dispatch:hover { background: var(--purple-bg); color: var(--purple); border-color: rgba(176,107,240,0.35); }
.tool-btn.td-remove { color: var(--red); border-color: rgba(233,107,107,0.18); opacity: 0.7; }
.tool-btn.td-remove:hover { background: var(--red-bg); color: var(--red); opacity: 1; border-color: rgba(233,107,107,0.30); }

/* ─── Empty state ─── */
.empty {
  color: var(--muted);
  font-style: italic;
  padding: 18px 20px;
  text-align: center;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}
.muted { color: var(--muted); padding: 18px 4px; }

/* ─── Awaiting-card buttons ─── */
.buttons { margin-top: 12px; display: flex; gap: 6px; flex-wrap: wrap; }
.btn {
  background: rgba(255,255,255,0.05);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 6px 12px;
  font: 500 11px/1.2 var(--mono);
  letter-spacing: 0.02em;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.btn:hover { background: rgba(255,255,255,0.08); border-color: var(--line-strong); }
.btn.busy { opacity: 0.5; }
.btn.ok  { background: var(--green-bg); color: var(--green); border-color: rgba(95,217,122,0.25); }
.btn.err { background: var(--red-bg);   color: var(--red);   border-color: rgba(233,107,107,0.25); }

.alt-link { color: var(--blue); text-decoration: none; font: 500 10px/1 var(--mono); margin-left: 8px; }
.alt-link:hover { text-decoration: underline; }

.footer {
  margin-top: 40px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
  color: var(--muted-2);
  font: 10px/1.6 var(--mono);
  letter-spacing: 0;
}
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
  showTab(['decisions', 'workspaces', 'todos'].includes(initial) ? initial : 'decisions');
  // Auto-refresh every 15s but preserve the current hash. We use location.reload()
  // (not <meta http-equiv="refresh">) because meta-refresh reloads from the original
  // href and drops the fragment on most browsers, snapping the user back to
  // the default Decisions tab even when they're reading TODOs.
  setInterval(() => { location.reload(); }, 15000);
});
async function openWs(btn) {
  const ws = btn.dataset.ws;
  const original = btn.dataset.original || btn.textContent;
  btn.dataset.original = original;
  btn.classList.add('busy');
  btn.textContent = 'opening…';
  try {
    const r = await fetch('http://127.0.0.1:9876/focus/' + ws, {method: 'POST'});
    if (r.ok) {
      btn.classList.remove('busy');
      btn.classList.add('ok');
      btn.textContent = '✓ ' + ws;
      setTimeout(() => { btn.classList.remove('ok'); btn.textContent = original; }, 1500);
    } else {
      const t = await r.text();
      btn.classList.remove('busy');
      btn.classList.add('err');
      btn.textContent = '✗ ' + (t || 'failed');
      setTimeout(() => { btn.classList.remove('err'); btn.textContent = original; }, 2500);
    }
  } catch (e) {
    btn.classList.remove('busy');
    btn.classList.add('err');
    btn.textContent = '✗ ' + (e.message || 'no server');
    setTimeout(() => { btn.classList.remove('err'); btn.textContent = original; }, 2500);
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
  <button class="tab" data-tab="workspaces" onclick="showTab('workspaces')">
    Workspaces <span class="tab-count">{ws_n}</span>
  </button>
  <button class="tab" data-tab="todos" onclick="showTab('todos')">
    TODOs <span class="tab-count">{p0_p1}</span>
  </button>
</div>

<div class="tab-panel" data-panel="decisions">
{decisions_html}
</div>

<div class="tab-panel" data-panel="workspaces">
{workspaces_html}
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
