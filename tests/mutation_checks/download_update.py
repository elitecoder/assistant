"""Checks for the download self-update (bin/app_update.py, its setting in src/assistant/mode.py, and where the pulse and
the page pick it). Every test they name keeps HOME under `tmp_path` and serves GitHub from a stand-in."""

T = "tests/test_app_update.py::"
AU = "bin/app_update.py"
MODE = "src/assistant/mode.py"
PULSE = "bin/pulse.py"
PAGE = "bin/render-assistant-page.py"

CHECKS = (
    (
        "only the JSON value true turns the download self-update on",
        MODE,
        '    return block if isinstance(block, dict) and block.get("download") is True else None',
        '    return block if isinstance(block, dict) and bool(block.get("download")) else None',
        T + "test_it_does_nothing_unless_the_setting_is_true",
    ),
    (
        "with the setting on, the pulse never runs the git self-update",
        PULSE,
        "        if mode.download_updates() is not None:\n",
        "        if False:\n",
        T + "test_with_the_setting_on_the_pulse_downloads_and_never_runs_the_git_updater",
    ),
    (
        "a commit with a conflict marker is refused",
        AU,
        "            if line:\n                return False, f\"conflict marker at {name}:{line}\"\n",
        "            if False:\n                return False, f\"conflict marker at {name}:{line}\"\n",
        T + "test_a_broken_commit_is_refused_and_current_stays",
    ),
    (
        "a commit whose Python won't parse is refused",
        AU,
        '                    compile(source, name, "exec", dont_inherit=True)\n',
        "                    pass\n",
        T + "test_a_broken_commit_is_refused_and_current_stays",
    ),
    (
        "an archive entry with .. is refused",
        AU,
        '        if m.name.startswith("/") or ".." in parts:\n',
        '        if m.name.startswith("/"):\n',
        T + "test_an_archive_that_reaches_outside_its_folder_is_refused",
    ),
    (
        "an archive link pointing outside its folder is refused",
        AU,
        '            if m.linkname.startswith("/") or not (target == top or target.startswith(top + "/")):\n',
        "            if False:\n",
        T + "test_an_archive_that_reaches_outside_its_folder_is_refused",
    ),
    (
        "an archive entry that isn't a file, folder, or link is refused",
        AU,
        "        elif not (m.isfile() or m.isdir()):\n",
        "        elif False:\n",
        T + "test_an_archive_that_reaches_outside_its_folder_is_refused",
    ),
    (
        "a reply that isn't a commit id is refused",
        AU,
        "    if not SHA.fullmatch(sha):\n",
        "    if not sha:\n",
        T + "test_an_answer_that_isnt_a_commit_id_is_a_fetch_failure",
    ),
    (
        "the repo setting must look like owner/name",
        AU,
        "    if not isinstance(slug, str) or not SLUG.fullmatch(slug):\n",
        "    if not isinstance(slug, str):\n",
        T + "test_a_bad_repo_setting_is_reported_without_a_request",
    ),
    (
        "install.sh runs from current, so its links go through current",
        AU,
        '        p = subprocess.run(["bash", str(current / "install.sh"), "--apply"],\n',
        '        p = subprocess.run(["bash", str(current.resolve() / "install.sh"), "--apply"],\n',
        T + "test_the_first_update_unpacks_main_points_current_at_it_and_installs_from_current",
    ),
    (
        "the pulse's install skips reloading the pulse",
        AU,
        '        env["ASSISTANT_SELF_UPDATE"] = "1"\n',
        "        pass\n",
        T + "test_the_first_update_unpacks_main_points_current_at_it_and_installs_from_current",
    ),
    (
        "previous points at the version current replaced",
        AU,
        '    if old and old != sha:\n        _point(root / "previous", old)\n',
        "    if old and old != sha:\n        pass\n",
        T + "test_a_new_commit_moves_previous_and_deletes_older_folders",
    ),
    (
        "prune keeps previous",
        AU,
        '    keep = {target(root / "current"), target(root / "previous")}\n',
        '    keep = {target(root / "current")}\n',
        T + "test_rollback_swaps_current_and_previous_and_installs",
    ),
    (
        "a refused commit isn't downloaded again for a day",
        AU,
        '    if marker.get("rejected") == sha and now - marker.get("rejected_ts", 0) < REJECT_REMIND_SEC:\n',
        "    if False:\n",
        T + "test_a_refused_commit_is_not_downloaded_again_for_a_day",
    ),
    (
        "up to date downloads and installs nothing",
        AU,
        '    if old == sha and (root / sha).is_dir():\n        return result\n',
        "",
        T + "test_up_to_date_downloads_nothing_and_installs_nothing",
    ),
    (
        "the hourly limit holds",
        AU,
        "    if not force and last and now - last < interval_sec:\n",
        "    if False:\n",
        T + "test_it_waits_an_hour_between_tries_unless_forced",
    ),
    (
        "repeated failures pause the download self-update",
        AU,
        "        if paused_until:\n            if log:\n",
        "        if False:\n            if log:\n",
        T + "test_three_identical_failures_pause_it_and_force_still_tries",
    ),
    (
        "a second update can't run while one holds the lock",
        AU,
        "    lock = _locked()\n    if lock is None:\n        return None\n",
        "    lock = open(os.devnull)\n",
        T + "test_a_second_update_while_one_runs_does_nothing",
    ),
    (
        "rollback points current back at previous",
        AU,
        '    _point(root / "current", prev)\n',
        "",
        T + "test_rollback_swaps_current_and_previous_and_installs",
    ),
    (
        "the page's try-now command runs the download updater when it's on",
        PAGE,
        '        script = "bin/app_update.py" if mode.download_updates() else "bin/self_update.py"\n',
        '        script = "bin/self_update.py"\n',
        "tests/test_renderer_failures.py::test_with_download_updates_on_the_command_runs_the_download_updater",
    ),
)
