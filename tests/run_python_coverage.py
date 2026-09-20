"""Capture Python coverage with source fingerprints from the measured run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def source_fingerprints(repo: Path) -> dict[str, str]:
    return {
        path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ("bin", "src")
        for path in sorted((repo / directory).rglob("*.py"))
        if path.is_file()
    }


def canonical_report_hash(report: dict) -> str:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def capture_coverage(repo: Path, output_dir: Path, pytest_args=(), *,
                     runner=subprocess.run) -> int:
    repo = repo.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    report_path = output_dir / f"python.{run_id}.json"
    manifest_path = output_dir / f"python.{run_id}.sources.json"
    head_command = ["git", "-C", str(repo), "rev-parse", "HEAD"]
    head = runner(head_command, check=True, capture_output=True, text=True).stdout.strip()
    before = source_fingerprints(repo)
    if not before:
        raise ValueError("No Python sources found under bin/ or src/")
    environment = os.environ.copy()
    environment["COVERAGE_FILE"] = str(output_dir / f"python.{run_id}.data")
    command = [
        sys.executable, "-m", "pytest", "tests/", *pytest_args,
        f"--cov={repo / 'bin'}", f"--cov={repo / 'src'}",
        f"--cov-config={repo / 'tests/coverage.ini'}",
        f"--cov-report=json:{report_path}",
    ]
    result = runner(command, cwd=repo, env=environment, check=False)
    after = source_fingerprints(repo)
    if before != after:
        changed = sorted(name for name in before.keys() | after.keys()
                         if before.get(name) != after.get(name))
        raise ValueError(f"Python sources changed during coverage capture: {', '.join(changed)}")
    after_head = runner(head_command, check=True, capture_output=True, text=True).stdout.strip()
    if head != after_head:
        raise ValueError("Git HEAD changed during coverage capture")
    if not report_path.is_file():
        raise ValueError(f"Pytest exited with code {result.returncode} without a new coverage report")
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Malformed Python coverage report: {exc}") from exc
    if (not isinstance(report, dict) or not isinstance(report.get("meta"), dict)
            or report["meta"].get("branch_coverage") is not True
            or not isinstance(report.get("files"), dict) or not report["files"]
            or any(not isinstance(row, dict) for row in report["files"].values())):
        raise ValueError("Malformed Python coverage report: expected nonempty branch coverage data")
    for name, row in report["files"].items():
        if any(not isinstance(row.get(key), list) for key in (
                "executed_lines", "missing_lines", "executed_branches", "missing_branches")):
            raise ValueError(f"Malformed Python coverage report: missing line or branch data for {name}")
    if not any(row["executed_lines"] or row["missing_lines"] for row in report["files"].values()):
        raise ValueError("Malformed Python coverage report: no executable Python lines measured")
    manifest = {
        "schema": "coverage-sources/1",
        "report_sha256": canonical_report_hash(report),
        "sources": before,
        "head_sha": head,
        "pytest_exit_code": result.returncode,
    }
    with manifest_path.open("x") as stream:
        stream.write(json.dumps(manifest, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(report_path, output_dir / "python.json")
    os.replace(manifest_path, output_dir / "python.sources.json")
    if result.returncode:
        print(f"Pytest failed with exit code {result.returncode}; coverage is for analysis only.",
              file=sys.stderr)
    return result.returncode


def main(argv=None, *, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pytest-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args(argv)
    try:
        return capture_coverage(Path(__file__).resolve().parents[1],
                                args.output_dir, args.pytest_args, runner=runner)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Coverage capture failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
