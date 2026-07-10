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

from . import config, decisions, todostore

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

# Dispatch cap for the capacity math — imported from the ONE source of truth
# (config.py, shared with pulse.py) rather than a hardcoded copy that could drift
# from the dispatcher's ceiling (m14). Never mutated here.
ACTIVE_WS_CAP = config.ACTIVE_WS_CAP

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


def _safe_int(v, default: int) -> int:
    """int(v) that NEVER raises — a hand-edited budget of "abc" defaults instead
    of crashing the whole planner (m5). bool is rejected (True→1 would be a
    silent budget of 1)."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return default
    return default


def _valid_goal(g) -> bool:
    """A goal is usable only if it has the required, measurable fields (design
    section 3: id, rank, title, outcome required) AND its structured containers
    are the right SHAPE — a `budget`/`links`/`playbook` that is present but not
    an object is a malformed goal (m5: one such field used to crash load/plan).
    A malformed goal is DROPPED from the view — never crashes the load, never
    crashes the pulse (honors the load_goals docstring contract)."""
    if not isinstance(g, dict):
        return False
    if not isinstance(g.get("id"), str) or not GOAL_ID_RE.match(g["id"]):
        return False
    if not isinstance(g.get("rank"), int) or isinstance(g.get("rank"), bool):
        return False
    if not isinstance(g.get("title"), str) or not g["title"].strip():
        return False
    if not isinstance(g.get("outcome"), str) or not g["outcome"].strip():
        return False
    # Present-but-wrong-typed containers are malformed (drop the goal). Absent
    # is fine — _normalize_goal fills the defaults.
    for key in ("links", "playbook", "budget"):
        if key in g and g[key] is not None and not isinstance(g[key], dict):
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
        # _safe_int so a hand-edited/proposed budget value that isn't a clean
        # int defaults instead of raising and blanking every goal (m5).
        "budget": {
            "maxActiveWs": _safe_int(
                budget.get("maxActiveWs"), DEFAULT_BUDGET["maxActiveWs"]),
            "maxStagedTodosPerNight": _safe_int(
                budget.get("maxStagedTodosPerNight"),
                DEFAULT_BUDGET["maxStagedTodosPerNight"]),
            "maxStrategistCallsPerDay": _safe_int(
                budget.get("maxStrategistCallsPerDay"),
                DEFAULT_BUDGET["maxStrategistCallsPerDay"]),
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


def _reindex_ranks(goals: list[dict], *, prefer: str | None = None,
                   prefer_rank: int | None = None) -> None:
    """Renumber goals to unique, contiguous 1..N ranks IN PLACE (m9). Order is
    by current rank; the `prefer` goal wins ties at `prefer_rank` so a freshly
    inserted goal keeps the slot the caller asked for. Mutates each goal's
    'rank'."""
    def _key(g):
        r = g.get("rank") if isinstance(g.get("rank"), int) else (1 << 30)
        if prefer is not None and g.get("id") == prefer and prefer_rank is not None:
            r = prefer_rank
        tie = 0 if (prefer is not None and g.get("id") == prefer) else 1
        return (r, tie, str(g.get("id") or ""))
    for i, g in enumerate(sorted(goals, key=_key), start=1):
        g["rank"] = i


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
        # Enforce unique, contiguous 1..N ranks like rerank does (m9): inserting
        # at an already-used rank must renumber, not leave two goals at the same
        # rank (which made rank-order planning nondeterministic). The new goal
        # keeps its requested slot; ties break so the new goal sorts FIRST at its
        # rank, then everyone is renumbered by that order.
        _reindex_ranks(goals, prefer=gid, prefer_rank=goal["rank"])
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
    # Type-guard the write path (m5): a bad links/playbook/budget must be
    # rejected HERE, not written and then crash the next load/plan.
    for key in ("links", "playbook", "budget"):
        if key in changes and not isinstance(changes[key], dict):
            return None, f"{key} must be an object"
    if "status" in changes and not isinstance(changes["status"], str):
        return None, "status must be a string"
    if "stallAfterHours" in changes and (
            isinstance(changes["stallAfterHours"], bool)
            or not isinstance(changes["stallAfterHours"], (int, float))):
        return None, "stallAfterHours must be a number"
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


def _pr_token(repo, num) -> str | None:
    """A REPO-QUALIFIED PR token '<repo>#<num>' (m17). A bare PR number matches
    across repos — an unrelated repo's PR #101 would reset this goal's stall
    clock. Requiring repo agreement kills that false progress. Returns None when
    the repo or number is unknown (an unqualifiable PR can never match)."""
    r = _norm_repo(repo)
    n = str(num).strip() if num is not None else ""
    if not r or not n:
        return None
    return f"{r}#{n}"


def goal_link_tokens(goal: dict) -> dict[str, set]:
    """Typed token sets a progress artifact must intersect to count. Typed (not
    a single flat set) so a PR number can never collide with a repo name — this
    precision is what keeps the stall-precision harness honest. PR tokens are
    additionally REPO-QUALIFIED (m17): a goal's PR lives in one of its linked
    repos, so each PR is paired with each repo as '<repo>#<num>'."""
    links = goal.get("links") or {}
    repos = {t for t in (_norm_repo(r) for r in (links.get("repos") or [])) if t}
    prnums = {str(x).strip() for x in (links.get("prs") or []) if str(x).strip()}
    prs = {tok for repo in repos for num in prnums
           if (tok := _pr_token(repo, num)) is not None}
    return {
        "goals": {goal.get("id")},
        "repos": repos,
        "prs": prs,
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
        repos = set()
        for v in (row.get("repo"), refs.get("repo")):
            t = _norm_repo(v)
            if t:
                repos.add(t)
        # PR tokens are repo-qualified (m17): pair each bare PR number with the
        # row's repo(s). A PR row with no repo yields no PR token (can't match).
        bare_prs = set()
        for v in (row.get("pr"), refs.get("pr")):
            if v:
                bare_prs.add(str(v))
        for v in (row.get("prs") or []):
            if v:
                bare_prs.add(str(v))
        prs = {tok for repo in repos for num in bare_prs
               if (tok := _pr_token(repo, num)) is not None}
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
        repo = _norm_repo(pr.get("repo"))
        # Repo-qualified PR tokens (m17): a merged PR only counts as progress on
        # a goal that links BOTH its repo and its number. A repo-less PR record
        # yields no PR token.
        prtok = set()
        for v in (pr.get("number"), pr.get("url")):
            tok = _pr_token(repo, v)
            if tok is not None:
                prtok.add(tok)
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
        repos = set()
        t = _norm_repo(refs.get("repo"))
        if t:
            repos.add(t)
        # Repo-qualified PR token (m17).
        prs = set()
        _prtok = _pr_token(refs.get("repo"), refs.get("pr"))
        if _prtok is not None:
            prs.add(_prtok)
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


def store_status() -> str:
    """Distinguish an UNREADABLE goals store from an empty/absent one (m16).
    load_goals() collapses both to a safe empty view, which silently turned a
    corrupt/truncated goals.json into a permanent unledgered planner no-op. This
    probe lets plan_pass ledger `planner:goals-unreadable` (like paused/stale)
    so a corrupt store is VISIBLE in the brief, not silent.

      'missing'    — no file (legitimately no goals yet) → planner just no-ops;
      'unreadable' — file exists but won't parse / wrong shape → ledger a skip;
      'ok'         — parseable store."""
    p = goals_path()
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "unreadable"
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        return "unreadable"
    return "ok"


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
    quiet pulse costs no write. Returns a summary.

    Honors the kill switch (m8): a `_paused:true` store freezes the WHOLE loop,
    including the mechanical stamp — a paused planner writes nothing (ledgered)."""
    now = now if now is not None else time.time()
    if load_goals().get("_paused"):
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": "planner:stamp-paused", "kind": "planner-skip",
            "ws_ref": "(goals)", "outcome": "skipped",
            "evidence": "_paused:true — progress stamp no-op",
        })
        return {"stamped": [], "n": 0, "paused": True}
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


def _open_decision_ref_tokens(d: dict) -> dict[str, set]:
    """Typed tokens for an open decision's refs, so a triage decision about the
    goal's repo/pr/td can be matched to the goal even when nothing set goal_refs
    (m13)."""
    refs = d.get("refs") if isinstance(d.get("refs"), dict) else {}
    repos = set()
    t = _norm_repo(refs.get("repo"))
    if t:
        repos.add(t)
    prs = set()
    prtok = _pr_token(refs.get("repo"), refs.get("pr"))
    if prtok is not None:
        prs.add(prtok)
    todos = {str(refs.get("td"))} if refs.get("td") else set()
    return {"repos": repos, "prs": prs, "todos": todos}


def goal_blocked_by_open_decision(goal_id: str, goal: dict | None = None) -> bool:
    """True when an OPEN decision already covers this goal — either it explicitly
    references the goal (goal_refs) OR its refs match one of the goal's links
    (m13: an open triage decision about the goal's repo/pr/td is the human
    already being asked, so the stall must not ALSO auto-nag)."""
    gtok = goal_link_tokens(goal) if isinstance(goal, dict) else None
    for d in decisions.open_decisions():
        if goal_id in (d.get("goal_refs") or []):
            return True
        if gtok is not None and _matches(gtok, _open_decision_ref_tokens(d)):
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
    if goal_blocked_by_open_decision(goal.get("id"), goal):
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


# Decision statuses that mean "the human moved this step FORWARD" (advance to
# the next step) vs "the human DECLINED this step" (advance past + never
# re-stage). Mirrors decisions.py terminal semantics.
_DECISION_DONE = ("accepted", "edited", "auto_done")


def _goal_step_decision_id(goal_id: str, step_class: str) -> str:
    """The decision id _stage_decision mints for (goal, step) — recomputed so we
    can read that step's disposition from the decision log without storing the
    source on the record. MUST mirror _stage_decision's event construction."""
    src = goal_source(goal_id, step_class)
    return decisions.decision_id(f"goal:{goal_id}", src, step_class)


def _step_states(goal: dict, todo_data: dict,
                 folded: dict) -> tuple[set, set, set]:
    """Classify each playbook step's source into (open, done, declined) across
    BOTH the TODO store and the decision log — the ONE place the control loop's
    advancement is decided (M1+M6+M12 unified). This is what un-wedges the loop:
    the safe default stages DECISIONS, so advancement CANNOT be read from `done`
    TODOs alone.

      OPEN     — an open goal TODO OR an open/snoozed goal_step decision: still
                 in flight; don't stack a second step.
      DONE     — a done goal TODO OR an accepted/edited/auto_done goal_step
                 decision: the human advanced this step (M1a) → go to the NEXT.
      DECLINED — a removed/deferred goal TODO OR a REJECTED goal_step decision:
                 the human said NO (M1b/M6/M12) → advance past it and never
                 re-stage it in EITHER namespace.

    An EXPIRED goal_step decision is in NONE of the sets on purpose: it falls
    through so open_decision RE-OPENS it on the next sighting (M1d, unchanged)."""
    gid = goal.get("id")
    open_src: set = set()
    done_src: set = set()
    declined_src: set = set()
    if isinstance(todo_data, dict):
        for name in ("items", "completed", "removed"):
            for it in (todo_data.get(name) or []):
                if not isinstance(it, dict):
                    continue
                src = it.get("source")
                if not isinstance(src, str) or not src.startswith("goal:"):
                    continue
                status = it.get("status")
                if name == "removed":
                    declined_src.add(src)          # human removed the TODO
                elif status == "done":
                    done_src.add(src)
                elif status == "deferred":
                    declined_src.add(src)          # human deferred the TODO
                elif status in ("open", "in-progress", "blocked", None):
                    open_src.add(src)
    playbook = goal.get("playbook") or {}
    classes = list(playbook.get("unattended") or []) + list(playbook.get("gated") or [])
    for step_class in classes:
        dec = folded.get(_goal_step_decision_id(gid, step_class))
        if not isinstance(dec, dict):
            continue
        src = goal_source(gid, step_class)
        st = dec.get("status")
        if st in _DECISION_DONE:
            done_src.add(src)
        elif st == "rejected":
            declined_src.add(src)
        elif st in ("open", "snoozed"):
            open_src.add(src)
        # expired → intentionally unclassified (re-open path)
    return open_src, done_src, declined_src


def _human_declined_step(goal_id: str, step_class: str) -> bool:
    """The SINGLE 'did the human decline this exact step?' check consulted by
    BOTH _stage_todo and _stage_decision (M6 unification). True when the step's
    source has a removed/deferred goal TODO OR its goal_step decision is
    rejected — in EITHER namespace. This is why a REJECT on the decision path
    can never be resurrected as an unattended TODO after the autoDispatch flag
    flips, and a human REMOVE of a goal TODO can never re-appear as a decision:
    the suppression spans both paths and both ISO weeks."""
    src = goal_source(goal_id, step_class)
    td = _read_json(todo_path())
    if isinstance(td, dict):
        for it in (td.get("removed") or []):
            if isinstance(it, dict) and it.get("source") == src:
                return True
        for it in (td.get("items") or []):
            if isinstance(it, dict) and it.get("source") == src \
                    and it.get("status") == "deferred":
                return True
    dec = decisions.fold(decisions.read_log()).get(
        _goal_step_decision_id(goal_id, step_class))
    if isinstance(dec, dict) and dec.get("status") == "rejected":
        return True
    return False


def select_next_step(goal: dict, todo_data: dict,
                     folded: dict | None = None) -> tuple[str, str, str] | None:
    """Deterministic next step for a stalled goal (NO LLM). Walk the playbook —
    unattended classes first, then gated — and return the FIRST class the human
    has neither finished nor declined:
      • class OPEN/in-flight (TODO or decision) → return None (never stack);
      • class DONE (done TODO or accepted decision) → advance to the next;
      • class DECLINED (removed/deferred TODO or rejected decision) → advance
        past it (never re-stage);
      • class untouched → stage it.
    Returns (step_class, title, detail) or None."""
    if folded is None:
        folded = decisions.fold(decisions.read_log())
    open_src, done_src, declined_src = _step_states(goal, todo_data, folded)
    playbook = goal.get("playbook") or {}
    classes = list(playbook.get("unattended") or []) + list(playbook.get("gated") or [])
    for step_class in classes:
        src = goal_source(goal.get("id"), step_class)
        if src in open_src:
            return None  # current step still in flight — don't stack another
        if src in done_src or src in declined_src:
            continue     # completed OR declined — advance to the next step
        suffix, detail = STEP_TEMPLATES.get(
            step_class, ("advance the goal", "Advance this goal's next step."))
        title = f"[{goal.get('id')}] {suffix}: {goal.get('title')}"
        return step_class, title[:200], detail
    return None


def _world_active_ws(world: dict) -> int:
    """ACTIVE workspace count from world.json for the headroom math — using the
    SAME predicate the dispatcher's count_active uses (config.ws_is_active), NOT
    a raw len(live_sessions) (m14). A fleet of five idle/cron sessions has zero
    ACTIVE workspaces, so the planner sees the same headroom the dispatcher
    would actually grant — the two can no longer disagree and starve the loop."""
    live = world.get("live_sessions")
    if not isinstance(live, list):
        return 0
    n = 0
    for s in live:
        if not isinstance(s, dict):
            continue
        if config.ws_is_active(s.get("agent_status"), s.get("last_turn_age_sec")):
            n += 1
    return n


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
    """Append a goal TODO to assistant-todo.json under the SHARED todo-file lock
    (M3: the todo-server, the pulse dispatch stamp, and triage.create_todo hold
    the same lock, so a concurrent write can't lose this append — the old code
    took the GOALS lock, which those three writers never touched). Deduped by
    exact source (idempotent across pulses/restarts). Refuses a step the human
    declined in EITHER namespace (M6). Returns (item, created)."""
    if _human_declined_step(goal.get("id"), step_class):
        return None, False  # human declined this step — never (re)dispatch it
    src = goal_source(goal.get("id"), step_class)
    tp = todo_path()
    with todostore.todo_lock():  # ONE lock shared with every todo.json writer
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
    if _human_declined_step(goal.get("id"), step_class):
        return None, False  # human declined this step — never resurrect it
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


def _ledger_soft_skip(goal_id: str, reason: str, now: float,
                      summary: dict, extra: str = "") -> None:
    """Record a NON-stage outcome in BOTH the summary and the ledger (M1c: honor
    plan_pass's own 'all of it also ledgered' docstring — the dedup/no-step skips
    used to land in the summary but NEVER the ledger, so a wedged loop left no
    audit trail). Also flags the pass as having done dedup-only work so pulse.py
    can surface an otherwise-invisible pass."""
    _ledger_skip(goal_id, reason, now, extra=extra)
    summary["skipped"].append({"goal": goal_id, "reason": reason})
    summary["dedup_only"] = True


def plan_pass(now: float | None = None) -> dict:
    """One planner pass (design section 6). Assumes progress is already stamped
    (pulse_step stamps first). Goal RANK order — goal #1 gets first claim on the
    leftover headroom. Returns a summary of everything it did/skipped, ALL of it
    also ledgered (M1c: no silent skips)."""
    now = now if now is not None else time.time()
    summary = {"paused": False, "stale_world": False, "unreadable": False,
               "staged_todos": [], "staged_decisions": [], "skipped": [],
               "stalls": 0, "dedup_only": False}

    # A corrupt/truncated goals.json is NOT the same as "no goals" — surface it
    # (m16) instead of a silent permanent no-op.
    if store_status() == "unreadable":
        summary["unreadable"] = True
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": "planner:goals-unreadable", "kind": "planner-skip",
            "ws_ref": "(goals)", "outcome": "skipped",
            "evidence": "goals.json present but unreadable — refusing to plan",
        })
        return summary

    store = load_goals()
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
        # Week-keyed dedup: no re-nag within an ISO week (now LEDGERED — M1c).
        wk = iso_week_key(gid, now)
        if ledger_has_key(wk):
            _ledger_soft_skip(gid, "week-deduped", now, summary)
            continue
        # Re-read the todo store + fold the decision log each goal so within-pass
        # staging (and the human's out-of-band resolutions) drive the next goal's
        # advancement/dedup/budget math.
        todo_data = _read_json(todo_path()) or {}
        folded = decisions.fold(decisions.read_log())
        step = select_next_step(goal, todo_data, folded)
        if step is None:
            _ledger_soft_skip(gid, "no-step-or-in-flight", now, summary)
            continue
        step_class, title, detail = step
        budget = goal.get("budget") or {}
        # Per-goal maxStagedTodosPerNight.
        if _goal_staged_tonight(gid, todo_data, now) >= budget.get(
                "maxStagedTodosPerNight", DEFAULT_BUDGET["maxStagedTodosPerNight"]):
            _ledger_soft_skip(gid, "budget-staged-per-night", now, summary)
            continue
        unattended = step_class in (goal.get("playbook") or {}).get("unattended", [])
        effective_autodispatch = autodispatch_on and unattended

        if effective_autodispatch:
            # Per-goal maxActiveWs + global leftover headroom (saturated → skip).
            if _goal_active_ws(gid, todo_data, world) >= budget.get(
                    "maxActiveWs", DEFAULT_BUDGET["maxActiveWs"]):
                _ledger_soft_skip(gid, "budget-max-active-ws", now, summary)
                continue
            if todos_staged >= headroom:
                _ledger_soft_skip(gid, "capacity-saturated", now, summary,
                                  extra=f"headroom={headroom} active={active_ws} "
                                        f"human={human_pending}")
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
                # created=False here means an exact-source TODO already exists OR
                # the human declined this step (M6) — either way, ledger it.
                _ledger_soft_skip(gid, "todo-dedup", now, summary)
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
                # created=False: the accepted/rejected/open decision already
                # exists for this exact step, OR the human declined it (M6). The
                # step-hash was terminal, so this is NOT progress — ledger the
                # skip (M1c) instead of silently masking it as done.
                _ledger_soft_skip(gid, "decision-dedup", now, summary)
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
