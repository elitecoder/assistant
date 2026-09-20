"""Exercise measurement provenance through the process and filesystem boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("run_python_coverage.py")
SPEC = importlib.util.spec_from_file_location("coverage_capture", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class CoverageCaptureTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        self.output = self.root / "results"
        for name in ("bin", "src/package", "tests"):
            (self.repo / name).mkdir(parents=True)
        self.source = self.repo / "src/package/example.py"
        self.source.write_text("answer = 42\n")
        (self.repo / "bin/tool.py").write_text("print('tool')\n")
        (self.repo / "src/data.txt").write_text("not code")
        (self.repo / "tests/test_example.py").write_text("assert True\n")
        self.report = {
            "meta": {"branch_coverage": True},
            "files": {"src/package/example.py": {
                "executed_lines": [1], "missing_lines": [],
                "executed_branches": [], "missing_branches": [],
            }},
        }
        self.calls = []
        self.exit_code = 0
        self.mutation = None
        self.payload = json.dumps(self.report)
        self.heads = ["a" * 40, "a" * 40]
        environment = patch.dict(os.environ, {
            "HOME": str(self.root),
            "COVERAGE_PROCESS_CONFIG": "inherited-coverage-configuration",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=self.heads.pop(0) + "\n")
        path = next(arg.removeprefix("--cov-report=json:") for arg in command
                    if arg.startswith("--cov-report=json:"))
        if self.payload is not None:
            Path(path).write_text(self.payload)
        if self.mutation:
            self.mutation()
        return subprocess.CompletedProcess(command, self.exit_code)

    def run_capture(self, *args):
        return capture.capture_coverage(self.repo, self.output, args, runner=self.runner)

    def test_fingerprints_include_all_production_python_and_hash_bytes(self):
        expected = {
            name: hashlib.sha256((self.repo / name).read_bytes()).hexdigest()
            for name in ("bin/tool.py", "src/package/example.py")
        }
        self.assertEqual(capture.source_fingerprints(self.repo), expected)
        self.source.write_bytes(b"answer = 42\r\n")
        self.assertNotEqual(capture.source_fingerprints(self.repo), expected)

    def test_report_hash_ignores_key_order_but_not_coverage_changes(self):
        other = {"files": self.report["files"], "meta": self.report["meta"]}
        expected = hashlib.sha256(json.dumps(
            self.report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(capture.canonical_report_hash(other), expected)
        other["meta"] = {"branch_coverage": False}
        self.assertNotEqual(capture.canonical_report_hash(other), expected)

    def test_real_report_is_bound_to_sources_and_inherited_process_configuration(self):
        before = capture.source_fingerprints(self.repo)
        self.assertEqual(self.run_capture("-q", "-k", "example"), 0)
        manifest = json.loads((self.output / "python.sources.json").read_text())
        report = json.loads((self.output / "python.json").read_text())
        self.assertEqual(report, self.report)
        self.assertEqual(manifest, {
            "schema": "coverage-sources/1", "sources": before, "head_sha": "a" * 40,
            "report_sha256": capture.canonical_report_hash(report), "pytest_exit_code": 0,
        })
        command, options = self.calls[1]
        self.assertEqual(command[:7], [sys.executable, "-m", "pytest", "tests/", "-q", "-k", "example"])
        self.assertIn(f"--cov={self.repo / 'bin'}", command)
        self.assertIn(f"--cov={self.repo / 'src'}", command)
        self.assertIn(f"--cov-config={self.repo / 'tests/coverage.ini'}", command)
        self.assertEqual(options["cwd"], self.repo)
        self.assertFalse(options["check"])
        self.assertEqual(options["env"]["COVERAGE_PROCESS_CONFIG"],
                         "inherited-coverage-configuration")
        self.assertEqual(Path(options["env"]["COVERAGE_FILE"]).parent, self.output)

    def test_modified_added_and_renamed_sources_refuse_publication(self):
        mutations = (
            lambda: self.source.write_text("answer = 99\n"),
            lambda: (self.repo / "bin/new.py").write_text("new = True\n"),
            lambda: self.source.rename(self.source.with_name("renamed.py")),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.heads = ["a" * 40, "a" * 40]
                self.mutation = mutation
                with self.assertRaisesRegex(ValueError, "sources changed"):
                    self.run_capture()
                self.assertFalse((self.output / "python.sources.json").exists())
                self.assertFalse((self.output / "python.json").exists())

    def test_head_change_refuses_publication(self):
        self.heads = ["a" * 40, "b" * 40]
        with self.assertRaisesRegex(ValueError, "HEAD changed"):
            self.run_capture()
        self.assertFalse((self.output / "python.sources.json").exists())

    def test_failed_tests_publish_only_explicitly_failed_measurements(self):
        self.exit_code = 1
        self.assertEqual(self.run_capture(), 1)
        manifest = json.loads((self.output / "python.sources.json").read_text())
        self.assertEqual(manifest["pytest_exit_code"], 1)
        self.assertEqual(manifest["report_sha256"], capture.canonical_report_hash(self.report))

    def test_missing_new_report_cannot_reuse_existing_operator_files(self):
        self.output.mkdir()
        old_report = self.output / "python.json"
        old_manifest = self.output / "python.sources.json"
        old_report.write_text(json.dumps(self.report))
        old_manifest.write_text('{"old": true}')
        operator_file = self.output / "notes.txt"
        operator_file.write_text("Keep this")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.payload = None
        self.exit_code = 4
        with self.assertRaisesRegex(ValueError, "code 4 without a new coverage report"):
            self.run_capture()
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_launch_failure_does_not_publish_or_reuse_old_report(self):
        def failure(command, **kwargs):
            if command[0] == "git":
                return self.runner(command, **kwargs)
            raise OSError("interpreter unavailable")

        with self.assertRaisesRegex(OSError, "interpreter unavailable"):
            capture.capture_coverage(self.repo, self.output, runner=failure)
        self.assertFalse((self.output / "python.sources.json").exists())

    def test_malformed_or_empty_report_is_an_explicit_error(self):
        for payload in ("{broken", "null", "[]", "{}", '{"files": {}}',
                        '{"meta": {"branch_coverage": true}, "files": {}}',
                        '{"meta": {"branch_coverage": true}, "files": {"x": null}}',
                        '{"meta": {"branch_coverage": true}, "files": {"x": {}}}',
                        json.dumps({"meta": {"branch_coverage": True}, "files": {"x": {
                            "executed_lines": [], "missing_lines": [],
                            "executed_branches": [], "missing_branches": [],
                        }}})):
            with self.subTest(payload=payload):
                self.heads = ["a" * 40, "a" * 40]
                self.payload = payload
                with self.assertRaisesRegex(ValueError, "Malformed Python coverage report"):
                    self.run_capture()
                self.assertFalse((self.output / "python.sources.json").exists())

    def test_manifest_publication_is_atomic_and_hash_detects_an_interrupted_pair(self):
        self.output.mkdir()
        old_manifest = self.output / "python.sources.json"
        old_manifest.write_text('{"report_sha256": "old-measurement"}')
        original_replace = os.replace

        def replace(source, destination):
            if Path(destination) == old_manifest:
                self.assertEqual(json.loads(Path(source).read_text())["sources"],
                                 capture.source_fingerprints(self.repo))
                raise OSError("manifest publication interrupted")
            return original_replace(source, destination)

        with patch.object(capture.os, "replace", replace):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                self.run_capture()
        self.assertEqual(json.loads(old_manifest.read_text())["report_sha256"], "old-measurement")
        self.assertNotEqual(capture.canonical_report_hash(
            json.loads((self.output / "python.json").read_text())), "old-measurement")

    def test_empty_source_tree_never_starts_pytest(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "No Python sources"):
            capture.capture_coverage(empty, self.output, runner=self.runner)
        self.assertTrue(all(command[0] == "git" for command, _ in self.calls))

    def test_command_interface_preserves_pytest_failure_and_reports_capture_errors(self):
        self.exit_code = 3
        stderr = io.StringIO()
        with patch.object(capture, "__file__", str(self.repo / "tests/run_python_coverage.py")):
            with redirect_stderr(stderr):
                status = capture.main(
                    ["--output-dir", str(self.output), "--pytest-args", "-q", "-k", "selected"],
                    runner=self.runner)
            self.assertEqual(status, 3)
            self.assertIn("analysis only", stderr.getvalue())
            command, _ = self.calls[1]
            self.assertEqual(command[4:7], ["-q", "-k", "selected"])
            self.heads = ["a" * 40, "a" * 40]
            self.payload = "not JSON"
            with redirect_stderr(stderr):
                status = capture.main(["--output-dir", str(self.output)], runner=self.runner)
            self.assertEqual(status, 2)
            self.assertIn("Coverage capture failed: Malformed", stderr.getvalue())

    def test_script_entry_point_returns_the_pytest_exit_code(self):
        with patch.object(sys, "argv", [
                str(SCRIPT), "--output-dir", str(self.output), "--pytest-args", "-q",
        ]), patch.object(subprocess, "run", self.runner):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(json.loads((self.output / "python.sources.json").read_text())[
            "pytest_exit_code"], 0)
