"""The user's Slack focus settings: the `slack_focus` block of comms/config.json.

Everything specific to one person lives here: the channels they watch, the
bots they ignore, the repos their PRs land in, and their Jira projects. Their
Slack identity and workspace come from the token, so they're never configured.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DAYS = 7
DEFAULT_TITLE = "Slack focus"


@dataclass(frozen=True)
class JiraSettings:
    enabled: bool = False
    base_url: str = ""
    token_env: str = "JIRA_API_TOKEN"
    email_env: str = ""
    project_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    configured: bool = False
    days: int = DEFAULT_DAYS
    title: str = DEFAULT_TITLE
    watched_channels: dict[str, str] = field(default_factory=dict)
    bot_authors: tuple[str, ...] = ()
    skip_channels: tuple[str, ...] = ()
    github_repos: tuple[str, ...] = ()
    github_hosts: tuple[str, ...] = ("github.com",)
    jira: JiraSettings = field(default_factory=JiraSettings)
    problem: str = ""


def config_path(home: Path | None = None) -> Path:
    return (home or Path(os.environ.get("HOME", str(Path.home())))) / ".assistant/comms/config.json"


def _strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


def parse(block) -> Settings:
    """Build Settings from the raw `slack_focus` block. Wrong-typed fields fall
    back to their defaults, so a hand-edit typo degrades one knob, not the tab."""
    if not isinstance(block, dict):
        return Settings()
    days = block.get("days")
    watched = block.get("watched_channels")
    jira = block.get("jira") if isinstance(block.get("jira"), dict) else {}
    hosts = _strings(block.get("github_hosts"))
    return Settings(
        configured=True,
        days=days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 30 else DEFAULT_DAYS,
        title=block.get("title") if isinstance(block.get("title"), str) and block["title"].strip() else DEFAULT_TITLE,
        watched_channels={str(k): str(v) for k, v in watched.items()} if isinstance(watched, dict) else {},
        bot_authors=_strings(block.get("bot_authors")),
        skip_channels=_strings(block.get("skip_channels")),
        github_repos=_strings(block.get("github_repos")),
        github_hosts=hosts or ("github.com",),
        jira=JiraSettings(
            enabled=jira.get("enabled") is True,
            base_url=str(jira.get("base_url") or "").rstrip("/"),
            token_env=str(jira.get("token_env") or "JIRA_API_TOKEN"),
            email_env=str(jira.get("email_env") or ""),
            project_keys=tuple(k.upper() for k in _strings(jira.get("project_keys"))),
        ),
    )


def load(home: Path | None = None) -> Settings:
    """Read the live settings. A missing config means the board isn't set up
    yet, which the tab shows as setup steps. A config that isn't valid JSON
    names the problem instead."""
    try:
        doc = json.loads(config_path(home).read_text())
    except OSError:
        return Settings()
    except json.JSONDecodeError as e:
        return Settings(problem=f"~/.assistant/comms/config.json isn't valid JSON: {e.msg} on line {e.lineno}.")
    return parse(doc.get("slack_focus") if isinstance(doc, dict) else None)
