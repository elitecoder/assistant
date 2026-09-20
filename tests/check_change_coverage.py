"""Require complete coverage of changed Python and embedded dashboard JavaScript."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

from coverage import Coverage
from coverage.parser import PythonParser

from run_python_coverage import canonical_report_hash


def changed_lines(repo: Path, base: str) -> dict[str, set[int]]:
    base_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        text=True).strip()
    changed_names = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=ACMR", "-z", base_commit],
        text=True).split("\0")
    changed_names += subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        text=True).split("\0")
    for name in set(changed_names) - {""}:
        file = Path(name)
        if file.parts[0] in {"tests", "evals", "docs", ".github"}:
            continue
        if file.suffix in {".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".sh"}:
            if file.suffix != ".py" or file.parts[0] not in {"bin", "src"}:
                raise ValueError(f"Coverage is not configured for changed executable code: {name}")
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--unified=0", base_commit,
         "--", "bin", "src"], check=True, capture_output=True, text=True)
    files: dict[str, set[int]] = {}
    name = None
    line = 0
    for text in result.stdout.splitlines():
        if text.startswith("+++ b/"):
            name = text[6:]
            if name.endswith(".py"):
                files.setdefault(name, set())
            else:
                name = None
        elif text.startswith("+++ /dev/null"):
            name = None
        elif text.startswith("+++ "):
            raise ValueError("Cannot map a quoted change path; use an unambiguous source filename")
        elif text.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", text)
            if match is None:
                raise ValueError("Git returned an unreadable change range")
            line = int(match.group(1))
        elif text.startswith("+") and not text.startswith("+++"):
            if name:
                files[name].add(line)
            line += 1
        elif text.startswith(" "):
            line += 1
    untracked = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z",
         "--", "bin", "src"], text=True)
    for name in untracked.split("\0"):
        if name.endswith(".py"):
            files[name] = set(range(1, len((repo / name).read_text().splitlines()) + 1))
    return {name: lines for name, lines in files.items() if lines}


def browser_source(repo: Path) -> ast.Constant:
    source = (repo / "bin/render-assistant-page.py").read_text()
    tree = ast.parse(source)
    matches = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "js" for target in node.targets)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    ]
    if len(matches) != 1:
        raise ValueError("Expected one literal dashboard script; update the coverage mapping")
    node = matches[0]
    literal = ast.get_source_segment(source, node)
    if (literal is None or not literal.lower().startswith(('r"""', "r'''"))
            or literal[4:-3] != node.value):
        raise ValueError("Dashboard JavaScript must use a raw literal with exact source-line mapping")
    return node


def check_coverage(repo: Path, changes: dict[str, set[int]],
                   python_report: dict, javascript_report: dict | None,
                   source_manifest: dict | None = None) -> dict:
    if not isinstance(source_manifest, dict) or source_manifest.get("schema") != "coverage-sources/1":
        raise ValueError("Python coverage needs its measurement-time source manifest")
    if source_manifest.get("pytest_exit_code") != 0:
        raise ValueError("The coverage measurement's test run did not pass")
    if source_manifest.get("report_sha256") != canonical_report_hash(python_report):
        raise ValueError("Python report differs from its captured measurement")
    if python_report.get("meta", {}).get("branch_coverage") is not True:
        raise ValueError("Python coverage must include branches")
    python_files = python_report.get("files")
    if not isinstance(python_files, dict):
        raise ValueError("Python coverage report has no file data")
    results = []
    for name, changed in sorted(changes.items()):
        report = python_files.get(name) or python_files.get(str(repo / name))
        if not isinstance(report, dict):
            raise ValueError(f"Coverage is missing for changed code: {name}")
        source_hash = hashlib.sha256((repo / name).read_bytes()).hexdigest()
        if source_manifest.get("sources", {}).get(name) != source_hash:
            raise ValueError(f"Source changed after coverage measurement: {name}")
        defaults = Coverage(config_file=False)
        parser = PythonParser(filename=str(repo / name),
                              exclude="|".join(defaults.get_exclude_list()))
        parser.parse_source()
        logical_changes = parser.translate_lines(changed)
        executable = parser.statements
        measured = set(report["executed_lines"]) | set(report["missing_lines"])
        if not executable <= measured:
            raise ValueError(f"Coverage omitted current executable statements: {name}")
        lines = logical_changes & executable
        branch_origins = {line for line, exits in parser.exit_counts().items() if exits > 1}
        no_branch = parser.lines_matching("|".join(defaults.get_exclude_list("partial")))
        expected_branches = {edge for edge in parser.arcs()
                             if edge[0] in logical_changes & branch_origins - no_branch}
        measured_branches = {tuple(edge) for key in ("executed_branches", "missing_branches")
                             for edge in report[key]}
        if not expected_branches <= measured_branches:
            raise ValueError(f"Coverage omitted current changed branch edges: {name}")
        branches = sorted(expected_branches)
        missing_edges = {tuple(edge) for edge in report["missing_branches"]}
        missing_branches = [list(edge) for edge in branches if edge in missing_edges]
        results.append({
            "file": name,
            "source_sha256": source_hash,
            "lines": len(lines),
            "missing_lines": sorted(lines & set(report["missing_lines"])),
            "branches": len(branches),
            "missing_branches": missing_branches,
            "excluded_changed_lines": sorted(logical_changes & (
                parser.excluded | set(report.get("excluded_lines", [])) | no_branch)),
        })
    browser = {"lines": 0, "missing_lines": [], "blocks": 0, "missing_blocks": []}
    renderer_changes = changes.get("bin/render-assistant-page.py", set())
    if renderer_changes:
        node = browser_source(repo)
        changed_script = {line for line in renderer_changes if node.lineno <= line <= node.end_lineno}
        if changed_script:
            if javascript_report is None:
                raise ValueError("Changed dashboard JavaScript needs browser coverage, not Python string coverage")
            if javascript_report.get("source") != node.value:
                raise ValueError("Browser coverage does not match the current dashboard script")
            reports = javascript_report.get("coverage", {})
            if not isinstance(reports, dict) or len(reports) != 1:
                raise ValueError("Expected exactly one dashboard JavaScript coverage record")
            report = next(iter(reports.values()))
            for key, statement in report["statementMap"].items():
                line = node.lineno + statement["start"]["line"] - 1
                if line in changed_script:
                    browser["lines"] += 1
                    if report["s"][key] <= 0:
                        browser["missing_lines"].append(line)
            for key, branch in report["branchMap"].items():
                line = node.lineno + branch["loc"]["start"]["line"] - 1
                if line in changed_script:
                    for count in report["b"][key]:
                        browser["blocks"] += 1
                        if count <= 0:
                            browser["missing_blocks"].append(line)
            if browser["lines"] == 0:
                raise ValueError("Browser report did not measure any changed JavaScript lines")
    passed = all(not (row["missing_lines"] or row["missing_branches"] or row["excluded_changed_lines"])
                 for row in results)
    passed = passed and not (browser["missing_lines"] or browser["missing_blocks"])
    return {"passed": passed, "required_percent": 100, "python": results, "browser": browser}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main")
    parser.add_argument("--python-report", type=Path, required=True)
    parser.add_argument("--browser-report", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    manifest_path = args.source_manifest or args.python_report.with_name(
        args.python_report.stem + ".sources.json")
    try:
        result = check_coverage(
            repo, changed_lines(repo, args.base),
            json.loads(args.python_report.read_text()),
            json.loads(args.browser_report.read_text()) if args.browser_report else None,
            json.loads(manifest_path.read_text()))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Coverage check failed: {exc}\n")
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
