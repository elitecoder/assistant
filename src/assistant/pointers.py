"""Ids and pointers: the short names Assistant's own records use in place of content.

A trace is the record of one piece of work, from the moment it arrives to the owner's decision on it. Its id is
minted once, when the work is first seen, and stored with it. Assistant's records (telemetry, the effects ledger)
hold these ids and pointers, never content: a Work queue row is identified by a link, and a link can carry a secret
or name a person, so a record holds the row's pointer instead.

Standard library only, no state, and it loads on the oldest Python a service can fall back to (3.9).
"""
from __future__ import annotations

import hashlib
import re
import secrets

_TRACE_ID = re.compile(r"t[rl]-[0-9a-f]{16}")


def new_trace_id() -> str:
    """A new trace id. Mint one when a piece of work is first seen, store it there, and never derive it again."""
    return "tr-" + secrets.token_hex(8)


def legacy_trace_id(kind: str, key: str) -> str:
    """The stand-in id for work that existed before ids were minted and was never given one: the same for the same
    kind and key every time, and marked `tl-` so it is never mistaken for a minted id."""
    return "tl-" + hashlib.sha256(f"{kind}\0{key}".encode("utf-8", "replace")).hexdigest()[:16]


def is_trace_id(value) -> bool:
    """True for a minted or a legacy trace id, and for nothing else."""
    return isinstance(value, str) and _TRACE_ID.fullmatch(value) is not None


def row_pointer(key: str) -> str:
    """The pointer for the Work queue row with this key: stable, the same in every process, and not reversible."""
    return "row:" + hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:16]
