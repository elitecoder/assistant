#!/usr/bin/env python3
"""Gated outbound dispatcher (Keel M7 — gated outbound drafts).

The ONE sanctioned executor for acting on an accepted decision. Two independent
gates, both of which must pass, and every refusal is ledgered:

  Gate 1 — AUTHORIZATION (lane != authorization): a class sitting in a lane is
           NOT permission to fire. Execution requires an *accepted* decision id
           in the store (decisions.jsonl). No accepted dec_id → refuse.
  Gate 2 — CLASS REGISTRY: the class must be enabled in action-classes.json and
           its gate must permit the effect. `forbidden` (email.send /
           slack.reply.send — also code-enforced) is an explicit, ledgered
           refusal, strictly stronger than a class being absent.

Idempotency: a dec_id+class already drafted/verified replays to a refusal — the
human's one accept fires at most once. The outbound-ledger is append-only and
every `drafted`/`verified`/`refused`/`unimplemented` row carries an accepted
dec_id (INVARIANT: no outbound-ledger row exists without an accepted decision).

M7.a–d is the SAFETY CHOKEPOINT ONLY: the gates, the ledger, the idempotency,
and the CI grep-guard. No send/draft/delegate handler is wired yet — a permitted
class returns `unimplemented`. The real effects (Gmail draft, Slack stage,
merge-pr-dispatch delegate) land in M7.e+ inside the box this file builds, so the
first send-capable scope is already gated, ledgered, and CI-fenced.

Envelope mirrors merge-pr-dispatch: a numbered gate/act result dict printed as
JSON, plus an exit code:
    0  acted (drafted / delegated / verified)   [future]
    1  refused (a gate failed)
    2  acted but unverified                     [future]
    3  usage error / precondition (bad args, or no handler wired yet)
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from assistant import action_classes, decisions  # noqa: E402

EXIT_ACTED = 0
EXIT_REFUSED = 1
EXIT_UNVERIFIED = 2
EXIT_USAGE = 3

DEC_ID_RE = re.compile(r"dec-[0-9a-f]{16}$")
# A dec whose current status is one of these was approved by the human.
_AUTHORIZED_STATUSES = ("accepted", "edited")
# Outcomes that are terminal side effects — replaying them must refuse.
_ACTIONED_OUTCOMES = ("drafted", "verified")


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def outbound_ledger_path() -> Path:
    return _home() / ".assistant" / "outbound-ledger.jsonl"


def outbound_lock_path() -> Path:
    return _home() / ".assistant" / "outbound-ledger.writer.lock"


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def _outbound_lock():
    """Single-writer flock (mirrors decisions._writer_lock): the idempotency
    check and the row append happen inside ONE hold so two concurrent dispatches
    of the same dec_id+class can't both act."""
    p = outbound_lock_path()
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


def read_outbound_ledger() -> list[dict]:
    """All parseable outbound-ledger rows, oldest-first. Corrupt lines skipped."""
    try:
        lines = outbound_ledger_path().read_text().splitlines()
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


def _already_actioned(dec_id: str, action_class: str) -> bool:
    for row in read_outbound_ledger():
        if row.get("dec_id") == dec_id and row.get("class") == action_class \
                and row.get("outcome") in _ACTIONED_OUTCOMES:
            return True
    return False


def _append_row(dec_id: str, action_class: str, target,
                outcome: str, evidence: str, now: float) -> dict:
    """Append ONE outbound-ledger row. Caller must hold _outbound_lock() when
    the append is part of a check-then-act reservation."""
    row = {
        "ts": _utc_iso(now),
        "dec_id": dec_id,
        "class": action_class,
        "target": target,
        "outcome": outcome,
        "evidence": evidence,
    }
    p = outbound_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _compact_if_needed()
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _actions_ledger_refusal(dec_id: str, action_class: str,
                            reason: str, now: float) -> None:
    """A Gate-1 refusal has NO accepted decision, so it must NOT write an
    outbound-ledger row (that store's invariant is 'every row has an accepted
    dec'). Record it in the actions-ledger for auditability instead."""
    try:
        p = _home() / ".assistant" / "actions-ledger.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({
                "ts": _utc_iso(now), "epoch": int(now),
                "key": f"outbound-refused:{dec_id}:{action_class}:{reason}",
                "kind": "outbound-refused",
                "ws_ref": "(outbound)",
                "outcome": "refused",
                "evidence": f"{action_class} for {dec_id} refused: {reason}",
            }) + "\n")
    except OSError:
        pass


MAX_LEDGER_BYTES = 5_000_000


def _compact_if_needed() -> None:
    """Caller holds _outbound_lock(). Bound the live ledger: when it exceeds
    MAX_LEDGER_BYTES, archive the full log to <name>.1 and rewrite the live file
    with ONLY the idempotency-relevant terminal rows (drafted/verified, folded
    latest per dec_id+class). Refusal/audit rows are archived, never lost to the
    live idempotency scan — replay protection survives rotation (mirrors
    decisions._maybe_compact)."""
    p = outbound_ledger_path()
    try:
        if not p.exists() or p.stat().st_size <= MAX_LEDGER_BYTES:
            return
    except OSError:
        return
    rows = read_outbound_ledger()
    try:
        os.replace(p, p.with_name(p.name + ".1"))
    except OSError:
        return
    keep: dict = {}
    for r in rows:
        if r.get("outcome") in _ACTIONED_OUTCOMES:
            keep[(r.get("dec_id"), r.get("class"))] = r
    with open(p, "w") as f:
        for r in keep.values():
            f.write(json.dumps(r) + "\n")


def _decision_record(dec_id: str) -> dict | None:
    latest = decisions.fold(decisions.read_log()).get(dec_id)
    return latest if isinstance(latest, dict) else None


def _accepted_class(record: dict | None) -> str | None:
    """The action class the decision was OPENED/accepted for — the dec_id
    cryptographically encodes it (decisions.decision_id hashes the class), and
    it is carried on recommended.class. None when the decision has no action."""
    reco = record.get("recommended") if isinstance(record, dict) else None
    return reco.get("class") if isinstance(reco, dict) else None


def dispatch(dec_id: str, action_class: str, target=None,
             now: float | None = None) -> tuple[dict, int]:
    now = time.time() if now is None else now
    out: dict = {"dec_id": dec_id, "class": action_class, "target": target,
                 "ts": _utc_iso(now)}

    # --- usage validation ---------------------------------------------------
    if not (isinstance(dec_id, str) and DEC_ID_RE.fullmatch(dec_id)):
        out["outcome"] = "usage_error"
        out["reason"] = "dec_id must match dec-[0-9a-f]{16}"
        return out, EXIT_USAGE
    if not (isinstance(action_class, str) and action_class):
        out["outcome"] = "usage_error"
        out["reason"] = "missing action class"
        return out, EXIT_USAGE

    # --- Gate 1: authorization — an ACCEPTED decision FOR THIS EXACT CLASS ---
    # The two gates must together prove "the human accepted THIS action", not
    # merely "some decision is accepted" + "this class is permitted". The dec_id
    # cryptographically encodes the action class, so any accepted dec_id would
    # otherwise be a bearer token for EVERY permitted class (a confused deputy:
    # accept a benign todo.create card, then dispatch a confirm-gated merge on
    # its id). Bind the requested class to the decision's own recommended.class.
    record = _decision_record(dec_id)
    status = record.get("status") if record else None
    accepted_class = _accepted_class(record)
    status_ok = status in _AUTHORIZED_STATUSES
    class_ok = accepted_class is not None and accepted_class == action_class
    authorized = status_ok and class_ok
    if status is None:
        reason = "no such decision"
    elif not status_ok:
        reason = f"decision status is {status!r}, not accepted"
    elif not class_ok:
        reason = (f"decision was accepted for {accepted_class!r}, "
                  f"not {action_class!r} — acceptance does not authorize this "
                  "action")
    else:
        reason = "accepted decision for this class"
    out["gate1_authorization"] = {
        "ok": authorized, "status": status,
        "accepted_class": accepted_class, "reason": reason,
    }
    if not authorized:
        # No valid human authorization FOR THIS class+dec → NO outbound-ledger
        # row (a class-mismatch refers to an unrelated class's acceptance, so it
        # would violate 'every row is authorized FOR its class'). Audit in the
        # actions-ledger and refuse.
        out["outcome"] = "refused"
        out["reason"] = reason
        _actions_ledger_refusal(
            dec_id, action_class,
            "class-mismatch" if status_ok else "no-accepted-decision", now)
        return out, EXIT_REFUSED

    # From here we HOLD an accepted decision → outbound-ledger rows are allowed.
    action_classes.ensure_action_classes_installed()
    gate = action_classes.resolve_gate(action_class)
    out["gate2_registry"] = {
        "ok": gate is not None and gate != "forbidden",
        "gate": gate,
        "reason": ("unknown or disabled class" if gate is None
                   else ("forbidden action class (explicit, code-enforced for "
                         "sends)" if gate == "forbidden" else f"gate={gate}")),
    }
    if gate is None:
        out["outcome"] = "refused"
        out["reason"] = "unknown or disabled action class"
        with _outbound_lock():
            _append_row(dec_id, action_class, target, "refused",
                        "unknown or disabled action class", now)
        return out, EXIT_REFUSED
    if gate == "forbidden":
        out["outcome"] = "refused"
        out["reason"] = "forbidden action class"
        with _outbound_lock():
            _append_row(dec_id, action_class, target, "refused",
                        "forbidden action class (explicit)", now)
        return out, EXIT_REFUSED

    # --- idempotency + act, atomically under one lock ----------------------
    with _outbound_lock():
        if _already_actioned(dec_id, action_class):
            out["idempotency"] = {"ok": False, "reason": "already actioned"}
            out["outcome"] = "refused"
            out["reason"] = "replay of an already-actioned dec_id+class"
            _append_row(dec_id, action_class, target, "refused",
                        "replay of an already-actioned dec_id+class", now)
            return out, EXIT_REFUSED
        out["idempotency"] = {"ok": True, "reason": "first dispatch"}
        # ACT: no send/draft/delegate handler is wired in the M7.a–d chokepoint.
        # The permitted class is fully boxed (gate + ledger + CI invariant); the
        # real effect lands in M7.e+ INSIDE this reservation and will write the
        # `drafted`/`verified` row that then blocks replay. We deliberately do
        # NOT ledger `unimplemented`: it carries no idempotency meaning and an
        # automated retry loop would balloon the ledger with no-op rows.
        out["outcome"] = "unimplemented"
        out["reason"] = (f"gate={gate} permitted; no outbound handler wired yet "
                         "(M7.a-d is the chokepoint only)")
        return out, EXIT_USAGE


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Gated outbound dispatcher (Keel M7): act on an ACCEPTED "
                    "decision through the action-class gate + outbound ledger.")
    p.add_argument("--dec", required=True, help="accepted decision id (dec-…)")
    p.add_argument("--class", dest="action_class", required=True,
                   help="action class, e.g. email.draft / github.merge")
    p.add_argument("--target", default=None,
                   help="optional target (recipient / thread / pr) for the row")
    args = p.parse_args(argv)
    out, code = dispatch(args.dec, args.action_class, target=args.target)
    print(json.dumps(out, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
