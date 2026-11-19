"""Prove critical regression tests fail when their protection is removed."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


REPO = Path(__file__).resolve().parents[1]
TEST_TIMEOUT_SEC = 90
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
        '            elif kind == "tool_use":\n                self.pending_tools.add(tool_id)',
        '            elif kind == "tool_use":\n                self.pending_tools.discard(tool_id)',
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
        "the installer finds cmux through CMUX_BIN",
        "install.sh",
        '    CMUX_CLI="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"\n',
        '    CMUX_CLI="/Applications/cmux.app/Contents/Resources/bin/cmux"\n',
        "tests/test_install_cmux_sessions.py::test_apply_installs_copilot_hooks_only_when_copilot_is_picked",
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
        "loads by default (--no-ws-numberer to skip); comms +",
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
        "the registry stands in only for sessions cmux lacks",
        "bin/cmux_sessions.py",
        "        if not session_id or session_id in known:\n",
        "        if not session_id:\n",
        "tests/test_cmux_sessions.py::test_registry_fallback_keeps_only_sessions_cmux_lacks",
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
        "a session cmux missed is verified from the registry",
        "bin/world-scanner.py",
        "    for source in (cmux_sessions.CMUX, cmux_sessions.REGISTRY):\n",
        "    for source in (cmux_sessions.CMUX,):\n",
        "tests/test_world_scanner.py::test_registry_fallback_verifies_a_session_cmux_never_recorded",
    ),
    (
        "a reused registry pid is not a live session",
        "bin/world-scanner.py",
        "        if record.source == cmux_sessions.REGISTRY and not cmux_sessions.agent_process(record.pid):\n            continue\n",
        "",
        "tests/test_world_scanner.py::test_registry_row_with_a_live_pid_must_be_an_agent",
    ),
    (
        "two active records for a surface verify neither",
        "bin/world-scanner.py",
        "    if len(matches) != 1:\n        return None\n    record = matches[0]",
        "    if not matches:\n        return None\n    record = matches[0]",
        "tests/test_world_scanner.py::test_two_active_records_for_one_surface_verify_neither",
    ),
    (
        "a dead-pid record never attaches a transcript",
        "bin/build-ws-context.py",
        "    if not _pid_alive(record.pid):\n        return None  # stale record — do NOT trust\n",
        "",
        "tests/test_build_ws_context_in_process.py::RecordedTranscriptTests::test_dead_pid_record_rejected",
    ),
    (
        "two cmux records for a surface are ambiguous",
        "bin/build-ws-context.py",
        "    if len(records) != 1:\n        return None\n    record = records[0]",
        "    if not records:\n        return None\n    record = records[0]",
        "tests/test_build_ws_context_in_process.py::RecordedTranscriptTests::test_two_active_cmux_records_for_the_surface_are_ambiguous",
    ),
    (
        "the forward waits for cmux to record the session",
        "hooks/cmux-auto-resume.py",
        '    if not any(isinstance(row, dict) and row.get("session_id") == sid for row in sessions):\n',
        "    if False:\n",
        "tests/test_cmux_auto_resume.py::test_a_session_cmux_never_records_is_ledgered_after_the_last_try",
    ),
    (
        "a failed forward is retried",
        "hooks/cmux-auto-resume.py",
        "        if reason is None:\n            return None\n    return reason\n",
        "        return reason\n    return reason\n",
        "tests/test_cmux_auto_resume.py::test_a_failed_forward_is_retried_with_backoff",
    ),
    (
        "SessionStart never waits for the forward",
        "hooks/cmux-auto-resume.py",
        '    subprocess.Popen(\n        [sys.executable, os.path.abspath(__file__), "--worker", job],',
        '    subprocess.run(\n        [sys.executable, os.path.abspath(__file__), "--worker", job],',
        "tests/test_cmux_auto_resume.py::test_the_hook_returns_at_once_and_the_detached_worker_finishes",
    ),
    (
        "the auto-resume hook is registered on a fresh machine",
        "install/patch-settings.py",
        "    added = ensure_auto_resume(hooks)\n",
        "    added = False\n",
        "tests/test_install_cmux_sessions.py::test_fresh_apply_links_and_registers_the_session_hooks",
    ),
    (
        "the ledger registrations are removed",
        "install/patch-settings.py",
        "    removed = remove_retired(hooks)\n",
        "    removed = []\n",
        "tests/test_install_cmux_sessions.py::test_apply_on_the_live_layout_drops_only_the_ledger",
    ),
    (
        "help documents the session hooks",
        "install.sh",
        "  - cmux session hooks: hooks/ → ~/.claude/hooks/ (symlinks),\n",
        "",
        "tests/test_install_cmux_sessions.py::test_help_documents_the_cmux_session_hooks",
    ),
    (
        "help says SessionStart runs the auto-resume hook",
        "install.sh",
        "       and Claude + Factory SessionStart patched to run cmux-auto-resume.py\n",
        "",
        "tests/test_install_cmux_sessions.py::test_help_documents_the_cmux_session_hooks",
    ),
    (
        "the session hook step is logged",
        "install.sh",
        'log "[4/5] Wiring cmux session hooks (hooks + CLI + settings)"\n',
        'log "[4/5]"\n',
        "tests/test_install_cmux_sessions.py::test_fresh_apply_links_and_registers_the_session_hooks",
    ),
    (
        "a failed settings patch is reported",
        "install.sh",
        '        || warn "patch-settings failed (continuing — cmux session hooks may be incomplete)"\n',
        "        || true\n",
        "tests/test_install_cmux_sessions.py::test_failed_settings_patch_warns_and_the_install_continues",
    ),
    (
        "a copilot-only machine continues past the prerequisite check",
        "install-bootstrap.sh",
        "    if command -v copilot >/dev/null 2>&1 || command -v droid >/dev/null 2>&1; then\n",
        "    if command -v droid >/dev/null 2>&1; then\n",
        "tests/test_install_bootstrap.py::test_a_copilot_only_machine_passes_the_prerequisite_check",
    ),
    (
        "no agent at all names every supported agent",
        "install-bootstrap.sh",
        '        die "no coding agent found — install Claude Code (default), GitHub Copilot CLI, or Factory Droid first"\n',
        '        die "no coding agent found"\n',
        "tests/test_install_bootstrap.py::test_a_machine_with_no_agent_stops_and_names_all_three",
    ),
    (
        "the bootstrap reports a found copilot",
        "install-bootstrap.sh",
        '    && ok "copilot found (GitHub Copilot available as opt-in)" \\\n',
        "    && : \\\n",
        "tests/test_install_bootstrap.py::test_a_copilot_only_machine_passes_the_prerequisite_check",
    ),
    (
        "comms setup accepts copilot as the provider",
        "bin/assistant-comms-setup.sh",
        '    provider = env_provider if env_provider in ("claude", "copilot", "droid") else "claude"\n',
        '    provider = env_provider if env_provider in ("claude", "droid") else "claude"\n',
        "tests/test_comms_setup.py::CommsSetupTests::test_env_provider_copilot_persists_and_installs_its_hooks",
    ),
    (
        "comms setup installs copilot hooks when copilot is picked",
        "bin/assistant-comms-setup.sh",
        'if "$PYTHON" "$REPO_DIR/bin/agent_session.py" copilot-chosen; then\n'
        '    "$PYTHON" "$REPO_DIR/bin/agent_session.py" install-copilot-hooks \\\n'
        '        || echo "     ✗ cmux Copilot hooks not installed; Copilot sessions won\'t show on the dashboard" >&2\n'
        '    "$PYTHON" "$REPO_DIR/bin/agent_session.py" link-copilot-skills \\\n'
        '        || echo "     ✗ Copilot skills not linked; Copilot sessions have no /cleanup, /todo, or /lesson" >&2\n'
        'fi\n',
        "",
        "tests/test_comms_setup.py::CommsSetupTests::test_env_provider_copilot_persists_and_installs_its_hooks",
    ),
    (
        "the installer installs copilot hooks when copilot is picked",
        "install.sh",
        '        if python3 "$REPO_ROOT/bin/agent_session.py" copilot-chosen; then\n'
        '            CMUX_BIN="${CMUX_BIN:-$CMUX_CLI}" python3 "$REPO_ROOT/bin/agent_session.py" install-copilot-hooks 2>&1 | sed \'s/^/  /\' \\\n'
        '                || warn "cmux Copilot hooks install failed (continuing — Copilot sessions may not show on the dashboard)"\n'
        '        fi\n',
        "",
        "tests/test_install_cmux_sessions.py::test_apply_with_copilot_picked_writes_its_cmux_hooks",
    ),
    (
        "a failed copilot hooks install is reported",
        "install.sh",
        '                || warn "cmux Copilot hooks install failed (continuing — Copilot sessions may not show on the dashboard)"\n',
        "                || true\n",
        "tests/test_install_cmux_sessions.py::test_a_failed_copilot_hooks_install_warns_and_the_install_continues",
    ),
    (
        "the installer links copilot's skills when copilot is picked",
        "install.sh",
        '        python3 "$REPO_ROOT/bin/agent_session.py" link-copilot-skills \\\n',
        "        true \\\n",
        "tests/test_install_cmux_sessions.py::test_apply_with_copilot_picked_links_copilots_skills_to_assistants",
    ),
    (
        "the skills link waits for a copilot pick",
        "install.sh",
        '# Copilot reads personal skills from ~/.copilot/skills.\n'
        'if python3 "$REPO_ROOT/bin/agent_session.py" copilot-chosen; then\n',
        '# Copilot reads personal skills from ~/.copilot/skills.\n'
        'if true; then\n',
        "tests/test_install_cmux_sessions.py::test_apply_without_copilot_leaves_copilots_skills_alone",
    ),
    (
        "a skills link that fails is reported",
        "install.sh",
        '            || warn "Copilot skills not linked; Copilot sessions have no /cleanup, /todo, or /lesson"\n',
        "            || true\n",
        "tests/test_install_cmux_sessions.py::test_an_existing_copilot_skills_folder_warns_and_the_install_continues",
    ),
    (
        "a dry run says it would link copilot's skills",
        "install.sh",
        '        note "would link ~/.copilot/skills → ~/.claude/skills (Copilot is picked)"\n',
        "        :\n",
        "tests/test_install_cmux_sessions.py::test_dry_run_with_copilot_picked_says_it_would_link_the_skills",
    ),
    (
        "comms setup links copilot's skills",
        "bin/assistant-comms-setup.sh",
        '    "$PYTHON" "$REPO_DIR/bin/agent_session.py" link-copilot-skills \\\n',
        "    true \\\n",
        "tests/test_comms_setup.py::CommsSetupTests::test_env_provider_copilot_persists_and_installs_its_hooks",
    ),
    (
        "comms setup reports a failed skills link",
        "bin/assistant-comms-setup.sh",
        '        || echo "     ✗ Copilot skills not linked; Copilot sessions have no /cleanup, /todo, or /lesson" >&2\n',
        "        || true\n",
        "tests/test_comms_setup.py::CommsSetupTests::test_copilot_setup_says_what_failed_and_finishes",
    ),
    (
        "comms setup reports failed copilot hooks",
        "bin/assistant-comms-setup.sh",
        '        || echo "     ✗ cmux Copilot hooks not installed; Copilot sessions won\'t show on the dashboard" >&2\n',
        "        || true\n",
        "tests/test_comms_setup.py::CommsSetupTests::test_copilot_setup_says_what_failed_and_finishes",
    ),
    (
        "a dry run says what it would patch",
        "install.sh",
        '    note "would install cmux Factory hooks (+ Copilot hooks when Copilot is picked) and patch Claude + Factory session hooks"\n',
        "    :\n",
        "tests/test_install_cmux_sessions.py::test_dry_run_reports_without_patching",
    ),
    (
        'default mode keeps the Pro skills out',
        "install.sh",
        'PRO_SKILLS=" goal attend back-off lesson "\n',
        'PRO_SKILLS=""\n',
        'tests/test_pro_mode.py::test_installer_modes_and_skills',
    ),
    (
        '--pro turns on Pro mode',
        "install.sh",
        '        --pro) SET_MODE=pro ;;\n',
        '',
        'tests/test_pro_mode.py::test_installer_modes_and_skills',
    ),
    (
        'help lists --no-pro',
        "install.sh",
        '  --no-pro          Switch to default mode and unlink the Pro skills.\n',
        '',
        'tests/test_pro_mode.py::test_help_documents_pro_mode',
    ),
    (
        'help says workspace numbers are on by default',
        "install.sh",
        '                       On by default; --no-ws-numberer turns it off.\n',
        '',
        'tests/test_install_optin_prompt.py::test_help_documents_ws_numberer',
    ),
    (
        '--no-ws-numberer stops a running copy',
        "install.sh",
        '            launchctl bootout "gui/$UID/com.assistant.cmux-ws-numberer" 2>/dev/null || true\n',
        '',
        'tests/test_install_optin_prompt.py::test_no_ws_numberer_stops_a_running_copy',
    ),
    (
        'workspace numbers turn on without asking',
        "install.sh",
        '    if [[ "$_feat" == "ws-numberer" && "${ASSISTANT_SELF_UPDATE:-0}" != "1" ]]; then\n        eval "$_var=1"',
        '    if false; then\n        eval "$_var=1"',
        'tests/test_install_optin_prompt.py::test_interactive_apply_turns_on_ws_numberer_without_asking',
    ),
    (
        'an install saves the inferred mode',
        "install.sh",
        '        python3 "$MODE_CLI" --ensure >/dev/null || warn "mode not saved — fix ~/.assistant/config.json"\n',
        '        true\n',
        'tests/test_pro_mode.py::test_self_update_keeps_an_existing_pro_install_and_its_skills',
    ),
    (
        'default mode skips the Pro skills',
        "install.sh",
        '    if [[ "$MODE" != "pro" && "$PRO_SKILLS" == *" $skill_name "* ]]; then\n',
        '    if false; then\n',
        'tests/test_pro_mode.py::test_installer_modes_and_skills',
    ),
    (
        'the hourly update leaves stopped workspace numbers off',
        "install.sh",
        '            && ! launchctl print "gui/$UID/$label" >/dev/null 2>&1; then\n        no_load=1\n',
        '            && false; then\n        no_load=1\n',
        'tests/test_pro_mode.py::test_the_hourly_update_never_starts_workspace_numbers_you_stopped',
    ),
    (
        "an install starts enabled workspace numbers that aren't running",
        "install.sh",
        'enabled but not running → will load)"\n            CHANGED_LABELS+=("$label")\n',
        'enabled but not running → will load)"\n',
        'tests/test_pro_mode.py::test_the_hourly_update_never_starts_workspace_numbers_you_stopped',
    ),
    (
        'an explicit --with-ws-numberer wins over the update rule',
        "install.sh",
        ' && $WS_NUMBERER_FLAG -eq 0 ]]',
        ' ]]',
        'tests/test_pro_mode.py::test_the_hourly_update_never_starts_workspace_numbers_you_stopped',
    ),
    (
        'the --with-ws-numberer flag is remembered before the opt-in loop',
        "install.sh",
        'WS_NUMBERER_FLAG=$WITH_WS_NUMBERER\n',
        'WS_NUMBERER_FLAG=0\n',
        'tests/test_pro_mode.py::test_the_hourly_update_never_starts_workspace_numbers_you_stopped',
    ),
    (
        'a failed restart keeps the old code stamp',
        "install.sh",
        '            failed_labels+=("$label")\n',
        '',
        'tests/test_pro_mode.py::test_installer_restarts_running_services_only_when_their_code_changes',
    ),
    (
        'only running services restart',
        "install.sh",
        ' | grep -q "state = running"',
        '',
        'tests/test_pro_mode.py::test_installer_restarts_running_services_only_when_their_code_changes',
    ),
    (
        'a failed restart leaves a note',
        "install.sh",
        'RESTART_NOTE="$HOME_DIR/.assistant/service-restart-failed"',
        'RESTART_NOTE="$HOME_DIR/.assistant/elsewhere"',
        'tests/test_pro_mode.py::test_installer_restarts_running_services_only_when_their_code_changes',
    ),
    (
        'a working restart clears the note',
        "install.sh",
        '        rm -f "$RESTART_NOTE"\n',
        '',
        'tests/test_pro_mode.py::test_installer_restarts_running_services_only_when_their_code_changes',
    ),
    (
        'the summary says how to switch modes',
        "install.sh",
        '    log "     run ./install.sh --apply --pro (or --no-pro). Pro mode adds"\n',
        '',
        'tests/test_pro_mode.py::test_installer_modes_and_skills',
    ),
    (
        'a pull that changes the Work queue runs the installer',
        "bin/self_update.py",
        '"src/assistant/", "work_queue/")',
        '"src/assistant/")',
        'tests/test_self_update.py::ClassifyTests::test_long_running_service_code_needs_install',
    ),
    (
        'a pulled Work queue file must parse',
        "bin/self_update.py",
        '"install.sh", "install-bootstrap.sh", "work_queue/")',
        '"install.sh", "install-bootstrap.sh")',
        'tests/test_self_update.py::SyntaxGateTests::test_broken_runtime_python_outside_bin_is_refused',
    ),
    (
        'a dry run plans a service whose plist is new',
        "install.sh",
        '        note "would load $label (bootstrap $plist once it\'s copied)"\n        return 0\n',
        '        false\n',
        'tests/test_work_queue_install.py::test_a_dry_run_before_the_first_install_plans_the_new_service',
    ),
    (
        'the help names the Work queue',
        "install.sh",
        '                    Also loads the Work queue page (docs/work-queue.md).\n',
        '',
        'tests/test_work_queue_install.py::test_help_names_the_work_queue',
    ),
    (
        'the Work queue is a feature daemon, not core',
        "install.sh",
        '        com.assistant.work-queue.plist)         echo "work-queue" ;;\n',
        '',
        'tests/test_work_queue_install.py::test_default_mode_copies_the_work_queue_but_never_loads_it',
    ),
    (
        'Pro mode loads the Work queue',
        "install.sh",
        '        work-queue)    [[ "$MODE" == "pro" ]] ;;\n',
        '',
        'tests/test_work_queue_install.py::test_pro_mode_loads_a_localhost_only_server_and_never_prompts_headless',
    ),
    (
        'default mode says the Work queue is Pro only',
        "install.sh",
        '                work-queue)\n                    note "FEATURE $base — Pro mode only (copied, NOT loaded; turn on with --pro)" ;;\n',
        '',
        'tests/test_work_queue_install.py::test_default_mode_copies_the_work_queue_but_never_loads_it',
    ),
    (
        'turning Pro mode on loads an already-copied Work queue',
        "install.sh",
        '( "$feat" == "ws-numberer" && "${ASSISTANT_SELF_UPDATE:-0}" != "1" ) || "$feat" == "work-queue" ]]',
        '( "$feat" == "ws-numberer" && "${ASSISTANT_SELF_UPDATE:-0}" != "1" ) ]]',
        'tests/test_work_queue_install.py::test_switching_modes_loads_and_unloads_the_work_queue',
    ),
    (
        'default mode unloads the Work queue',
        "install.sh",
        '    launchctl bootout "gui/$UID/com.assistant.work-queue" 2>/dev/null || true\n',
        '    true\n',
        'tests/test_work_queue_install.py::test_default_mode_copies_the_work_queue_but_never_loads_it',
    ),
    (
        'Work queue code changes count toward a restart',
        "install.sh",
        'src/assistant work_queue -type f',
        'src/assistant -type f',
        'tests/test_work_queue_install.py::test_the_work_queue_restarts_when_its_code_changes',
    ),
    (
        'a running Work queue restarts after its code changes',
        "install.sh",
        'com.assistant.assistant-todo-server com.assistant.work-queue com.assistant.connector-',
        'com.assistant.assistant-todo-server com.assistant.connector-',
        'tests/test_work_queue_install.py::test_the_work_queue_restarts_when_its_code_changes',
    ),
    (
        'a headless install never prompts to link sources',
        "install.sh",
        'elif [[ ! -t 0 || "${ASSISTANT_SELF_UPDATE:-0}" == "1" ]]; then\n    note "non-interactive, so not linking',
        'elif [[ "${ASSISTANT_SELF_UPDATE:-0}" == "1" ]]; then\n    note "non-interactive, so not linking',
        'tests/test_work_queue_install.py::test_pro_mode_loads_a_localhost_only_server_and_never_prompts_headless',
    ),
    (
        'the summary says how to link Work queue sources',
        "install.sh",
        '    log "     ./bin/work-queue-setup.py link <source> (see docs/work-queue.md)."\n',
        '',
        'tests/test_work_queue_install.py::test_pro_mode_loads_a_localhost_only_server_and_never_prompts_headless',
    ),
    (
        'the Work queue server stays down in default mode',
        "work_queue/server.py",
        '    if not mode.is_pro():\n        print("Pro mode is off, so the Work queue server',
        '    if False:\n        print("Pro mode is off, so the Work queue server',
        'tests/test_work_queue_server.py::test_default_mode_never_starts_the_server',
    ),
    (
        'the Work queue server binds localhost only',
        "work_queue/server.py",
        'ThreadingHTTPServer(("127.0.0.1", port), Handler)',
        'ThreadingHTTPServer(("0.0.0.0", port), Handler)',
        'tests/test_work_queue_server.py::test_the_server_binds_localhost_only_and_raises_other_socket_errors',
    ),
    (
        'the Work queue refuses cross-origin posts',
        "work_queue/server.py",
        'if self.headers.get("Origin") not in (',
        'if False and self.headers.get("Origin") not in (',
        'tests/test_work_queue_server.py::test_a_cross_origin_post_is_refused_and_creates_nothing',
    ),
    (
        'agents start Slack replies with Automated Message',
        "work_queue/server.py",
        '"file the tickets, post the Slack replies (start each with \\"Automated Message\\"), comment',
        '"file the tickets, post the Slack replies, comment',
        'tests/test_work_queue_server.py::test_send_creates_an_auto_dispatch_todo_with_the_next_id_and_finish_instructions',
    ),
    (
        'marks only accept https rows',
        "work_queue/server.py",
        'isinstance(k, str) and k.startswith("https://") and isinstance(v, dict)',
        'isinstance(k, str) and isinstance(v, dict)',
        'tests/test_work_queue_server.py::test_marks_are_saved_merged_and_cleared',
    ),
    (
        'the refresh skips unlinked sources',
        "work_queue/refresh.py",
        '        if name not in doc:\n',
        '        if False:\n',
        'tests/test_work_queue_refresh.py::test_only_linked_sources_run',
    ),
    (
        'a recently active Slack save stays open',
        "work_queue/inputs/slack.py",
        '    if idle < close_after_days:\n',
        '    if False:\n',
        'tests/test_work_queue_inputs.py::test_slack_rules_sort_every_later_save',
    ),
    (
        'Slack apply verifies each save against the open list',
        "work_queue/slack_browser.py",
        'result["verified"] = not is_open if j["mark"] == "completed" else is_open',
        'result["verified"] = True',
        'tests/test_work_queue_slack_browser.py::test_apply_marks_saves_and_verifies_each_against_the_open_list',
    ),
    (
        'the dashboard hides the Work queue tab in default mode',
        "bin/render-assistant-page.py",
        '    tabs.append(("workspaces", "Session details", workspaces_html))\n',
        '    tabs.append(("workspaces", "Session details", workspaces_html))\n'
        '    tabs.append(("work-queue", "Work queue", render_work_queue_tab()))\n',
        'tests/test_work_queue_dashboard.py::test_default_mode_hides_the_work_queue_tab',
    ),
    (
        'every Work queue request re-checks Pro mode',
        'work_queue/server.py',
        '        if not mode.is_pro():\n            self.send_json(403',
        '        if False:\n            self.send_json(403',
        'tests/test_work_queue_server.py::test_turning_pro_mode_off_refuses_requests_and_stops_the_server',
    ),
    (
        'the Work queue server stops on its own when Pro mode turns off',
        'work_queue/server.py',
        '        if not mode.is_pro():\n            srv.shutdown()',
        '        if False:\n            srv.shutdown()',
        'tests/test_work_queue_server.py::test_the_server_stops_on_its_own_soon_after_pro_mode_turns_off',
    ),
    (
        'the running Work queue server watches the mode',
        'work_queue/server.py',
        '    threading.Thread(target=watch_mode, args=(server,), daemon=True).start()\n',
        '',
        'tests/test_work_queue_server.py::test_switching_to_default_mode_while_the_server_runs_stops_it_cleanly',
    ),
    (
        'the dashboard mode switch loads and unloads the Work queue',
        'bin/todo-server.py',
        '    sync_work_queue(value == "pro")\n',
        '',
        'tests/test_work_queue_dashboard.py::test_the_dashboard_switch_loads_and_unloads_the_work_queue',
    ),
    (
        "the hourly update never loads a Work queue that isn't running",
        'install.sh',
        '    if [[ "$feat" == "work-queue" && "${ASSISTANT_SELF_UPDATE:-0}" == "1" ]] && ! service_running "$label"; then\n        no_load=1\n    fi\n',
        '',
        'tests/test_work_queue_install.py::test_the_hourly_update_never_loads_a_work_queue_that_isnt_loaded[True]',
    ),
    (
        'the installer reads whether a service is running',
        'install.sh',
        '    launchctl print "gui/$UID/$1" 2>/dev/null | grep "state = running" >/dev/null\n',
        '    true\n',
        'tests/test_work_queue_install.py::test_the_hourly_update_never_restarts_a_work_queue_you_stopped[True]',
    ),
    (
        'the help says default mode unloads the Work queue',
        'install.sh',
        '                    Also unloads the Work queue server.\n',
        '',
        'tests/test_work_queue_install.py::test_help_says_default_mode_unloads_the_work_queue',
    ),
    (
        "a Pro install starts a loaded Work queue that isn't running",
        'install.sh',
        '        elif [[ "$feat" == "work-queue" ]] && ! service_running "$label"; then',
        '        elif false; then',
        'tests/test_work_queue_install.py::test_a_pro_install_starts_a_work_queue_that_macos_loaded_but_stopped',
    ),
    (
        'a snooze ends when its time passes',
        'work_queue/page.py',
        "v.state==='snoozed'&&v.until>now",
        "v.state==='snoozed'",
        'tests/test_work_queue_browser.py::test_a_snooze_hides_a_row_only_until_its_time_passes',
    ),
    (
        'selecting Done again undoes it',
        'work_queue/page.py',
        "const body=cur===state?{key:k,state:'clear'}:",
        "const body=false?{key:k,state:'clear'}:",
        'tests/test_work_queue_browser.py::test_done_can_be_undone',
    ),
    (
        'the Apply bar stays disabled with nothing to apply',
        'work_queue/page.py',
        'bar.disabled=!API||run||!(approved+undo);',
        'bar.disabled=false;',
        'tests/test_work_queue_browser.py::test_the_apply_bar_stays_disabled_until_something_can_be_applied',
    ),
    (
        'the Slack helper runs the pinned Playwright',
        'work_queue/tools.py',
        'PLAYWRIGHT = "playwright==1.63.0"',
        'PLAYWRIGHT = "playwright"',
        'tests/test_work_queue_setup.py::test_the_install_hint_uses_the_pinned_playwright',
    ),
    (
        'the Work queue refuses a request for another host',
        'work_queue/server.py',
        'if self.headers.get("Host") not in (',
        'if False and self.headers.get("Host") not in (',
        'tests/test_work_queue_server.py::test_a_request_for_another_host_is_refused',
    ),
    (
        'Send to Assistant puts the safety rules before the row',
        'work_queue/server.py',
        '"detail": f"{SAFETY.replace(\'<td-id>\', td_id)}\\n\\n{row}"',
        '"detail": f"{row}\\n\\n{SAFETY.replace(\'<td-id>\', td_id)}"',
        'tests/test_work_queue_server.py::test_send_creates_an_auto_dispatch_todo_with_the_next_id_and_finish_instructions',
    ),
    (
        "apply skips a row that's already done",
        'work_queue/server.py',
        'if d["decision"] == "approve" and not done:',
        'if d["decision"] == "approve":',
        'tests/test_work_queue_server.py::test_apply_skips_rows_already_in_the_state_asked_for_and_reports_what_it_reads_back',
    ),
    (
        "undo skips a row that isn't done",
        'work_queue/server.py',
        'elif d["decision"] == "undo" and done:',
        'elif d["decision"] == "undo":',
        'tests/test_work_queue_server.py::test_apply_skips_rows_already_in_the_state_asked_for_and_reports_what_it_reads_back',
    ),
    (
        'Gmail apply reports what it reads back',
        'work_queue/server.py',
        '"verified": all(t != undo for t in trashed)',
        '"verified": True',
        'tests/test_work_queue_server.py::test_apply_skips_rows_already_in_the_state_asked_for_and_reports_what_it_reads_back',
    ),
    (
        'Slack undo reopens the save',
        'work_queue/server.py',
        '"mark": "uncompleted" if u else "completed"',
        '"mark": "completed"',
        'tests/test_work_queue_server.py::test_apply_skips_rows_already_in_the_state_asked_for_and_reports_what_it_reads_back',
    ),
    (
        'unsubscribe leaves out senders with mail you keep',
        'work_queue/page.py',
        'm["addr"] not in keep and ',
        '',
        'tests/test_work_queue_refresh.py::test_unsubscribe_offers_only_senders_whose_mail_can_all_go',
    ),
    (
        'a new TODO id counts removed TODOs',
        'work_queue/server.py',
        'for b in (items, doc.get("completed", []), doc.get("removed", []))',
        'for b in (items, doc.get("completed", []))',
        'tests/test_work_queue_server.py::test_a_removed_todo_id_is_never_reused',
    ),
    (
        'a missing TODO store reads as empty',
        'work_queue/todo_result.py',
        '    except FileNotFoundError:\n        return {"items": []}',
        '    except FileNotFoundError:\n        raise',
        'tests/test_work_queue_server.py::test_a_fresh_home_without_a_todo_store_gets_one',
    ),
    (
        'the server writes the TODO store through the shared writer',
        'work_queue/server.py',
        '        write_store(doc)\n',
        '        config.write_json(todostore.todo_path(), doc)\n',
        'tests/test_work_queue_server.py::test_the_server_and_the_result_command_write_the_store_one_way',
    ),
    (
        'an apply that crashes still finishes',
        'work_queue/server.py',
        '    except Exception as exc:  # noqa: BLE001 — record any failure so the page doesn\'t stay on "Working"\n        error = f"Apply stopped: {exc}"[:300]\n    finally:\n',
        '    finally:\n',
        'tests/test_work_queue_server.py::test_an_apply_that_crashes_still_finishes_and_says_why',
    ),
    (
        'two apply clicks start one apply',
        'work_queue/server.py',
        '    with lock:\n        if config.read_json(path("apply-status.json"), {}).get("running"):',
        '    if True:\n        if config.read_json(path("apply-status.json"), {}).get("running"):',
        'tests/test_work_queue_server.py::test_two_apply_clicks_at_the_same_moment_start_one_apply',
    ),
    (
        'two refresh clicks start one refresh',
        'work_queue/server.py',
        '    with lock:\n        if refresh_proc is not None',
        '    if True:\n        if refresh_proc is not None',
        'tests/test_work_queue_server.py::test_two_refresh_clicks_at_the_same_moment_start_one_refresh',
    ),
    (
        'the Work queue port must be 1 to 65535',
        'work_queue/config.py',
        '    if type(value) is not int or not 1 <= value <= 65535:',
        '    if type(value) is not int:',
        'tests/test_work_queue_server.py::test_the_port_must_be_a_whole_number_from_1_to_65535',
    ),
    (
        'an unreadable Work queue setting never breaks the dashboard',
        'bin/render-assistant-page.py',
        '        port = work_queue_config.port(work_queue_config.load())\n    except (OSError, ValueError) as exc:',
        '        port = work_queue_config.port(work_queue_config.load())\n    except ValueError as exc:',
        'tests/test_work_queue_dashboard.py::test_an_unreadable_settings_file_never_breaks_the_dashboard',
    ),
    (
        'unsubscribe never reaches a private address',
        'work_queue/server.py',
        '    if any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in found):',
        '    if False:',
        'tests/test_work_queue_server.py::test_unsubscribe_never_reaches_your_own_machine_or_network',
    ),
    (
        'unsubscribe never follows a redirect',
        'work_queue/server.py',
        '            with opener.open(req, timeout=30) as r:',
        '            with urllib.request.urlopen(req, timeout=30) as r:',
        'tests/test_work_queue_server.py::test_unsubscribe_never_follows_a_redirect',
    ),
    (
        'a snooze time must be a number',
        'work_queue/server.py',
        '        except (TypeError, ValueError, OverflowError):\n            return 400, {"error": "until must be a number"}',
        '        except ZeroDivisionError:\n            return 400, {"error": "until must be a number"}',
        'tests/test_work_queue_server.py::test_a_snooze_until_that_isnt_a_number_is_refused',
    ),
    (
        "a bug in a source reader isn't a source error",
        'work_queue/refresh.py',
        'FAILURES = (tools.SourceError, SlackError, OSError, ValueError)',
        'FAILURES = (tools.SourceError, SlackError, OSError, ValueError, KeyError)',
        'tests/test_work_queue_refresh.py::test_a_bug_in_a_source_reader_isnt_reported_as_that_source_failing',
    ),
    (
        'unlinking Slack deletes its browser profile',
        'bin/work-queue-setup.py',
        '        if args.source == "slack" and config.browser_profile().exists():',
        '        if False:',
        'tests/test_work_queue_setup.py::test_unlinking_slack_also_deletes_its_browser_profile_and_cookies',
    ),
    (
        'one item reads as one item',
        'work_queue/refresh.py',
        'f"read {count} item{\'\' if count == 1 else \'s\'}"',
        'f"read {count} items"',
        'tests/test_work_queue_refresh.py::test_one_item_reads_as_one_item',
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
        cwd=repo, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_SEC)


def run_check(name: str, filename: str, original: str, broken: str, selector: str) -> dict:
    """Run selector on a copy of the sources, then on a copy with the mutation; the mutant must fail.

    A mutant whose test runs past TEST_TIMEOUT_SEC counts as rejected: the test didn't pass."""
    started = time.monotonic()
    with TemporaryDirectory(prefix="assistant-mutation-") as temporary:
        root = Path(temporary)
        repo, home = root / "repo", root / "home"
        repo.mkdir()
        home.mkdir()
        copy_sources(repo)
        try:
            baseline = run_test(repo, home, selector)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{name}: baseline {selector} didn't finish within {TEST_TIMEOUT_SEC} seconds") from None
        if baseline.returncode != 0:
            raise RuntimeError(f"{name}: baseline failed\n{baseline.stdout}\n{baseline.stderr}")
        target = repo / filename
        source = target.read_text()
        if source.count(original) != 1:
            raise RuntimeError(f"{name}: expected one mutation target")
        target.write_text(source.replace(original, broken, 1))
        reject_invalid_shell(name, target)
        try:
            mutant = run_test(repo, home, selector)
        except subprocess.TimeoutExpired:
            outcome, failure = "rejected (timed out)", [f"TIMEOUT {selector} ran past {TEST_TIMEOUT_SEC} seconds"]
        else:
            if mutant.returncode != 1 or "FAILED" not in mutant.stdout:
                raise RuntimeError(f"{name}: mutant was not rejected by {selector}\n{mutant.stdout}\n{mutant.stderr}")
            outcome, failure = "rejected", [line for line in mutant.stdout.splitlines() if line.startswith("FAILED")]
    return {"check": name, "test": selector, "baseline": "passed", "mutant": outcome, "failure": failure,
            "seconds": round(time.monotonic() - started, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [run_check(*check) for check in CHECKS]
    args.output.write_text(json.dumps({"passed": True, "checks": results}, indent=2))
    timed_out = sum(result["mutant"] == "rejected (timed out)" for result in results)
    print(f"{len(results)} targeted mutations rejected ({timed_out} by timing out); production files were not edited.")


if __name__ == "__main__":
    main()
