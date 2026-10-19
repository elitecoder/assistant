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
        "        --with-ws-numberer) ;;\n",
        "tests/test_install_tiers.py::test_with_ws_numberer_loads_numberer_only",
    ),
    (
        "--with-all turns the numberer on",
        "install.sh",
        "WITH_CRASH_RESUME=1; WITH_WS_NUMBERER=1 ;;",
        "WITH_CRASH_RESUME=1 ;;",
        "tests/test_install_tiers.py::test_with_all_loads_every_opt_in_feature",
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
    (
        "cmux session records include every saved record",
        "bin/cmux_sessions.py",
        '[cli, "sessions", "list", "--json", "--all"],',
        '[cli, "sessions", "list", "--json"],',
        "tests/test_cmux_sessions.py::test_lists_every_saved_record_with_typed_fields",
    ),
    (
        "a session cleared off its surface is not live",
        "bin/world-scanner.py",
        "if not (record.active_for_surface and record.provider and pid_alive(record.pid)):",
        "if not (record.provider and pid_alive(record.pid)):",
        "tests/test_world_scanner.py::test_build_live_sessions_keeps_live_active_fleet_records",
    ),
    (
        "a resumed session is verified by its latest hook update",
        "bin/world-scanner.py",
        "    recorded = record.updated_at\n",
        "    recorded = record.started_at\n",
        "tests/test_world_scanner.py::test_current_session_record_verifies_missing_native_binding",
    ),
    (
        "a dead-pid cmux record never attaches a transcript",
        "bin/build-ws-context.py",
        "    if not _pid_alive(record.pid):\n        return None  # stale record — do NOT trust\n",
        "",
        "tests/test_build_ws_context_in_process.py::RecordedTranscriptTests::test_dead_pid_record_rejected",
    ),
    (
        "help says cmux records sessions itself",
        "install.sh",
        "  - cmux records agent sessions itself: hooks/cmux-auto-resume.py → ~/.claude/hooks/\n"
        "       for agents started outside cmux's wrapper, Factory hooks via cmux, and the\n"
        "       retired hand-written session hooks removed from Claude and Factory settings\n",
        "",
        "tests/test_install_cmux_sessions.py::test_help_documents_native_cmux_sessions",
    ),
    (
        "help no longer lists the registry among untouched files",
        "install.sh",
        "  - ~/.claude/cache/, ~/.claude/projects/\n",
        "  - ~/.claude/cache/\n",
        "tests/test_install_cmux_sessions.py::test_help_documents_native_cmux_sessions",
    ),
    (
        "apply removes a retired link",
        "install.sh",
        '            rm "$link"\n',
        "            :\n",
        "tests/test_install_cmux_sessions.py::test_apply_removes_the_retired_links_and_registrations",
    ),
    (
        "a dry run keeps retired links",
        "install.sh",
        '        if [[ $APPLY -eq 1 ]]; then\n            rm "$link"\n',
        '        if true; then\n            rm "$link"\n',
        "tests/test_install_cmux_sessions.py::test_dry_run_reports_without_removing",
    ),
    (
        "only links into this checkout are removed",
        "install.sh",
        '    if [[ -L "$link" && "$(readlink "$link")" == "$target" ]]; then\n',
        '    if [[ -L "$link" ]]; then\n',
        "tests/test_install_cmux_sessions.py::test_retired_link_into_another_checkout_is_kept",
    ),
    (
        "the retired restore CLI link is removed",
        "install.sh",
        'remove_retired_link "$HOME_DIR/.local/bin/cmux-restore-sessions" "$REPO_ROOT/bin/cmux-restore-sessions.py"\n',
        "",
        "tests/test_install_cmux_sessions.py::test_apply_removes_the_retired_links_and_registrations",
    ),
    (
        "the auto-resume hook stays linked",
        "install.sh",
        'ensure_symlink "$HOME_DIR/.claude/hooks/cmux-auto-resume.py" "$REPO_ROOT/hooks/cmux-auto-resume.py"\n',
        "",
        "tests/test_install_cmux_sessions.py::test_fresh_apply_links_the_auto_resume_hook",
    ),
    (
        "the auto-resume hook stays registered",
        "install/patch-settings.py",
        'RETIRED_HOOK_SCRIPTS = (\n    "cmux-session-ledger.py",\n',
        'RETIRED_HOOK_SCRIPTS = (\n    "cmux-auto-resume.py",\n    "cmux-session-ledger.py",\n',
        "tests/test_patch_settings.py::test_removes_every_retired_hook_and_keeps_the_rest",
    ),
    (
        "a failed settings cleanup is reported",
        "install.sh",
        '        || warn "patch-settings failed (continuing — retired session hooks may still be registered)"\n',
        "        || true\n",
        "tests/test_install_cmux_sessions.py::test_failed_settings_cleanup_warns_and_the_install_continues",
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
