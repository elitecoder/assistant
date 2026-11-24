"""Require complete coverage of changed Python and embedded browser JavaScript (the
dashboard's and the Work queue page's), and a targeted mutation for every changed
hunk of a mutation-proven shell file."""

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

from mutation_smoke import CHECKS
from run_python_coverage import PYTHON_SOURCE_DIRS, canonical_report_hash

# Shell files with no line-coverage tool; each changed code hunk needs a
# mutation_smoke check that breaks only changed code lines in it. A check can
# still be written to dodge this, so review new checks like any test.
MUTATION_PROVEN = {"install.sh", "install-bootstrap.sh", "bin/assistant-comms-setup.sh"}

# Browser JavaScript kept as one raw string literal in a Python file:
# (file, the variable it's assigned to, its name in errors). Changed lines in
# the literal need coverage from a real browser, not Python string coverage.
DASHBOARD = ("bin/render-assistant-page.py", "js", "dashboard")
WORK_QUEUE_PAGE = ("work_queue/page.py", "JS", "Work queue page")

# The Slack reactor's Node source. Node's own test runner measures it:
#   node --test --experimental-test-coverage --test-reporter=lcov \
#     --test-reporter-destination=<dir>/reactor.lcov slack-reactor/test/*.test.js
# and --reactor-lcov hands that report to this gate.
REACTOR_SOURCE = "slack-reactor/src/"
REACTOR_TESTS = "slack-reactor/test/"  # test code, like tests/, needs no coverage


def is_reactor_source(name: str) -> bool:
    return name.startswith(REACTOR_SOURCE) and name.endswith(".js")


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
        if file.parts[0] in {"tests", "evals", "docs", ".github"} or name.startswith(REACTOR_TESTS):
            continue
        if name in MUTATION_PROVEN or is_reactor_source(name):
            continue
        if file.suffix in {".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".sh"}:
            if file.suffix != ".py" or file.parts[0] not in PYTHON_SOURCE_DIRS:
                raise ValueError(f"Coverage is not configured for changed executable code: {name}")
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--unified=0", base_commit,
         "--", *PYTHON_SOURCE_DIRS], check=True, capture_output=True, text=True)
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
         "--", *PYTHON_SOURCE_DIRS], text=True)
    for name in untracked.split("\0"):
        if name.endswith(".py"):
            files[name] = set(range(1, len((repo / name).read_text().splitlines()) + 1))
    return {name: lines for name, lines in files.items() if lines}


def changed_reactor_lines(repo: Path, base: str) -> dict[str, set[int]]:
    """Changed (or new) lines of each reactor source file, by repo-relative name."""
    base_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        text=True).strip()
    diff = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--unified=0", base_commit, "--", REACTOR_SOURCE],
        text=True)
    files: dict[str, set[int]] = {}
    name = None
    line = 0
    for text in diff.splitlines():
        if text.startswith("+++ b/"):
            name = text[6:] if is_reactor_source(text[6:]) else None
            if name:
                files.setdefault(name, set())
        elif text.startswith("+++ "):
            name = None
        elif text.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", text)
            if match is None:
                raise ValueError("Git returned an unreadable change range")
            line = int(match.group(1))
        elif text.startswith("+") and name:
            files[name].add(line)
            line += 1
    untracked = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z", "--", REACTOR_SOURCE],
        text=True)
    for name in untracked.split("\0"):
        if is_reactor_source(name):
            files[name] = set(range(1, len((repo / name).read_text().splitlines()) + 1))
    return {name: lines for name, lines in files.items() if lines}


def parse_lcov(text: str) -> dict[str, dict]:
    """{file: {"lines": {line: hits}, "branches": [(line, taken)]}} from an lcov report."""
    files: dict[str, dict] = {}
    current = None
    for record in text.splitlines():
        if record.startswith("SF:"):
            current = files.setdefault(record[3:], {"lines": {}, "branches": []})
        elif current is not None and record.startswith("DA:"):
            line, hits = record[3:].split(",")[:2]
            current["lines"][int(line)] = int(hits)
        elif current is not None and record.startswith("BRDA:"):
            line, _block, _branch, taken = record[5:].split(",")
            current["branches"].append((int(line), 0 if taken == "-" else int(taken)))
    return files


def check_reactor(repo: Path, changes: dict[str, set[int]], lcov: str | None,
                  report_mtime: float | None = None) -> dict:
    """Every changed executable line and branch of the reactor's Node source must run in a test."""
    rows = []
    if changes and lcov is None:
        raise ValueError("Changed reactor JavaScript needs a Node coverage report (--reactor-lcov)")
    measured = parse_lcov(lcov) if lcov is not None else {}
    for name, changed in sorted(changes.items()):
        report = measured.get(name) or measured.get(str(repo / name))
        if report is None:
            raise ValueError(f"Coverage is missing for changed code: {name}")
        if report_mtime is not None and (repo / name).stat().st_mtime > report_mtime:
            raise ValueError(f"Source changed after coverage measurement: {name}")
        lines = changed & report["lines"].keys()
        branches = [(line, taken) for line, taken in report["branches"] if line in changed]
        rows.append({
            "file": name,
            "lines": len(lines),
            "missing_lines": sorted(line for line in lines if report["lines"][line] <= 0),
            "branches": len(branches),
            "missing_branches": sorted(line for line, taken in branches if taken <= 0),
        })
    return {"passed": all(not (row["missing_lines"] or row["missing_branches"]) for row in rows), "files": rows}


def changed_hunks(repo: Path, base: str) -> dict[str, list[set[int]]]:
    base_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        text=True).strip()
    hunks: dict[str, list[set[int]]] = {}
    for name in sorted(MUTATION_PROVEN):
        diff = subprocess.check_output(
            ["git", "-C", str(repo), "diff", "--no-ext-diff", "--no-color", "--unified=0",
             "--inter-hunk-context=0", base_commit, "--", name],
            text=True)
        line = 0
        for text in diff.splitlines():
            if text.startswith("@@"):
                match = re.search(r"\+(\d+)(?:,\d+)?", text)
                if match is None:
                    raise ValueError("Git returned an unreadable change range")
                line = int(match.group(1))
                hunks.setdefault(name, []).append(set())
            elif text.startswith("+") and not text.startswith("+++"):
                hunks[name][-1].add(line)
                line += 1
    return {name: kept for name, found in hunks.items() if (kept := [hunk for hunk in found if hunk])}


def broken_lines(source: str, original: str, broken: str) -> set[int]:
    old, new = original.splitlines(keepends=True), broken.splitlines(keepends=True)
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    start = source[:source.index(original)].count("\n") + 1
    return set(range(start + prefix, start + len(old) - suffix))


def check_mutation_proof(repo: Path, hunks: dict[str, list[set[int]]], checks) -> list[dict]:
    results = []
    for name, file_hunks in sorted(hunks.items()):
        source = (repo / name).read_text()
        lines = source.splitlines()
        spans = []
        for check, filename, original, broken, _selector in checks:
            if filename != name:
                continue
            if source.count(original) != 1:
                raise ValueError(f"Mutation target must appear once in {name}: {check}")
            spans.append(broken_lines(source, original, broken))
        unproven = []
        for hunk in file_hunks:
            code = {line for line in hunk
                    if lines[line - 1].strip() and not lines[line - 1].lstrip().startswith("#")}
            if code and not any(span and span <= code for span in spans):
                unproven.append(sorted(code))
        results.append({"file": name, "hunks": len(file_hunks), "unproven_hunks": unproven})
    return results


def browser_source(repo: Path, script: tuple[str, str, str] = DASHBOARD) -> ast.Constant:
    filename, variable, label = script
    source = (repo / filename).read_text()
    tree = ast.parse(source)
    matches = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one literal {label} script; update the coverage mapping")
    node = matches[0]
    literal = ast.get_source_segment(source, node)
    if (literal is None or not literal.lower().startswith(('r"""', "r'''"))
            or literal[4:-3] != node.value):
        raise ValueError(f"{label[0].upper()}{label[1:]} JavaScript must use a raw literal "
                         "with exact source-line mapping")
    return node


def check_browser(repo: Path, changes: dict[str, set[int]], javascript_report: dict | None,
                  script: tuple[str, str, str]) -> dict:
    """Line and block coverage of the changed lines in one embedded browser script."""
    filename, _, label = script
    browser = {"lines": 0, "missing_lines": [], "blocks": 0, "missing_blocks": []}
    file_changes = changes.get(filename, set())
    if not file_changes:
        return browser
    node = browser_source(repo, script)
    changed_script = {line for line in file_changes if node.lineno <= line <= node.end_lineno}
    if not changed_script:
        return browser
    if javascript_report is None:
        raise ValueError(f"Changed {label} JavaScript needs browser coverage, not Python string coverage")
    if javascript_report.get("source") != node.value:
        raise ValueError(f"Browser coverage does not match the current {label} script")
    reports = javascript_report.get("coverage", {})
    if not isinstance(reports, dict) or len(reports) != 1:
        raise ValueError(f"Expected exactly one {label} JavaScript coverage record")
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
    return browser


def check_coverage(repo: Path, changes: dict[str, set[int]],
                   python_report: dict, javascript_report: dict | None,
                   source_manifest: dict | None = None,
                   work_queue_report: dict | None = None) -> dict:
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
    browser = check_browser(repo, changes, javascript_report, DASHBOARD)
    work_queue_browser = check_browser(repo, changes, work_queue_report, WORK_QUEUE_PAGE)
    passed = all(not (row["missing_lines"] or row["missing_branches"] or row["excluded_changed_lines"])
                 for row in results)
    passed = passed and not any(b["missing_lines"] or b["missing_blocks"] for b in (browser, work_queue_browser))
    return {"passed": passed, "required_percent": 100, "python": results, "browser": browser,
            "work_queue_browser": work_queue_browser}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main")
    parser.add_argument("--python-report", type=Path, required=True)
    parser.add_argument("--browser-report", type=Path)
    parser.add_argument("--work-queue-browser-report", type=Path,
                        help="browser coverage of the Work queue page's script in work_queue/page.py")
    parser.add_argument("--reactor-lcov", type=Path,
                        help="Node lcov coverage of slack-reactor/src (see REACTOR_SOURCE)")
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
            json.loads(manifest_path.read_text()),
            json.loads(args.work_queue_browser_report.read_text()) if args.work_queue_browser_report else None)
        result["reactor"] = check_reactor(
            repo, changed_reactor_lines(repo, args.base),
            args.reactor_lcov.read_text() if args.reactor_lcov else None,
            args.reactor_lcov.stat().st_mtime if args.reactor_lcov else None)
        result["passed"] = result["passed"] and result["reactor"]["passed"]
        result["mutation_proven"] = check_mutation_proof(repo, changed_hunks(repo, args.base), CHECKS)
        result["passed"] = result["passed"] and not any(
            row["unproven_hunks"] for row in result["mutation_proven"])
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Coverage check failed: {exc}\n")
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
