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
    (
        "completed tool traffic invalidates old close-out notes",
        "bin/session-context-watcher.py",
        '"history": self.history_digest.hexdigest(),',
        '"history": "",',
        "tests/test_session_context_watcher.py::test_completed_tool_roundtrip_cannot_reuse_closeout_note",
    ),
    (
        "unknown terminals block workspace close-out",
        "bin/render-assistant-page.py",
        "tools_complete = bool(associated) and not unknown_terminals and all(",
        "tools_complete = bool(associated) and all(",
        "tests/test_dashboard_overview.py::OverviewTests::test_unknown_second_terminal_blocks_observed_and_reviewed_close_out",
    ),
    (
        "ws-numberer is off by default",
        "install.sh",
        "WITH_WS_NUMBERER=0\n",
        "WITH_WS_NUMBERER=1\n",
        "tests/test_install_tiers.py::test_bare_install_loads_core_not_features",
    ),
    (
        "--with-ws-numberer turns the numberer on",
        "install.sh",
        "        --with-ws-numberer) WITH_WS_NUMBERER=1 ;;\n",
        "",
        "tests/test_install_tiers.py::test_with_ws_numberer_loads_numberer_only",
    ),
    (
        "help lists --with-ws-numberer",
        "install.sh",
        "  --with-ws-numberer   number and color cmux workspaces (cmux-ws-numberer).\n",
        "",
        "tests/test_install_optin_prompt.py::test_help_documents_ws_numberer",
    ),
    (
        "help lists the numberer among feature plists",
        "install.sh",
        "(--with-ws-numberer); comms +",
        "comms +",
        "tests/test_install_optin_prompt.py::test_help_documents_ws_numberer",
    ),
    (
        "--with-ws-numberer is remembered as yes",
        "install.sh",
        '        ws-numberer)   echo "$WITH_WS_NUMBERER" ;;\n',
        "",
        "tests/test_install_optin_prompt.py::test_ws_numberer_flag_persists_yes_on_apply",
    ),
    (
        "a running numberer is adopted",
        "install.sh",
        '        ws-numberer)   echo "com.assistant.cmux-ws-numberer" ;;\n',
        "",
        "tests/test_install_optin_prompt.py::test_ws_numberer_adopted_when_already_running",
    ),
    (
        "a running legacy numberer is adopted",
        "install.sh",
        '    [[ "$1" == "ws-numberer" ]] && launchctl print "gui/$UID/com.mukuls.cmux-ws-numberer" >/dev/null 2>&1\n',
        "    return 1\n",
        "tests/test_install_optin_prompt.py::test_ws_numberer_adopted_when_legacy_label_runs",
    ),
    (
        "the numberer prompt explains the feature",
        "install.sh",
        '        ws-numberer)   echo "The workspace numberer adds a [N] suffix to every cmux workspace title and gives each an unused sidebar color. Enable? [y/N] " ;;\n',
        "",
        "tests/test_install_optin_prompt.py::test_interactive_apply_offers_ws_numberer",
    ),
    (
        "a remembered numberer answer is read",
        "install.sh",
        " 'ws-numberer:WITH_WS_NUMBERER'; do",
        "; do",
        "tests/test_install_optin_prompt.py::test_ws_numberer_remembered_yes_loads",
    ),
    (
        "the numberer plist is a feature, not core",
        "install.sh",
        '        com.assistant.cmux-ws-numberer.plist)   echo "ws-numberer" ;;\n',
        "",
        "tests/test_install_tiers.py::test_bare_install_loads_core_not_features",
    ),
    (
        "the numberer loads only when enabled",
        "install.sh",
        "        ws-numberer)   [[ $WITH_WS_NUMBERER -eq 1 ]] ;;\n",
        "",
        "tests/test_install_tiers.py::test_with_ws_numberer_loads_numberer_only",
    ),
    (
        "an unenabled numberer says how to enable it",
        "install.sh",
        "memory|crash-resume|ws-numberer)",
        "memory|crash-resume)",
        "tests/test_install_tiers.py::test_bare_install_loads_core_not_features",
    ),
    (
        "the legacy numberer plist is removed",
        "install.sh",
        '    "com.mukuls.cmux-ws-numberer"\n',
        "",
        "tests/test_install_optin_prompt.py::test_apply_removes_the_legacy_ws_numberer_plist",
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


def reject_invalid_shell(name: str, target: Path):
    if target.suffix != ".sh":
        return
    syntax = subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True)
    if syntax.returncode != 0:
        raise RuntimeError(f"{name}: mutant is not valid shell\n{syntax.stderr}")


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
            reject_invalid_shell(name, target)
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
