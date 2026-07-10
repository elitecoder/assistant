"""goals — the ranked goals store + deterministic planner control loop (Keel M4).

Two things live here, both pure stdlib, both LLM-free (R3: M4 adds ZERO new LLM
spend — a structural test asserts nothing in this module reaches a `claude`
call path):

  1. The GOALS STORE at ~/.claude/assistant-goals.json (design section 3): a
     sibling of assistant-todo.json — same atomic tmp+os.replace write idiom,
     same flock'd read-modify-write for mutations. Malformed store → a safe
     empty view, never a crashed pulse. The /goal skill + todo-server routes are
     the HUMAN edit path (localhost only); ANY automation that would rerank,
     change status, or call a goal "done" files a confirmation-gated
     `goal_update` proposal instead (file_goal_update_proposal) — it is NEVER
     auto-applied.

  2. The PLANNER (design section 6, "Goals are a control loop, not a scoring
     signal"). Every pulse, in goal RANK order (goal #1 gets first claim):
       - progress is stamped MECHANICALLY (last_progress_at over the actions
         ledger, merged PRs, resolved decisions, TODO completions whose refs
         match the goal's links) — never by prose, never by an LLM. Because it
         is derived-not-judgment it is written straight into goals.json
         (stamp_progress), NOT routed through the confirmation-gated proposal
         path the way a status/rerank change is;
       - a goal is STALLED when now − lastProgressAt > stallAfterHours AND it is
         active AND no open decision already blocks it. Week-keyed dedup
         (`goal-stall:<id>:<iso-week>` in the ledger) means no re-nag within an
         ISO week;
       - a stalled goal with capacity gets its NEXT playbook step staged. The
         step class is chosen from a fixed TEMPLATE table (no LLM); the staged
         TODO carries source="goal:<id>:<step-hash>" so the todo-store's
         exact-source dedup makes staging idempotent across pulses/restarts with
         zero new locking. A step whose class is in playbook.unattended AND the
         planner.autoDispatch config flag is ON → autoDispatch=true (dispatched
         overnight by the EXISTING capped dispatcher). Anything else — a gated
         class, or the SAFE DEFAULT with the flag OFF — becomes a staged
         DECISION for the morning brief instead.

SAFETY DEFAULT (M4 exit criterion: "stall precision >80% hand-verified before
autoDispatch defaults on"): planner_autodispatch() defaults to FALSE, so until
Mukul flips ~/.assistant/comms/config.json {"planner":{"autoDispatch":true}},
even unattended-class work is STAGED AS A DECISION rather than dispatched
unattended. The full autoDispatch path ships and is tested; only its effective
default is safe.

Capacity: human-originated TODOs dispatch FIRST — goal TODOs are staged at a
low priority (so the untouched dispatcher, which sorts by priority, always
prefers human work) and only into leftover ACTIVE_WS_CAP headroom after
subtracting live workspaces and pending human TODOs. Per-goal budgets
(maxActiveWs, maxStagedTodosPerNight) are enforced here. The global caps and
MAX_DISPATCH_PER_PULSE in pulse.py are NEVER touched. A saturated cap yields a
LEDGERED skip, never a silent one.

Kill switch + guards: `_paused:true` in goals.json → planner no-op (ledgered).
A stale world.json (reusing the M1/M3 staleness signal) → do NOT stage
(ledgered) — planning off a stale picture of the fleet could double-spawn work.

Paths are computed per-call (not module constants) so tests that point $HOME at
a tmp dir see fresh paths even when this module stays cached in sys.modules.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import decisions

SCHEMA = 1

# Goal ids mirror the td-NNN / dec-<hex> id discipline: a fixed prefix + a
# bounded, greppable suffix, validated by ONE regex shared with the routes
# (todo-server mirrors DEC_ID_RE the same way).
GOAL_ID_RE = re.compile(r"^goal-[0-9]{1,6}$")

# Playbook defaults (design section 3 schema). `unattended` classes are the
# reversible ones the planner may dispatch overnight; everything else is gated
# to a brief decision. Widening a playbook is fixture-gated config, never a
# code change here.
DEFAULT_UNATTENDED = ["research", "doc-draft", "pr-scaffold", "test-backfill"]
DEFAULT_GATED = ["code-change", "config-change", "external-comms"]
DEFAULT_PLAYBOOK = {"unattended": list(DEFAULT_UNATTENDED),
                    "gated": list(DEFAULT_GATED)}
DEFAULT_BUDGET = {"maxActiveWs": 2, "maxStagedTodosPerNight": 2,
                  "maxStrategistCallsPerDay": 1}
DEFAULT_STALL_AFTER_HOURS = 48

# Deterministic step templates: class → (title-suffix, detail). NO LLM picks
# these — the class is chosen by walking the goal's playbook in order and the
# text is filled from this table. The Strategist (M6) later upgrades the text;
# M4 proves the loop with templates only.
STEP_TEMPLATES = {
    "research": ("research the next unknown",
                 "Investigate the current blockers/unknowns for this goal and "
                 "write findings to a doc. Read-only research; no code changes."),
    "doc-draft": ("draft the design/status doc",
                  "Draft or update the design/status doc that moves this goal "
                  "forward. Document only — no code, no sends."),
    "pr-scaffold": ("scaffold the next PR",
                    "Scaffold the next PR for this goal in an owned repo "
                    "(structure, stubs, tests) — a reversible draft PR, never a "
                    "merge."),
    "test-backfill": ("backfill missing tests",
                      "Add the missing tests for the code this goal owns. "
                      "Reversible; opens a draft PR."),
    "code-change": ("make the next code change",
                    "Implement the next code change for this goal (gated: needs "
                    "review before it runs unattended)."),
    "config-change": ("apply the next config change",
                      "Apply the next config change for this goal (gated)."),
    "external-comms": ("draft the external message",
                       "Draft the external communication for this goal (gated; "
                       "draft-only, never sent)."),
}

# The window over which "overnight" progress and staged-work acceptance are
# measured — mirrors brief.WINDOW_SEC.
WINDOW_SEC = 24 * 3600
# How stale world.json may be before the planner refuses to stage (design:
# reuse the M1/M3 staleness signal — config.DEFAULT_STALE_HEARTBEAT_SEC=1200).
DEFAULT_WORLD_STALE_SEC = 1800

# Dispatch caps mirrored READ-ONLY from pulse.py for the capacity math. They are
# NOT the source of truth (pulse.py owns them) and are never mutated here — the
# planner only needs to know the ceiling to compute leftover headroom.
ACTIVE_WS_CAP = 5

# Staged goal TODOs get the lowest priority so the untouched dispatcher (which
# sorts bucket_b by priority) always dispatches human work first (design
# section 6: "human-originated TODOs dispatch first").
GOAL_TODO_PRIORITY = "P4"


# ─── paths (per-call, $HOME-rooted) ──────────────────────────────────────────

def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def goals_path() -> Path:
    return _home() / ".claude" / "assistant-goals.json"


def lock_path() -> Path:
    return _home() / ".claude" / "assistant-goals.lock"


def todo_path() -> Path:
    return _home() / ".claude" / "assistant-todo.json"


def ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def world_path() -> Path:
    return _home() / ".claude" / "cache" / "world.json"


def config_path() -> Path:
    return _home() / ".assistant" / "comms" / "config.json"


def proposals_path() -> Path:
    return _home() / ".assistant" / "comms" / "proposals.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts) -> float | None:
    return decisions.parse_iso(ts)


def iso_week_key(goal_id: str, now: float) -> str:
    """Week-keyed stall-nag dedup key (design section 6): one nag per goal per
    ISO week. Local calendar week so it lines up with Mukul's mornings."""
    y, w, _ = datetime.fromtimestamp(now).isocalendar()
    return f"goal-stall:{goal_id}:{y}-W{int(w):02d}"


# ─── config knobs (safe defaults; a mangled config never breaks the planner) ─

def _read_config() -> dict:
    try:
        raw = json.loads(config_path().read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _planner_cfg() -> dict:
    p = _read_config().get("planner")
    return p if isinstance(p, dict) else {}


def planner_autodispatch() -> bool:
    """The SAFE DEFAULT switch (design item 10 / M4 exit criterion). FALSE until
    Mukul flips it: stalled+capacity STAGES A DECISION instead of dispatching
    unattended work overnight, so the >80%-stall-precision bar is cleared by
    hand before any unattended action fires by default."""
    return bool(_planner_cfg().get("autoDispatch", False))


def world_stale_sec() -> int:
    v = _planner_cfg().get("world_stale_sec", DEFAULT_WORLD_STALE_SEC)
    return int(v) if isinstance(v, (int, float)) else DEFAULT_WORLD_STALE_SEC


# ─── ledger (best-effort; a ledger failure never blocks a store write) ───────

def _append_ledger(entry: dict) -> None:
    try:
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ledger_has_key(key: str, *, since: float | None = None) -> bool:
    """Has `key` been ledgered (optionally since `since` epoch)? The week-keyed
    stall dedup reads this to avoid re-nagging within an ISO week."""
    p = ledger_path()
    try:
        lines = p.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("key") != key:
            continue
        if since is None:
            return True
        ep = row.get("epoch")
        if not isinstance(ep, (int, float)):
            ep = parse_iso(row.get("ts"))
        if ep is None or ep >= since:
            return True
    return False


# ─── store I/O + validation ──────────────────────────────────────────────────

def _empty_store() -> dict:
    return {"_schema": SCHEMA, "_paused": False, "goals": []}


def _valid_goal(g) -> bool:
    """A goal is usable only if it has the required, measurable fields (design
    section 3: id, rank, title, outcome required). A malformed goal is dropped
    from the view — never crashes the load, never crashes the pulse."""
    if not isinstance(g, dict):
        return False
    if not isinstance(g.get("id"), str) or not GOAL_ID_RE.match(g["id"]):
        return False
    if not isinstance(g.get("rank"), int):
        return False
    if not isinstance(g.get("title"), str) or not g["title"].strip():
        return False
    if not isinstance(g.get("outcome"), str) or not g["outcome"].strip():
        return False
    return True


def _normalize_goal(g: dict) -> dict:
    """Fill schema defaults on a valid goal so downstream code never guesses."""
    links = g.get("links") if isinstance(g.get("links"), dict) else {}
    playbook = g.get("playbook") if isinstance(g.get("playbook"), dict) else {}
    budget = g.get("budget") if isinstance(g.get("budget"), dict) else {}
    return {
        "id": g["id"],
        "rank": int(g["rank"]),
        "title": g["title"],
        "outcome": g["outcome"],
        "status": g.get("status") if isinstance(g.get("status"), str) else "active",
        "horizon": g.get("horizon"),
        "links": {
            "repos": list(links.get("repos") or []),
            "channels": list(links.get("channels") or []),
            "jql": links.get("jql"),
            "senders": list(links.get("senders") or []),
            "todos": list(links.get("todos") or []),
            "prs": list(links.get("prs") or []),
        },
        "stallAfterHours": g.get("stallAfterHours")
        if isinstance(g.get("stallAfterHours"), (int, float))
        else DEFAULT_STALL_AFTER_HOURS,
        "lastProgressAt": g.get("lastProgressAt"),
        "createdAt": g.get("createdAt"),
        "playbook": {
            # An explicit empty list is honored (a goal may deliberately have
            # NO unattended steps); only an absent/wrong-typed key falls back to
            # the default — `x or DEFAULT` would wrongly resurrect the default
            # for a deliberate [].
            "unattended": list(playbook["unattended"])
            if isinstance(playbook.get("unattended"), list) else list(DEFAULT_UNATTENDED),
            "gated": list(playbook["gated"])
            if isinstance(playbook.get("gated"), list) else list(DEFAULT_GATED),
        },
        "budget": {
            "maxActiveWs": int(budget.get("maxActiveWs", DEFAULT_BUDGET["maxActiveWs"])),
            "maxStagedTodosPerNight": int(budget.get(
                "maxStagedTodosPerNight", DEFAULT_BUDGET["maxStagedTodosPerNight"])),
            "maxStrategistCallsPerDay": int(budget.get(
                "maxStrategistCallsPerDay", DEFAULT_BUDGET["maxStrategistCallsPerDay"])),
        },
    }


def load_goals() -> dict:
    """The validated store view. A missing/corrupt/wrong-shape file yields a
    safe empty store (never raises) so the planner degrades to a no-op instead
    of crashing the pulse. Invalid individual goals are dropped; valid ones are
    normalized with schema defaults."""
    try:
        raw = json.loads(goals_path().read_text())
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return _empty_store()
    if not isinstance(raw, dict) or not isinstance(raw.get("goals"), list):
        return _empty_store()
    goals = [_normalize_goal(g) for g in raw["goals"] if _valid_goal(g)]
    return {
        "_schema": SCHEMA,
        "_paused": bool(raw.get("_paused", False)),
        "goals": goals,
    }


@contextlib.contextmanager
def _goals_lock():
    """Single-writer flock for every read-modify-write of goals.json (the
    /goal routes, the mechanical progress stamp). Same rationale/idiom as
    decisions._writer_lock: the read and the write must be ONE critical section
    or a route edit racing the pulse's stamp loses one of the two writes."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _save_goals_unlocked(data: dict) -> None:
    """Atomic tmp+os.replace write (repo idiom, sibling to assistant-todo.json).
    MUST be called under _goals_lock for any read-modify-write."""
    p = goals_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def _load_raw_unlocked() -> dict:
    """Raw on-disk store (validated to the store shape but goals NOT dropped)
    for read-modify-write under the lock — a route edit must round-trip fields
    it doesn't understand rather than silently normalizing them away."""
    try:
        raw = json.loads(goals_path().read_text())
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return _empty_store()
    if not isinstance(raw, dict) or not isinstance(raw.get("goals"), list):
        return _empty_store()
    raw.setdefault("_schema", SCHEMA)
    raw.setdefault("_paused", False)
    return raw


def list_goals() -> list[dict]:
    """Goals in rank order (the view the /goal/list route + brief boost use)."""
    goals = load_goals()["goals"]
    return sorted(goals, key=lambda g: (g.get("rank", 1 << 30), g.get("id", "")))


def next_goal_id(goals: list[dict]) -> str:
    used = set()
    for g in goals:
        gid = g.get("id") if isinstance(g, dict) else None
        if isinstance(gid, str) and gid.startswith("goal-"):
            try:
                used.add(int(gid[len("goal-"):]))
            except ValueError:
                pass
    return f"goal-{(max(used) + 1) if used else 1}"


# ─── human edit path (routes/skill call these directly; automation does NOT) ──

def add_goal(*, title: str, outcome: str, rank: int | None = None,
             horizon=None, links=None, playbook=None, budget=None,
             stall_after_hours: float | None = None,
             now: float | None = None) -> tuple[dict | None, str | None]:
    """Add a goal (HUMAN path — the /goal/add route). Automation never calls
    this; it files a goal_update proposal instead. Returns (goal, error)."""
    now = now if now is not None else time.time()
    title = (title or "").strip()
    outcome = (outcome or "").strip()
    if not title:
        return None, "title required"
    if not outcome:
        return None, "outcome required (must be measurable)"
    with _goals_lock():
        raw = _load_raw_unlocked()
        goals = raw["goals"]
        gid = next_goal_id(goals)
        if rank is None:
            ranks = [g.get("rank") for g in goals if isinstance(g.get("rank"), int)]
            rank = (max(ranks) + 1) if ranks else 1
        goal = {
            "id": gid,
            "rank": int(rank),
            "title": title,
            "outcome": outcome,
            "status": "active",
            "horizon": horizon,
            "links": links if isinstance(links, dict) else {},
            "stallAfterHours": float(stall_after_hours)
            if isinstance(stall_after_hours, (int, float))
            else DEFAULT_STALL_AFTER_HOURS,
            "lastProgressAt": None,
            "createdAt": utc_iso(now),
            "playbook": playbook if isinstance(playbook, dict)
            else dict(DEFAULT_PLAYBOOK),
            "budget": budget if isinstance(budget, dict) else dict(DEFAULT_BUDGET),
        }
        goals.append(goal)
        _save_goals_unlocked(raw)
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"goal:add:{gid}", "kind": "goal-edit",
        "ws_ref": "(goals)", "outcome": "verified",
        "evidence": f"added {gid} rank={goal['rank']}: {title[:100]}",
    })
    return goal, None


# Fields a human route may edit directly. rank changes go through rerank().
_HUMAN_EDITABLE = {"title", "outcome", "status", "horizon", "links",
                   "stallAfterHours", "playbook", "budget"}


def update_goal(goal_id: str, changes: dict,
                now: float | None = None) -> tuple[dict | None, str | None]:
    """Edit a goal in place (HUMAN path — the /goal/update route). Only the
    human-editable fields are touched; lastProgressAt is mechanical and cannot
    be set here. Returns (goal, error)."""
    now = now if now is not None else time.time()
    if not isinstance(changes, dict):
        return None, "changes must be an object"
    bad = set(changes) - _HUMAN_EDITABLE
    if bad:
        return None, f"not editable: {sorted(bad)}"
    with _goals_lock():
        raw = _load_raw_unlocked()
        target = next((g for g in raw["goals"] if g.get("id") == goal_id), None)
        if target is None:
            return None, f"goal {goal_id!r} not found"
        for k, v in changes.items():
            target[k] = v
        target["updatedAt"] = utc_iso(now)
        _save_goals_unlocked(raw)
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"goal:update:{goal_id}", "kind": "goal-edit",
        "ws_ref": "(goals)", "outcome": "verified",
        "evidence": f"updated {goal_id}: {sorted(changes)}",
    })
    return target, None


def rerank(order: list[str],
           now: float | None = None) -> tuple[bool, str | None]:
    """Reassign ranks so `order` becomes ranks 1..N (HUMAN path — the
    /goal/rerank route). Every listed id must exist; unlisted goals keep their
    relative order after the listed ones. Ranks stay unique."""
    now = now if now is not None else time.time()
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return False, "order must be a list of goal ids"
    with _goals_lock():
        raw = _load_raw_unlocked()
        by_id = {g.get("id"): g for g in raw["goals"]}
        for gid in order:
            if gid not in by_id:
                return False, f"goal {gid!r} not found"
        rank = 1
        seen = set()
        for gid in order:
            by_id[gid]["rank"] = rank
            seen.add(gid)
            rank += 1
        # Unlisted goals keep their prior relative order, appended after.
        for g in sorted((g for g in raw["goals"] if g.get("id") not in seen),
                        key=lambda g: g.get("rank", 1 << 30)):
            g["rank"] = rank
            rank += 1
        _save_goals_unlocked(raw)
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": "goal:rerank", "kind": "goal-edit",
        "ws_ref": "(goals)", "outcome": "verified",
        "evidence": f"rerank order={order[:10]}",
    })
    return True, None


def set_paused(paused: bool, now: float | None = None) -> None:
    """Flip the kill switch (HUMAN path). _paused:true → planner no-op."""
    now = now if now is not None else time.time()
    with _goals_lock():
        raw = _load_raw_unlocked()
        raw["_paused"] = bool(paused)
        _save_goals_unlocked(raw)
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"goal:paused:{bool(paused)}", "kind": "goal-edit",
        "ws_ref": "(goals)", "outcome": "verified",
        "evidence": f"_paused={bool(paused)}",
    })


# ─── confirmation-gated automation path (never auto-applies) ──────────────────

def _pending_goal_update_keys(path: Path) -> set[str]:
    """(goal_id, sorted-change-fields) keys with a live goal_update proposal —
    blocks an automation re-file (mirrors policy._pending_policy_keys)."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return set()
    keys: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "goal_update":
            continue
        if obj.get("status") not in ("pending", "confirmed"):
            continue
        gid = obj.get("goal_id")
        fields = ",".join(sorted((obj.get("changes") or {}).keys()))
        keys.add(f"{gid}|{fields}")
    return keys


def file_goal_update_proposal(goal_id: str, changes: dict, *, reason: str,
                              source: str = "planner",
                              path: Path | None = None) -> dict | None:
    """The ONLY way automation may touch a goal's rank/status/"looks done": a
    confirmation-gated type=`goal_update` proposal appended to proposals.jsonl
    (reusing the M2 low-volume human-confirmation channel). NEVER auto-applied —
    Mukul confirms it in the brief. Deduped against a pending/confirmed
    proposal for the same (goal, change-fields). Returns the entry or None."""
    if not isinstance(changes, dict) or not changes:
        return None
    p = path if path is not None else proposals_path()
    fields = ",".join(sorted(changes.keys()))
    if f"{goal_id}|{fields}" in _pending_goal_update_keys(p):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    entry = {
        "ts": ts,
        "id": ts,
        "type": "goal_update",
        "status": "pending",
        "source": source,
        "goal_id": goal_id,
        "changes": changes,
        "reason": (reason or "")[:300],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ─── mechanical progress linker (pure; NO prose, NO LLM) ──────────────────────

def _norm_repo(r) -> str | None:
    """Normalize a repo ref to a comparable token: lowercased basename, so
    'elitecoder/assistant', 'github.com/elitecoder/assistant' and 'assistant'
    all match."""
    if not isinstance(r, str) or not r.strip():
        return None
    s = r.strip().lower().rstrip("/")
    return s.rsplit("/", 1)[-1] if "/" in s else s


def goal_link_tokens(goal: dict) -> dict[str, set]:
    """Typed token sets a progress artifact must intersect to count. Typed (not
    a single flat set) so a PR number can never collide with a repo name — this
    precision is what keeps the stall-precision harness honest."""
    links = goal.get("links") or {}
    return {
        "goals": {goal.get("id")},
        "repos": {t for t in (_norm_repo(r) for r in (links.get("repos") or [])) if t},
        "prs": {str(x).strip() for x in (links.get("prs") or []) if str(x).strip()},
        "todos": {str(x).strip() for x in (links.get("todos") or []) if str(x).strip()},
        "channels": {str(x).strip() for x in (links.get("channels") or []) if str(x).strip()},
        "senders": {str(x).strip().lower() for x in (links.get("senders") or []) if str(x).strip()},
    }


def _matches(goal_tokens: dict[str, set], art_tokens: dict[str, set]) -> bool:
    """A candidate artifact belongs to the goal iff ANY typed set intersects
    (or the artifact is explicitly tagged with the goal id)."""
    for kind, gset in goal_tokens.items():
        if gset and (art_tokens.get(kind) or set()) & gset:
            return True
    return False


def ledger_artifacts(rows: list[dict]) -> list[dict]:
    """Normalize actions-ledger rows to {ts, tokens}. Matches on the structured
    ref fields a row may carry (td, repo, pr/prs, ws_ref goal tag, an explicit
    goal field) — NEVER a free-text evidence scan, which would inflate false
    positives and wreck stall precision."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("epoch")
        if not isinstance(ts, (int, float)):
            ts = parse_iso(row.get("ts"))
        if ts is None:
            continue
        refs = row.get("refs") if isinstance(row.get("refs"), dict) else {}
        todos = set()
        for v in (row.get("td"), refs.get("td")):
            if v:
                todos.add(str(v))
        prs = set()
        for v in (row.get("pr"), refs.get("pr")):
            if v:
                prs.add(str(v))
        for v in (row.get("prs") or []):
            if v:
                prs.add(str(v))
        repos = set()
        for v in (row.get("repo"), refs.get("repo")):
            t = _norm_repo(v)
            if t:
                repos.add(t)
        goals = set()
        for v in (row.get("goal"), refs.get("goal_id"), refs.get("goal")):
            if v:
                goals.add(str(v))
        channels = set()
        for v in (row.get("channel"), refs.get("channel")):
            if v:
                channels.add(str(v))
        senders = set()
        for v in (row.get("sender"), refs.get("sender")):
            if v:
                senders.add(str(v).lower())
        out.append({"ts": float(ts), "kind": row.get("kind"),
                    "tokens": {"todos": todos, "prs": prs, "repos": repos,
                               "goals": goals, "channels": channels,
                               "senders": senders}})
    return out


def pr_artifacts(prs: list[dict]) -> list[dict]:
    """Merged-PR records → {ts, tokens}. Only MERGED PRs count as progress."""
    out: list[dict] = []
    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        if (pr.get("state") or "").lower() != "merged":
            continue
        ts = pr.get("merged_epoch")
        if not isinstance(ts, (int, float)):
            ts = parse_iso(pr.get("merged_at"))
        if ts is None:
            continue
        prtok = set()
        for v in (pr.get("number"), pr.get("url")):
            if v is not None and str(v).strip():
                prtok.add(str(v).strip())
        repo = _norm_repo(pr.get("repo"))
        out.append({"ts": float(ts), "kind": "merged-pr",
                    "tokens": {"prs": prtok,
                               "repos": {repo} if repo else set()}})
    return out


def decision_artifacts(folded: dict[str, dict]) -> list[dict]:
    """RESOLVED decisions (accepted/edited/auto_done) → {ts, tokens}. A resolved
    decision that references the goal (goal_refs) or a linked pr/repo/todo is a
    mechanical progress signal."""
    out: list[dict] = []
    for rec in folded.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in ("accepted", "edited", "auto_done"):
            continue
        res = rec.get("resolution") or {}
        ts = parse_iso(res.get("ts"))
        if ts is None:
            ts = rec.get("epoch")
        if not isinstance(ts, (int, float)):
            continue
        refs = rec.get("refs") if isinstance(rec.get("refs"), dict) else {}
        goals = {str(x) for x in (rec.get("goal_refs") or []) if x}
        prs = {str(refs.get("pr"))} if refs.get("pr") else set()
        repos = set()
        t = _norm_repo(refs.get("repo"))
        if t:
            repos.add(t)
        todos = {str(refs.get("td"))} if refs.get("td") else set()
        out.append({"ts": float(ts), "kind": "resolved-decision",
                    "tokens": {"goals": goals, "prs": prs, "repos": repos,
                               "todos": todos}})
    return out


def todo_artifacts(todo_data: dict) -> list[dict]:
    """COMPLETED TODOs → {ts, tokens}. A done TODO whose id is a goal link, or
    whose source is goal:<id>:…, is progress on that goal."""
    out: list[dict] = []
    if not isinstance(todo_data, dict):
        return out
    buckets = []
    for name in ("items", "completed"):
        b = todo_data.get(name)
        if isinstance(b, list):
            buckets.extend(b)
    for it in buckets:
        if not isinstance(it, dict) or it.get("status") != "done":
            continue
        ts = parse_iso(it.get("statusUpdatedAt")) or parse_iso(it.get("completedAt"))
        if ts is None:
            continue
        goals = set()
        src = it.get("source") or ""
        if isinstance(src, str) and src.startswith("goal:"):
            parts = src.split(":")
            if len(parts) >= 2:
                goals.add(parts[1])
        tid = it.get("id")
        todos = {str(tid)} if tid else set()
        out.append({"ts": float(ts), "kind": "done-todo",
                    "tokens": {"goals": goals, "todos": todos}})
    return out


def last_progress_at(goal: dict, artifacts: list[dict],
                     now: float | None = None) -> float | None:
    """PURE progress linker (design section 6): lastProgressAt = max(ts) over
    every artifact whose refs match the goal's links. No prose, no LLM — the
    whole function is a typed set-intersection + a max. Returns an epoch or
    None (no matching artifact ever seen)."""
    gtok = goal_link_tokens(goal)
    best: float | None = None
    for art in artifacts:
        ts = art.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if now is not None and ts > now:
            continue
        if _matches(gtok, art.get("tokens") or {}):
            if best is None or ts > best:
                best = ts
    return best


def gather_artifacts(now: float) -> list[dict]:
    """Assemble every mechanical progress source under one roof: the actions
    ledger, merged PRs derived from the ledger's merge rows, resolved decisions,
    and completed TODOs. Read-only — no store is mutated."""
    ledger_rows = _read_jsonl(ledger_path())
    prs = _merged_prs_from_ledger(ledger_rows)
    folded = decisions.fold(decisions.read_log())
    todo_data = _read_json(todo_path())
    arts = ledger_artifacts(ledger_rows)
    arts += pr_artifacts(prs)
    arts += decision_artifacts(folded)
    arts += todo_artifacts(todo_data if isinstance(todo_data, dict) else {})
    return arts


def _merged_prs_from_ledger(rows: list[dict]) -> list[dict]:
    """Derive merged-PR artifacts from the ledger's merge rows (kind
    merge-dispatched/merge-pr). Stdlib only, no gh/network in the linker."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") not in ("merge-dispatched", "merge-pr"):
            continue
        ep = row.get("epoch")
        if not isinstance(ep, (int, float)):
            ep = parse_iso(row.get("ts"))
        if ep is None:
            continue
        refs = row.get("refs") if isinstance(row.get("refs"), dict) else {}
        out.append({
            "state": "merged",
            "merged_epoch": ep,
            "number": row.get("pr") or refs.get("pr"),
            "url": refs.get("pr_url"),
            "repo": row.get("repo") or refs.get("repo"),
        })
    return out


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def stamp_progress(now: float | None = None) -> dict:
    """Stamp each active goal's lastProgressAt = last_progress_at(goal). Written
    STRAIGHT into goals.json (not via the confirmation-gated proposal path):
    lastProgressAt is DERIVED-NOT-JUDGMENT — a pure max over mechanical
    artifacts — so gating it behind a human tap would be ceremony. Only advances
    (never rewinds) the stamp, and only writes when something changed, so a
    quiet pulse costs no write. Returns a summary."""
    now = now if now is not None else time.time()
    arts = gather_artifacts(now)
    stamped: list[dict] = []
    with _goals_lock():
        raw = _load_raw_unlocked()
        changed = False
        for g in raw["goals"]:
            if not _valid_goal(g):
                continue
            norm = _normalize_goal(g)
            if norm.get("status") != "active":
                continue
            lp = last_progress_at(norm, arts, now=now)
            if lp is None:
                continue
            prior = parse_iso(g.get("lastProgressAt"))
            if prior is None or lp > prior:
                g["lastProgressAt"] = utc_iso(lp)
                changed = True
                stamped.append({"id": g.get("id"), "lastProgressAt": g["lastProgressAt"]})
        if changed:
            _save_goals_unlocked(raw)
    return {"stamped": stamped, "n": len(stamped)}


# ─── stall detection ─────────────────────────────────────────────────────────

def _stall_anchor(goal: dict) -> float | None:
    """The epoch the stall clock ticks from: lastProgressAt if we ever saw
    progress, else the goal's createdAt (a brand-new goal that has done nothing
    for stallAfterHours is legitimately stalled). None when neither is known —
    can't measure, so never stalled."""
    lp = parse_iso(goal.get("lastProgressAt"))
    if lp is not None:
        return lp
    return parse_iso(goal.get("createdAt"))


def goal_blocked_by_open_decision(goal_id: str) -> bool:
    """True when an OPEN decision already references this goal — a stall we're
    already asking the human about must not also be auto-nagged."""
    for d in decisions.open_decisions():
        if goal_id in (d.get("goal_refs") or []):
            return True
    return False


def is_stalled(goal: dict, now: float | None = None) -> bool:
    """now − lastProgressAt > stallAfterHours AND status=active AND no open
    decision blocks it (design section 6). Pure over the goal + decision store."""
    now = now if now is not None else time.time()
    if goal.get("status") != "active":
        return False
    anchor = _stall_anchor(goal)
    if anchor is None:
        return False
    stall_after = goal.get("stallAfterHours")
    if not isinstance(stall_after, (int, float)):
        stall_after = DEFAULT_STALL_AFTER_HOURS
    if now - anchor <= stall_after * 3600:
        return False
    if goal_blocked_by_open_decision(goal.get("id")):
        return False
    return True


# ─── planner ─────────────────────────────────────────────────────────────────

def _step_hash(goal_id: str, step_class: str) -> str:
    """Stable step hash → source="goal:<id>:<hash>". One hash per (goal, class)
    so re-running the same pulse produces the SAME source and the todo-store's
    exact-source dedup makes staging idempotent (design section 6)."""
    return hashlib.sha256(f"{goal_id}:{step_class}".encode()).hexdigest()[:8]


def goal_source(goal_id: str, step_class: str) -> str:
    return f"goal:{goal_id}:{_step_hash(goal_id, step_class)}"


def _staged_sources(todo_data: dict) -> tuple[set, set]:
    """(open_or_inflight_sources, done_sources) for goal:<…> TODOs already in
    the store. Drives step selection: an OPEN goal step is still in progress
    (don't stage a new one); a DONE one lets the planner advance to the next
    playbook step."""
    open_src: set = set()
    done_src: set = set()
    if not isinstance(todo_data, dict):
        return open_src, done_src
    for name in ("items", "completed", "removed"):
        for it in (todo_data.get(name) or []):
            if not isinstance(it, dict):
                continue
            src = it.get("source")
            if not isinstance(src, str) or not src.startswith("goal:"):
                continue
            if it.get("status") == "done":
                done_src.add(src)
            elif name == "items" and it.get("status") in (
                    "open", "in-progress", "blocked", None):
                open_src.add(src)
    return open_src, done_src


def select_next_step(goal: dict,
                     todo_data: dict) -> tuple[str, str, str] | None:
    """Deterministic next step for a stalled goal (NO LLM). Walk the playbook —
    unattended classes first, then gated — and return the FIRST class that has
    no live goal-TODO yet:
      • class already OPEN/in-flight → return None (still working it; never
        stack a second step);
      • class already DONE → advance to the next class;
      • class not staged → stage it.
    Returns (step_class, title, detail) or None when nothing to stage (all steps
    done, or the current one is still in flight)."""
    open_src, done_src = _staged_sources(todo_data)
    playbook = goal.get("playbook") or {}
    classes = list(playbook.get("unattended") or []) + list(playbook.get("gated") or [])
    for step_class in classes:
        src = goal_source(goal.get("id"), step_class)
        if src in open_src:
            return None  # current step still in flight — don't stack another
        if src in done_src:
            continue     # completed — advance to the next playbook step
        suffix, detail = STEP_TEMPLATES.get(
            step_class, ("advance the goal", "Advance this goal's next step."))
        title = f"[{goal.get('id')}] {suffix}: {goal.get('title')}"
        return step_class, title[:200], detail
    return None


def _world_active_ws(world: dict) -> int:
    """Live-session count from world.json (the same signal pick-open-todos uses
    for in-flight detection)."""
    live = world.get("live_sessions")
    if isinstance(live, list):
        return len(live)
    return 0


def _world_is_stale(world: dict | None, now: float) -> bool:
    if not isinstance(world, dict):
        return True
    built = parse_iso((world.get("_meta") or {}).get("built_at"))
    if built is None:
        return True
    return (now - built) > world_stale_sec()


def _pending_human_todos(todo_data: dict) -> int:
    """Open autoDispatch TODOs that will consume ws headroom this pulse and are
    NOT goal-staged — human work dispatches first, so it reserves headroom."""
    n = 0
    if not isinstance(todo_data, dict):
        return 0
    for it in (todo_data.get("items") or []):
        if not isinstance(it, dict) or it.get("status") != "open":
            continue
        if it.get("autoDispatch") is not True:
            continue
        if it.get("dispatchedAt") or it.get("dispatchedWs"):
            continue
        src = it.get("source") or ""
        if isinstance(src, str) and src.startswith("goal:"):
            continue
        n += 1
    return n


def _goal_active_ws(goal_id: str, todo_data: dict, world: dict) -> int:
    """Live workspaces attributable to a goal: goal:<id>:… TODOs dispatched to
    a ws that is still live (per-goal maxActiveWs budget)."""
    live = set()
    for s in (world.get("live_sessions") or []):
        if isinstance(s, dict) and s.get("ws_ref"):
            live.add(s.get("ws_ref"))
    n = 0
    prefix = f"goal:{goal_id}:"
    for it in (todo_data.get("items") or []):
        if not isinstance(it, dict):
            continue
        src = it.get("source") or ""
        if not (isinstance(src, str) and src.startswith(prefix)):
            continue
        dws = it.get("dispatchedWs") or it.get("dispatched_ws")
        if dws and dws in live:
            n += 1
    return n


def _goal_staged_tonight(goal_id: str, todo_data: dict, now: float) -> int:
    """goal:<id>:… TODOs created within the last 24h (maxStagedTodosPerNight)."""
    cutoff = now - WINDOW_SEC
    prefix = f"goal:{goal_id}:"
    n = 0
    for name in ("items", "completed"):
        for it in (todo_data.get(name) or []):
            if not isinstance(it, dict):
                continue
            src = it.get("source") or ""
            if not (isinstance(src, str) and src.startswith(prefix)):
                continue
            created = parse_iso(it.get("createdAt")) or parse_iso(it.get("stagedAt"))
            if created is None or created >= cutoff:
                n += 1
    return n


def _stage_todo(goal: dict, step_class: str, title: str, detail: str,
                autodispatch: bool, now: float) -> tuple[dict | None, bool]:
    """Append a goal TODO to assistant-todo.json under a flock, deduped by exact
    source (idempotent across pulses/restarts, zero new locking beyond this one
    lock). Returns (item, created)."""
    src = goal_source(goal.get("id"), step_class)
    tp = todo_path()
    with _goals_lock():  # reuse the goals lock — one writer for goal-staged work
        data = _read_json(tp)
        if not isinstance(data, dict):
            data = {"_schema": 1, "items": []}
        items = data.setdefault("items", [])
        for name in ("items", "completed", "removed"):
            for it in (data.get(name) or []):
                if isinstance(it, dict) and it.get("source") == src:
                    return it, False  # exact-source dedup → idempotent
        # Monotonic id across every bucket (mirrors the /todo skill's next_id).
        used = set()
        for name in ("items", "completed", "removed"):
            for it in (data.get(name) or []):
                m = (it.get("id") or "")
                if m.startswith("td-"):
                    try:
                        used.add(int(m[len("td-"):]))
                    except ValueError:
                        pass
        tid = f"td-{(max(used) + 1) if used else 1:03d}"
        item = {
            "id": tid,
            "priority": GOAL_TODO_PRIORITY,
            "title": title,
            "detail": detail,
            "source": src,
            "goalId": goal.get("id"),
            "stepClass": step_class,
            "createdAt": utc_iso(now),
            "status": "open",
            "autoDispatch": bool(autodispatch),
        }
        items.append(item)
        data["_lastUpdated"] = utc_iso(now)
        tmp = tp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, tp)
    return item, True


def _stage_decision(goal: dict, step_class: str, title: str, detail: str,
                    now: float) -> tuple[dict | None, bool]:
    """Stage a goal step as a brief DECISION (the gated path AND the safe
    default). Uses the M2 decision machinery with a synthesized goal event, so
    the decision id is idempotent on (source, step-hash, class) exactly like the
    TODO source key. goal_refs carries the goal so the brief's goal_boost and
    staged_accept_rate see it."""
    src = goal_source(goal.get("id"), step_class)
    event = {
        "id": f"goalstep-{src}",
        "source": f"goal:{goal.get('id')}",
        "external_id": src,
        "kind": "goal_step",
        "title": title,
        "snippet": detail,
        "ts": utc_iso(now),
        "refs": {"goal_id": goal.get("id")},
        "raw_path": None,
    }
    rec, created = decisions.open_decision(
        event=event, lane="staged", policy_id="planner",
        action={"class": step_class}, urgency="low", ttl_h=72,
        goal_refs=[goal.get("id")], now=now)
    return rec, created


def plan_pass(now: float | None = None) -> dict:
    """One planner pass (design section 6). Assumes progress is already stamped
    (pulse_step stamps first). Goal RANK order — goal #1 gets first claim on the
    leftover headroom. Returns a summary of everything it did/skipped, all of it
    also ledgered."""
    now = now if now is not None else time.time()
    store = load_goals()
    summary = {"paused": False, "stale_world": False, "staged_todos": [],
               "staged_decisions": [], "skipped": [], "stalls": 0}

    if store.get("_paused"):
        summary["paused"] = True
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": "planner:paused", "kind": "planner-skip",
            "ws_ref": "(goals)", "outcome": "skipped",
            "evidence": "_paused:true — planner no-op",
        })
        return summary

    world = _read_json(world_path())
    if _world_is_stale(world, now):
        summary["stale_world"] = True
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": "planner:stale-world", "kind": "planner-skip",
            "ws_ref": "(goals)", "outcome": "skipped",
            "evidence": "world.json missing/stale — refusing to stage",
        })
        return summary

    todo_data = _read_json(todo_path()) or {}
    active_ws = _world_active_ws(world)
    human_pending = _pending_human_todos(todo_data)
    # Leftover ACTIVE_WS_CAP headroom AFTER reserving live ws + human TODOs.
    headroom = ACTIVE_WS_CAP - active_ws - human_pending
    autodispatch_on = planner_autodispatch()
    todos_staged = 0

    for goal in sorted(store["goals"],
                       key=lambda g: (g.get("rank", 1 << 30), g.get("id"))):
        gid = goal.get("id")
        if not is_stalled(goal, now):
            continue
        summary["stalls"] += 1
        # Week-keyed dedup: no re-nag within an ISO week.
        wk = iso_week_key(gid, now)
        if ledger_has_key(wk):
            summary["skipped"].append({"goal": gid, "reason": "week-deduped"})
            continue
        # Re-read the todo store each goal so within-pass staging is visible to
        # the next goal's dedup/budget math.
        todo_data = _read_json(todo_path()) or {}
        step = select_next_step(goal, todo_data)
        if step is None:
            summary["skipped"].append({"goal": gid, "reason": "no-step-or-in-flight"})
            continue
        step_class, title, detail = step
        budget = goal.get("budget") or {}
        # Per-goal maxStagedTodosPerNight.
        if _goal_staged_tonight(gid, todo_data, now) >= budget.get(
                "maxStagedTodosPerNight", DEFAULT_BUDGET["maxStagedTodosPerNight"]):
            _ledger_skip(gid, "budget-staged-per-night", now)
            summary["skipped"].append({"goal": gid, "reason": "budget-staged-per-night"})
            continue
        unattended = step_class in (goal.get("playbook") or {}).get("unattended", [])
        effective_autodispatch = autodispatch_on and unattended

        if effective_autodispatch:
            # Per-goal maxActiveWs + global leftover headroom (saturated → skip).
            if _goal_active_ws(gid, todo_data, world) >= budget.get(
                    "maxActiveWs", DEFAULT_BUDGET["maxActiveWs"]):
                _ledger_skip(gid, "budget-max-active-ws", now)
                summary["skipped"].append({"goal": gid, "reason": "budget-max-active-ws"})
                continue
            if todos_staged >= headroom:
                _ledger_skip(gid, "capacity-saturated", now,
                             extra=f"headroom={headroom} active={active_ws} "
                                   f"human={human_pending}")
                summary["skipped"].append({"goal": gid, "reason": "capacity-saturated"})
                continue
            item, created = _stage_todo(goal, step_class, title, detail,
                                        autodispatch=True, now=now)
            if created and item is not None:
                todos_staged += 1
                summary["staged_todos"].append({"goal": gid, "todo": item.get("id"),
                                                "step": step_class})
                _ledger_stage(gid, item.get("id"), step_class, "todo", now)
                _append_ledger({
                    "ts": utc_iso(now), "epoch": int(now), "key": wk,
                    "kind": "goal-stall-nag", "ws_ref": "(goals)",
                    "outcome": "verified",
                    "evidence": f"staged todo {item.get('id')} for {gid}",
                })
            else:
                summary["skipped"].append({"goal": gid, "reason": "todo-dedup"})
        else:
            rec, created = _stage_decision(goal, step_class, title, detail, now)
            if created and rec is not None:
                summary["staged_decisions"].append({"goal": gid, "dec": rec.get("id"),
                                                    "step": step_class})
                _ledger_stage(gid, rec.get("id"), step_class, "decision", now)
                _append_ledger({
                    "ts": utc_iso(now), "epoch": int(now), "key": wk,
                    "kind": "goal-stall-nag", "ws_ref": "(goals)",
                    "outcome": "verified",
                    "evidence": f"staged decision {rec.get('id')} for {gid} "
                                f"(safe-default or gated class {step_class})",
                })
            else:
                summary["skipped"].append({"goal": gid, "reason": "decision-dedup"})
    return summary


def _ledger_skip(goal_id: str, reason: str, now: float, extra: str = "") -> None:
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"planner:skip:{goal_id}:{reason}", "kind": "planner-skip",
        "ws_ref": "(goals)", "outcome": "skipped",
        "evidence": f"{reason} for {goal_id}" + (f"; {extra}" if extra else ""),
    })


def _ledger_stage(goal_id: str, ref: str, step_class: str, kind: str,
                  now: float) -> None:
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"planner:stage:{goal_id}:{ref}", "kind": "planner-stage",
        "ws_ref": "(goals)", "outcome": "verified",
        "evidence": f"staged {kind} {ref} ({step_class}) for {goal_id}",
    })


# ─── metrics helpers (imported by brief.compute_daily_metrics) ────────────────

def goals_progressed_overnight(now: float | None = None) -> int:
    """Design section 3/11 metric: active goals whose lastProgressAt lands in
    the last 24h — the control loop's core number, computed mechanically."""
    now = now if now is not None else time.time()
    cutoff = now - WINDOW_SEC
    n = 0
    for g in load_goals()["goals"]:
        if g.get("status") != "active":
            continue
        lp = parse_iso(g.get("lastProgressAt"))
        if lp is not None and cutoff <= lp <= now:
            n += 1
    return n


def staged_accept_rate(records: list[dict] | None = None,
                       now: float | None = None) -> float:
    """Design metric: goal-staged work kept / staged. Over goal-linked decisions
    (goal_refs non-empty) resolved in the last 24h: (accepted+edited)/resolved.
    Day-one guard: nothing resolved → 0.0."""
    now = now if now is not None else time.time()
    records = records if records is not None else decisions.read_log()
    cutoff = now - WINDOW_SEC
    kept = resolved = 0
    for rec in decisions.fold(records).values():
        if not (rec.get("goal_refs") or []):
            continue
        status = rec.get("status")
        if status not in ("accepted", "edited", "rejected"):
            continue
        ep = parse_iso((rec.get("resolution") or {}).get("ts"))
        if ep is None or not (cutoff <= ep <= now):
            continue
        resolved += 1
        if status in ("accepted", "edited"):
            kept += 1
    return round(kept / resolved, 4) if resolved else 0.0


# ─── pulse step ──────────────────────────────────────────────────────────────

def pulse_step(now: float | None = None, log=None) -> dict:
    """The pulse's planner step (Keel M4). Stamp mechanical progress first (so
    stall detection reads a fresh picture), then run one planner pass. Every
    failure mode inside is already a no-op-with-ledger; this wrapper adds no
    load-bearing behavior of its own — pulse.py fences the whole call anyway."""
    now = now if now is not None else time.time()
    stamp = stamp_progress(now=now)
    plan = plan_pass(now=now)
    return {"stamp": stamp, "plan": plan}
