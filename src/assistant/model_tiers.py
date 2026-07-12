"""model_tiers — the ONE place a concrete model id lives (Keel M8).

Every LLM caller asks for a SEMANTIC TIER (frontier | balanced | cheap), never a
provider id. This module resolves a tier to the model-id string the ACTIVE
provider expects — so switching backend (Bedrock ↔ Anthropic ↔ Vertex) or
bumping a model version is a ONE-FILE change, and "are we on Bedrock?" stops
leaking into pulse.py / the connectors / mem0.

**How the provider is known** — the SAME way the Claude Code CLI itself picks a
backend: the CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX flags, read from the
env with a ~/.zprofile fallback (launchd does not source it, so the fleet's flag
lives there — the same rationale as pulse.load_bedrock_env). Detecting off the
CLI's own routing flags means the resolver can never disagree with the backend
that is actually live. MODEL_PROVIDER forces it explicitly.

Resolution precedence for a tier:
  1. an explicit per-tier override env  ASSISTANT_MODEL_<TIER>  (operator pins an
     exact id without touching code);
  2. the built-in default for the detected provider.

The 1M-context `[1m]` suffix is a Bedrock-only, call-path-specific quirk (the CLI
appends it for the 1M beta; Bedrock REJECTS it on some paths — e.g. the mem0
extractor). So it is opt-in per call via long_context=True and is ONLY ever added
on Bedrock. Metering/pricing match by the substrings opus/sonnet/haiku, so a
resolved id costs correctly on every provider. Pure stdlib; no imports beyond os/re.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TIERS = ("frontier", "balanced", "cheap")

# provider → tier → BARE model id. Bump versions HERE, once. Bedrock uses
# inference-profile ids (us.anthropic.*); the direct API + Vertex use bare ids.
# Any of these is overridable per tier via ASSISTANT_MODEL_<TIER>, so an operator
# on an unlisted provider (or a renamed profile) never has to edit this file.
_DEFAULTS = {
    "bedrock": {
        "frontier": "us.anthropic.claude-opus-4-8",
        "balanced": "us.anthropic.claude-sonnet-4-6",
        "cheap":    "us.anthropic.claude-haiku-4-5",
    },
    "anthropic": {
        "frontier": "claude-opus-4-8",
        "balanced": "claude-sonnet-4-6",
        "cheap":    "claude-haiku-4-5",
    },
    "vertex": {
        "frontier": "claude-opus-4-8",
        "balanced": "claude-sonnet-4-6",
        "cheap":    "claude-haiku-4-5",
    },
}

_TRUE = ("1", "true", "yes", "on")


def _truthy(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in _TRUE


def _zprofile_flag(key: str) -> str | None:
    """Read one `export KEY=...` from ~/.zprofile — launchd does not source it, so
    the fleet's provider flag lives there (same reader shape as the pulse's
    Bedrock-env parse). Best-effort; any problem yields None."""
    try:
        home = Path(os.environ.get("HOME", str(Path.home())))
        text = (home / ".zprofile").read_text()
    except (OSError, FileNotFoundError):
        return None
    matches = re.findall(rf'^\s*export\s+{re.escape(key)}\s*=\s*(.+?)\s*$',
                         text, re.M)
    if not matches:
        return None
    # LAST export wins — matches how the shell (and pulse.load_bedrock_env) reads
    # a duplicated export, so the resolver can't disagree with the live backend.
    v = matches[-1].strip()
    if v[:1] in ("'", '"') and v[0] in v[1:]:
        v = v[1:v.index(v[0], 1)]          # quoted value → the quoted contents
    else:
        v = v.split("#", 1)[0].split()[0] if v.split("#", 1)[0].split() else ""
    return v or None


def _flag(key: str) -> bool:
    """env first, then ~/.zprofile — the same precedence the pulse uses when it
    merges the Bedrock env for a subprocess."""
    v = os.environ.get(key)
    if v is None:
        v = _zprofile_flag(key)
    return _truthy(v)


def provider() -> str:
    """'bedrock' | 'vertex' | 'anthropic'. MODEL_PROVIDER forces it; otherwise it
    is detected from the CLI's OWN routing flags (env, then ~/.zprofile), so the
    resolver and the live backend can never disagree. Defaults to 'anthropic'
    (portable bare ids) when nothing indicates a cloud backend."""
    forced = (os.environ.get("MODEL_PROVIDER") or "").strip().lower()
    if forced in _DEFAULTS:
        return forced
    if _flag("CLAUDE_CODE_USE_BEDROCK"):
        return "bedrock"
    if _flag("CLAUDE_CODE_USE_VERTEX"):
        return "vertex"
    return "anthropic"


def model_for(tier: str, *, long_context: bool = False,
              provider_hint: str | None = None) -> str:
    """Resolve a semantic tier to the concrete model id for the active provider.

    A per-tier override env (ASSISTANT_MODEL_FRONTIER / _BALANCED / _CHEAP) wins
    and is taken VERBATIM — an operator pinning an exact id gets exactly that id
    (no [1m] mangling), which is the whole point of the escape hatch.

    `provider_hint` lets a caller whose OWN backend is fixed independently of the
    CLI's routing pin the id format (mem0's LLM is hardcoded to Bedrock, so it
    passes provider_hint="bedrock" — otherwise a host where the CLI routes
    non-Bedrock would hand mem0 a bare id its Bedrock backend rejects). An
    unknown hint falls back to detection.

    long_context adds the Bedrock-only `[1m]` 1M-context suffix — opt-in per call
    because Bedrock rejects it on some paths, so it is never added on any other
    provider, when not requested, or to a verbatim override."""
    if tier not in TIERS:
        raise ValueError(f"unknown model tier {tier!r} (expected {TIERS})")
    override = os.environ.get(f"ASSISTANT_MODEL_{tier.upper()}")
    if override:
        return override  # verbatim — the operator pinned an EXACT id
    hint = (provider_hint or "").strip().lower()
    prov = hint if hint in _DEFAULTS else provider()
    base = _DEFAULTS.get(prov, _DEFAULTS["anthropic"])[tier]
    if long_context and prov == "bedrock" and not base.endswith("[1m]"):
        base = base + "[1m]"
    return base
