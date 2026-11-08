"""Default or Pro mode.

Pro mode turns on notifications and triage, the connectors, goals and the
planner, and the model-heavy background jobs. Set it with
`{"mode": "pro"}` in ~/.assistant/config.json, or `ASSISTANT_MODE=pro`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PRO_TABS = ("brief", "decisions", "fleet", "connections")


def config_path() -> Path:
    return Path(os.environ["HOME"]) / ".assistant" / "config.json"


def is_pro() -> bool:
    override = os.environ.get("ASSISTANT_MODE", "").strip().lower()
    if override:
        return override == "pro"
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("mode") == "pro"
