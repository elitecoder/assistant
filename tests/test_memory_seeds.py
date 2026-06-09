"""Unit tests for bin/tools/memory_seeds.py and the LOCAL-store / facade branches
of bin/tools/mem0_backend.py not already covered by tests/test_mem0_tools.py.

memory_seeds binds HOME-derived path constants at import time, so every test
that touches a file source monkeypatches the module-level constant to a tmp
path. mem0_backend binds MEM0_DATA_DIR / LOCAL_STORE at import too — and could
re-exec into the real venv — so MEM0_FORCE_LOCAL=1 is forced for the whole
module and the data-dir constants are pointed at tmp. No real cmux/AWS/mem0 is
ever touched: _cmux_workspaces() (or the module's subprocess.run) is always
monkeypatched.

Both modules live under bin/tools/, which conftest.py does NOT put on sys.path,
so they are loaded by path via importlib exactly like test_mem0_tools.py does.
"""
from __future__ import annotations

import importlib.util
import json
import os
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SEEDS = REPO / "bin/tools/memory_seeds.py"
BACKEND = REPO / "bin/tools/mem0_backend.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ms():
    """A fresh memory_seeds module per test (cheap; keeps constants isolated)."""
    return _load("memory_seeds_under_test", SEEDS)


@pytest.fixture(autouse=True)
def force_local(monkeypatch):
    """Never let mem0_backend reach real mem0 / AWS / a venv re-exec."""
    monkeypatch.setenv("MEM0_FORCE_LOCAL", "1")


@pytest.fixture
def mb():
    """A fresh mem0_backend module per test."""
    return _load("mem0_backend_under_test", BACKEND)


# ════════════════════════════════════════════════════════════════════════════
# memory_seeds — helpers
# ════════════════════════════════════════════════════════════════════════════

def test_seed_default_and_explicit_content(ms):
    s = ms._seed("Title", "Body text", "decision", ["t1"], {"k": "v"})
    assert s == {
        "title": "Title", "body": "Body text", "category": "decision",
        "tags": ["t1"], "frontmatter": {"k": "v"},
        "content": "Title. Body text",
    }
    # explicit content passes through unchanged
    s2 = ms._seed("T", "B", "c", [], {}, content="explicit one-liner")
    assert s2["content"] == "explicit one-liner"
    # empty body still strips the trailing space ("Title. ".strip())
    s3 = ms._seed("OnlyTitle", "", "c", [], {})
    assert s3["content"] == "OnlyTitle."


def test_read_jsonl_skips_blank_malformed_and_nondict(ms, tmp_path):
    p = tmp_path / "data.jsonl"
    p.write_text(
        '{"a": 1}\n'
        "\n"                      # blank -> skipped
        "   \n"                   # whitespace -> skipped
        "{not valid json\n"       # malformed -> skipped
        '"a bare string"\n'       # valid json but not a dict -> skipped
        '42\n'                    # valid json, not a dict -> skipped
        '{"b": 2}\n'
    )
    rows = ms._read_jsonl(p)
    assert rows == [{"a": 1}, {"b": 2}]


def test_read_jsonl_missing_file_returns_empty(ms, tmp_path):
    assert ms._read_jsonl(tmp_path / "nope.jsonl") == []


def test_working_style_seeds(ms):
    seeds = ms.working_style_seeds()
    assert len(seeds) == len(ms._WORKING_STYLE)
    for s in seeds:
        assert s["category"] == "working_style"
        assert "working-style" in s["tags"]
        assert "preference" in s["tags"]
        assert s["content"].startswith("Mukul's working style:")
        assert "source" in s["frontmatter"]
    # content carries the title and body, grounded in source
    first_title = ms._WORKING_STYLE[0][0]
    assert seeds[0]["title"] == first_title
    assert first_title in seeds[0]["content"]


def _fake_run(returncode: int, stdout: str):
    def _run(*args, **kwargs):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)
    return _run


def test_cmux_workspaces_happy(ms, monkeypatch):
    payload = json.dumps({"workspaces": [
        {"ref": "workspace:1", "title": "X"},
        {"ref": "workspace:2", "title": "  Y  "},   # trimmed
        {"ref": "", "title": "no ref"},             # dropped
        {"title": "missing ref key"},               # dropped
    ]})
    monkeypatch.setattr(ms.subprocess, "run", _fake_run(0, payload))
    assert ms._cmux_workspaces() == {"workspace:1": "X", "workspace:2": "Y"}


def test_cmux_workspaces_nonzero_rc(ms, monkeypatch):
    monkeypatch.setattr(ms.subprocess, "run", _fake_run(1, "garbage"))
    assert ms._cmux_workspaces() == {}


def test_cmux_workspaces_empty_stdout(ms, monkeypatch):
    monkeypatch.setattr(ms.subprocess, "run", _fake_run(0, "   "))
    assert ms._cmux_workspaces() == {}


def test_cmux_workspaces_exception(ms, monkeypatch):
    def _boom(*a, **k):
        raise OSError("cmux not found")
    monkeypatch.setattr(ms.subprocess, "run", _boom)
    assert ms._cmux_workspaces() == {}


def test_domain_for(ms):
    assert "Connections" in ms._domain_for("FFP Connections work")
    assert ms._domain_for("squirrel timeline trim") == ms._PROJECT_DOMAINS[1][1]
    assert ms._domain_for("totally unknown project") == ""


def test_ledger_latest_by_ws_last_wins(ms):
    rows = [
        {"ws_ref": "w1", "ts": "2026-01-01", "kind": "a"},
        {"ws_ref": "w2", "ts": "2026-01-02", "kind": "b"},
        {"ws_ref": "w1", "ts": "2026-01-03", "kind": "c"},   # newer w1
        {"kind": "no-ws"},                                    # skipped
    ]
    latest = ms._ledger_latest_by_ws(rows)
    assert set(latest) == {"w1", "w2"}
    assert latest["w1"]["ts"] == "2026-01-03"
    assert latest["w2"]["ts"] == "2026-01-02"


def test_observer_classifications(ms, tmp_path, monkeypatch):
    report = tmp_path / "observer.json"
    report.write_text(json.dumps({"candidate_actions": [
        {"_source_ws": "w1", "_classification": "needs_user"},
        {"params": {"ws_ref": "w2"}, "_classification": "stranded"},
        {"_source_ws": "w1", "_classification": "cleanup"},   # first w1 wins
        {"_source_ws": "w3"},                                  # no class -> dropped
        {"_classification": "orphan"},                         # no ws -> dropped
    ]}))
    monkeypatch.setattr(ms, "OBSERVER_REPORT", report)
    cls = ms._observer_classifications()
    assert cls == {"w1": "needs_user", "w2": "stranded"}


def test_observer_classifications_missing_file(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "OBSERVER_REPORT", tmp_path / "absent.json")
    assert ms._observer_classifications() == {}


def test_observer_classifications_malformed(ms, tmp_path, monkeypatch):
    report = tmp_path / "bad.json"
    report.write_text("{not json")
    monkeypatch.setattr(ms, "OBSERVER_REPORT", report)
    assert ms._observer_classifications() == {}


def test_project_name(ms):
    assert ms._project_name("Connections feature [12]") == "Connections feature"
    assert ms._project_name("✳️ Squirrel trim [3]") == "Squirrel trim"
    # leading non-word glyphs stripped, no trailing locator
    assert ms._project_name("--- mem0 backend") == "mem0 backend"
    # empty after stripping -> falls back to original title.strip()
    assert ms._project_name("  [9]  ") == "[9]"


# ════════════════════════════════════════════════════════════════════════════
# memory_seeds — project_seeds
# ════════════════════════════════════════════════════════════════════════════

def test_project_seeds(ms, tmp_path, monkeypatch):
    workspaces = {
        "emptyws": "",                           # skipped (empty title)
        "warmws": "warm comms session",          # skipped (warm)
        "mgrws": "Assistant Manager dashboard",  # skipped (assistant manager)
        "w_old": "Squirrel trim [1]",            # same project as w_new
        "w_new": "Squirrel trim [2]",            # newer ledger ts -> wins
        "w_conn": "Connections anchors [5]",
    }
    monkeypatch.setattr(ms, "_cmux_workspaces", lambda: workspaces)

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ws_ref": "w_old", "ts": "2026-01-01T00:00:00", "kind": "emit-card",
         "evidence": "old activity"},
        {"ws_ref": "w_new", "ts": "2026-02-02T00:00:00", "kind": "emit-card",
         "evidence": "newer milestone evidence"},
        {"ws_ref": "w_conn", "ts": "2026-03-03T00:00:00", "kind": "emit-card",
         "evidence": "connection cascade"},
    ]))
    monkeypatch.setattr(ms, "LEDGER", ledger)

    observer = tmp_path / "observer.json"
    observer.write_text(json.dumps({"candidate_actions": [
        {"_source_ws": "w_conn", "_classification": "needs_user"},
    ]}))
    monkeypatch.setattr(ms, "OBSERVER_REPORT", observer)

    seeds = ms.project_seeds()
    by_name = {s["title"]: s for s in seeds}
    # warm + manager skipped; squirrel collapsed to one project
    assert set(by_name) == {"Squirrel trim", "Connections anchors"}
    assert all(s["category"] == "project" for s in seeds)

    sq = by_name["Squirrel trim"]
    # newer-ledger workspace chosen
    assert sq["frontmatter"]["workspace"] == "w_new"
    assert "newer milestone evidence" in sq["body"]
    assert "2026-02-02" in sq["body"]            # last-activity date line
    assert sq["tags"] == ["project"]             # no observer status for this ws

    conn = by_name["Connections anchors"]
    # domain line present for a matched keyword
    assert ms._domain_for("Connections anchors [5]") in conn["body"]
    # observer status line + status tag + frontmatter status
    assert "**Observer status:** needs_user" in conn["body"]
    assert conn["frontmatter"]["status"] == "needs_user"
    assert "needs_user" in conn["tags"]


def test_project_seeds_no_ledger_detail(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_cmux_workspaces",
                        lambda: {"w1": "Lonely project [1]"})
    monkeypatch.setattr(ms, "LEDGER", tmp_path / "absent-ledger.jsonl")
    monkeypatch.setattr(ms, "OBSERVER_REPORT", tmp_path / "absent-obs.json")
    seeds = ms.project_seeds()
    assert len(seeds) == 1
    assert seeds[0]["body"] == "Active workspace; no ledger detail yet."
    assert seeds[0]["frontmatter"] == {"workspace": "w1"}


# ════════════════════════════════════════════════════════════════════════════
# memory_seeds — work_history_seeds
# ════════════════════════════════════════════════════════════════════════════

def test_work_history_seeds(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_cmux_workspaces",
                        lambda: {"w1": "Squirrel trim [1]"})
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in [
        # verified emit-card WITH done-signal -> kept (older for w1)
        {"ws_ref": "w1", "ts": "2026-01-01T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "shipped the first cut",
         "verified_via": "observer"},
        # verified emit-card WITH done-signal, NEWER for w1 -> wins
        {"ws_ref": "w1", "ts": "2026-02-02T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "merged the final PR",
         "verified_via": "ci"},
        # verified emit-card WITHOUT done-signal -> excluded
        {"ws_ref": "w2", "ts": "2026-03-03T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "routine pulse card awaiting review"},
        # done-signal but not verified -> excluded
        {"ws_ref": "w3", "ts": "2026-04-04T00:00:00", "kind": "emit-card",
         "outcome": "pending", "evidence": "shipped but unverified"},
        # done-signal verified but wrong kind -> excluded
        {"ws_ref": "w4", "ts": "2026-05-05T00:00:00", "kind": "note",
         "outcome": "verified", "evidence": "shipped via wrong kind"},
        # done-signal verified emit-card with NO ws_ref -> falls back to key
        {"key": "k-fallback", "ts": "2026-05-15T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "committed via key fallback"},
        # another project, newest overall -> sorts first
        {"ws_ref": "wz", "ts": "2026-06-06T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "delivered feature z",
         "verified_via": "observer"},
    ]))
    monkeypatch.setattr(ms, "LEDGER", ledger)

    seeds = ms.work_history_seeds()
    # w1 (newest done entry), wz, and the key-fallback row qualify
    refs = [s["frontmatter"]["workspace"] for s in seeds]
    assert refs == ["wz", "k-fallback", "w1"]    # newest-first sort
    w1_seed = [s for s in seeds if s["frontmatter"]["workspace"] == "w1"][0]
    assert "merged the final PR" in w1_seed["body"]
    assert w1_seed["frontmatter"]["date"] == "2026-02-02"
    assert w1_seed["title"].startswith("Squirrel trim")   # title from cmux title
    assert w1_seed["category"] == "work_history"
    assert "verified" in w1_seed["tags"]


def test_work_history_seeds_limit(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_cmux_workspaces", lambda: {})
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"ws_ref": f"w{i}", "ts": f"2026-01-{i+1:02d}T00:00:00",
         "kind": "emit-card", "outcome": "verified",
         "evidence": f"shipped milestone {i}"}
        for i in range(5)
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(ms, "LEDGER", ledger)
    seeds = ms.work_history_seeds(limit=2)
    assert len(seeds) == 2
    # newest two dates
    assert [s["frontmatter"]["date"] for s in seeds] == ["2026-01-05", "2026-01-04"]


# ════════════════════════════════════════════════════════════════════════════
# memory_seeds — decisions
# ════════════════════════════════════════════════════════════════════════════

def _msg(content):
    return json.dumps({"message": {"role": "user", "content": content}})


def test_iter_user_texts(ms, tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        _msg("plain string content"),
        json.dumps({"message": {"role": "user", "content": [
            {"type": "text", "text": "part one"},
            {"type": "image", "source": {}},          # non-text part -> skipped
            {"type": "text", "text": "part two"},
        ]}}),
        json.dumps({"message": {"role": "assistant", "content": "ignored"}}),  # not user
        json.dumps({"message": "not a dict"}),         # msg not a dict -> skipped
        json.dumps({"no_message_key": 1}),             # no message -> skipped
        "",                                            # blank -> skipped
        "{malformed json",                             # malformed -> skipped
    ]) + "\n")
    texts = list(ms._iter_user_texts(p))
    assert texts == ["plain string content", "part one", "part two"]


def test_iter_user_texts_oserror(ms, tmp_path):
    # path that cannot be opened (a directory) -> OSError -> nothing yielded
    assert list(ms._iter_user_texts(tmp_path)) == []


def test_date_from_mtime(ms, tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("x")
    date = ms._date_from_mtime(p)
    assert len(date) == 10 and date[4] == "-" and date[7] == "-"
    int(date.replace("-", ""))   # numeric YYYYMMDD


def test_decision_seeds_missing_dir(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "TRANSCRIPT_DIR", tmp_path / "no-such-dir")
    assert ms.decision_seeds() == []


def test_decision_seeds(ms, tmp_path, monkeypatch):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    f = tdir / "session.jsonl"
    f.write_text("\n".join([
        _msg("let's go with mem0"),                          # directive one-liner
        _msg("We decided to use Lit for the timeline"),      # prose pattern
        _msg("[telegram chat=1] lets ship it"),              # TG prefix stripped
        _msg("approving the auto-merge queue config"),       # _DECISION_SKIP noise
        _msg("let's go with mem0"),                          # dup of first -> deduped
        _msg("x"),                                           # too short -> ignored
        _msg("z" * 2001),                                    # giant paste -> skipped
    ]) + "\n")
    monkeypatch.setattr(ms, "TRANSCRIPT_DIR", tdir)

    seeds = ms.decision_seeds()
    fragments = [s["content"] for s in seeds]
    bodies = " || ".join(fragments)
    # the three real decisions present, in encounter order
    assert any("let's go with mem0" in c for c in fragments)
    assert any("decided to use Lit" in c for c in fragments)
    assert any("lets ship it" in c for c in fragments)
    # TG prefix removed, not present in any captured fragment
    assert "[telegram" not in bodies
    # auto-merge noise filtered out
    assert "auto-merge" not in bodies
    # dedup: "let's go with mem0" appears exactly once
    assert sum("let's go with mem0" in c for c in fragments) == 1
    for s in seeds:
        assert s["category"] == "decision"
        assert s["tags"] == ["decision"]
        assert s["frontmatter"]["source"] == "transcript"


def test_decision_seeds_limit(ms, tmp_path, monkeypatch):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    f = tdir / "session.jsonl"
    # many distinct directive decisions; limit should cap the result
    f.write_text("\n".join(
        _msg(f"let's go with option {i}") for i in range(10)
    ) + "\n")
    monkeypatch.setattr(ms, "TRANSCRIPT_DIR", tdir)
    seeds = ms.decision_seeds(limit=3)
    assert len(seeds) == 3


def test_decision_seeds_prose_pattern_limit(ms, tmp_path, monkeypatch):
    # exercise the inner-pattern _emit return-early path
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    f = tdir / "session.jsonl"
    f.write_text("\n".join(
        # long prose lines so they don't trip the directive branch (<220 chars);
        # each carries one decided-to phrase.
        json.dumps({"message": {"role": "user", "content":
            f"After much deliberation across the whole team we decided to use "
            f"approach number {i} for the rollout because it balances the "
            f"competing constraints and keeps the blast radius small enough."}})
        for i in range(6)
    ) + "\n")
    monkeypatch.setattr(ms, "TRANSCRIPT_DIR", tdir)
    seeds = ms.decision_seeds(limit=2)
    assert len(seeds) == 2
    assert all("decided to use approach" in s["content"] for s in seeds)


# ════════════════════════════════════════════════════════════════════════════
# memory_seeds — confirmed_lessons + all_seeds
# ════════════════════════════════════════════════════════════════════════════

def test_confirmed_lessons(ms, tmp_path, monkeypatch):
    proposals = tmp_path / "proposals.jsonl"
    proposals.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "lesson", "status": "confirmed", "trigger": "When X happens",
         "rule": "Always do Y", "scope": "ffp", "target": "assistant",
         "confirmed_at": "2026-05-01", "source": "transcript"},
        {"type": "lesson", "status": "pending", "trigger": "P", "rule": "PR"},
        {"type": "decision", "status": "confirmed", "trigger": "Q", "rule": "QR"},
    ]))
    monkeypatch.setattr(ms, "PROPOSALS", proposals)

    out = ms.confirmed_lessons()
    assert len(out) == 1                          # only the confirmed lesson
    s = out[0]
    assert s["category"] == "lesson"
    assert s["title"] == "When X happens"
    assert "**Rule:** Always do Y" in s["body"]
    assert "**Trigger:** When X happens" in s["body"]
    assert "**Scope:** ffp" in s["body"]
    assert s["tags"] == ["lesson", "ffp", "assistant"]
    assert s["frontmatter"] == {"target": "assistant", "scope": "ffp",
                                "source": "transcript"}
    assert s["content"] == "Lesson — when When X happens: Always do Y"


def test_confirmed_lessons_defaults(ms, tmp_path, monkeypatch):
    # confirmed lesson missing optional fields -> defaults applied
    proposals = tmp_path / "proposals.jsonl"
    proposals.write_text(json.dumps(
        {"type": "lesson", "status": "confirmed", "rule": "bare rule"}) + "\n")
    monkeypatch.setattr(ms, "PROPOSALS", proposals)
    out = ms.confirmed_lessons()
    assert len(out) == 1
    s = out[0]
    assert s["title"] == "lesson"                 # trigger defaulted to "lesson"
    assert s["frontmatter"] == {"target": "assistant", "scope": "general",
                                "source": "manual"}
    assert s["tags"] == ["lesson", "general", "assistant"]


def test_all_seeds_offline(ms, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_cmux_workspaces",
                        lambda: {"w1": "Squirrel trim [1]"})
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(
        {"ws_ref": "w1", "ts": "2026-02-02T00:00:00", "kind": "emit-card",
         "outcome": "verified", "evidence": "shipped it"}) + "\n")
    monkeypatch.setattr(ms, "LEDGER", ledger)
    monkeypatch.setattr(ms, "OBSERVER_REPORT", tmp_path / "obs.json")
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "s.jsonl").write_text(_msg("let's go with mem0") + "\n")
    monkeypatch.setattr(ms, "TRANSCRIPT_DIR", tdir)

    out = ms.all_seeds()
    assert set(out) == {"working_style", "project", "work_history", "decision"}
    assert len(out["working_style"]) == len(ms._WORKING_STYLE)
    assert len(out["project"]) == 1
    assert len(out["work_history"]) == 1
    assert len(out["decision"]) == 1


# ════════════════════════════════════════════════════════════════════════════
# mem0_backend — top-up (branches not covered by test_mem0_tools.py)
# ════════════════════════════════════════════════════════════════════════════

def test_tokenize(mb):
    assert mb._tokenize("Hello, World 42!") == ["hello", "world", "42"]
    assert mb._tokenize("") == []


def test_memory_id_determinism(mb):
    a = mb._memory_id("same content", {"category": "x", "project": "p"})
    b = mb._memory_id("same content", {"project": "p", "category": "x"})  # key order
    assert a == b                                  # sorted-keys -> stable
    c = mb._memory_id("same content", {"category": "y"})
    assert a != c                                  # different metadata -> different id
    d = mb._memory_id("other content", {"category": "x", "project": "p"})
    assert a != d                                  # different content -> different id
    assert len(a) == 16


def test_ensure_venv_force_local_returns(mb, monkeypatch):
    # MEM0_FORCE_LOCAL=1 (autouse) -> early return, no exec
    called = {"exec": False}
    monkeypatch.setattr(mb.os, "execve",
                        lambda *a, **k: called.__setitem__("exec", True))
    mb.ensure_venv()
    assert called["exec"] is False


def test_ensure_venv_reexec_guard_returns(mb, monkeypatch):
    monkeypatch.delenv("MEM0_FORCE_LOCAL", raising=False)
    monkeypatch.setenv("MEM0_VENV_REEXEC", "1")
    called = {"exec": False}
    monkeypatch.setattr(mb.os, "execve",
                        lambda *a, **k: called.__setitem__("exec", True))
    mb.ensure_venv()
    assert called["exec"] is False


def test_silence_noise(mb):
    # clear so we can prove the default is set
    for k in ("ANONYMIZED_TELEMETRY", "POSTHOG_DISABLED", "TOKENIZERS_PARALLELISM"):
        os.environ.pop(k, None)
    mb._silence_noise()
    assert os.environ["ANONYMIZED_TELEMETRY"] == "False"
    assert os.environ["POSTHOG_DISABLED"] == "1"
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
    import logging
    assert logging.getLogger("chromadb").level == logging.ERROR


def test_try_real_mem0_force_local_guard(mb):
    # MEM0_FORCE_LOCAL=1 (autouse) -> the early guard returns None, no import attempt
    assert mb._try_real_mem0() is None


def test_local_store_user_id_filter(mb, tmp_path):
    s = mb.LocalStore(tmp_path / "memories.jsonl")
    s.add("brevity matters for mukul", {"category": "working_style"},
          user_id="mukul")
    s.add("brevity matters for someone else", {"category": "working_style"},
          user_id="other")
    hits = s.search("brevity", limit=5, user_id="mukul")
    assert len(hits) == 1
    assert "mukul" in hits[0]["memory"]


def test_local_store_load_skips_blank_and_malformed(mb, tmp_path):
    # _load must skip blank lines and undecodable JSON but keep valid dicts.
    path = tmp_path / "memories.jsonl"
    good = mb._memory_id("good content here", {"category": "decision"})
    path.write_text(
        "\n"                                       # blank -> skipped
        "{malformed json\n"                        # JSONDecodeError -> skipped
        + json.dumps({"id": good, "user_id": "mukul",
                      "content": "good content here",
                      "metadata": {"category": "decision"}}) + "\n"
    )
    s = mb.LocalStore(path)
    hits = s.search("good content", limit=5)
    assert len(hits) == 1
    assert hits[0]["memory"] == "good content here"


def test_local_store_search_empty_after_category_filter(mb, tmp_path):
    s = mb.LocalStore(tmp_path / "memories.jsonl")
    s.add("a decision note", {"category": "decision"})
    # records exist for the user but none in this category -> early empty return
    assert s.search("decision", limit=5, category="nonexistent") == []


def test_local_store_skips_empty_content(mb, tmp_path):
    s = mb.LocalStore(tmp_path / "memories.jsonl")
    # a record with empty content -> no tokens -> skipped in scoring,
    # so it never appears even though the query would "match" nothing.
    s.add("", {"category": "decision"})
    s.add("real searchable squirrel content", {"category": "decision"})
    hits = s.search("squirrel", limit=5)
    assert len(hits) == 1
    assert hits[0]["memory"] == "real searchable squirrel content"
    assert 0.0 < hits[0]["score"] <= 1.0           # normalized score


def test_memory_backend_local(mb, tmp_path, monkeypatch):
    # LocalStore's default path arg is bound at class-definition time, so
    # patching the module constant alone won't isolate the store the facade
    # builds. Wrap the class so MemoryBackend's LocalStore() lands in tmp.
    store_path = tmp_path / "memories.jsonl"
    real_cls = mb.LocalStore
    monkeypatch.setattr(mb, "LocalStore", lambda *a, **k: real_cls(store_path))
    b = mb.MemoryBackend()
    assert b.provider == "local-jsonl"
    assert b._real is None
    res = b.add("a routed decision about Lit", {"category": "decision"})
    assert res["status"] == "added"
    hits = b.search("Lit decision", limit=5)
    assert hits and "Lit" in hits[0]["memory"]
    # second add is idempotent through the facade
    assert b.add("a routed decision about Lit",
                 {"category": "decision"})["status"] == "exists"


def test_extract_real_id(mb):
    assert mb._extract_real_id({"results": [{"id": "abc"}]}) == "abc"
    assert mb._extract_real_id([{"id": "xyz"}]) == "xyz"
    assert mb._extract_real_id({"results": []}) is None
    assert mb._extract_real_id([]) is None
    assert mb._extract_real_id("not a container") is None
    assert mb._extract_real_id([{"no_id": 1}]) is None
