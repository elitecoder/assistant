"""Where the Work queue keeps its settings and data, and which sources you linked.

sources.json holds no secrets. Tokens stay in your environment or in each
tool's own store (`gh`, `gog`, Work IQ, and the browser profile for Slack).

    {
      "port": 8851,
      "slack": {"team_id": "T0123", "workspace_url": "https://example.slack.com", "close_after_days": 14},
      "gmail": {"accounts": ["you@example.com"]},
      "calendar": {"accounts": ["you@example.com"]},
      "outlook": {"address": "you@example.com"},
      "github": {"skip_authors": []}
    }

A source that's missing from the file isn't linked, and every refresh skips it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SOURCES = ("slack", "gmail", "calendar", "outlook", "github")
DEFAULT_PORT = 8851


def home() -> Path:
    return Path(os.environ["HOME"])


def data_dir() -> Path:
    return Path(os.environ.get("WQ_DATA") or home() / ".claude/work-queue")


def settings_dir() -> Path:
    return home() / ".assistant/work-queue"


def sources_path() -> Path:
    return settings_dir() / "sources.json"


def browser_profile() -> Path:
    return settings_dir() / "browser-profile"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def load() -> dict:
    """The linked sources. {} when nothing is linked; ValueError when the file is unreadable."""
    try:
        text = sources_path().read_text()
    except FileNotFoundError:
        return {}
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{sources_path()} isn't valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{sources_path()} isn't a JSON object")
    return doc


def save(doc: dict) -> None:
    write_json(sources_path(), doc)


def port(doc: dict) -> int:
    return int(os.environ.get("REVIEW_PORT") or doc.get("port") or DEFAULT_PORT)
