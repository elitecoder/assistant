"""policy — deterministic, ordered, first-match-wins event laning (Keel M2).

Every WorldEvent that the event spine appends to ~/.assistant/events.jsonl is
laned by THIS module before any LLM sees it. Rules are declarative JSON in
~/.assistant/policies/policies.json (installed from the repo's
config/policies.bootstrap.json on first run); evaluation is pure Python,
microseconds per event, no LLM anywhere in this file.

Lanes: auto | staged | escalate | digest | drop.

Hard invariants live IN CODE here, not in config — a mis-written policies.json
cannot weaken them:

  - Fallthrough never drops. An event no rule matches is returned as
    ``unmatched`` (the caller batches those to the suggestion-only triage LLM);
    any ambiguity — unreadable policies file, invalid matched rule, an ``auto``
    rule whose action isn't a known standing class — lanes to ``escalate``.
  - ``drop`` and ``pageable`` are reachable ONLY via an explicit, enabled rule.
    There is no code path that defaults an event into either.
  - The ``auto`` lane is reachable ONLY via a policy id. TRIAGE_LANE_MAP — the
    complete vocabulary the triage LLM's suggestions are validated against —
    structurally lacks an ``auto`` key (and a ``drop`` key), so no LLM output
    string can ever mint an auto action. A structural unit test pins this.

Also home to the policy-proposal miner: >=3 identical triage suggestions for
the same (source, kind) within 7 days auto-generate a confirmation-gated
``policy`` proposal into the existing proposals queue (mirrors
lesson-extractor's write_proposal pattern). Never auto-applied.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

POLICY_SCHEMA_VERSION = 1

LANES = ("auto", "staged", "escalate", "digest", "drop")

# The COMPLETE lane vocabulary a triage-LLM suggestion may resolve to. This is
# the load-bearing structural invariant: no "auto" key (an LLM can never reach
# the acting lane) and no "drop" key (dropping requires an explicit rule).
# Extending this map is a design change, not a config edit.
TRIAGE_LANE_MAP = {
    "staged": "staged",
    "escalate": "escalate",
    "digest": "digest",
}

# The only action classes an `auto` rule may pre-declare — existing channels
# only (design section 4). Anything else in an auto rule is ambiguity and the
# event escalates instead of acting.
AUTO_ACTION_CLASSES = ("todo.create", "digest.append")

# Predicate operators. An unknown op invalidates the rule at load time (the
# rule is skipped and reported) — it never half-matches at eval time.
PREDICATE_OPS = ("eq", "ne", "contains", "not_contains", "prefix", "regex",
                 "exists", "missing", "in")

# Default TTLs per lane (hours) when the rule doesn't set one. Escalate never
# expires without a rule saying so (design section 5).
DEFAULT_TTL_H = {"digest": 24, "staged": 72, "escalate": None,
                 "auto": None, "drop": None}

# Miner thresholds: >=3 identical suggestions for one (source, kind) in 7 days.
MINER_MIN_SUGGESTIONS = 3
MINER_WINDOW_SEC = 7 * 86400

_REPO = Path(__file__).resolve().parents[2]


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def policies_dir() -> Path:
    return _home() / ".assistant" / "policies"


def policies_path() -> Path:
    return policies_dir() / "policies.json"


def bootstrap_path() -> Path:
    return _REPO / "config" / "policies.bootstrap.json"


def proposals_path() -> Path:
    return _home() / ".assistant" / "comms" / "proposals.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ─── install ────────────────────────────────────────────────────────────────

def ensure_policies_installed() -> bool:
    """Copy the repo bootstrap into ~/.assistant/policies/ when no live
    policies.json exists yet. Returns True when the bootstrap was installed
    this call. Never overwrites an existing file — the live store is Mukul's
    (confirmation-gated proposals mutate it, not this function)."""
    dst = policies_path()
    if dst.exists():
        return False
    src = bootstrap_path()
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".json.tmp")
    shutil.copyfile(str(src), str(tmp))
    os.replace(tmp, dst)
    return True


# ─── load + validate ────────────────────────────────────────────────────────

def validate_rule(rule) -> str | None:
    """Return an error string when the rule is malformed, else None. A rule
    that fails validation is SKIPPED at load (reported in `invalid`) — events
    it would have matched fall through to triage → escalate, never to a
    weaker lane."""
    if not isinstance(rule, dict):
        return "rule is not an object"
    rid = rule.get("id")
    if not isinstance(rid, str) or not rid:
        return "rule missing id"
    lane = rule.get("lane")
    if lane not in LANES:
        return f"rule {rid!r}: invalid lane {lane!r}"
    match = rule.get("match")
    if not isinstance(match, dict):
        return f"rule {rid!r}: missing match object"
    for key in ("source", "kind"):
        v = match.get(key)
        if v is not None and not isinstance(v, str):
            return f"rule {rid!r}: match.{key} must be a string"
    preds = match.get("predicates", [])
    if not isinstance(preds, list):
        return f"rule {rid!r}: predicates must be a list"
    for p in preds:
        if not isinstance(p, dict):
            return f"rule {rid!r}: predicate is not an object"
        if not isinstance(p.get("field"), str) or not p.get("field"):
            return f"rule {rid!r}: predicate missing field"
        if p.get("op") not in PREDICATE_OPS:
            return f"rule {rid!r}: unknown predicate op {p.get('op')!r}"
        if p.get("op") == "regex":
            try:
                re.compile(p.get("value") or "")
            except re.error as e:
                return f"rule {rid!r}: bad regex: {e}"
        if p.get("op") == "in" and not isinstance(p.get("value"), list):
            return f"rule {rid!r}: 'in' predicate needs a list value"
    action = rule.get("action")
    if lane == "auto":
        if not isinstance(action, dict) or \
                action.get("class") not in AUTO_ACTION_CLASSES:
            return (f"rule {rid!r}: auto lane requires action.class in "
                    f"{AUTO_ACTION_CLASSES}")
    if not isinstance(rule.get("pageable", False), bool):
        return f"rule {rid!r}: pageable must be a bool"
    return None


def load_policies(path: Path | None = None) -> tuple[list[dict], list[str], str | None]:
    """Load the ordered rule list. Returns (rules, invalid, error).

    error is non-None ONLY for a whole-file failure (missing after an install
    attempt, unreadable, unparseable, wrong shape) — the caller must then lane
    EVERY event to escalate (ambiguity never weakens). Individually invalid
    rules are skipped and listed in `invalid`; the rest of the file stands."""
    p = path if path is not None else policies_path()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return [], [], "policies.json missing"
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [], [], f"policies.json unreadable: {e}"
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), list):
        return [], [], "policies.json has no policies[] list"
    rules: list[dict] = []
    invalid: list[str] = []
    seen_ids: set[str] = set()
    for rule in raw["policies"]:
        err = validate_rule(rule)
        if err is None and rule["id"] in seen_ids:
            err = f"rule {rule['id']!r}: duplicate id"
        if err is not None:
            invalid.append(err)
            continue
        seen_ids.add(rule["id"])
        rules.append(rule)
    return rules, invalid, None


# ─── evaluation ─────────────────────────────────────────────────────────────

def get_field(event: dict, dotted: str):
    """Resolve a dotted field path ('refs.ws_ref') against the event dict."""
    cur = event
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def eval_predicate(pred: dict, event: dict) -> bool:
    op = pred.get("op")
    val = get_field(event, pred.get("field", ""))
    want = pred.get("value")
    if op == "exists":
        return val is not None
    if op == "missing":
        return val is None
    if op == "eq":
        return val == want
    if op == "ne":
        return val != want
    if op == "in":
        return val in (want or [])
    text = "" if val is None else str(val)
    if op == "contains":
        return str(want) in text
    if op == "not_contains":
        return str(want) not in text
    if op == "prefix":
        return text.startswith(str(want))
    if op == "regex":
        try:
            return re.search(str(want), text) is not None
        except re.error:
            return False
    return False  # unreachable for load-validated rules; never matches


def match_rule(rule: dict, event: dict) -> bool:
    match = rule.get("match") or {}
    src = match.get("source")
    if src is not None and src != "*" and event.get("source") != src:
        return False
    kind = match.get("kind")
    if kind is not None and kind != "*" and event.get("kind") != kind:
        return False
    return all(eval_predicate(p, event) for p in match.get("predicates", []))


def _decision(lane: str, *, policy_id=None, urgency=None, ttl_h=None,
              action=None, pageable=False, reason=None) -> dict:
    return {"lane": lane, "policy_id": policy_id, "urgency": urgency,
            "ttl_h": ttl_h, "action": action, "pageable": bool(pageable),
            "reason": reason}


def lane_event(event: dict, rules: list[dict], error: str | None = None) -> dict:
    """Lane one WorldEvent. First enabled matching rule wins.

    Returns a lane decision dict:
        {lane, policy_id, urgency, ttl_h, action, pageable, reason}

    lane == "unmatched" means no rule fired — the caller sends the event to
    the suggestion-only triage LLM (whose vocabulary is TRIAGE_LANE_MAP, no
    auto, no drop). Any ambiguity — load error, non-dict event — lanes to
    escalate. This function NEVER returns drop or auto except via an explicit
    enabled rule."""
    if error is not None:
        return _decision("escalate", reason=f"policy-load-failure: {error}")
    if not isinstance(event, dict):
        return _decision("escalate", reason="event is not an object")
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if not match_rule(rule, event):
            continue
        lane = rule["lane"]
        ttl_h = rule.get("ttl_h")
        if ttl_h is None:
            ttl_h = DEFAULT_TTL_H.get(lane)
        return _decision(
            lane,
            policy_id=rule["id"],
            urgency=rule.get("urgency"),
            ttl_h=ttl_h,
            action=rule.get("action"),
            pageable=bool(rule.get("pageable", False)),
        )
    return _decision("unmatched")


def valid_triage_lane(suggested) -> str | None:
    """Map a triage-LLM lane suggestion onto a real lane, or None when the
    suggestion is not in the (auto-less, drop-less) triage vocabulary. The
    ONLY validation path for LLM lane output — everything else is refused."""
    if not isinstance(suggested, str):
        return None
    return TRIAGE_LANE_MAP.get(suggested)


# ─── policy-proposal miner ──────────────────────────────────────────────────

def _pending_policy_keys(path: Path) -> set[str]:
    """(source,kind,lane) keys already represented by a pending/confirmed
    policy proposal — both block a re-propose (mirrors lesson-extractor's
    pending_proposal_stems)."""
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
        if not isinstance(obj, dict) or obj.get("type") != "policy":
            continue
        if obj.get("status") not in ("pending", "confirmed"):
            continue
        pp = obj.get("proposed_policy") or {}
        match = pp.get("match") or {}
        keys.add(f"{match.get('source')}|{match.get('kind')}|{pp.get('lane')}")
    return keys


def _covered_by_rule(source: str, kind: str, rules: list[dict]) -> bool:
    """True when an enabled predicate-free rule already lanes this exact
    (source, kind) — no point proposing what config already does."""
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        match = rule.get("match") or {}
        if match.get("source") == source and match.get("kind") == kind \
                and not match.get("predicates"):
            return True
    return False


def mine_policy_proposals(decision_records: list[dict], *, now: float,
                          rules: list[dict],
                          path: Path | None = None) -> list[dict]:
    """>=MINER_MIN_SUGGESTIONS identical triage suggestions for one
    (source, kind) within MINER_WINDOW_SEC → ONE confirmation-gated `policy`
    proposal appended to proposals.jsonl. Suggestions must agree on the lane
    ("identical"), come from distinct decisions, and not already be covered by
    an enabled rule or a live proposal. Returns the proposals written.

    Suggestion-only in, suggestion-only out: the proposal is pending until a
    human confirms it — nothing here mutates policies.json."""
    p = path if path is not None else proposals_path()
    cutoff = now - MINER_WINDOW_SEC
    groups: dict[tuple[str, str, str], set[str]] = {}
    for rec in decision_records:
        if not isinstance(rec, dict) or rec.get("policy_id") != "triage":
            continue
        triage = rec.get("triage") or {}
        lane = valid_triage_lane(triage.get("suggested_lane"))
        if lane is None:
            continue
        epoch = rec.get("epoch")
        if not isinstance(epoch, (int, float)) or epoch < cutoff:
            continue
        source, kind = rec.get("source"), rec.get("kind")
        if not source or not kind:
            continue
        groups.setdefault((source, kind, lane), set()).add(rec.get("id"))

    pending = _pending_policy_keys(p)
    written: list[dict] = []
    for (source, kind, lane), dec_ids in sorted(groups.items()):
        if len(dec_ids) < MINER_MIN_SUGGESTIONS:
            continue
        if f"{source}|{kind}|{lane}" in pending:
            continue
        if _covered_by_rule(source, kind, rules):
            continue
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        entry = {
            "ts": ts,
            "id": ts,
            "type": "policy",
            "status": "pending",
            "source": "policy-miner",
            "proposed_policy": {
                "id": f"mined-{source}-{kind}-{lane}",
                "match": {"source": source, "kind": kind},
                "lane": lane,
                "action": None,
                "urgency": None,
                "ttl_h": DEFAULT_TTL_H.get(lane),
                "pageable": False,
                "enabled": True,
            },
            "evidence": {
                "suggestion_count": len(dec_ids),
                "decision_ids": sorted(d for d in dec_ids if d)[:10],
                "window_days": MINER_WINDOW_SEC // 86400,
            },
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        written.append(entry)
        pending.add(f"{source}|{kind}|{lane}")
    return written
