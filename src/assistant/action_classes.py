"""Outbound action-class gate registry (Keel M7 — gated outbound drafts).

The SECOND of two independent gates in bin/outbound-dispatch.py (the first is
"is there an accepted decision for this id"). This module owns the class → gate
map: whether a given outbound action class is enabled and what effect its gate
permits. Pure stdlib, no LLM, delete-safe (rebuilt from the repo bootstrap).

    gate ∈ {forbidden, draft_only, confirm, standing}
      forbidden  — an EXPLICIT, ledgered refusal (stronger than absence).
      draft_only — stage a draft (Gmail draft / Slack paste-text); the human's
                   native Send button is the only actuator. NEVER auto-sends.
      confirm    — a gated executor delegates (e.g. github.merge → merge-pr-
                   dispatch); acts only on an accepted decision.
      standing   — a pre-authorized, reversible side effect (todo.create).

CODE-ENFORCED FORBID: the known send classes are forbidden in code
(`_CODE_FORBIDDEN`), independent of this file's contents, so a future edit to
the live action-classes.json can NOT un-forbid a send. Enabling any send
requires a schema change + a new gate + its own eval — not a config flip.

Install/upgrade mirrors policy.ensure_policies_installed: additive-only from the
repo bootstrap, never overwriting an operator's live edits.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

GATES = frozenset({"forbidden", "draft_only", "confirm", "standing"})

# Send classes that are forbidden in CODE regardless of the config file. A
# config edit cannot promote these out of `forbidden`; outbound-dispatch always
# refuses them. This is the load-bearing "no auto-send" guarantee.
_CODE_FORBIDDEN = frozenset({"email.send", "slack.reply.send"})


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def bootstrap_path() -> Path:
    return _REPO / "config" / "action-classes.bootstrap.json"


def action_classes_dir() -> Path:
    return _home() / ".assistant" / "policies"


def action_classes_path() -> Path:
    return action_classes_dir() / "action-classes.json"


def _ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _append_ledger(entry: dict) -> None:
    try:
        p = _ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _atomic_write(dst: Path, data: dict) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, dst)


def ensure_action_classes_installed() -> bool:
    """Install or version-upgrade the live action-classes.json from the repo
    bootstrap. Returns True when this call changed the live file.

    - No live file → copy the bootstrap verbatim (first run).
    - Live file at an older `version` → ADDITIVE upgrade: add bootstrap classes
      whose keys are absent from the live file, stamp the new version, ledger
      it. Operator-edited or operator-added classes are NEVER overwritten.
    - Unreadable/oddly-shaped live file → do nothing (never clobber)."""
    src = bootstrap_path()
    if not src.exists():
        return False
    dst = action_classes_path()
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".json.tmp")
        shutil.copyfile(str(src), str(tmp))
        os.replace(tmp, dst)
        return True

    try:
        boot = json.loads(src.read_text())
        live = json.loads(dst.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(boot, dict) or not isinstance(live, dict) \
            or not isinstance(boot.get("classes"), dict) \
            or not isinstance(live.get("classes"), dict):
        return False
    boot_version = boot.get("version")
    live_version = live.get("version")
    if not isinstance(boot_version, int):
        return False
    if isinstance(live_version, int) and live_version >= boot_version:
        return False

    added = [k for k in boot["classes"] if k not in live["classes"]]
    for k in added:
        live["classes"][k] = boot["classes"][k]
    live["version"] = boot_version
    _atomic_write(dst, live)
    now = datetime.now(timezone.utc).timestamp()
    _append_ledger({
        "ts": _utc_iso(now), "epoch": int(now),
        "key": f"action-classes-bootstrap-upgrade:v{live_version}->v{boot_version}",
        "kind": "action-classes-bootstrap-upgrade",
        "ws_ref": "(action-classes)",
        "outcome": "verified",
        "evidence": (f"bootstrap v{live_version}->v{boot_version}: added "
                     f"{len(added)} class(es) [{', '.join(added[:6])}]; "
                     "operator classes untouched"),
    })
    return True


def load_action_classes() -> dict:
    """The live class → {gate, enabled} map. Falls back to the repo bootstrap if
    the live file is missing/unreadable, so gate resolution NEVER opens up (a
    missing registry means every class is unknown → refused, plus the code-
    forbidden set still holds)."""
    for path in (action_classes_path(), bootstrap_path()):
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        classes = doc.get("classes") if isinstance(doc, dict) else None
        if isinstance(classes, dict):
            return classes
    return {}


def resolve_gate(action_class: str, registry: dict | None = None) -> str | None:
    """The effective gate for a class, or None when it is unknown/disabled/
    malformed (→ the caller refuses). CODE-FORBIDDEN classes always resolve to
    'forbidden' regardless of the registry — a config edit cannot un-forbid a
    send."""
    if action_class in _CODE_FORBIDDEN:
        return "forbidden"
    reg = load_action_classes() if registry is None else registry
    entry = reg.get(action_class) if isinstance(reg, dict) else None
    if not isinstance(entry, dict) or entry.get("enabled") is not True:
        return None
    gate = entry.get("gate")
    return gate if gate in GATES else None
