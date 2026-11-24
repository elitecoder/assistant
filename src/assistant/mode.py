"""Default or Pro mode.

Pro mode turns on notifications and triage, goals and the planner, and the
model-heavy background jobs. The setting is `{"mode": "pro"}` or
`{"mode": "default"}` in ~/.assistant/config.json. When the file or the key
is missing, an install that already has Pro data (goals, notifications, a saved
brief, or the Pro skills) counts as Pro, and a fresh install counts as default.
An unreadable file counts as default.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("default", "pro")
PRO_SKILLS = ("goal", "attend", "back-off", "lesson")
REPO = Path(__file__).resolve().parents[2]


def _home() -> Path:
    return Path(os.environ["HOME"])


def config_path() -> Path:
    return _home() / ".assistant" / "config.json"


def _read_config() -> dict:
    """The config as a dict; {} when absent. Raises ValueError when unreadable."""
    try:
        text = config_path().read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{config_path()} isn't valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{config_path()} isn't a JSON object")
    return data


def config_error() -> str | None:
    try:
        _read_config()
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


def saved_mode() -> str | None:
    try:
        value = _read_config().get("mode")
    except (OSError, ValueError):
        return None
    return value if value in MODES else None


def _link_state(link: Path, name: str, repo: Path) -> str:
    """"ours" (into this repo), "stale" (a dangling link to an old Assistant
    checkout), or "other"."""
    if not link.is_symlink():
        return "other"
    try:
        if link.resolve() == (repo / "skills" / name).resolve():
            return "ours"
    except (OSError, RuntimeError):
        return "other"
    target = Path(os.readlink(link))
    if not link.exists() and target.parts[-2:] == ("skills", name):
        return "stale"
    return "other"


def _is_old_copy(folder: Path, name: str) -> bool:
    """A real folder an early installer copied in instead of linking."""
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        text = (folder / "SKILL.md").read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return f"\nname: {name}\n" in text and "Assistant" in text


def has_pro_state() -> bool:
    home = _home()
    for name in PRO_SKILLS:
        link = home / ".claude/skills" / name
        if _link_state(link, name, REPO) == "ours" or _is_old_copy(link, name):
            return True
    for path in (home / ".claude/assistant-goals.json",
                 home / ".assistant/decisions/decisions.jsonl"):
        try:
            if path.stat().st_size > 0:
                return True
        except OSError:
            pass
    return any((home / ".assistant/brief").glob("brief-????-??-??.json"))


def current() -> str:
    try:
        value = _read_config().get("mode")
    except (OSError, ValueError):
        return "default"
    if value is None:
        return "pro" if has_pro_state() else "default"
    return value if value in MODES else "default"


def is_pro() -> bool:
    return current() == "pro"


def observer_enabled() -> bool:
    """True only when config.json has {"observer": {"enabled": true}}. A
    missing or unreadable file, a missing key, or any other value leaves the
    Observer off."""
    try:
        observer = _read_config().get("observer")
    except (OSError, ValueError):
        return False
    return isinstance(observer, dict) and observer.get("enabled") is True


# The background jobs that are off until someone turns them on, each with the
# variable that turns it on for one run. `decisions` is the job that turns
# GitHub and session events into the decision list.
JOB_ENV = {"decisions": "ASSISTANT_DECISIONS", "brief": "ASSISTANT_BRIEF",
           "planner": "ASSISTANT_PLANNER"}
_ON_WORDS = ("1", "true", "yes", "on")


def env_on(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in _ON_WORDS


def job_settings() -> dict[str, bool]:
    """Which of the opt-in jobs are turned on. A job is on when config.json has
    {"jobs": {"<name>": true}} or its variable is set to 1. Only the JSON value
    true counts; a missing key, any other value, or an unreadable file leaves
    the job off. Reads the config once, so callers read it once per pulse."""
    try:
        jobs = _read_config().get("jobs")
    except (OSError, ValueError):
        jobs = None
    if not isinstance(jobs, dict):
        jobs = {}
    return {name: jobs.get(name) is True or env_on(var) for name, var in JOB_ENV.items()}


def save(value: str) -> None:
    """Write the mode into config.json, keeping every other key.

    Refuses (ValueError) to touch a config it can't parse."""
    if value not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    data = _read_config()
    data["mode"] = value
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def sync_skills(pro: bool, repo: Path = REPO) -> list[str]:
    """Link the Pro skills in Pro mode; unlink our own links in default mode.

    Pro mode also replaces broken links left by a moved Assistant checkout.
    Leaves everything else alone. Returns the changes made."""
    skills = _home() / ".claude/skills"
    changes = []
    for name in PRO_SKILLS:
        target, source = skills / name, repo / "skills" / name
        state = _link_state(target, name, repo)
        if (state == "stale" and pro) or (state == "ours" and not pro):
            target.unlink()
            changes.append(f"unlinked {name}")
        if pro and not target.exists() and not target.is_symlink() and source.is_dir():
            skills.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
            changes.append(f"linked {name}")
    return changes


def switch(value: str, repo: Path = REPO) -> list[str]:
    """Link or unlink the Pro skills, then save the mode. Saves nothing when
    the config can't be read or the skills can't be changed."""
    if value not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    _read_config()
    changes = sync_skills(value == "pro", repo)
    save(value)
    return changes
