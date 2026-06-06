"""Integration tests for the Obsidian memory-layer tools.

Drives the REAL bin/tools/obsidian-write.py and obsidian-search.py against a
sandbox vault in tmp_path — notes are written to disk and read back, search
greps them for real. No mocking. The four spec cases (write creates file,
no-overwrite suffix, search finds, search misses) plus frontmatter routing and
the --category subfolder mapping.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "bin/tools/obsidian-write.py"
SEARCH = REPO / "bin/tools/obsidian-search.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ow():
    return _load("obsidian_write_mod", WRITE)


@pytest.fixture
def os_():
    return _load("obsidian_search_mod", SEARCH)


# ── write ────────────────────────────────────────────────────────────────────

def test_obsidian_write_creates_file(ow, tmp_path):
    vault = tmp_path / "vault"
    res = ow.write_note(
        vault=vault, title="Connections P8 parity — shipped 2026-06-06",
        body="PR #11034, CI green, reviewer approved.",
        category="work_history", tags=["shipped", "pr-merged"],
        frontmatter={"project": "firefly-platform", "pr": 11034},
        date="2026-06-06")
    assert res["status"] == "written"
    p = Path(res["path"])
    assert p.exists()
    # Routed into the work_history folder, dated, slugified filename.
    assert p.parent == vault / "Work Log"
    assert p.name == "2026-06-06-connections-p8-parity-shipped-2026-06-06.md"

    text = p.read_text()
    assert text.startswith("---\n")
    assert "category: work_history" in text
    assert "project: firefly-platform" in text
    assert "pr: 11034" in text
    assert "- shipped" in text and "- pr-merged" in text
    # Body H1 present, no leftover tmp file.
    assert "# Connections P8 parity — shipped 2026-06-06" in text
    assert list(p.parent.glob("*.tmp")) == []


def test_obsidian_write_no_overwrite(ow, tmp_path):
    vault = tmp_path / "vault"
    a = ow.write_note(vault=vault, title="Same Title", body="first",
                      category="lesson", date="2026-06-06")
    b = ow.write_note(vault=vault, title="Same Title", body="second",
                      category="lesson", date="2026-06-06")
    pa, pb = Path(a["path"]), Path(b["path"])
    assert pa != pb
    assert pa.name == "2026-06-06-same-title.md"
    assert pb.name == "2026-06-06-same-title-2.md"
    # Neither was clobbered — both bodies survive.
    assert "first" in pa.read_text()
    assert "second" in pb.read_text()


def test_obsidian_write_category_folders(ow, tmp_path):
    vault = tmp_path / "vault"
    cases = {
        "lesson": "Assistant/Lessons",
        "working_style": "Assistant/Working Style",
        "decision": "Assistant/Decisions",
        "project": "Projects",
        "work_history": "Work Log",
    }
    for category, folder in cases.items():
        res = ow.write_note(vault=vault, title=f"{category} note",
                            body="b", category=category, date="2026-06-06")
        assert Path(res["path"]).parent == vault / folder


def test_obsidian_write_folder_overrides_category(ow, tmp_path):
    vault = tmp_path / "vault"
    res = ow.write_note(vault=vault, title="Monthly log", body="b",
                        category="work_history", folder="Work Log/2026-06",
                        date="2026-06-06")
    assert Path(res["path"]).parent == vault / "Work Log/2026-06"
    # category still recorded in frontmatter for search filtering.
    assert "category: work_history" in Path(res["path"]).read_text()


# ── search ─────────────────────────────────────────────────────────────────--

def test_obsidian_search_finds_match(ow, os_, tmp_path):
    vault = tmp_path / "vault"
    ow.write_note(vault=vault, title="Connections P6 move parity",
                  body="Move parity matched Horizon for cross-track drags.",
                  category="work_history", date="2026-06-06")
    ow.write_note(vault=vault, title="Unrelated note",
                  body="Nothing about that topic here.",
                  category="lesson", date="2026-06-06")
    res = os_.search(vault, query="cross-track", field=None, value=None, limit=5)
    assert len(res["results"]) == 1
    hit = res["results"][0]
    assert hit["title"] == "Connections P6 move parity"
    assert "cross-track" in hit["snippet"].lower()


def test_obsidian_search_no_results(ow, os_, tmp_path):
    vault = tmp_path / "vault"
    ow.write_note(vault=vault, title="Some note", body="content here",
                  category="lesson", date="2026-06-06")
    res = os_.search(vault, query="quantumchromodynamics", field=None,
                     value=None, limit=5)
    assert res["results"] == []


def test_obsidian_search_frontmatter_filter(ow, os_, tmp_path):
    vault = tmp_path / "vault"
    ow.write_note(vault=vault, title="A lesson", body="x",
                  category="lesson", date="2026-06-06")
    ow.write_note(vault=vault, title="A project", body="x",
                  category="project", date="2026-06-06")
    res = os_.search(vault, query=None, field="category", value="project",
                     limit=5)
    assert len(res["results"]) == 1
    assert res["results"][0]["title"] == "A project"


def test_obsidian_search_missing_vault_is_empty(os_, tmp_path):
    res = os_.search(tmp_path / "nope", query="anything", field=None,
                     value=None, limit=5)
    assert res["results"] == []


# ── slug / yaml helpers (pure) ───────────────────────────────────────────────

def test_slugify(ow):
    assert ow.slugify("Connections P8 — shipped!") == "connections-p8-shipped"
    assert ow.slugify("") == "note"
    assert ow.slugify("___$$$___") == "note"


def test_yaml_quotes_risky_scalars(ow):
    # A bare date-like or colon-bearing value must be quoted to stay valid YAML.
    fm = ow.build_frontmatter("T", "2026-06-06", "lesson", [], {"k": "a: b"})
    assert 'k: "a: b"' in fm
    assert 'created: "2026-06-06"' in fm
