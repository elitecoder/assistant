"""The change-coverage gate measures the Slack reactor's Node source from Node's own lcov report."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("check_change_coverage.py")
_spec = importlib.util.spec_from_file_location("reactor_gate", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

NAME = "slack-reactor/src/example.js"
LCOV = """TN:
SF:{name}
DA:1,3
DA:2,0
DA:3,1
BRDA:3,0,0,1
BRDA:3,0,1,-
end_of_record
SF:slack-reactor/src/other.js
DA:1,1
end_of_record
"""


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git(tmp_path, "commit", "--allow-empty", "-qm", "base")
    (tmp_path / "slack-reactor/src").mkdir(parents=True)
    return tmp_path


def test_changed_reactor_javascript_is_a_known_adapter_not_unsupported_code(repo):
    (repo / NAME).write_text("a\nb\nc\n")
    (repo / "slack-reactor/test").mkdir()
    (repo / "slack-reactor/test/x.test.js").write_text("test()\n")
    (repo / "slack-reactor/test/fixtures.mjs").write_text("export {}\n")
    assert gate.changed_lines(repo, "HEAD") == {}
    assert gate.changed_reactor_lines(repo, "HEAD") == {NAME: {1, 2, 3}}


def test_reactor_diff_lists_added_lines_of_tracked_files_only_for_js(repo):
    (repo / NAME).write_text("a\nb\n")
    (repo / "slack-reactor/src/notes.md").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    (repo / NAME).write_text("a\nB\nb\nnew\n")
    (repo / "slack-reactor/src/notes.md").write_text("y\n")
    (repo / "slack-reactor/src/gone.js").write_text("z\n")
    git(repo, "add", "slack-reactor/src/gone.js")
    git(repo, "commit", "-qm", "two")
    (repo / "slack-reactor/src/gone.js").unlink()
    assert gate.changed_reactor_lines(repo, "HEAD~1") == {NAME: {2, 4}}
    # A file deleted in the working tree contributes nothing; the edit to example.js is still uncommitted.
    assert gate.changed_reactor_lines(repo, "HEAD") == {NAME: {2, 4}}


def test_unreadable_range_is_an_error(repo, monkeypatch):
    real = subprocess.check_output
    monkeypatch.setattr(gate.subprocess, "check_output",
                        lambda cmd, **kw: "+++ b/%s\n@@ unreadable @@\n" % NAME if "diff" in cmd else real(cmd, **kw))
    with pytest.raises(ValueError, match="unreadable change range"):
        gate.changed_reactor_lines(repo, "HEAD")


def test_lines_and_branches_of_changed_code_must_all_run(repo):
    (repo / NAME).write_text("a\nb\nc\n")
    lcov = LCOV.format(name=NAME)
    result = gate.check_reactor(repo, {NAME: {1, 2, 3}}, lcov)
    assert result == {"passed": False, "files": [
        {"file": NAME, "lines": 3, "missing_lines": [2], "branches": 2, "missing_branches": [3]}]}
    assert gate.check_reactor(repo, {NAME: {1}}, lcov)["passed"] is True
    assert gate.check_reactor(repo, {}, None) == {"passed": True, "files": []}


def test_a_missing_or_stale_report_cannot_pass(repo):
    (repo / NAME).write_text("a\n")
    with pytest.raises(ValueError, match="needs a Node coverage report"):
        gate.check_reactor(repo, {NAME: {1}}, None)
    with pytest.raises(ValueError, match="missing for changed code"):
        gate.check_reactor(repo, {"slack-reactor/src/absent.js": {1}}, LCOV.format(name=NAME))
    older = (repo / NAME).stat().st_mtime - 10
    with pytest.raises(ValueError, match="changed after coverage measurement"):
        gate.check_reactor(repo, {NAME: {1}}, LCOV.format(name=NAME), report_mtime=older)
    assert gate.check_reactor(repo, {NAME: {1}}, LCOV.format(name=NAME), report_mtime=older + 100)["passed"]


def test_cli_checks_reactor_coverage(repo, monkeypatch, capsys):
    import json
    from run_python_coverage import canonical_report_hash
    (repo / NAME).write_text("a\nb\nc\n")
    py = {"meta": {"branch_coverage": True}, "files": {}}
    (repo / "python.json").write_text(json.dumps(py))
    (repo / "python.sources.json").write_text(json.dumps({
        "schema": "coverage-sources/1", "pytest_exit_code": 0, "sources": {},
        "report_sha256": canonical_report_hash(py)}))
    (repo / "reactor.lcov").write_text(LCOV.format(name=NAME))
    monkeypatch.setattr(gate, "__file__", str(repo / "tests/check_change_coverage.py"))
    monkeypatch.setattr("sys.argv", ["check", "--base", "HEAD", "--python-report", str(repo / "python.json"),
                                     "--reactor-lcov", str(repo / "reactor.lcov")])
    with pytest.raises(SystemExit) as result:
        gate.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["reactor"]["files"][0]["missing_lines"] == [2]
