"""Prove critical regression tests fail when their protection is removed."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


REPO = Path(__file__).resolve().parents[1]
CHECKS = (
    (
        "changed conversation invalidates guidance",
        "src/assistant/session_guidance.py",
        '        and note["source_version"] == version\n',
        "",
        "tests/test_session_guidance.py::SessionGuidanceTests::test_changed_source_invalidates_note_without_hiding_new_reply",
    ),
    (
        "focus rejects a changed workspace identity",
        "bin/todo-server.py",
        "target = checked_workspace_id(ws_ref, workspace_id) if workspace_id is not None else ws_ref",
        "target = ws_ref",
        "tests/test_todo_server.py::test_post_focus_checks_observed_workspace_identity",
    ),
    (
        "unfinished tool calls remain pending",
        "bin/session-context-watcher.py",
        "self.pending_tools.add(tool_id)",
        "self.pending_tools.discard(tool_id)",
        "tests/test_session_context_watcher.py::test_pending_tools_mixed_text_parallel_and_matching_results",
    ),
    (
        "changed open decisions invalidate cleanup approval",
        "src/assistant/decisions.py",
        'latest.get("status") != OPEN or record_fingerprint(latest) != change["fingerprint"]',
        'latest.get("status") != OPEN',
        "tests/test_decision_backlog_cleanup.py::BacklogCleanupTests::test_changed_open_decision_invalidates_its_approved_fingerprint",
    ),
)


def copy_sources(destination: Path):
    files = subprocess.check_output(
        ["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        text=True).split("\0")
    for name in sorted(set(files) - {""}):
        source = REPO / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, target)


def run_test(repo: Path, home: Path, selector: str):
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo / "bin"))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", selector, "-q", "--tb=short"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for name, filename, original, broken, selector in CHECKS:
        with TemporaryDirectory(prefix="assistant-mutation-") as temporary:
            root = Path(temporary)
            repo, home = root / "repo", root / "home"
            repo.mkdir()
            home.mkdir()
            copy_sources(repo)
            baseline = run_test(repo, home, selector)
            if baseline.returncode != 0:
                raise RuntimeError(f"{name}: baseline failed\n{baseline.stdout}\n{baseline.stderr}")
            target = repo / filename
            source = target.read_text()
            if source.count(original) != 1:
                raise RuntimeError(f"{name}: expected one mutation target")
            target.write_text(source.replace(original, broken, 1))
            mutant = run_test(repo, home, selector)
            if mutant.returncode != 1 or "FAILED" not in mutant.stdout:
                raise RuntimeError(f"{name}: mutant was not rejected\n{mutant.stdout}\n{mutant.stderr}")
            results.append({
                "check": name, "test": selector, "baseline": "passed", "mutant": "rejected",
                "failure": [line for line in mutant.stdout.splitlines() if line.startswith("FAILED")],
            })
    args.output.write_text(json.dumps({"passed": True, "checks": results}, indent=2))
    print(f"{len(results)} targeted mutations rejected; production files were not edited.")


if __name__ == "__main__":
    main()
