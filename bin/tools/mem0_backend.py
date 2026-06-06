#!/usr/bin/env python3
"""mem0_backend — the memory store behind mem0-add / mem0-search.

Three-tier store, picked at construction, all behind one `add()` / `search()`
facade so the CLI tools and their tests don't care which is live:

  1. mem0ai + AWS Bedrock Titan embeddings  (primary — real semantic search)
  2. mem0ai + local fastembed embeddings     (fallback — semantic, no creds/net)
  3. local JSONL + lexical (token-overlap) scoring  (last resort — stdlib only)

Tier 1 is the spec's target: mem0ai (https://github.com/mem0ai/mem0) over a
local chroma vector store, embedding with amazon.titan-embed-text-v2:0 (the box
is already authorised via AWS_BEARER_TOKEN_BEDROCK). If a real embed probe
fails (no creds, region down) we drop to tier 2 — fastembed's BAAI/bge model,
fully local. If mem0ai itself can't be imported (e.g. the venv is missing) we
drop to tier 3, the dependency-free JSONL store the parallel Obsidian workstream
shipped first so these tools were never blocked on mem0 landing.

## Runtime environment

mem0ai (+ chroma + boto3 + fastembed) only installs cleanly under Python 3.12,
so it lives in a dedicated venv at <repo>/.venv-mem0, not the system python3 the
tool dispatcher runs (3.14, no mem0). `ensure_venv()` — called first thing by
each CLI's main() — re-execs the process into that interpreter when `import
mem0` would otherwise fail, so the tools can be launched with any python3 and
still reach tier 1/2. Tests set MEM0_FORCE_LOCAL=1, which skips the hop and
pins tier 3 for deterministic, offline runs.

## Adds are verbatim and idempotent

We add with mem0's `infer=False`: curated content (a lesson, a decision) is
stored exactly as written, with no per-add LLM fact-extraction pass — adds are
deterministic and free. A `mem_hash` of (content + sorted metadata), the same id
the local store uses, is stamped into metadata and checked before every add, so
re-running a seed is a no-op (`status: "exists"`) rather than a duplicate.

Idempotency: a memory's id is a stable hash of (content + sorted metadata), so
re-adding the same content+metadata is a no-op that returns the existing id.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent.parent
VENV_PYTHON = REPO / ".venv-mem0" / "bin" / "python3"

MEM0_DATA_DIR = Path.home() / ".assistant" / "mem0"
LOCAL_STORE = MEM0_DATA_DIR / "memories.jsonl"
USER_ID = "mukul"
COLLECTION_NAME = "assistant_memory"

AWS_REGION = os.environ.get("AWS_REGION") or "us-west-2"
# NB: the [1m] suffix Claude Code puts on harness model ids is NOT a valid
# Bedrock model id — Bedrock rejects it. Use the bare inference-profile id.
BEDROCK_LLM_MODEL = "us.anthropic.claude-sonnet-4-6"
BEDROCK_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _memory_id(content: str, metadata: dict[str, Any]) -> str:
    """Stable id from content + metadata so identical adds collapse. Shared by
    the local store (as the record id) and the real backend (as `mem_hash`), so
    idempotency means the same thing in every tier."""
    payload = content.strip() + " " + json.dumps(metadata, sort_keys=True,
                                                      ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def ensure_venv() -> None:
    """Re-exec the calling script under .venv-mem0 if mem0 isn't importable here.

    The tool dispatcher runs scripts with the system python3, which has no mem0.
    Rather than make every caller know about the venv, the CLI mains call this
    first and we transparently hop into the venv interpreter (argv preserved).
    No-ops when: mem0 already imports, MEM0_FORCE_LOCAL is set (tests / pinned
    tier 3), we've already hopped (guard flag), or the venv is missing — in the
    last case tier 3 still answers, just lexically."""
    if os.environ.get("MEM0_FORCE_LOCAL") == "1":
        return
    if os.environ.get("MEM0_VENV_REEXEC") == "1":
        return  # already hopped; never loop
    try:
        import mem0  # noqa: F401
        return
    except Exception:
        pass
    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv],
                  dict(os.environ, MEM0_VENV_REEXEC="1"))
    # else: fall through — MemoryBackend will land on tier 3.


def _silence_noise() -> None:
    """mem0 / chroma / posthog are chatty. Keep tool output to JSON on stdout."""
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    os.environ.setdefault("POSTHOG_DISABLED", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    for name in ("mem0", "chromadb", "httpx", "posthog", "fastembed", "urllib3"):
        logging.getLogger(name).setLevel(logging.ERROR)


# ── tier 1/2: real mem0ai probe + build ───────────────────────────────────────

def _bedrock_embeds_work() -> bool:
    """Probe one real Bedrock embedding so we decide tier 1 vs 2 ONCE, up front,
    rather than half-build a Memory whose embedder dies on the first add."""
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        resp = client.invoke_model(
            body=json.dumps({"inputText": "probe"}),
            modelId=BEDROCK_EMBED_MODEL,
            accept="application/json", contentType="application/json")
        return bool(json.loads(resp["body"].read()).get("embedding"))
    except Exception as e:  # noqa: BLE001
        logging.getLogger("mem0_backend").warning(
            "Bedrock embed probe failed (%s); falling back to fastembed", e)
        return False


def _real_config(use_bedrock_embedder: bool) -> dict[str, Any]:
    embedder = (
        {"provider": "aws_bedrock",
         "config": {"model": BEDROCK_EMBED_MODEL, "aws_region": AWS_REGION}}
        if use_bedrock_embedder else
        {"provider": "fastembed", "config": {"model": FASTEMBED_MODEL}}
    )
    return {
        "vector_store": {
            "provider": "chroma",
            "config": {"collection_name": COLLECTION_NAME,
                       "path": str(MEM0_DATA_DIR / "chroma")},
        },
        # The LLM is configured but never invoked (we add with infer=False). It
        # must be Bedrock, not mem0's OpenAI default, so __init__ needs no key.
        "llm": {"provider": "aws_bedrock",
                "config": {"model": BEDROCK_LLM_MODEL, "aws_region": AWS_REGION,
                           "temperature": 0.1, "max_tokens": 2000}},
        "embedder": embedder,
    }


def _try_real_mem0() -> tuple[Any, str] | None:
    """Return (mem0.Memory, embedder_kind) or None if mem0ai isn't importable /
    can't be built. Best-effort and silent — the local backend is always a valid
    answer. embedder_kind is "bedrock" or "fastembed"."""
    if os.environ.get("MEM0_FORCE_LOCAL") == "1":
        return None
    try:
        from mem0 import Memory  # type: ignore
    except Exception:
        return None
    _silence_noise()
    MEM0_DATA_DIR.mkdir(parents=True, exist_ok=True)
    use_bedrock = _bedrock_embeds_work()
    try:
        return Memory.from_config(_real_config(use_bedrock)), \
            ("bedrock" if use_bedrock else "fastembed")
    except Exception as e:  # noqa: BLE001
        # Bedrock embedder built but something downstream broke — retry once on
        # the fully-local fastembed path before conceding to tier 3.
        if use_bedrock:
            logging.getLogger("mem0_backend").warning(
                "Bedrock Memory build failed (%s); retrying with fastembed", e)
            try:
                return Memory.from_config(_real_config(False)), "fastembed"
            except Exception:
                return None
        return None


# ── tier 3: local JSONL backend ────────────────────────────────────────────────

class LocalStore:
    """Append-only JSONL memory store with lexical search."""

    def __init__(self, path: Path = LOCAL_STORE):
        self.path = path

    def _load(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def add(self, content: str, metadata: dict[str, Any],
            user_id: str = USER_ID) -> dict[str, Any]:
        mid = _memory_id(content, metadata)
        existing = self._load()
        for m in existing:
            if m.get("id") == mid:
                return {"memory_id": mid, "status": "exists"}
        record = {"id": mid, "user_id": user_id, "content": content,
                  "metadata": metadata}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"memory_id": mid, "status": "added"}

    def search(self, query: str, limit: int = 5, user_id: str = USER_ID,
               category: str | None = None) -> list[dict[str, Any]]:
        records = [m for m in self._load() if m.get("user_id", USER_ID) == user_id]
        if category:
            records = [m for m in records
                       if (m.get("metadata") or {}).get("category") == category]
        if not records:
            return []

        # IDF over the corpus so common words ("the") don't dominate the score.
        docs = [_tokenize(m.get("content", "")) for m in records]
        df: dict[str, int] = {}
        for toks in docs:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = len(records)
        q_tokens = _tokenize(query)

        scored = []
        for m, toks in zip(records, docs):
            if not toks:
                continue
            tokset = set(toks)
            raw = 0.0
            for qt in set(q_tokens):
                if qt in tokset:
                    raw += math.log((n + 1) / (df.get(qt, 0) + 1)) + 1.0
            if raw <= 0:
                continue
            # Normalize to ~0..1 by the best achievable score for this query.
            denom = sum(math.log((n + 1) / (df.get(qt, 1) + 1)) + 1.0
                        for qt in set(q_tokens)) or 1.0
            scored.append((raw / denom, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"memory": m.get("content", ""),
                 "score": round(score, 4),
                 "metadata": m.get("metadata", {})}
                for score, m in scored[:limit]]


# ── unified facade ───────────────────────────────────────────────────────────

class MemoryBackend:
    """Thin facade: uses real mem0 (tier 1/2) if available, else local (tier 3)."""

    def __init__(self):
        real = _try_real_mem0()
        if real is not None:
            self._real, self._embedder = real
        else:
            self._real, self._embedder = None, None
        self._local = LocalStore()

    @property
    def provider(self) -> str:
        """e.g. 'mem0ai+bedrock', 'mem0ai+fastembed', or 'local-jsonl'."""
        if self._real is not None:
            return f"mem0ai+{self._embedder}"
        return "local-jsonl"

    def add(self, content: str, metadata: dict[str, Any],
            user_id: str = USER_ID) -> dict[str, Any]:
        if self._real is not None:
            try:
                return self._real_add(content, metadata, user_id)
            except Exception:
                pass  # fall through to local on any runtime failure
        return self._local.add(content, metadata, user_id)

    def _real_add(self, content: str, metadata: dict[str, Any],
                  user_id: str) -> dict[str, Any]:
        """Idempotent verbatim add against the real store.

        `mem_hash` (the same id LocalStore uses) is stamped into metadata and
        checked first, so a re-seed returns the existing id with status "exists"
        instead of duplicating. infer=False stores the text verbatim."""
        mid = _memory_id(content, metadata)
        meta = dict(metadata, mem_hash=mid)
        existing = self._real.get_all(filters={"user_id": user_id, "mem_hash": mid})
        hits = existing.get("results", []) if isinstance(existing, dict) else []
        if hits:
            return {"memory_id": hits[0].get("id", mid), "status": "exists",
                    "provider": self.provider}
        res = self._real.add(content, user_id=user_id, metadata=meta, infer=False)
        rid = _extract_real_id(res) or mid
        return {"memory_id": rid, "status": "added", "provider": self.provider}

    def search(self, query: str, limit: int = 5, user_id: str = USER_ID,
               category: str | None = None) -> list[dict[str, Any]]:
        if self._real is not None:
            try:
                # mem0 2.x: entity ids and metadata filters go in `filters=`.
                filters: dict[str, Any] = {"user_id": user_id}
                if category:
                    filters["category"] = category
                res = self._real.search(query, filters=filters, limit=limit)
                items = res.get("results", res) if isinstance(res, dict) else res
                out = []
                for it in items or []:
                    meta = it.get("metadata", {}) or {}
                    score = it.get("score", 0.0)
                    out.append({"memory": it.get("memory", it.get("text", "")),
                                "score": round(score, 4)
                                if isinstance(score, (int, float)) else score,
                                "metadata": meta})
                return out[:limit]
            except Exception:
                pass
        return self._local.search(query, limit, user_id, category)


def _extract_real_id(res: Any) -> str | None:
    if isinstance(res, dict):
        items = res.get("results", [])
    elif isinstance(res, list):
        items = res
    else:
        items = []
    if items and isinstance(items[0], dict):
        return items[0].get("id")
    return None
