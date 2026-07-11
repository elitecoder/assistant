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
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html import escape as e
from pathlib import Path

HOME = Path(os.environ["HOME"])
REPO = Path(__file__).resolve().parents[1]  # repo root (src/ for the connector base)
WORLD_PATH = HOME / ".claude/cache/world.json"
ASSISTANT_STATE = HOME / ".claude/cache/assistant-state.json"
LEGACY_TRIAGE_STATE = HOME / ".claude/cache/triage-state.json"
OBSERVER_REPORT = HOME / ".assistant/observer-latest-report.json"
RECEIPTS_DIR = HOME / ".assistant/receipts"
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


def render_metering_stats():
    """Fleet cost/behavior tiles from the last 7 days of the pulse metering
    log (~/.assistant/metrics.jsonl, written by pulse.py each pulse):
    Observer calls/day, $/day estimate, verdict-change rate, skip rate.
    The aggregation math AND the log path live in bin/metering.py (single
    source of truth, shared with the pulse); any failure — no log yet,
    module missing, aggregate missing a key — degrades to an empty string,
    never a broken page. The tile-row f-string subscripts agg, so it MUST
    stay inside the try: a KeyError here would otherwise kill the whole
    dashboard render."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import metering  # noqa: PLC0415
        agg = metering.aggregate(
            metering.read_metrics(metering.metrics_path()),
            now=int(utc_now().timestamp()),
            window_days=7,
            cost_rows=metering.read_cost_ledger(),
        )
        if not agg.get("n_pulses"):
            return ""
        return f"""
<div class="stats">
  <div class="stat"><div class="v">{agg['observer_calls_per_day']:.0f}</div><div class="k">Observer calls/day</div></div>
  <div class="stat"><div class="v">${agg['cost_per_day_usd']:.2f}</div><div class="k">$/day est · 7d (incl. triage ${agg['cost_ledger_per_day_usd']:.2f})</div></div>
  <div class="stat"><div class="v">{agg['verdict_change_rate'] * 100:.0f}%</div><div class="k">Verdict-change rate</div></div>
  <div class="stat"><div class="v">{agg['skip_rate'] * 100:.0f}%</div><div class="k">Skip rate (ws carried, no Observer call)</div></div>
</div>
"""
    except Exception:
        return ""


BRIEF_DIR = HOME / ".assistant/brief"
BRIEF_METRICS_PATH = BRIEF_DIR / "brief-metrics.jsonl"


def _brief_trend_svg(width=140, height=30, n=14):
    """Inline-SVG sparkline of decisions_pending_at_brief over the last n
    daily north-star rows. Empty string when fewer than 2 rows exist (or on
    any read problem) — the tile just shows the number alone."""
    try:
        lines = BRIEF_METRICS_PATH.read_text().splitlines()[-n:]
    except Exception:
        return ""
    vals = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        v = row.get("decisions_pending_at_brief")
        if isinstance(v, (int, float)):
            vals.append(float(v))
    if len(vals) < 2:
        return ""
    vmax = max(max(vals), 1.0)
    step = (width - 4) / (len(vals) - 1)
    pts = " ".join(
        f"{2 + i * step:.1f},{height - 3 - (v / vmax) * (height - 6):.1f}"
        for i, v in enumerate(vals))
    return (f'<svg class="brief-spark" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" stroke-linejoin="round"/></svg>')


def render_brief_tab():
    """The Brief tab (Keel M3): the latest ~/.assistant/brief/brief-<date>.json
    rendered as the design's four sections — ranked decision queue with
    one-tap action buttons (POSTing the existing /decision/act route),
    handled-overnight receipts, collapsed FYI digest, health tiles — plus the
    north-star trend from brief-metrics.jsonl. Returns (html, n_open).

    The WHOLE body is fenced: any failure (no brief yet, corrupt JSON,
    unexpected shape) degrades to a message div and never breaks the page —
    M0's lesson, same contract as render_fleet_tab."""
    try:
        return _render_brief_tab_inner()
    except Exception as exc:
        return (f'<div class="empty">Brief unavailable — {e(str(exc)[:200])}.'
                f' Rebuild on demand: <code>bin/build-morning-brief.py</code>'
                f'</div>'), 0


def _render_brief_tab_inner():
    briefs = sorted(BRIEF_DIR.glob("brief-????-??-??.json"))
    if not briefs:
        return ('<div class="empty">No brief yet — the first pulse after '
                'wake_hour builds one, or run '
                '<code>bin/build-morning-brief.py</code>.</div>'), 0
    path = briefs[-1]
    date_str = path.name[len("brief-"):-len(".json")]
    try:
        brief = json.loads(path.read_text())
    except Exception:
        return (f'<div class="empty">Brief {e(date_str)} unreadable — '
                f'rebuild with <code>bin/build-morning-brief.py</code> '
                f'(the brief is a pure derivation; nothing is lost).</div>'), 0

    queue = brief.get("queue") or []
    receipts = brief.get("handled_overnight") or []
    digest = brief.get("digest") or {}
    health = brief.get("health") or {}
    interrupts = health.get("interrupts") or {}
    cost = health.get("cost") or {}

    # ─── stats strip + north-star trend ───
    spark = _brief_trend_svg()
    trend_tile = (
        f'<div class="stat brief-trend"><div class="v">{len(queue)}'
        f'{spark}</div>'
        f'<div class="k">Decisions pending (north star)</div></div>')
    stats = f"""
<div class="stats">
  {trend_tile}
  <div class="stat"><div class="v">{len(receipts)}</div><div class="k">Handled overnight</div></div>
  <div class="stat"><div class="v">{sum(len(v) for v in digest.values())}</div><div class="k">FYI digest rows</div></div>
  <div class="stat"><div class="v">${float(cost.get('cost_per_day_usd') or 0.0):.2f}</div><div class="k">$/day · 7d</div></div>
  <div class="stat"><div class="v">{int(interrupts.get('delivered_24h') or 0)} / {int(interrupts.get('denied_24h') or 0)}</div><div class="k">Interrupts delivered / denied</div></div>
  <div class="stat"><div class="v">{int(health.get('expired_unseen_24h') or 0)}</div><div class="k">Expired unseen · 24h</div></div>
</div>"""

    # ─── 1. decision queue ───
    # Cap the rendered queue with an "N more" row, consistent with the capped
    # receipts (:40) and digest (:50) sections — an unbounded queue on an
    # incident day would blow the page up while the others stay bounded (F18).
    # The queue is lane-partitioned (escalate first), so the cap always shows
    # the highest-priority decisions; the overflow lives on the Decisions tab.
    QUEUE_RENDER_CAP = 40
    if queue:
        rows = []
        for d in queue[:QUEUE_RENDER_CAP]:
            dec_id = d.get("id") or ""
            lane = (d.get("lane") or "?")
            lane_cls = {"escalate": "t3", "staged": "t2", "digest": "scope"}.get(lane, "scope")
            urgency = d.get("urgency")
            urgency_pill = (f'<span class="pill held">{e(str(urgency))}</span>'
                            if urgency else "")
            prov = d.get("policy_id") or "triage"
            triage_info = d.get("triage") or {}
            triage_html = ""
            if triage_info.get("suggested_lane"):
                triage_html = (f'<div class="alts">triage suggests '
                               f'<code>{e(str(triage_info["suggested_lane"]))}</code>'
                               f' — {e(str(triage_info.get("rationale") or "")[:160])}</div>')
            ws_ref = d.get("ws_ref")
            ws_btn = (f'<button class="btn" data-ws="{e(ws_ref)}" '
                      f'onclick="openWs(this)">Open {e(ws_ref)}</button>'
                      if ws_ref else "")
            # Strategist-prepared decision context (Keel M6 pre-research), inline
            # when present so the spend actually reaches a human (S-O-1/S-F-2).
            # This is LLM output authored over attacker-controllable connector
            # text (PR/Slack titles + snippets), so it MUST be HTML-escaped via
            # e() — a markdown source does not make it safe (M3's XSS lesson): a
            # <script> in the context renders as inert text, never executes.
            strat_ctx = d.get("strategist_context")
            strat_html = (f'<div class="strat-ctx"><span class="strat-ctx-h">'
                          f'Strategist context</span>{e(str(strat_ctx))}</div>'
                          if strat_ctx else "")
            rows.append(f"""
<div class="card brief-dec" data-dec-row="{e(dec_id)}">
  <div class="pills">
    <span class="pill {lane_cls}">{e(lane)}</span>
    {urgency_pill}
    <span class="pill scope">via {e(str(prov))}</span>
    <span class="age">score {float(d.get('score') or 0):.0f} · {float(d.get('age_h') or 0):.1f}h old</span>
  </div>
  <div class="action">{e(d.get('title') or dec_id)}</div>
  <div class="reason">{e(d.get('snippet') or '')}</div>
  {strat_html}
  {triage_html}
  <div class="buttons">
    <button class="btn dec-act" data-dec="{e(dec_id)}" data-action="accept">{e(d.get('default_label') or 'Accept')}</button>
    <button class="btn dec-act" data-dec="{e(dec_id)}" data-action="snooze" data-minutes="60">Snooze 1h</button>
    <button class="btn dec-act" data-dec="{e(dec_id)}" data-action="reject">Reject</button>
    <button class="btn dec-act" data-dec="{e(dec_id)}" data-action="wrong_lane" title="Reject AND file a policy proposal to re-lane this (source, kind)">Wrong lane</button>
    {ws_btn}
  </div>
</div>""")
        if len(queue) > QUEUE_RENDER_CAP:
            rows.append(
                f'<div class="row more">… {len(queue) - QUEUE_RENDER_CAP} '
                f'more decision(s) — see the Decisions tab</div>')
        queue_html = "".join(rows)
    else:
        queue_html = '<div class="empty">Queue clear — nothing needs a decision.</div>'

    # ─── 2. handled overnight ───
    if receipts:
        rrows = []
        for r in receipts[:40]:
            rrows.append(f"""
<div class="row">
  <span class="ts">{e((r.get('ts') or '')[11:19])}</span>
  <span class="pill scope">{e(r.get('kind') or '')}</span>
  <span class="action-text" title="{e(r.get('evidence') or '')}">{e(r.get('evidence') or r.get('key') or '')}</span>
  <span class="outcome">{e(r.get('ws_ref') or '')}</span>
</div>""")
        receipts_html = f'<div class="feed">{"".join(rrows)}</div>'
    else:
        receipts_html = '<div class="empty">Nothing auto-handled in the last 24h.</div>'

    # ─── 3. FYI digest (grouped by source, collapsed) ───
    if digest:
        groups = []
        for src, items in digest.items():
            lis = "".join(
                f'<div class="row"><span class="ts">{e((it.get("ts") or "")[11:19])}</span>'
                f'<span class="pill scope">{e(it.get("kind") or "")}</span>'
                f'<span class="action-text">{e(it.get("title") or "")}</span>'
                f'<span class="outcome">{e(it.get("policy_id") or "")}</span></div>'
                for it in items)
            groups.append(
                f'<details class="digest-group"><summary>{e(src)} '
                f'<span class="count">{len(items)}</span></summary>'
                f'<div class="feed">{lis}</div></details>')
        digest_html = "".join(groups)
    else:
        digest_html = '<div class="empty">No FYI items in the last 24h.</div>'

    # ─── 4. health ───
    chips = []
    for src, info in sorted((health.get("event_sources") or {}).items()):
        age = info.get("latest_age_sec")
        if age is None:
            cls, label = "cold", "never"
        elif age < 3600:
            cls, label = "fresh", age_str(age)
        elif age < 6 * 3600:
            cls, label = "warm", age_str(age)
        else:
            cls, label = "cold", age_str(age)
        chips.append(f'<span class="ws-live-age {cls}" '
                     f'title="{int(info.get("count_24h") or 0)} events · 24h">'
                     f'{e(src)} · {e(label)}</span>')
    for name, hb in sorted((health.get("connectors") or {}).items()):
        # A dead connector (stale last_poll) or an expired OAuth token shows
        # cold + a reason; a healthy one shows fresh. The verdict is computed
        # in brief.py; the renderer only colors it (design M5: visible within
        # one morning). A not_configured (opted-out) connector is DELIBERATELY
        # omitted from the health chips — an optional connector nobody set up is
        # not a health signal; it lives in the Connections panel as "available".
        if hb.get("status") == "not_configured":
            continue
        stale = bool(hb.get("stale"))
        token_expired = bool(hb.get("token_expired"))
        if token_expired:
            cls, tail = "cold", "token expired"
        elif stale:
            cls, tail = "cold", "stale"
        elif hb.get("status") == "error" or not hb.get("ok", True):
            # F2: a connector that ERRORS every poll refreshes last_poll on each
            # failed poll, so it is never "stale" — but classify_connector (the
            # single source of truth) already marked it error/ok:false. Alarm on
            # THAT, else an errored-but-polling connector reads "fresh" forever
            # and never surfaces as needing attention.
            errs = hb.get("errors") or []
            tail = "error"
            if errs:
                tail = "error · " + "; ".join(str(x) for x in errs[:2])
            cls = "cold"
        else:
            cls, tail = "fresh", str(hb.get("last_poll") or "?")
        title = f"connector heartbeat · {name}"
        if hb.get("token_expiry"):
            title += f" · token_expiry {hb.get('token_expiry')}"
        chips.append(f'<span class="ws-live-age {cls}" '
                     f'title="{e(title)}">{e(name)} · {e(tail)}</span>')
    q_pending = health.get("quarantine_pending")
    if q_pending:
        chips.append(f'<span class="ws-live-age cold">quarantine · {int(q_pending)}</span>')
    health_html = (
        f'<div class="brief-health-chips">{"".join(chips)}</div>'
        if chips else
        '<div class="empty">No event-source health data (world.json has no events section yet).</div>')

    budget = interrupts.get("budget") or {}
    budget_note = (
        f'<div class="meta" style="margin-top:8px;">noise budget: '
        f'page {int(budget.get("page") or 0)}/day · '
        f'notify {int(budget.get("notify") or 0)}/day — denials are ledgered, '
        f'silence is verified, not assumed</div>')

    seen = (BRIEF_DIR / f"brief-{date_str}.seen.json").exists()
    seen_note = "seen" if seen else "unseen — viewing this tab records it"
    html = f"""
<div class="brief-root" data-brief-date="{e(date_str)}">
<div class="meta">brief {e(date_str)} · built {e(brief.get('ts') or '?')} · {e(seen_note)} · pure derivation — delete-safe, rebuild via <code>bin/build-morning-brief.py</code></div>
{stats}

<div class="section">
  <h2>Decide <span class="count">{len(queue)}</span></h2>
  {queue_html}
</div>

<div class="section">
  <h2>Handled overnight <span class="count">{len(receipts)}</span></h2>
  {receipts_html}
</div>

<div class="section">
  <h2>FYI digest <span class="count">{sum(len(v) for v in digest.values())} · grouped by source</span></h2>
  {digest_html}
</div>

<div class="section">
  <h2>Health</h2>
  {health_html}
  {budget_note}
</div>
</div>
"""
    return html, len(queue)


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
    metering_html = render_metering_stats()
    return f"""
<div class="stats">
  <div class="stat"><div class="v">{awaiting_n}</div><div class="k">Awaiting input</div></div>
  <div class="stat"><div class="v">{actions_24h}</div><div class="k">Assistant actions</div></div>
  <div class="stat"><div class="v">{counts.get('truly_active_30m', 0)}</div><div class="k">Active sessions</div></div>
  <div class="stat"><div class="v">{counts.get('human_sessions', 0)}</div><div class="k">Live · {counts.get('cron_sessions', 0)} cron hidden</div></div>
  <div class="stat"><div class="v">{triage_age}</div><div class="k">Last Assistant pulse</div></div>
</div>
{metering_html}

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

    # Read the back-off list — those workspaces should NOT appear as rows
    # in the Workspaces tab (the Assistant ignores them, so the dashboard
    # row is misleading: stale verdict + stale "NEXT" we'll never act on).
    back_off_path = HOME / ".assistant/back-off.json"
    backed_off: dict[str, dict] = {}
    if back_off_path.exists():
        try:
            for w in (json.loads(back_off_path.read_text()).get("workspaces") or []):
                ref = w.get("ws_ref")
                if ref:
                    backed_off[ref] = {
                        "reason": w.get("reason", ""),
                        "added_ts": w.get("added_ts", 0),
                    }
        except Exception:
            pass

    # Pull live signals from world.json: open workspaces + per-workspace
    # last_turn_age_sec / agent_status / cwd / pr_refs.
    open_ws: set[str] = set()
    ws_meta: dict[str, dict] = {}
    try:
        world_data = json.loads(WORLD_PATH.read_text())
        for w in world_data.get("workspaces", []) or []:
            ref = w.get("ws_ref") or w.get("ref") or w.get("workspace_ref")
            if ref and ref not in backed_off:
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

    def last_activity(transcript_path: str | None, max_chars: int = 200) -> str | None:
        """Return the most recent assistant TEXT turn from the transcript.
        Skips tool_use blocks entirely — the user cares what the agent is
        SAYING, not which tool it's invoking. None if no transcript or no
        text turn found.

        Reads only the tail (~32KB) — agents can emit many tool_uses
        between text turns, so we need a bigger window than the 12KB
        used for tool_use scanning."""
        if not transcript_path or not os.path.exists(transcript_path):
            return None
        try:
            size = os.path.getsize(transcript_path)
            with open(transcript_path, "rb") as f:
                f.seek(max(0, size - 32768))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for c in reversed(content):
                if not isinstance(c, dict):
                    continue
                if c.get("type") != "text":
                    continue
                text = (c.get("text", "") or "").strip()
                if not text:
                    continue
                text = " ".join(text.split())  # collapse whitespace
                if len(text) > max_chars:
                    text = text[:max_chars - 1] + "…"
                return text
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
        next_step = data.get("next") or ""
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

        # NOW line — most recent assistant text turn from transcript.
        now_text = None
        if sess and sess.get("transcript_path"):
            now_text = last_activity(sess["transcript_path"])

        rows.append({
            "ws_ref": ws_ref, "title": title, "cwd": cwd, "verdict": verdict,
            "summary": summary, "next": next_step, "age_sec": age_sec, "ts": ts,
            "agent_status": agent_status, "last_turn_age_sec": last_turn_age,
            "pr_refs": pr_refs, "now_text": now_text,
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

        # NOW line — agent's most recent narrative text.
        if r["now_text"]:
            now_html = (
                f'<div class="ws-now now-text">'
                f'<span class="ws-now-label">NOW</span>'
                f'<span class="ws-now-text">{e(r["now_text"])}</span>'
                f'</div>'
            )
        else:
            now_html = (
                '<div class="ws-now now-empty">'
                '<span class="ws-now-label">NOW</span>'
                '<span class="ws-now-text">no recent narrative</span>'
                '</div>'
            )

        # NEXT line — Observer's prediction of the next step.
        if r["next"]:
            next_html = (
                f'<div class="ws-next">'
                f'<span class="ws-next-label">NEXT</span>'
                f'<span class="ws-next-text">{e(r["next"])}</span>'
                f'</div>'
            )
        else:
            next_html = (
                '<div class="ws-next next-empty">'
                '<span class="ws-next-label">NEXT</span>'
                '<span class="ws-next-text">unknown</span>'
                '</div>'
            )

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
            f'  {now_html}'
            f'  {next_html}'
            f'  <div class="ws-summary">{e(r["summary"])}</div>'
            f'</div>'
        )

    # Backed-off workspaces — show a card per ws with the signals the user
    # most likely wants while deciding whether to /attend: title (so you
    # can tell which work you parked), agent's last-turn age (still
    # working?), open PR refs, when you backed it off, and the reason.
    # Pulled from world.json (live cmux state) joined with the most recent
    # observer summary on disk and the back-off list itself.
    backoff_html = ""
    if backed_off:
        # Re-fetch session signals for backed-off workspaces too. We
        # filtered them out of `open_ws` and `ws_meta` earlier; rebuild
        # a tiny meta map just for the banner.
        bo_meta: dict[str, dict] = {}
        try:
            for w in world_data.get("workspaces", []) or []:
                ref = w.get("ws_ref") or w.get("ref") or w.get("workspace_ref")
                if ref and ref in backed_off:
                    bo_meta[ref] = w
        except Exception:
            pass

        cards = []
        now_ts = utc_now().timestamp()
        for ref in sorted(backed_off.keys()):
            entry = backed_off[ref]
            world_w = bo_meta.get(ref) or {}
            title = world_w.get("title") or ""

            # Most-recent observer summary (might be stale — that's fine,
            # it's still the last thing we knew about the ws).
            summ_path = summaries_dir / f"{ref.replace(':', '_')}.json"
            sess = None
            pr_refs = []
            if summ_path.exists():
                try:
                    sd = json.loads(summ_path.read_text())
                    pr_refs = sd.get("pr_refs") or []
                    cwd_for_sess = sd.get("cwd") or ""
                    if cwd_for_sess:
                        sess = session_for_ws(cwd_for_sess)
                except Exception:
                    pass

            # Last-turn age (best effort)
            la_chip = ""
            if sess and sess.get("last_turn_age_sec") is not None:
                la = sess["last_turn_age_sec"]
                la_str = fmt_age(la)
                la_class = "fresh" if la < 600 else ("warm" if la < 1800 else "cold")
                la_chip = f'<span class="ws-live-age {la_class}" title="Last turn {la_str} ago">⟳ {la_str}</span>'

            pr_chips = ""
            if pr_refs:
                chips = "".join(f'<span class="ws-pr-chip">PR #{p}</span>' for p in pr_refs[:3])
                pr_chips = f'<span class="ws-pr-chips">{chips}</span>'

            # When backed off
            added_ts = int(entry.get("added_ts") or 0)
            since_chip = ""
            if added_ts:
                since = max(0, int(now_ts - added_ts))
                since_chip = f'<span class="backoff-since" title="Backed off {fmt_age(since)} ago">backed off {fmt_age(since)} ago</span>'

            reason = entry.get("reason") or ""
            cards.append(
                f'<div class="backoff-card">'
                f'  <div class="backoff-card-head">'
                f'    <button class="ws-ref-btn" data-ws="{e(ref)}" onclick="openWs(this)" title="Focus this workspace in cmux">{e(ref)}</button>'
                f'    <span class="ws-title">{e(title)}</span>'
                f'    {pr_chips}'
                f'    {la_chip}'
                f'    {since_chip}'
                f'  </div>'
                f'  <div class="backoff-card-reason">{e(reason)}</div>'
                f'</div>'
            )

        backoff_html = (
            f'<div class="backoff-section">'
            f'  <div class="backoff-section-head">'
            f'    <span class="backoff-label">Backed off · {len(backed_off)}</span>'
            f'    <span class="backoff-hint">run <code>/attend</code> inside the workspace to re-enable</span>'
            f'  </div>'
            f'  {"".join(cards)}'
            f'</div>'
        )

    return f'{backoff_html}<div class="ws-list">{"".join(html_rows)}</div>', len(rows)


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
            detail_text = it.get("detail") or ""
            # Per-row action toolbar — only on truly editable TODOs
            # (skip 'closed-in' historical entries shown in "Recently completed").
            if td_id:
                ad_state = "true" if ad_flag is True else ("false" if ad_flag is False else "null")
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


def _cmux_workspaces() -> dict[str, dict]:
    """Run `cmux list-workspaces --json` and return a {ws_ref: {title, color}}
    map. Graceful: any failure (cmux not running, bad JSON, timeout) returns an
    empty dict so the Fleet tab falls back to ws_ref-as-title with no color."""
    try:
        out = subprocess.run(
            ["cmux", "list-workspaces", "--json"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for w in data.get("workspaces", []) or []:
        ref = w.get("ref")
        if not ref:
            continue
        result[ref] = {
            "title": w.get("title") or "",
            "color": w.get("custom_color"),  # '#RRGGBB' or None
        }
    return result


def _first_sentence(text: str, max_len: int = 120) -> str:
    """First sentence of `text` (split on '. '), capped at max_len."""
    s = (text or "").strip()
    if not s:
        return ""
    for sep in (". ", ".\n", "\n"):
        idx = s.find(sep)
        if 0 < idx < max_len:
            s = s[:idx + 1] if sep.startswith(".") else s[:idx]
            break
    s = s.strip()
    if len(s) > max_len:
        s = s[:max_len - 1].rstrip() + "…"
    return s


def _latest_receipt(ws_ref: str) -> dict | None:
    """Newest work receipt for ws_ref (by mtime), parsed, or None. Mirrors
    write-receipt.py's slug convention (workspace:43 -> workspace-43)."""
    import glob
    slug = ws_ref.replace(":", "-")
    matches = glob.glob(str(RECEIPTS_DIR / f"{slug}-*.json"))
    if not matches:
        return None
    try:
        latest = max(matches, key=os.path.getmtime)
        return json.loads(Path(latest).read_text())
    except Exception:
        return None


def _receipt_badge_html(receipt: dict | None) -> str:
    """Quality badge + PR link + reviewer status for a DONE card.

    Dot rules (match the build spec):
      green  : ci_status=green AND reviewer_approved=true
      red    : ci_status=red   OR  outcome=abandoned
      yellow : ci_status=green OR  reviewer_approved=true (partial)
      grey   : no receipt
    Order matters: red (a known-bad signal) wins over a partial-green.
    """
    if not receipt:
        return ('<div class="receipt-badge receipt-none">'
                '<span class="receipt-dot grey"></span>'
                '<span class="receipt-label">no receipt</span></div>')

    ci = receipt.get("ci_status")
    approved = receipt.get("reviewer_approved")
    outcome = receipt.get("outcome")
    if ci == "red" or outcome == "abandoned":
        dot = "red"
    elif ci == "green" and approved is True:
        dot = "green"
    elif ci == "green" or approved is True:
        dot = "yellow"
    else:
        dot = "grey"

    parts = [f'<span class="receipt-dot {dot}"></span>']
    # PR link, if any.
    pr_url = receipt.get("pr_url")
    pr_number = receipt.get("pr_number")
    if pr_url and pr_number:
        parts.append(
            f'<a class="receipt-pr" href="{e(pr_url)}" target="_blank" '
            f'rel="noopener">PR #{e(str(pr_number))}</a>')
    # Reviewer status.
    if approved is True:
        parts.append('<span class="receipt-rev approved">approved</span>')
    elif approved is False:
        parts.append('<span class="receipt-rev pending">not approved</span>')
    # CI status word, when known.
    if ci in ("green", "red"):
        parts.append(f'<span class="receipt-ci ci-{ci}">CI {ci}</span>')
    return f'<div class="receipt-badge">{"".join(parts)}</div>'


def render_fleet_tab():
    """Kanban board of every workspace the Observer reported a candidate action
    for, grouped into four columns by classification:

      ACTIVE     — ACTIVE classification or kind=noop (running, no action yet)
      NEEDS YOU  — AWAITING_USER classification or kind=emit-card (your decision)
      STRANDED   — STRANDED or BROKEN classification (manual intervention)
      DONE       — DONE classification (finished, may need cleanup)

    Data: observer-latest-report.json -> candidate_actions[] (classification +
    evidence + summary), merged by ws_ref with `cmux list-workspaces --json`
    (title + custom_color). cmux unavailable -> ws_ref as title, no color.

    Returns (html, total_card_count). Degrades gracefully if the report is
    missing/malformed — never raises."""
    COLUMNS = [
        ("active",    "ACTIVE",    "Running · no action yet"),
        ("needs_you", "NEEDS YOU", "Waiting on your decision"),
        ("stranded",  "STRANDED",  "Needs manual intervention"),
        ("done",      "DONE",      "Finished · may need cleanup"),
    ]

    # ─── Load observer report; degrade gracefully on any failure ───
    if not OBSERVER_REPORT.exists():
        return (
            '<div class="fleet-unavailable">Observer data unavailable — '
            'report file not found (last updated: unknown)</div>'
        ), 0
    try:
        report = json.loads(OBSERVER_REPORT.read_text())
    except Exception:
        try:
            mtime = datetime.fromtimestamp(
                OBSERVER_REPORT.stat().st_mtime, timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            mtime = "unknown"
        return (
            f'<div class="fleet-unavailable">Observer data unavailable — '
            f'report malformed (last updated: {e(mtime)})</div>'
        ), 0

    cmux = _cmux_workspaces()

    # Classification → column key. ACTIVE/noop → active, AWAITING_USER/emit-card
    # → needs_you, STRANDED/BROKEN → stranded, DONE → done.
    CLASS_TO_COL = {
        "ACTIVE": "active",
        "AWAITING_USER": "needs_you",
        "STRANDED": "stranded",
        "BROKEN": "stranded",
        "DONE": "done",
    }

    # Merge candidate_actions by ws_ref. A workspace can appear in several
    # actions (e.g. status-flip + cleanup for the same stranded slot); keep the
    # first action seen for each ws_ref so each workspace is one card.
    cards: dict[str, dict] = {}
    for a in report.get("candidate_actions", []) or []:
        params = a.get("params") or {}
        ws_ref = params.get("ws_ref") or a.get("_source_ws")
        if not ws_ref:
            continue
        classification = (a.get("_classification") or "").upper()
        kind = a.get("kind") or ""
        # Column: classification first, then kind as a fallback signal.
        col = CLASS_TO_COL.get(classification)
        if col is None:
            if kind == "noop":
                col = "active"
            elif kind == "emit-card":
                col = "needs_you"
            else:
                col = "stranded"  # unknown → safest bucket for attention
        if ws_ref in cards:
            continue
        ws_info = cmux.get(ws_ref) or {}
        evidence = a.get("evidence") or ""
        summary = a.get("summary") or ""
        cards[ws_ref] = {
            "ws_ref": ws_ref,
            "title": ws_info.get("title") or ws_ref,
            "color": ws_info.get("color"),
            "col": col,
            "summary": summary,
            "evidence": evidence,
        }

    # Bucket cards into columns.
    buckets: dict[str, list] = {key: [] for key, _, _ in COLUMNS}
    for c in cards.values():
        buckets.setdefault(c["col"], []).append(c)

    total = len(cards)

    # ─── Render each column ───
    col_html = []
    for key, label, subtitle in COLUMNS:
        items = buckets.get(key, [])
        # Stable order: by ws_ref number, so the board doesn't reshuffle.
        def _ws_num(c):
            try:
                return int(str(c["ws_ref"]).split(":")[-1])
            except Exception:
                return 1 << 30
        items.sort(key=_ws_num)

        card_html = []
        for c in items:
            color = c["color"] or "#888"
            # First sentence of evidence = "what the work is".
            what = _first_sentence(c["evidence"] or c["summary"], 120)
            # Action needed = summary trimmed (only for NEEDS YOU / STRANDED).
            action_html = ""
            if key in ("needs_you", "stranded") and c["summary"]:
                act = c["summary"]
                if len(act) > 100:
                    act = act[:99].rstrip() + "…"
                action_html = (
                    f'<div class="fleet-card-action">{e(act)}</div>'
                )
            what_html = (
                f'<div class="fleet-card-what">{e(what)}</div>' if what else ""
            )
            # DONE cards carry a work-receipt quality badge: green/yellow/red
            # dot + PR link + reviewer status, or muted "no receipt" if none.
            receipt_html = ""
            if key == "done":
                receipt_html = _receipt_badge_html(_latest_receipt(c["ws_ref"]))
            card_html.append(
                f'<div class="fleet-card fleet-card-{key}" '
                f'style="border-left-color:{e(color)}">'
                f'  <div class="fleet-card-title" title="{e(c["title"])}">{e(c["title"])}</div>'
                f'  {what_html}'
                f'  {action_html}'
                f'  {receipt_html}'
                f'  <div class="fleet-card-ref">{e(c["ws_ref"])}</div>'
                f'</div>'
            )
        body = "".join(card_html) if card_html else (
            '<div class="fleet-col-empty">Nothing here</div>'
        )
        col_html.append(
            f'<div class="fleet-col fleet-col-{key}">'
            f'  <div class="fleet-col-head">'
            f'    <span class="fleet-col-label">{e(label)}</span>'
            f'    <span class="fleet-col-count">{len(items)}</span>'
            f'  </div>'
            f'  <div class="fleet-col-sub">{e(subtitle)}</div>'
            f'  <div class="fleet-col-body">{body}</div>'
            f'</div>'
        )

    return f'<div class="fleet-board">{"".join(col_html)}</div>', total


def render_pulse_health() -> str:
    """One-line banner showing whether the assistant-pulse cron is alive.
    Reads ~/.assistant/heartbeat.json and color-codes by age:
      < 10 min  → green  (healthy)
      < 30 min  → amber  (slow)
      >=30 min  → red    (probably broken; user should investigate)
    Also surfaces last pulse_idx and which orchestrator wrote the heartbeat
    (python-mechanical = pulse.py, sonnet-4-6-1m = legacy LLM Assistant)."""
    hb_path = HOME / ".assistant/heartbeat.json"
    if not hb_path.exists():
        return ('<div class="pulse-health pulse-bad">'
                '<span class="pulse-dot"></span>'
                '<span class="pulse-text">No heartbeat — Assistant has never run</span>'
                '</div>')
    try:
        hb = json.loads(hb_path.read_text())
    except Exception:
        return ('<div class="pulse-health pulse-bad">'
                '<span class="pulse-dot"></span>'
                '<span class="pulse-text">heartbeat.json unreadable</span>'
                '</div>')
    last_ts = int(hb.get("last_pulse_ts") or 0)
    if not last_ts:
        return ('<div class="pulse-health pulse-bad">'
                '<span class="pulse-dot"></span>'
                '<span class="pulse-text">heartbeat present but no last_pulse_ts</span>'
                '</div>')
    age_sec = max(0, int(utc_now().timestamp() - last_ts))
    if age_sec < 600:
        cls = "pulse-ok"
        msg = "Pulse healthy"
    elif age_sec < 1800:
        cls = "pulse-warn"
        msg = "Pulse slow"
    else:
        cls = "pulse-bad"
        msg = "Pulse stale — orchestrator may be down"
    if age_sec < 60:
        age_str = f"{age_sec}s"
    elif age_sec < 3600:
        age_str = f"{age_sec // 60}m"
    elif age_sec < 86400:
        age_str = f"{age_sec // 3600}h{(age_sec % 3600) // 60}m"
    else:
        age_str = f"{age_sec // 86400}d"
    pulse_idx = hb.get("pulse_idx", "?")
    model = hb.get("model", "?")
    return (
        f'<div class="pulse-health {cls}">'
        f'<span class="pulse-dot"></span>'
        f'<span class="pulse-text">{msg}</span>'
        f'<span class="pulse-meta">last pulse {age_str} ago · #{pulse_idx} · {e(str(model))}</span>'
        f'</div>'
    )


def _known_connectors_registry():
    """The wave-1 connector registry (display name + how-to-connect hint) from
    the connector base — the SINGLE source, so the panel can list a connector
    that has never run. Imported the standalone way (put src/ on the path); any
    failure yields an empty registry and the panel simply renders whatever
    connectors world.json carries (never breaks)."""
    try:
        if str(REPO / "src") not in sys.path:
            sys.path.insert(0, str(REPO / "src"))
        from assistant import connector as _c  # noqa: PLC0415
        return list(_c.KNOWN_CONNECTORS)
    except Exception:  # noqa: BLE001 — the panel must never break the page
        return []


def render_connections_panel(world):
    """The Connections panel (Keel M5): every KNOWN connector + its tri-state,
    so the user sees what they are connected to and what is merely available.

    Reads the SAME world.json `connectors` block the brief health section is
    derived from (single source of truth — world-scanner already classified each
    with the connector base's classify_connector; no new polling here). Optional
    connectors are honest:
      * Connected (ok)              — green dot, last-poll relative time + token
                                      expiry if present.
      * Available, not connected    — muted dot + the how-to-connect hint;
        (not_configured)             informational, NEVER styled as an error.
      * Needs attention (error)     — amber dot + the stale/expired/error reason.

    A brand-new install with nothing configured renders EVERY connector as
    "available, not connected" — an honest empty state, not blank, not an error.
    Every connector-derived string (name, hint, error text, token expiry) is
    escaped with html.escape(quote=True) — connector `errors` can carry upstream
    text and are an XSS vector into the dashboard (M3's F-class lesson). The
    whole body is fenced: any failure degrades to a small message and never
    breaks the page (M0/M3), exactly like render_brief_tab / render_fleet_tab.
    Returns (html, n_connected)."""
    try:
        return _render_connections_panel_inner(world)
    except Exception as exc:  # noqa: BLE001
        return (f'<div class="empty">Connections unavailable — '
                f'{e(str(exc)[:200])}.</div>'), 0


def _render_connections_panel_inner(world):
    registry = _known_connectors_registry()
    meta = {c.get("name"): c for c in registry if isinstance(c, dict)}
    world_conns = world.get("connectors")
    if not isinstance(world_conns, dict):
        world_conns = {}

    # Enumerate the registry FIRST (stable order, so a never-run connector still
    # shows), then any extra connector world.json carries that isn't registered.
    names = [c.get("name") for c in registry if isinstance(c, dict) and c.get("name")]
    for name in sorted(world_conns):
        if name not in names:
            names.append(name)

    rows = []
    n_connected = 0
    for name in names:
        view = world_conns.get(name)
        if not isinstance(view, dict):
            view = {"status": "not_configured"}  # known but never ran
        status = view.get("status") or "not_configured"
        info = meta.get(name) or {}
        display = info.get("display") or name
        hint = info.get("hint") or ""

        if status == "ok":
            n_connected += 1
            dot_cls, state_cls, state_label = "ok", "ok", "Connected"
            age = age_str(view.get("age_sec")) if view.get("age_sec") is not None \
                else (e(str(view.get("last_poll") or "?")))
            detail = f'last poll {e(age)}'
            texp = view.get("token_expiry")
            if texp:
                detail += f' · token expiry {e(str(texp))}'
        elif status == "error":
            dot_cls, state_cls, state_label = "attention", "attention", "Needs attention"
            if view.get("token_expired"):
                reason = "OAuth token expired — re-authorize"
            elif view.get("stale"):
                reason = "no recent poll — connector may be down"
            else:
                reason = "polling error"
            errs = view.get("errors")
            if not isinstance(errs, list):
                # F4 defense-in-depth: a wrong-typed errors field must degrade
                # THIS row, never crash errs[:3] and let the fence blank the
                # WHOLE panel (healthy rows included). classify_connector already
                # coerces to a list; the panel never trusts its input either.
                errs = [] if errs in (None, "") else [str(errs)]
            if errs:
                # Upstream/API text — escape (quote=True) against XSS (M3 F-class).
                reason += " · " + "; ".join(e(str(x)) for x in errs[:3])
            else:
                reason = e(reason)
            detail = reason
        else:  # not_configured — available, NEVER an error
            dot_cls, state_cls, state_label = "available", "available", "Available"
            detail = e(hint) if hint else "not connected"

        rows.append(
            f'<div class="conn-row">'
            f'<span class="conn-dot {dot_cls}"></span>'
            f'<span class="conn-name">{e(display)}</span>'
            f'<span class="conn-state {state_cls}">{state_label}</span>'
            f'<span class="conn-detail">{detail}</span>'
            f'</div>')

    if not rows:
        body = ('<div class="empty">No connectors known yet — the connector '
                'registry is empty.</div>')
    else:
        body = f'<div class="conn-list">{"".join(rows)}</div>'
    html = f"""
<div class="section">
  <h2>Connections <span class="count">{n_connected} connected</span></h2>
  <div class="meta">optional read-only sources · connect any time · derived from world.json (delete-safe)</div>
  {body}
</div>
"""
    return html, n_connected


def render():
    if not WORLD_PATH.exists():
        DASHBOARD_HTML.write_text("<h1>world.json not present yet — Scanner hasn't run.</h1>")
        return
    world = json.loads(WORLD_PATH.read_text())
    decisions_html, awaiting_n = render_decisions_tab(world)
    todos_html, p0_p1 = render_todos_tab(world)
    workspaces_html, ws_n = render_workspaces_tab()
    fleet_html, fleet_n = render_fleet_tab()
    brief_html, brief_n = render_brief_tab()
    connections_html, connected_n = render_connections_panel(world)
    pulse_health_html = render_pulse_health()
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

/* ─── Pulse-health banner ─── */
.pulse-health {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 8px 0 16px;
  padding: 8px 12px;
  border-radius: 5px;
  font-size: 13px;
  border: 1px solid var(--line);
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.pulse-text {
  font-weight: 600;
  letter-spacing: 0.01em;
}
.pulse-meta {
  margin-left: auto;
  font-size: 11px;
  font-family: 'Geist Mono', 'SF Mono', monospace;
  opacity: 0.75;
}
.pulse-ok {
  background: rgba(46, 160, 67, 0.06);
  border-color: rgba(46, 160, 67, 0.25);
  color: #4cd778;
}
.pulse-ok .pulse-dot {
  background: #2ea043;
  box-shadow: 0 0 6px rgba(46, 160, 67, 0.55);
}
.pulse-warn {
  background: rgba(255, 184, 108, 0.06);
  border-color: rgba(255, 184, 108, 0.28);
  color: #ffc471;
}
.pulse-warn .pulse-dot {
  background: #d4a04c;
}
.pulse-bad {
  background: rgba(220, 60, 60, 0.08);
  border-color: rgba(220, 60, 60, 0.32);
  color: #ff7777;
}
.pulse-bad .pulse-dot {
  background: #d83a3a;
  box-shadow: 0 0 6px rgba(220, 60, 60, 0.6);
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
.strat-ctx { white-space: pre-wrap; font: 11px/1.6 var(--mono); color: var(--text-2);
  background: rgba(255,255,255,0.03); border-radius: 6px; padding: 8px 10px;
  margin-top: 10px; max-height: 220px; overflow: auto; }
.strat-ctx .strat-ctx-h { display: block; font: 10px/1.4 var(--sans); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); margin-bottom: 6px; }
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
.backoff-section {
  margin-bottom: 14px;
  padding: 10px 12px 4px;
  background: rgba(255, 184, 108, 0.04);
  border: 1px solid rgba(255, 184, 108, 0.16);
  border-radius: 4px;
}
.backoff-section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
}
.backoff-label {
  font-weight: 600;
  color: rgba(255, 184, 108, 0.95);
  letter-spacing: 0.02em;
}
.backoff-hint {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.7;
  color: var(--muted);
}
.backoff-hint code {
  background: rgba(255,255,255,0.06);
  padding: 1px 4px;
  border-radius: 2px;
  font-family: 'Geist Mono', 'SF Mono', monospace;
}
.backoff-card {
  padding: 8px 0;
  border-top: 1px dashed rgba(255, 184, 108, 0.14);
}
.backoff-card:first-of-type { border-top: none; }
.backoff-card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.backoff-card-reason {
  margin-top: 4px;
  margin-left: 2px;
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
}
.backoff-since {
  margin-left: auto;
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
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
  margin-top: 6px;
  opacity: 0.86;
}
/* NOW line — agent's most recent narrative text */
.ws-now {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 11px;
  margin: 8px 0 0 4px;
  border-radius: 6px;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--line);
}
.ws-now-label {
  font: 600 9px/1.4 var(--mono);
  letter-spacing: 0.14em;
  color: var(--muted);
  padding: 2px 7px;
  border-radius: 3px;
  background: rgba(255,255,255,0.05);
  flex-shrink: 0;
  margin-top: 1px;
}
.ws-now-text {
  font: 12px/1.5 var(--sans);
  color: var(--text);
  flex: 1;
  opacity: 0.88;
}
.ws-now.now-empty {
  background: transparent;
  border-style: dashed;
}
.ws-now.now-empty .ws-now-text { color: var(--muted); font-style: italic; opacity: 0.7; }

/* NEXT line — Observer's prediction (forward-looking, not a fact) */
.ws-next {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 11px;
  margin: 6px 0 0 4px;
  border-radius: 6px;
  background: transparent;
  border: 1px dashed var(--line);
  position: relative;
}
.ws-next::before {
  content: "→";
  position: absolute;
  left: -14px;
  top: 8px;
  color: var(--muted);
  font-size: 12px;
  opacity: 0.45;
}
.ws-next-label {
  font: 600 9px/1.4 var(--mono);
  letter-spacing: 0.14em;
  color: var(--muted);
  padding: 2px 7px;
  border-radius: 3px;
  background: rgba(255,255,255,0.04);
  flex-shrink: 0;
  margin-top: 1px;
  opacity: 0.75;
}
.ws-next-text {
  font: 12px/1.5 var(--sans);
  color: var(--text-2);
  flex: 1;
  opacity: 0.78;
  font-style: italic;
}
.ws-next.next-empty .ws-next-text { color: var(--muted); opacity: 0.5; }

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

/* ─── Fleet tab — kanban board ─── */
.fleet-board {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  overflow-x: auto;
  padding-bottom: 8px;
}
.fleet-col {
  flex: 1 1 0;
  min-width: 240px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 12px 14px;
}
.fleet-col-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.fleet-col-label {
  font: 600 11px/1.4 var(--sans);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-2);
}
.fleet-col-count {
  margin-left: auto;
  font: 600 10px/1.5 var(--mono);
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--line-strong);
  color: var(--text-2);
}
.fleet-col-sub {
  font: 10px/1.4 var(--sans);
  color: var(--muted);
  letter-spacing: 0.02em;
  margin-bottom: 12px;
}
/* Column accents — match the verdict palette used on the Workspaces tab. */
.fleet-col-active   .fleet-col-label { color: var(--blue); }
.fleet-col-active   .fleet-col-count { background: var(--blue-bg);  color: var(--blue); }
.fleet-col-needs_you .fleet-col-label { color: var(--red); }
.fleet-col-needs_you .fleet-col-count { background: var(--red-bg);   color: var(--red); }
.fleet-col-stranded .fleet-col-label { color: var(--amber); }
.fleet-col-stranded .fleet-col-count { background: var(--amber-bg); color: var(--amber); }
.fleet-col-done     .fleet-col-label { color: var(--green); }
.fleet-col-done     .fleet-col-count { background: var(--green-bg); color: var(--green); }
.fleet-col-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.fleet-card {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid #888;
  border-radius: 8px;
  padding: 10px 11px;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.fleet-card:hover { border-color: var(--line-strong); background: var(--hover); }
/* NEEDS YOU cards get a subtle warm highlight so they stand out. */
.fleet-card-needs_you {
  background: rgba(233,107,107,0.06);
}
.fleet-card-needs_you:hover { background: rgba(233,107,107,0.09); }
.fleet-card-title {
  font: 500 12.5px/1.4 var(--sans);
  color: var(--text);
  letter-spacing: -0.005em;
  margin-bottom: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.fleet-card-what {
  font: 11.5px/1.5 var(--sans);
  color: var(--text-2);
  opacity: 0.82;
  margin-bottom: 6px;
}
.fleet-card-action {
  font: 11px/1.5 var(--sans);
  color: var(--text-2);
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 6px 8px;
  margin-bottom: 6px;
}
.fleet-card-ref {
  font: 500 10px/1.3 var(--mono);
  color: var(--muted);
  letter-spacing: 0;
}
.fleet-col-empty {
  font: 12px/1.5 var(--sans);
  font-style: italic;
  color: var(--muted);
  text-align: center;
  padding: 18px 8px;
  border: 1px dashed var(--line);
  border-radius: 8px;
}
.fleet-unavailable {
  color: var(--muted);
  font-style: italic;
  padding: 18px 20px;
  text-align: center;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}

/* ─── Brief tab (Keel M3) ─── */
.brief-trend .v { display: flex; align-items: flex-end; gap: 10px; }
.brief-spark { color: var(--blue); opacity: 0.85; margin-bottom: 2px; }
.digest-group {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  margin-bottom: 8px;
  overflow: hidden;
}
.digest-group summary {
  cursor: pointer;
  padding: 10px 16px;
  font: 500 12px/1.4 var(--sans);
  color: var(--text-2);
  list-style: none;
}
.digest-group summary::-webkit-details-marker { display: none; }
.digest-group summary .count {
  color: var(--muted);
  font: 500 11px/1 var(--mono);
  margin-left: 8px;
}
.digest-group[open] summary { border-bottom: 1px solid var(--line); }
.digest-group .feed { border: none; border-radius: 0; }
.brief-health-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.card.brief-dec { border-left: 2px solid var(--line-strong); }

/* ─── Connections panel (Keel M5: optional connector tri-state) ─── */
.conn-list { display: flex; flex-direction: column; gap: 1px; margin-top: 10px;
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.conn-row { display: flex; align-items: center; gap: 10px; padding: 10px 12px;
  background: var(--panel); }
.conn-row:hover { background: var(--hover); }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto;
  background: var(--muted-2); }
.conn-dot.ok { background: var(--green); box-shadow: 0 0 6px var(--green-glow); }
.conn-dot.attention { background: var(--amber); box-shadow: 0 0 6px var(--amber-glow); }
.conn-dot.available { background: var(--muted-2); }
.conn-name { font-weight: 600; color: var(--text); min-width: 150px; }
.conn-state { font: 500 11px/1 var(--mono); padding: 2px 7px; border-radius: 5px;
  border: 1px solid var(--line); color: var(--muted); }
.conn-state.ok { color: var(--green); background: var(--green-bg);
  border-color: var(--green-glow); }
.conn-state.attention { color: var(--amber); background: var(--amber-bg);
  border-color: var(--amber-glow); }
.conn-state.available { color: var(--muted); }
.conn-detail { color: var(--text-2); font-size: 12px; margin-left: auto;
  text-align: right; font-family: var(--mono); }

/* ─── Work-receipt quality badge (DONE column) ─── */
.receipt-badge {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 4px 0 6px;
  font: 500 10px/1.4 var(--mono);
}
.receipt-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  display: inline-block;
}
.receipt-dot.green  { background: var(--green); box-shadow: 0 0 5px var(--green-glow); }
.receipt-dot.yellow { background: var(--amber); box-shadow: 0 0 5px var(--amber-glow); }
.receipt-dot.red    { background: var(--red);   box-shadow: 0 0 5px var(--red-glow); }
.receipt-dot.grey   { background: transparent; border: 1px solid var(--line-strong); }
.receipt-none .receipt-label { color: var(--muted-2); font-style: italic; }
.receipt-pr {
  color: var(--blue);
  text-decoration: none;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(107,190,240,0.08);
  border: 1px solid rgba(107,190,240,0.16);
}
.receipt-pr:hover { text-decoration: underline; }
.receipt-rev.approved { color: var(--green); }
.receipt-rev.pending  { color: var(--amber); }
.receipt-ci.ci-green  { color: var(--green); }
.receipt-ci.ci-red    { color: var(--red); }
"""

    js = """
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
  if (window.location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
  if (name === 'brief') pingBriefSeen();
}
// /brief/seen signal (Keel M3): viewing the Brief tab stamps the seen
// sidecar so the unseen-degradation pass knows the brief was looked at.
// Fired at most once per page load; failures (server down) are swallowed —
// the signal is best-effort by design. The URL is ABSOLUTE to the todo-server
// (127.0.0.1:9876) so it reaches the server even when the dashboard is opened
// as a file:// page — a relative fetch resolves to file:///brief/seen and
// never arrives, silently arming the destructive unseen-TTL (F19).
let briefSeenSent = false;
function pingBriefSeen() {
  if (briefSeenSent) return;
  const root = document.querySelector('[data-brief-date]');
  if (!root) return;
  briefSeenSent = true;
  try {
    fetch('http://127.0.0.1:9876/brief/seen?date='
            + encodeURIComponent(root.dataset.briefDate),
          {method: 'POST', mode: 'cors'}).catch(() => {});
  } catch (e) { /* best-effort — ignore */ }
}
window.addEventListener('DOMContentLoaded', () => {
  const initial = (window.location.hash || '#brief').replace('#', '');
  showTab(['brief', 'decisions', 'workspaces', 'fleet', 'connections', 'todos'].includes(initial) ? initial : 'brief');
  // Auto-refresh every 15s but preserve the current hash. We use location.reload()
  // (not <meta http-equiv="refresh">) because meta-refresh reloads from the original
  // href and drops the fragment on most browsers, snapping the user back to
  // the default Brief tab even when they're reading TODOs.
  setInterval(() => { location.reload(); }, 15000);
});
async function openWs(btn) {
  const ws = btn.dataset.ws;
  const original = btn.dataset.original || btn.textContent;
  btn.dataset.original = original;
  btn.classList.add('busy');
  btn.textContent = 'opening…';
  try {
    const r = await fetch('/focus/' + ws, {method: 'POST'});
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

// Delegated click handler for Brief-tab decision buttons (Keel M3): one tap
// POSTs the existing /decision/act route; the 15s auto-refresh re-renders
// canonical queue state afterwards.
async function handleDecisionActClick(ev) {
  const btn = ev.target.closest('.dec-act');
  if (!btn) return;
  const dec = btn.dataset.dec;
  const action = btn.dataset.action;
  if (!dec || !action) return;
  let url = `/decision/act/${dec}?action=${action}`;
  if (btn.dataset.minutes) url += `&minutes=${btn.dataset.minutes}`;
  const originalText = btn.textContent;
  btn.classList.add('busy');
  try {
    const r = await fetch(url, {method: 'POST'});
    const t = await r.text();
    if (r.ok) {
      btn.classList.remove('busy'); btn.classList.add('ok');
      btn.textContent = '✓ ' + originalText;
      const row = btn.closest('[data-dec-row]');
      if (row) { row.style.opacity = '0.35'; row.style.transition = 'opacity 0.5s'; }
    } else {
      btn.classList.remove('busy'); btn.classList.add('err');
      btn.textContent = '✗ ' + (t || 'failed');
      setTimeout(() => { btn.classList.remove('err'); btn.textContent = originalText; }, 2500);
    }
  } catch (e) {
    btn.classList.remove('busy'); btn.classList.add('err');
    btn.textContent = '✗ ' + (e.message || 'no server');
    setTimeout(() => { btn.classList.remove('err'); btn.textContent = originalText; }, 2500);
  }
}
document.addEventListener('click', handleDecisionActClick);

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
      const r = await fetch(`/toggle/${id}?flag=autoDispatch&value=${value}`, {method: 'POST'});
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
      const r = await fetch(`/remove/${id}`, {method: 'POST'});
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
      const r = await fetch(`/dispatch-now/${id}`, {method: 'POST'});
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
      const r = await fetch(`/append-detail/${id}`, {
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Assistant</title>
<style>{css}</style>
<script>{js}</script>
</head><body>

<h1>Assistant</h1>
<div class="meta">{e(utc_now().strftime('%H:%M:%S UTC'))} · auto-refresh 15s · v3 (one Scanner, one Evaluator)</div>
{pulse_health_html}

<div class="tabs">
  <button class="tab" data-tab="brief" onclick="showTab('brief')">
    Brief <span class="tab-count">{brief_n}</span>
  </button>
  <button class="tab" data-tab="decisions" onclick="showTab('decisions')">
    Decisions <span class="tab-count">{awaiting_n}</span>
  </button>
  <button class="tab" data-tab="workspaces" onclick="showTab('workspaces')">
    Workspaces <span class="tab-count">{ws_n}</span>
  </button>
  <button class="tab" data-tab="fleet" onclick="showTab('fleet')">
    Fleet <span class="tab-count">{fleet_n}</span>
  </button>
  <button class="tab" data-tab="connections" onclick="showTab('connections')">
    Connections <span class="tab-count">{connected_n}</span>
  </button>
  <button class="tab" data-tab="todos" onclick="showTab('todos')">
    TODOs <span class="tab-count">{p0_p1}</span>
  </button>
</div>

<div class="tab-panel" data-panel="brief">
{brief_html}
</div>

<div class="tab-panel" data-panel="decisions">
{decisions_html}
</div>

<div class="tab-panel" data-panel="workspaces">
{workspaces_html}
</div>

<div class="tab-panel" data-panel="fleet">
{fleet_html}
</div>

<div class="tab-panel" data-panel="connections">
{connections_html}
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
