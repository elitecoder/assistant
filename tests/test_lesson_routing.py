"""Tests for project-scoped lesson routing in bin/assistant-curator.py.

Covers the routing table added so that project-specific lessons land in the
project's own `.claude/rules/*.md` (and auto-commit there) instead of polluting
the personal CLAUDE.md store:

  - writing with --target ffp / archffp lands the block in the project file,
    with the path-scoped frontmatter a fresh file needs to actually load
  - the misrouted-lesson audit flags a project-scoped lesson sitting in CLAUDE.md
  - a project-target write fires exactly one `git commit` of just the rules file

These drive the real curator code (argparse + cmd_write + find_misrouted), not
mocks of it — only `subprocess.run` and the TARGETS paths are redirected so the
tests touch tmp dirs instead of the real repos.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(REPO / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cur = _load("assistant_curator_mod", "assistant-curator.py")


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_targets(tmp_path, monkeypatch):
    """Redirect every TARGET path under tmp_path so writes never touch real
    repos, and stub out the cross-machine memory sync. Returns the patched
    TARGETS dict so a test can read the resulting files back."""
    claude = tmp_path / "claude" / "CLAUDE.md"
    assistant = tmp_path / "assistant" / "prompts" / "observer-batch-prompt.md"
    ffp_repo = tmp_path / "ffp"
    archffp_repo = tmp_path / "archffp"
    assistant_repo = tmp_path / "assistant"

    targets = {
        "claude": {
            "path": claude,
            "scopes": {"global", "security", "dashboard"},
            "default_scope": "global",
        },
        "assistant": {
            "path": assistant,
            "scopes": {"verdict", "cleanup", "general"},
            "default_scope": "general",
        },
        "ffp": {
            "path": ffp_repo / ".claude/rules/ffp-lessons.md",
            "scopes": {"squirrel", "e2e", "ffp", "general"},
            "default_scope": "general",
            "repo": ffp_repo,
            "blurb": cur._project_blurb("FFP"),
            "preamble": cur._project_preamble("FFP"),
        },
        "archffp": {
            "path": archffp_repo / ".claude/rules/archffp-lessons.md",
            "scopes": {"pipeline", "cleanup", "eval", "archffp", "general"},
            "default_scope": "general",
            "repo": archffp_repo,
            "blurb": cur._project_blurb("archffp"),
            "preamble": cur._project_preamble("archffp"),
        },
    }
    monkeypatch.setattr(cur, "TARGETS", targets)
    monkeypatch.setattr(cur, "_sync_to_memory_repo", lambda: None)
    return targets


def _write_args(target, trigger, rule, scope=None, slug=None, added=None):
    return argparse.Namespace(
        target=target, trigger=trigger, rule=rule,
        scope=scope, slug=slug, added=added,
    )


# ─── Part 1: project targets write to their rules file ───────────────────────

def test_ffp_target_writes_to_ffp_rules(tmp_targets, monkeypatch):
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    rc = cur.cmd_write(_write_args(
        "ffp", "About to run E2E in a spawned workspace",
        "Pin --project=chromium-headless.", scope="e2e", slug="ffp-e2e-pin"))
    assert rc == 0

    path = tmp_targets["ffp"]["path"]
    text = path.read_text()
    # Landed in the ffp file, nowhere else.
    assert path.exists()
    assert not tmp_targets["claude"]["path"].exists()
    # The block is present and parseable by the curator.
    lessons = list(cur.iter_lessons(text))
    assert [L["slug"] for L in lessons] == ["ffp-e2e-pin"]
    assert lessons[0]["scope"] == "e2e"
    # A fresh project file gets the path-scoped frontmatter it needs to LOAD —
    # without `paths:` the rule is never injected into a session.
    assert text.startswith("---\npaths:")
    assert "## Lessons" in text


def test_archffp_target_writes_to_archffp_rules(tmp_targets, monkeypatch):
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    rc = cur.cmd_write(_write_args(
        "archffp", "About to run archffp cleanup teardown",
        "Reconcile the header port against the real listening PID.",
        scope="cleanup", slug="archffp-cleanup-port"))
    assert rc == 0

    path = tmp_targets["archffp"]["path"]
    lessons = list(cur.iter_lessons(path.read_text()))
    assert [L["slug"] for L in lessons] == ["archffp-cleanup-port"]
    assert lessons[0]["scope"] == "cleanup"
    assert path.read_text().startswith("---\npaths:")


def test_added_date_is_preserved_on_migration(tmp_targets, monkeypatch):
    """A migrated lesson keeps its original date via --added."""
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    rc = cur.cmd_write(_write_args(
        "ffp", "trigger", "rule", scope="ffp", slug="dated", added="2026-05-22"))
    assert rc == 0
    lessons = list(cur.iter_lessons(tmp_targets["ffp"]["path"].read_text()))
    assert lessons[0]["added"] == "2026-05-22"


def test_scope_with_digit_parses(tmp_targets, monkeypatch):
    """Scopes like `e2e` (a digit in the middle) must round-trip — the header
    regex was widened from [a-z]+ to [a-z0-9]+ to allow them."""
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    cur.cmd_write(_write_args("ffp", "t", "r", scope="e2e", slug="digit-scope"))
    lessons = list(cur.iter_lessons(tmp_targets["ffp"]["path"].read_text()))
    assert lessons[0]["scope"] == "e2e"


# ─── Part 2: misrouted-lesson audit ──────────────────────────────────────────

def test_wrong_target_audit(tmp_targets, monkeypatch):
    """A project-scoped lesson (scope=ffp) sitting in CLAUDE.md is detected as
    misrouted and pointed at the ffp store."""
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    # Allow the ffp scope into the claude store only for this fixture, to model
    # the legacy state where ffp lessons lived in CLAUDE.md.
    tmp_targets["claude"]["scopes"].add("ffp")
    cur.cmd_write(_write_args(
        "claude", "Filing Jira tickets for unimplemented work",
        "File as Story, not Bug.", scope="ffp", slug="ffp-jira-type"))

    misrouted = cur.find_misrouted(tmp_targets)
    assert len(misrouted) == 1
    m = misrouted[0]
    assert m["slug"] == "ffp-jira-type"
    assert m["current_target"] == "claude"
    assert m["suggested_target"] == "ffp"


def test_audit_clean_when_project_lessons_in_project_store(tmp_targets, monkeypatch):
    """A correctly-routed project lesson is NOT flagged."""
    monkeypatch.setattr(cur, "_stage_lesson_in_repo", lambda *a, **k: None)
    cur.cmd_write(_write_args("ffp", "t", "r", scope="ffp", slug="ok"))
    # And a genuinely personal lesson in claude is fine too.
    cur.cmd_write(_write_args("claude", "t2", "r2", scope="global", slug="personal"))
    assert cur.find_misrouted(tmp_targets) == []


# ─── Part 1: auto-commit to the project repo ─────────────────────────────────

def test_commit_to_repo(tmp_targets, monkeypatch):
    """Writing to a project target stages ONLY the rules file (git add, no commit).

    Project repos like firefly-platform require admin merge / branch protection;
    an auto-commit would bypass that. The lesson travels with the code when the
    developer's next PR includes the staged rules file change.
    """
    calls = []

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _CP()

    monkeypatch.setattr(cur.subprocess, "run", fake_run)

    rc = cur.cmd_write(_write_args(
        "ffp", "trigger here", "rule here", scope="ffp", slug="committed"))
    assert rc == 0

    # Exactly one git add (staging only — no commit in this loop).
    assert len(calls) == 1
    add = calls[0]
    assert add[:4] == ["git", "-C", str(tmp_targets["ffp"]["repo"]), "add"]
    # The staged pathspec is ONLY the rules file (relative), never `.` or -A.
    rel = ".claude/rules/ffp-lessons.md"
    assert rel in add
    assert "." not in add and "-A" not in add


def test_no_commit_for_session_targets(tmp_targets, monkeypatch):
    """The personal/Observer stores have no repo key — no git commit fires."""
    calls = []
    monkeypatch.setattr(cur.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd))
    cur.cmd_write(_write_args("claude", "t", "r", scope="global", slug="no-commit"))
    assert calls == []


def test_commit_failure_is_non_blocking(tmp_targets, monkeypatch):
    """A git failure must not fail the write — the lesson is already on disk."""
    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "not a git repository"

    monkeypatch.setattr(cur.subprocess, "run", lambda cmd, *a, **k: _Fail())
    monkeypatch.setattr(cur, "_audit", lambda msg: None)
    rc = cur.cmd_write(_write_args("ffp", "t", "r", scope="ffp", slug="still-written"))
    assert rc == 0
    lessons = list(cur.iter_lessons(tmp_targets["ffp"]["path"].read_text()))
    assert [L["slug"] for L in lessons] == ["still-written"]
