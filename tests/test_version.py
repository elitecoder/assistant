"""Version-sync gate — pyproject.toml and src/assistant/__init__.py must agree.

The version lives in two places; a bump that touches only one drifts them out of
sync. This test is the mechanical gate that catches that (there is no release
tooling to enforce it otherwise). It also checks the CHANGELOG has an entry for
the current version so the number stays meaningful.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def _package_version() -> str:
    text = (REPO / "src" / "assistant" / "__init__.py").read_text()
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "no __version__ in src/assistant/__init__.py"
    return m.group(1)


def test_version_surfaces_agree():
    assert _pyproject_version() == _package_version(), (
        f"version drift: pyproject={_pyproject_version()} "
        f"__init__={_package_version()} — bump both together"
    )


def test_changelog_has_entry_for_current_version():
    version = _package_version()
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert f"[{version}]" in changelog, (
        f"CHANGELOG.md has no section for the current version {version}"
    )
