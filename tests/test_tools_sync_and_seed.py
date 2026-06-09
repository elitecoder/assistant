"""Coverage for the three small tool scripts' uncovered branches.

Targets the branches NOT already exercised by test_obsidian_tools.py and
test_work_receipts.py:
  - memory_repo_sync.py: every no-op + happy + exception path of
    sync_to_memory_repo (Popen recorded, never spawned), _expand, _load_config.
  - obsidian-write.py: the seeding path (_seed_note, run_seed) and main()'s
    CLI argument branches (--seed-all, --backfill-lessons, --title, bad
    frontmatter, write failures).
  - write-receipt.py: parse_tristate, cmux_title, mirror_receipt_to_vault, the
    write_receipt vault-mirror / cmux fallback branches, and main().

No real subprocess/cmux/git/bash ever runs — subprocess.run / subprocess.Popen
are monkeypatched. HOME-derived path constants are redirected at import via the
test_work_receipts.py pattern (set HOME before load) and/or monkeypatched after.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEMORY_REPO_SYNC = REPO / "bin/tools/memory_repo_sync.py"
OBSIDIAN_WRITE = REPO / "bin/tools/obsidian-write.py"
WRITE_RECEIPT = REPO / "bin/tools/write-receipt.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_with_home(name: str, path: Path, home: Path):
    """Import a bin script with HOME pointed at `home` so its import-time path
    constants (HOME/RECEIPTS_DIR/LOG_PATH/...) bind to the sandbox."""
    os.environ["HOME"] = str(home)
    return _load(name, path)


# ── fake memory_seeds module ──────────────────────────────────────────────────


def _seed(title, body, category, tags=None, frontmatter=None):
    return {"title": title, "body": body, "category": category,
            "tags": tags or [], "frontmatter": frontmatter or {},
            "content": f"{title}. {body}"}


def _install_fake_seeds(monkeypatch, *, all_seeds=None, lessons=None, raise_on=None):
    """Register a fake `memory_seeds` module in sys.modules so the bare
    `import memory_seeds` inside run_seed()/main() resolves to it."""
    fake = types.ModuleType("memory_seeds")

    def _all_seeds():
        if raise_on == "all_seeds":
            raise RuntimeError("seed mine blew up")
        return all_seeds if all_seeds is not None else {}

    def _confirmed_lessons():
        if raise_on == "lessons":
            raise RuntimeError("lesson backfill blew up")
        return lessons if lessons is not None else []

    fake.all_seeds = _all_seeds
    fake.confirmed_lessons = _confirmed_lessons
    monkeypatch.setitem(sys.modules, "memory_seeds", fake)
    return fake


# ═══════════════════════════════════════════════════════════════════════════════
# memory_repo_sync.py
# ═══════════════════════════════════════════════════════════════════════════════


class _FakePopen:
    """Records the Popen invocation without spawning anything."""
    calls: list[dict] = []

    def __init__(self, args, **kwargs):
        _FakePopen.calls.append({"args": args, **kwargs})


@pytest.fixture
def mrs():
    return _load("memory_repo_sync_mod", MEMORY_REPO_SYNC)


@pytest.fixture
def fake_popen(monkeypatch, mrs):
    _FakePopen.calls = []
    monkeypatch.setattr(mrs.subprocess, "Popen", _FakePopen)
    return _FakePopen


def _make_repo(tmp_path: Path, with_script: bool = True) -> Path:
    repo = tmp_path / "memrepo"
    if with_script:
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "sync-push.sh").write_text("#!/bin/bash\necho hi\n")
    else:
        repo.mkdir(parents=True)
    return repo


def _cfg(repo: Path, sync: dict | None = None) -> dict:
    cfg = {"memory_repo": {"local_path": str(repo)}}
    if sync is not None:
        cfg["sync"] = sync
    return cfg


def test_mrs_expand(mrs):
    assert mrs._expand("~/foo") == Path(os.path.expanduser("~/foo"))
    assert mrs._expand("/abs/path") == Path("/abs/path")


def test_mrs_load_config_valid(mrs, monkeypatch, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"a": 1}))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    assert mrs._load_config() == {"a": 1}


def test_mrs_load_config_missing(mrs, monkeypatch, tmp_path):
    monkeypatch.setattr(mrs, "CONFIG", tmp_path / "absent.json")
    assert mrs._load_config() is None


def test_mrs_load_config_malformed(mrs, monkeypatch, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text("{ not valid json")
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    assert mrs._load_config() is None


def test_mrs_noop_when_sync_in_progress(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_SYNC_IN_PROGRESS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mrs, "CONFIG", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps(_cfg(repo)))
    mrs.sync_to_memory_repo("lesson")
    assert fake_popen.calls == []


def test_mrs_noop_when_config_absent(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    monkeypatch.setattr(mrs, "CONFIG", tmp_path / "absent.json")
    mrs.sync_to_memory_repo("lesson")
    assert fake_popen.calls == []


def test_mrs_noop_when_lesson_toggle_disabled(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo, {"push_on_lesson_confirm": False})))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    mrs.sync_to_memory_repo("lesson")
    assert fake_popen.calls == []


def test_mrs_noop_when_memory_toggle_disabled(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo, {"push_on_memory_add": False})))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    mrs.sync_to_memory_repo("memory")
    assert fake_popen.calls == []


def test_mrs_noop_when_script_missing(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path, with_script=False)  # no scripts/sync-push.sh
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo)))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    mrs.sync_to_memory_repo("lesson")
    assert fake_popen.calls == []


def test_mrs_happy_path_spawns_and_audits(mrs, fake_popen, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo, {"push_on_lesson_confirm": True})))
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    monkeypatch.setattr(mrs, "AUDIT_LOG", audit)

    mrs.sync_to_memory_repo("lesson")

    assert len(fake_popen.calls) == 1
    call = fake_popen.calls[0]
    script = str(repo / "scripts" / "sync-push.sh")
    assert call["args"] == ["bash", script]
    assert call["cwd"] == str(repo)
    assert call["env"]["MEMORY_SYNC_IN_PROGRESS"] == "1"
    assert call["start_new_session"] is True
    log_text = audit.read_text()
    assert "[memory-sync] push triggered (lesson)" in log_text


def test_mrs_default_on_when_toggle_absent(mrs, fake_popen, monkeypatch, tmp_path):
    # No sync block at all → toggle defaults on → proceeds to spawn.
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo)))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    monkeypatch.setattr(mrs, "AUDIT_LOG", tmp_path / "audit.log")
    mrs.sync_to_memory_repo("memory")
    assert len(fake_popen.calls) == 1


def test_mrs_unknown_reason_proceeds(mrs, fake_popen, monkeypatch, tmp_path):
    # An unmapped reason has no toggle → never short-circuits.
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo, {"push_on_lesson_confirm": False})))
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    monkeypatch.setattr(mrs, "AUDIT_LOG", tmp_path / "audit.log")
    mrs.sync_to_memory_repo("manual")  # not "lesson"/"memory"
    assert len(fake_popen.calls) == 1


def test_mrs_exception_path_is_suppressed_and_audited(mrs, monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo)))
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    monkeypatch.setattr(mrs, "AUDIT_LOG", audit)

    def _boom(*a, **k):
        raise RuntimeError("popen exploded")

    monkeypatch.setattr(mrs.subprocess, "Popen", _boom)
    # Must not raise out of the function.
    mrs.sync_to_memory_repo("lesson")
    assert "[memory-sync] push hook errored (suppressed)" in audit.read_text()


def test_mrs_exception_path_when_audit_log_also_unwritable(mrs, monkeypatch, tmp_path):
    # Both the outer body and the recovery write hit the audit log; if the log
    # path is a directory, open(..., "a") raises OSError — the inner
    # `except OSError: pass` (lines 84-85) swallows it. Function must not raise.
    monkeypatch.delenv("MEMORY_SYNC_IN_PROGRESS", raising=False)
    repo = _make_repo(tmp_path)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(_cfg(repo)))
    audit_dir = tmp_path / "audit_is_a_dir"
    audit_dir.mkdir()  # opening a directory for append raises IsADirectoryError
    monkeypatch.setattr(mrs, "CONFIG", cfg_file)
    monkeypatch.setattr(mrs, "AUDIT_LOG", audit_dir)
    # Should complete silently — no exception escapes.
    mrs.sync_to_memory_repo("lesson")


# ═══════════════════════════════════════════════════════════════════════════════
# obsidian-write.py
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def ow():
    return _load("obsidian_write_seed_mod", OBSIDIAN_WRITE)


def test_seed_note_work_history_foldered_by_month(ow, tmp_path):
    vault = tmp_path / "vault"
    s = _seed("Shipped a thing", "body text", "work_history",
              tags=["shipped"], frontmatter={"date": "2026-06-06", "pr": 5})
    ow._seed_note(vault, s)
    target = vault / "Work Log" / "2026-06"
    files = list(target.glob("*.md"))
    assert len(files) == 1
    assert files[0].name == "2026-06-06-shipped-a-thing.md"


def test_seed_note_non_work_history_uses_category_folder(ow, tmp_path):
    vault = tmp_path / "vault"
    s = _seed("A decision", "body", "decision", frontmatter={"date": "2026-06-06"})
    ow._seed_note(vault, s)
    files = list((vault / "Assistant" / "Decisions").glob("*.md"))
    assert len(files) == 1
    assert files[0].name == "2026-06-06-a-decision.md"


def test_run_seed_writes_all_categories_and_lessons(ow, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    all_seeds = {
        "working_style": [_seed("Work style one", "b", "working_style",
                                frontmatter={"date": "2026-06-06"})],
        "decision": [_seed("Decision one", "b", "decision",
                            frontmatter={"date": "2026-06-06"})],
    }
    lessons = [_seed("never do X", "b", "lesson", tags=["lesson"],
                     frontmatter={"date": "2026-06-06"})]
    _install_fake_seeds(monkeypatch, all_seeds=all_seeds, lessons=lessons)

    counts = ow.run_seed(vault, include_lessons=True)

    assert counts == {"lesson": 1, "working_style": 1, "decision": 1}
    assert list((vault / "Assistant" / "Lessons").glob("*.md"))
    assert list((vault / "Assistant" / "Working Style").glob("*.md"))
    assert list((vault / "Assistant" / "Decisions").glob("*.md"))


def test_run_seed_without_lessons(ow, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    all_seeds = {"project": [_seed("Proj", "b", "project",
                                   frontmatter={"date": "2026-06-06"})]}
    _install_fake_seeds(monkeypatch, all_seeds=all_seeds, lessons=[_seed(
        "should-not-be-written", "b", "lesson")])
    counts = ow.run_seed(vault, include_lessons=False)
    assert "lesson" not in counts
    assert counts == {"project": 1}
    assert not (vault / "Assistant" / "Lessons").exists()


def _run_main(mod, argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mod.main(argv)
    return rc, out.getvalue()


def test_main_seed_all(ow, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    all_seeds = {
        "working_style": [_seed("WS", "b", "working_style",
                                frontmatter={"date": "2026-06-06"})],
        "project": [_seed("P", "b", "project", frontmatter={"date": "2026-06-06"})],
    }
    lessons = [_seed("a lesson", "b", "lesson", frontmatter={"date": "2026-06-06"})]
    _install_fake_seeds(monkeypatch, all_seeds=all_seeds, lessons=lessons)

    rc, out = _run_main(ow, ["--seed-all", "--vault", str(vault)])
    assert rc == 0
    data = json.loads(out)
    assert data["status"] == "written"
    assert data["total"] == 3  # 1 lesson + 1 ws + 1 project
    assert data["seeded"]["lesson"] == 1
    assert list((vault / "Assistant" / "Lessons").glob("*.md"))


def test_main_backfill_lessons_only(ow, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    lessons = [_seed("lesson a", "b", "lesson", frontmatter={"date": "2026-06-06"}),
               _seed("lesson b", "b", "lesson", frontmatter={"date": "2026-06-06"})]
    _install_fake_seeds(monkeypatch, all_seeds={"project": [
        _seed("must-not-write", "b", "project")]}, lessons=lessons)

    rc, out = _run_main(ow, ["--backfill-lessons", "--vault", str(vault)])
    assert rc == 0
    data = json.loads(out)
    assert data["seeded"] == {"lesson": 2}
    assert data["total"] == 2
    # Only lessons written — no project notes.
    assert not (vault / "Projects").exists()
    assert len(list((vault / "Assistant" / "Lessons").glob("*.md"))) == 2


def test_main_seed_exception_returns_error(ow, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _install_fake_seeds(monkeypatch, raise_on="all_seeds")
    rc, out = _run_main(ow, ["--seed-all", "--vault", str(vault)])
    assert rc == 1
    data = json.loads(out)
    assert data["status"] == "error"
    assert "seed mine blew up" in data["error"]


def test_main_no_title_no_seed_is_error(ow, tmp_path):
    rc, out = _run_main(ow, ["--vault", str(tmp_path / "vault")])
    assert rc == 1
    data = json.loads(out)
    assert data["status"] == "error"
    assert "--title is required" in data["error"]


def test_main_title_writes_note(ow, tmp_path):
    vault = tmp_path / "vault"
    rc, out = _run_main(ow, [
        "--vault", str(vault),
        "--title", "Hello World",
        "--body", "the body",
        "--category", "lesson",
        "--tags", "a", "b",
        "--frontmatter", json.dumps({"k": "v"}),
        "--date", "2026-06-06",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["status"] == "written"
    p = Path(data["path"])
    assert p.exists()
    assert p.parent == vault / "Assistant" / "Lessons"
    assert p.name == "2026-06-06-hello-world.md"
    text = p.read_text()
    assert "k: v" in text and "- a" in text and "- b" in text


def test_main_bad_frontmatter_json(ow, tmp_path):
    rc, out = _run_main(ow, [
        "--vault", str(tmp_path / "vault"),
        "--title", "T", "--frontmatter", "{not json",
    ])
    assert rc == 1
    data = json.loads(out)
    assert data["status"] == "error"
    assert "bad --frontmatter" in data["error"]


def test_main_frontmatter_not_object(ow, tmp_path):
    rc, out = _run_main(ow, [
        "--vault", str(tmp_path / "vault"),
        "--title", "T", "--frontmatter", "[1, 2]",
    ])
    assert rc == 1
    data = json.loads(out)
    assert data["status"] == "error"
    assert "bad --frontmatter" in data["error"]


def test_main_write_note_raises_returns_error(ow, tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ow, "write_note", _boom)
    rc, out = _run_main(ow, [
        "--vault", str(tmp_path / "vault"), "--title", "T",
    ])
    assert rc == 1
    data = json.loads(out)
    assert data["status"] == "error"
    assert "disk full" in data["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# write-receipt.py
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeRun:
    """A fake subprocess.run result + factory you can hand canned behavior."""

    def __init__(self, returncode=0, stdout="", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self._raises = raises

    def __call__(self, *args, **kwargs):
        if self._raises is not None:
            raise self._raises
        # record the cmd for assertions
        _FakeRun.last_cmd = args[0] if args else kwargs.get("args")
        return self

    last_cmd = None


@pytest.fixture
def wr(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return _load_with_home("write_receipt_seed_mod", WRITE_RECEIPT, home), home


# ── parse_tristate ──


def test_parse_tristate(wr):
    mod, _ = wr
    assert mod.parse_tristate("true") is True
    assert mod.parse_tristate("TRUE") is True
    assert mod.parse_tristate("false") is False
    assert mod.parse_tristate("False") is False
    assert mod.parse_tristate("unknown") is None
    assert mod.parse_tristate(None) is None
    assert mod.parse_tristate("garbage") is None


# ── cmux_title ──


def test_cmux_title_match(wr, monkeypatch):
    mod, _ = wr
    payload = {"workspaces": [
        {"ref": "workspace:1", "title": "Other"},
        {"ref": "workspace:5", "title": "My Title"},
    ]}
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=0, stdout=json.dumps(payload)))
    assert mod.cmux_title("workspace:5") == "My Title"


def test_cmux_title_no_match(wr, monkeypatch):
    mod, _ = wr
    payload = {"workspaces": [{"ref": "workspace:1", "title": "Other"}]}
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=0, stdout=json.dumps(payload)))
    assert mod.cmux_title("workspace:5") is None


def test_cmux_title_nonzero_rc(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=1, stdout="whatever"))
    assert mod.cmux_title("workspace:5") is None


def test_cmux_title_empty_stdout(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=0, stdout="   "))
    assert mod.cmux_title("workspace:5") is None


def test_cmux_title_empty_title_is_none(wr, monkeypatch):
    mod, _ = wr
    payload = {"workspaces": [{"ref": "workspace:5", "title": "   "}]}
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=0, stdout=json.dumps(payload)))
    assert mod.cmux_title("workspace:5") is None


def test_cmux_title_exception(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(raises=RuntimeError("cmux down")))
    assert mod.cmux_title("workspace:5") is None


# ── mirror_receipt_to_vault ──


def _full_receipt(mod):
    return {
        "ts": "2026-06-06T12:00:00Z",
        "ws_ref": "workspace:5",
        "project": "MyProject",
        "pr_number": 42,
        "pr_url": "https://example/pull/42",
        "ci_status": "green",
        "reviewer_approved": True,
        "test_count": 9,
        "summary": "did the thing",
        "outcome": "shipped",
        "quality_score": "high",
    }


def test_mirror_receipt_to_vault_happy(wr, monkeypatch):
    mod, _ = wr
    fake = _FakeRun(returncode=0, stdout=json.dumps({"path": "/v/note.md"}))
    monkeypatch.setattr(mod.subprocess, "run", fake)
    # OBSIDIAN_WRITE must exist (it's the real file by default) — keep it.
    assert mod.OBSIDIAN_WRITE.exists()

    out = mod.mirror_receipt_to_vault(_full_receipt(mod))
    assert out == "/v/note.md"

    cmd = _FakeRun.last_cmd
    assert str(mod.OBSIDIAN_WRITE) in cmd
    assert "--category" in cmd
    assert cmd[cmd.index("--category") + 1] == "work_history"
    assert "--folder" in cmd
    assert cmd[cmd.index("--folder") + 1] == "Work Log/2026-06"
    # All body-line branches present in the --body argument.
    body = cmd[cmd.index("--body") + 1]
    assert "**Tests:** 9" in body
    assert "[42](https://example/pull/42)" in body
    assert "did the thing" in body
    # pr lands in frontmatter.
    fm = json.loads(cmd[cmd.index("--frontmatter") + 1])
    assert fm["pr"] == 42 and fm["project"] == "MyProject"


def test_mirror_receipt_to_vault_missing_writer(wr, monkeypatch, tmp_path):
    mod, _ = wr
    monkeypatch.setattr(mod, "OBSIDIAN_WRITE", tmp_path / "does-not-exist.py")
    called = {"ran": False}

    def _should_not_run(*a, **k):
        called["ran"] = True
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(mod.subprocess, "run", _should_not_run)
    assert mod.mirror_receipt_to_vault(_full_receipt(mod)) is None
    assert called["ran"] is False


def test_mirror_receipt_to_vault_subprocess_raises(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(raises=RuntimeError("boom")))
    assert mod.mirror_receipt_to_vault(_full_receipt(mod)) is None


def test_mirror_receipt_to_vault_empty_stdout(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod.subprocess, "run",
                        _FakeRun(returncode=0, stdout="   "))
    assert mod.mirror_receipt_to_vault(_full_receipt(mod)) is None


def test_mirror_receipt_to_vault_minimal_receipt(wr, monkeypatch):
    # No test_count / pr_url / summary → those body-lines are skipped, no pr fm.
    mod, _ = wr
    fake = _FakeRun(returncode=0, stdout=json.dumps({"path": "/v/n.md"}))
    monkeypatch.setattr(mod.subprocess, "run", fake)
    receipt = {"ts": "2026-06-06T00:00:00Z", "ws_ref": "workspace:9",
               "outcome": "abandoned"}
    out = mod.mirror_receipt_to_vault(receipt)
    assert out == "/v/n.md"
    cmd = _FakeRun.last_cmd
    body = cmd[cmd.index("--body") + 1]
    assert "**Tests:**" not in body
    assert "**PR:**" not in body
    # abandoned outcome → 'abandoned' tag.
    assert "abandoned" in cmd
    fm = json.loads(cmd[cmd.index("--frontmatter") + 1])
    assert "pr" not in fm


# ── write_receipt vault-mirror + cmux fallback branches ──


def test_write_receipt_attaches_obsidian_note(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod, "mirror_receipt_to_vault", lambda r: "/v/note.md")
    receipt = mod.write_receipt(
        ws_ref="workspace:5", project="P", pr=None, ci_status="green",
        reviewer_approved=True, test_count=None, summary="s", outcome="shipped")
    assert receipt["_obsidian_note"] == "/v/note.md"


def test_write_receipt_no_obsidian_note_when_mirror_none(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod, "mirror_receipt_to_vault", lambda r: None)
    receipt = mod.write_receipt(
        ws_ref="workspace:5", project="P", pr=None, ci_status="green",
        reviewer_approved=True, test_count=None, summary="s", outcome="shipped")
    assert "_obsidian_note" not in receipt


def test_write_receipt_project_falls_back_to_cmux_title(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod, "mirror_receipt_to_vault", lambda r: None)
    monkeypatch.setattr(mod, "cmux_title", lambda ref: "FromCmux")
    receipt = mod.write_receipt(
        ws_ref="workspace:5", project=None, pr=None, ci_status="green",
        reviewer_approved=True, test_count=None, summary="s", outcome="shipped")
    assert receipt["project"] == "FromCmux"


def test_write_receipt_project_falls_back_to_ws_ref(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod, "mirror_receipt_to_vault", lambda r: None)
    monkeypatch.setattr(mod, "cmux_title", lambda ref: None)
    receipt = mod.write_receipt(
        ws_ref="workspace:5", project=None, pr=None, ci_status="green",
        reviewer_approved=True, test_count=None, summary="s", outcome="shipped")
    assert receipt["project"] == "workspace:5"


# ── main ──


def test_main_happy(wr, monkeypatch):
    mod, _ = wr
    monkeypatch.setattr(mod, "mirror_receipt_to_vault", lambda r: None)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mod.main(["--ws", "workspace:5", "--project", "P",
                       "--ci-status", "green", "--reviewer-approved", "true",
                       "--summary", "s", "--outcome", "shipped"])
    assert rc == 0
    data = json.loads(out.getvalue())
    assert data["ws_ref"] == "workspace:5"
    assert data["project"] == "P"
    assert data["reviewer_approved"] is True
    assert data["quality_score"] == "high"


def test_main_write_receipt_raises(wr, monkeypatch):
    mod, _ = wr

    def _boom(**kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(mod, "write_receipt", _boom)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mod.main(["--ws", "workspace:5", "--project", "P"])
    assert rc == 1
    data = json.loads(out.getvalue())
    assert data["ws_ref"] == "workspace:5"
    assert "disk gone" in data["error"]
