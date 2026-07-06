"""assistant — single-process daemon collapsing the pulse + heartbeat scripts
into one binary.

The daemon (`python -m assistant`) owns every long-running subsystem in its own
thread of one process, instead of the previous model of a pulse.py LaunchAgent
on a 5-min timer plus a swarm of subprocess-invoked CLI scripts.

It does NOT change the file-based interfaces the rest of the system reads/writes
(actions-ledger.jsonl, heartbeat.json, the Observer summaries/runs, the
proposals queue) and it does NOT rewrite the per-tool scripts under bin/tools/.
The daemon is *additive*: the existing pulse.py keeps working until the operator
explicitly switches the LaunchAgents over.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.1"
