"""Exercise the coverage gate with changed files and coverage-result inputs."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


SCRIPT = Path(__file__).with_name("check_change_coverage.py")
SPEC = importlib.util.spec_from_file_location("change_coverage_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_browser_dependencies_use_public_download_urls():
    lock = json.loads(SCRIPT.with_name("package-lock.json").read_text())
    dependencies = [package for name, package in lock["packages"].items() if name]
    assert dependencies
    for package in dependencies:
        url = urlsplit(package["resolved"])
        assert url.scheme == "https"
        assert url.netloc == "registry.npmjs.org"


def python_report(*, executed=(1, 2), missing=(), branches=((1, 2), (1, -1)), missed=(), excluded=()):
    return {"meta": {"branch_coverage": True}, "files": {"src/example.py": {
        "executed_lines": list(executed), "missing_lines": list(missing),
        "executed_branches": list(branches), "missing_branches": list(missed),
        "excluded_lines": list(excluded),
    }}}


def manifest(repo, report):
    return {
        "schema": "coverage-sources/1", "pytest_exit_code": 0,
        "report_sha256": gate.canonical_report_hash(report),
        "sources": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for directory in ("bin", "src") for path in (repo / directory).rglob("*.py")},
    }


def check(repo, changes, python, browser=None, proof=None):
    return gate.check_coverage(repo, changes, python, browser,
                               manifest(repo, python) if proof is None else proof)


@pytest.fixture
def source_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("if enabled:\n    do_work()\n")
    return tmp_path


def test_complete_changed_code_passes(source_repo):
    result = check(source_repo, {"src/example.py": {1, 2}}, python_report())
    assert result["passed"] is True
    assert result["python"][0]["lines"] == 2
    assert result["python"][0]["branches"] == 2


@pytest.mark.parametrize("report", [
    python_report(executed=(1,), missing=(2,)),
    python_report(branches=((1, -1),), missed=((1, 2),)),
    python_report(excluded=(2,)),
])
def test_missing_lines_branches_or_excluded_new_code_fail(source_repo, report):
    result = check(source_repo, {"src/example.py": {1, 2}}, report)
    assert result["passed"] is False


def test_old_uncovered_code_does_not_change_the_new_code_requirement(source_repo):
    report = python_report(executed=(2,), missing=(1,), branches=(), missed=((1, 2),))
    result = check(source_repo, {"src/example.py": {2}}, report)
    assert result["passed"] is True


def test_missing_or_line_only_reports_are_explicit_errors(source_repo):
    with pytest.raises(ValueError, match="include branches"):
        check(source_repo, {"src/example.py": {1}}, {"meta": {}})
    with pytest.raises(ValueError, match="missing for changed code"):
        check(source_repo, {"src/example.py": {1}},
              {"meta": {"branch_coverage": True}, "files": {}})


@pytest.mark.parametrize("unsupported", ["hooks/new.py", "src/new.js"])
def test_git_diff_and_untracked_production_files_are_both_measured(tmp_path, unsupported):
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
    (tmp_path / unsupported).parent.mkdir(exist_ok=True)
    (tmp_path / unsupported).write_text("run_hook()\n")
    with pytest.raises(ValueError, match="not configured"):
        gate.changed_lines(tmp_path, "HEAD")


def browser_fixture(repo):
    (repo / "bin").mkdir()
    path = repo / "bin/render-assistant-page.py"
    path.write_text('js = r"""\nfunction run() { return 1; }\n"""\n')
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
        check(tmp_path, changes, python)
    assert check(tmp_path, changes, python, browser)["passed"] is True
    browser["source"] = "an older dashboard version"
    with pytest.raises(ValueError, match="does not match"):
        check(tmp_path, changes, python, browser)


@pytest.mark.parametrize("missing", ["statement", "block"])
def test_unexecuted_changed_browser_code_fails(tmp_path, missing):
    python, browser = browser_fixture(tmp_path)
    entry = browser["coverage"]["inline.js"]
    if missing == "statement":
        entry["s"]["0"] = 0
    else:
        entry["b"]["0"] = [0]
    result = check(tmp_path, {"bin/render-assistant-page.py": {2}}, python, browser)
    assert result["passed"] is False


@pytest.mark.parametrize(("complete", "publish"), [(True, True), (False, True), (False, False)])
def test_cli_publishes_the_actual_coverage_result(source_repo, monkeypatch, capsys, complete, publish):
    subprocess.run(["git", "init", "-q", str(source_repo)], check=True)
    subprocess.run(["git", "-C", str(source_repo), "-c", "user.name=Coverage fixture",
                    "-c", "user.email=coverage@example.invalid", "commit", "--allow-empty",
                    "-qm", "Initial fixture"], check=True)
    path = source_repo / "coverage.json"
    report = python_report() if complete else python_report(executed=(1,), missing=(2,))
    path.write_text(json.dumps(report))
    path.with_name("coverage.sources.json").write_text(json.dumps(manifest(source_repo, report)))
    monkeypatch.setattr(gate, "__file__", str(source_repo / "tests/check_change_coverage.py"))
    monkeypatch.setattr(gate, "__name__", "coverage_cli_fixture")
    output = source_repo / "result.json"
    arguments = ["check", "--base", "HEAD", "--python-report", str(path)]
    if publish:
        arguments.extend(["--output", str(output)])
    monkeypatch.setattr("sys.argv", arguments)
    with pytest.raises(SystemExit) as result:
        gate.main()
    assert result.value.code == (0 if complete else 1)
    result_data = json.loads(capsys.readouterr().out)
    assert result_data["passed"] is complete
    if publish:
        assert json.loads(output.read_text()) == result_data
    else:
        assert not output.exists()


def test_multiline_condition_edits_require_the_logical_statement_and_branches(source_repo):
    (source_repo / "src/example.py").write_text(
        "def choose(flag):\n    if (flag\n            and dangerous_check()):\n        return 1\n    return 0\n")
    report = python_report(executed=(1,), missing=(2, 4, 5),
                           branches=(), missed=((2, 4), (2, 5)))
    result = check(source_repo, {"src/example.py": {3}}, report)
    assert result["passed"] is False
    assert result["python"][0]["missing_lines"] == [2]
    assert result["python"][0]["missing_branches"] == [[2, 4], [2, 5]]


def test_empty_measurement_cannot_remove_executable_statements(source_repo):
    report = python_report(executed=(), missing=(), branches=())
    with pytest.raises(ValueError, match="omitted current executable"):
        check(source_repo, {"src/example.py": {1, 2}}, report)


def test_source_changes_after_measurement_reject_the_report(source_repo):
    report = python_report()
    proof = manifest(source_repo, report)
    (source_repo / "src/example.py").write_text("different_work()\n")
    with pytest.raises(ValueError, match="Source changed after"):
        check(source_repo, {"src/example.py": {1}}, report, proof=proof)


def test_report_changes_after_capture_reject_the_report(source_repo):
    report = python_report()
    proof = manifest(source_repo, report)
    report["files"]["src/example.py"]["missing_lines"] = [2]
    with pytest.raises(ValueError, match="differs from its captured"):
        check(source_repo, {"src/example.py": {1, 2}}, report, proof=proof)


def test_missing_provenance_and_failed_measurement_cannot_pass(source_repo):
    with pytest.raises(ValueError, match="source manifest"):
        gate.check_coverage(source_repo, {"src/example.py": {1}}, python_report(), None)
    report = python_report()
    proof = {**manifest(source_repo, report), "pytest_exit_code": 1}
    with pytest.raises(ValueError, match="test run did not pass"):
        check(source_repo, {"src/example.py": {1}}, report, proof=proof)


def test_git_context_deletions_and_nonexecutable_files_do_not_add_changed_code(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "tests").mkdir()
    source = tmp_path / "src/example.py"
    source.write_text("a = 1\nb = 2\nc = 3\n")
    deleted = tmp_path / "src/deleted.py"
    deleted.write_text("old = True\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Coverage fixture",
                    "-c", "user.email=coverage@example.invalid", "commit", "-qm", "Initial fixture"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "diff.interHunkContext", "2"], check=True)
    source.write_text("a = 4\nb = 2\nc = 5\n")
    deleted.unlink()
    (tmp_path / "bin/notes.txt").write_text("Not executable code.\n")
    (tmp_path / "tests/example.py").write_text("test_only = True\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    assert gate.changed_lines(tmp_path, "HEAD") == {"src/example.py": {1, 3}}
    (tmp_path / 'src/quoted"name.py').write_text("must_not_be_omitted = True\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    with pytest.raises(ValueError, match="quoted change path"):
        gate.changed_lines(tmp_path, "HEAD")


def test_unreadable_git_change_range_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "check_output", lambda *args, **kwargs: "")
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout="+++ b/src/example.py\n@@ unreadable @@\n"))
    with pytest.raises(ValueError, match="unreadable change range"):
        gate.changed_lines(tmp_path, "HEAD")


def test_ambiguous_dashboard_literals_require_a_new_mapping(tmp_path):
    browser_fixture(tmp_path)
    (tmp_path / "bin/render-assistant-page.py").write_text('js = "first"\njs = "second"\n')
    with pytest.raises(ValueError, match="Expected one literal"):
        gate.browser_source(tmp_path)


def test_python_file_inventory_and_changed_arcs_cannot_be_omitted(source_repo):
    with pytest.raises(ValueError, match="no file data"):
        check(source_repo, {"src/example.py": {1}},
              {"meta": {"branch_coverage": True}, "files": []})
    with pytest.raises(ValueError, match="omitted current changed branch edges"):
        check(source_repo, {"src/example.py": {1}}, python_report(branches=((1, -1),)))


@pytest.mark.parametrize("records", [None, {}, {"first": {}, "second": {}}])
def test_browser_coverage_requires_one_script_record(tmp_path, records):
    python, browser = browser_fixture(tmp_path)
    browser["coverage"] = records
    with pytest.raises(ValueError, match="exactly one"):
        check(tmp_path, {"bin/render-assistant-page.py": {2}}, python, browser)


def test_renderer_changes_outside_javascript_do_not_require_browser_coverage(tmp_path):
    python, _ = browser_fixture(tmp_path)
    with (tmp_path / "bin/render-assistant-page.py").open("a") as stream:
        stream.write('title = "Dashboard"\n')
    python["files"]["bin/render-assistant-page.py"]["executed_lines"].append(4)
    assert check(tmp_path, {"bin/render-assistant-page.py": {4}}, python)["passed"] is True


def test_unchanged_browser_lines_are_not_counted_and_missing_changed_lines_fail(tmp_path):
    python, browser = browser_fixture(tmp_path)
    record = browser["coverage"]["inline.js"]
    record["statementMap"]["1"] = {"start": {"line": 1}, "end": {"line": 1}}
    record["s"]["1"] = 0
    record["branchMap"]["1"] = {"loc": {"start": {"line": 1}}}
    record["b"]["1"] = [0]
    assert check(tmp_path, {"bin/render-assistant-page.py": {2}}, python, browser)["passed"] is True
    record["statementMap"].pop("0")
    with pytest.raises(ValueError, match="did not measure any"):
        check(tmp_path, {"bin/render-assistant-page.py": {2}}, python, browser)


def test_script_entrypoint_fails_without_measurement_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["check", "--base", "HEAD",
                                    "--python-report", str(tmp_path / "missing.json")])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert result.value.code == 2
    assert "Coverage check failed:" in capsys.readouterr().err


def test_decoded_python_newlines_cannot_shift_browser_coverage(tmp_path):
    python, browser = browser_fixture(tmp_path)
    (tmp_path / "bin/render-assistant-page.py").write_text(
        'js = """\nconst text = `one\\ntwo`;\nfunction run() { return 1; }\n"""\n')
    with pytest.raises(ValueError, match="raw literal with exact"):
        check(tmp_path, {"bin/render-assistant-page.py": {3}}, python, browser)


def test_raw_literal_preserves_escaped_newlines_and_template_line_mapping(tmp_path):
    python, browser = browser_fixture(tmp_path)
    path = tmp_path / "bin/render-assistant-page.py"
    path.write_text('js = r"""\nconst text = `one\\ntwo\nthree`;\nfunction run() { return 1; }\n"""\n')
    source = "\nconst text = `one\\ntwo\nthree`;\nfunction run() { return 1; }\n"
    browser["source"] = source
    browser["coverage"]["inline.js"]["statementMap"]["0"]["start"]["line"] = 4
    browser["coverage"]["inline.js"]["branchMap"]["0"]["loc"]["start"]["line"] = 4
    browser["coverage"]["inline.js"]["s"]["0"] = 0
    result = check(tmp_path, {"bin/render-assistant-page.py": {4}}, python, browser)
    assert result["browser"]["missing_lines"] == [4]
    assert result["passed"] is False
