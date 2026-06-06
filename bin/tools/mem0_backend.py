#!/usr/bin/env python3
"""mem0_backend — the memory store behind mem0-add / mem0-search.

Design: prefer the real mem0ai package when it's importable AND configurable;
otherwise use a dependency-free local JSONL store with lexical (token-overlap)
scoring. Both expose the same `add()` / `search()` surface, so the CLI tools and
their tests don't care which is live, and the day mem0ai + an embedder land
(the parallel Mem0 workstream), the same tools start using it with no rewrite.

The local backend is the spec's sanctioned fallback ("If no embedder is
available at all…"): it stores one JSON object per memory in
~/.assistant/mem0/memories.jsonl and ranks search results by token overlap with
an IDF-ish weighting. It is not semantic, but it is correct, idempotent, and
ships today with stdlib only.

Idempotency: a memory's id is a stable hash of (content + sorted metadata), so
re-adding the same content+metadata is a no-op that returns the existing id.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

MEM0_DATA_DIR = Path.home() / ".assistant" / "mem0"
LOCAL_STORE = MEM0_DATA_DIR / "memories.jsonl"
USER_ID = "mukul"

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _memory_id(content: str, metadata: dict[str, Any]) -> str:
    """Stable id from content + metadata so identical adds collapse."""
    payload = content.strip() + " " + json.dumps(metadata, sort_keys=True,
                                                      ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ── real mem0ai probe ────────────────────────────────────────────────────────

def _try_real_mem0() -> Any | None:
    """Return a configured mem0.Memory instance, or None if mem0ai isn't
    importable / can't be configured without credentials. Best-effort and
    silent — the local backend is always a valid answer."""
    if os.environ.get("MEM0_FORCE_LOCAL") == "1":
        return None
    try:
        from mem0 import Memory  # type: ignore
    except Exception:
        return None
    config = {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "assistant_memory",
                "path": str(MEM0_DATA_DIR / "chroma"),
            },
        },
    }
    try:
        return Memory.from_config(config)
    except Exception:
        # No embedder / no creds / chroma missing — fall back to local.
        return None


# ── local JSONL backend ──────────────────────────────────────────────────────

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
    """Thin facade: uses real mem0 if available, else the local store."""

    def __init__(self):
        self._real = _try_real_mem0()
        self._local = LocalStore()

    @property
    def provider(self) -> str:
        return "mem0ai" if self._real is not None else "local-jsonl"

    def add(self, content: str, metadata: dict[str, Any],
            user_id: str = USER_ID) -> dict[str, Any]:
        if self._real is not None:
            try:
                res = self._real.add(content, user_id=user_id, metadata=metadata)
                # mem0 returns a list of {id,...} or {"results":[...]} by version.
                mid = _extract_real_id(res) or _memory_id(content, metadata)
                return {"memory_id": mid, "status": "added", "provider": "mem0ai"}
            except Exception:
                pass  # fall through to local on any runtime failure
        return self._local.add(content, metadata, user_id)

    def search(self, query: str, limit: int = 5, user_id: str = USER_ID,
               category: str | None = None) -> list[dict[str, Any]]:
        if self._real is not None:
            try:
                res = self._real.search(query, user_id=user_id, limit=limit)
                items = res.get("results", res) if isinstance(res, dict) else res
                out = []
                for it in items or []:
                    meta = it.get("metadata", {}) or {}
                    if category and meta.get("category") != category:
                        continue
                    out.append({"memory": it.get("memory", it.get("text", "")),
                                "score": it.get("score", 0.0), "metadata": meta})
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
