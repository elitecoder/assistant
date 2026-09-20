"""Exercise the coverage gate with changed files and coverage-result inputs."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("check_change_coverage.py")
SPEC = importlib.util.spec_from_file_location("change_coverage_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def python_report(*, executed=(1, 2), missing=(), branches=((1, 2),), missed=(), excluded=()):
    return {"meta": {"branch_coverage": True}, "files": {"src/example.py": {
        "executed_lines": list(executed), "missing_lines": list(missing),
        "executed_branches": list(branches), "missing_branches": list(missed),
        "excluded_lines": list(excluded),
    }}}


@pytest.fixture
def source_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("if enabled:\n    do_work()\n")
    return tmp_path


def test_complete_changed_code_passes(source_repo):
    result = gate.check_coverage(source_repo, {"src/example.py": {1, 2}}, python_report(), None)
    assert result["passed"] is True
    assert result["python"][0]["lines"] == 2
    assert result["python"][0]["branches"] == 1


@pytest.mark.parametrize("report", [
    python_report(executed=(1,), missing=(2,)),
    python_report(branches=(), missed=((1, 2),)),
    python_report(excluded=(2,)),
])
def test_missing_lines_branches_or_excluded_new_code_fail(source_repo, report):
    result = gate.check_coverage(source_repo, {"src/example.py": {1, 2}}, report, None)
    assert result["passed"] is False


def test_old_uncovered_code_does_not_change_the_new_code_requirement(source_repo):
    report = python_report(executed=(2,), missing=(1,), branches=(), missed=((1, 2),))
    result = gate.check_coverage(source_repo, {"src/example.py": {2}}, report, None)
    assert result["passed"] is True


def test_missing_or_line_only_reports_are_explicit_errors(source_repo):
    with pytest.raises(ValueError, match="include branches"):
        gate.check_coverage(source_repo, {"src/example.py": {1}}, {"meta": {}}, None)
    with pytest.raises(ValueError, match="missing for changed code"):
        gate.check_coverage(source_repo, {"src/example.py": {1}},
                            {"meta": {"branch_coverage": True}, "files": {}}, None)


def test_git_diff_and_untracked_production_files_are_both_measured(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    file = tmp_path / "src/example.py"
    file.write_text("one = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/example.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Coverage fixture",
                    "-c", "user.email=coverage@example.invalid", "commit", "-qm", "Initial fixture"],
                   check=True)
    file.write_text("one = 1\ntwo = 2\n")
    (tmp_path / "src/new.py").write_text("new = True\n")
    assert gate.changed_lines(tmp_path, "HEAD") == {
        "src/example.py": {2}, "src/new.py": {1}}


def browser_fixture(repo):
    (repo / "bin").mkdir()
    path = repo / "bin/render-assistant-page.py"
    path.write_text('js = """\nfunction run() { return 1; }\n"""\n')
    source = "\nfunction run() { return 1; }\n"
    report = {"source": source, "coverage": {"inline.js": {
        "statementMap": {"0": {"start": {"line": 2}, "end": {"line": 2}}},
        "s": {"0": 1},
        "branchMap": {"0": {"loc": {"start": {"line": 2}}}},
        "b": {"0": [1]},
    }}}
    python = {"meta": {"branch_coverage": True}, "files": {
        "bin/render-assistant-page.py": {
            "executed_lines": [1], "missing_lines": [],
            "executed_branches": [], "missing_branches": [], "excluded_lines": [],
        }}}
    return python, report


def test_browser_code_requires_its_own_matching_coverage(tmp_path):
    python, browser = browser_fixture(tmp_path)
    changes = {"bin/render-assistant-page.py": {1, 2, 3}}
    with pytest.raises(ValueError, match="needs browser coverage"):
        gate.check_coverage(tmp_path, changes, python, None)
    assert gate.check_coverage(tmp_path, changes, python, browser)["passed"] is True
    browser["source"] = "an older dashboard version"
    with pytest.raises(ValueError, match="does not match"):
        gate.check_coverage(tmp_path, changes, python, browser)


@pytest.mark.parametrize("missing", ["statement", "block"])
def test_unexecuted_changed_browser_code_fails(tmp_path, missing):
    python, browser = browser_fixture(tmp_path)
    entry = browser["coverage"]["inline.js"]
    if missing == "statement":
        entry["s"]["0"] = 0
    else:
        entry["b"]["0"] = [0]
    result = gate.check_coverage(
        tmp_path, {"bin/render-assistant-page.py": {2}}, python, browser)
    assert result["passed"] is False


def test_cli_returns_failure_for_incomplete_coverage(source_repo, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", str(source_repo)], check=True)
    subprocess.run(["git", "-C", str(source_repo), "-c", "user.name=Coverage fixture",
                    "-c", "user.email=coverage@example.invalid", "commit", "--allow-empty",
                    "-qm", "Initial fixture"], check=True)
    path = source_repo / "coverage.json"
    path.write_text(json.dumps(python_report(executed=(1,), missing=(2,))))
    monkeypatch.setattr(gate, "__file__", str(source_repo / "tests/check_change_coverage.py"))
    monkeypatch.setattr(gate, "__name__", "coverage_cli_fixture")
    monkeypatch.setattr("sys.argv", ["check", "--base", "HEAD", "--python-report", str(path)])
    with pytest.raises(SystemExit) as result:
        gate.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False
