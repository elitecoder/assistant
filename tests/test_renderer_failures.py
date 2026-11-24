"""The dashboard shows a failure that keeps repeating once, with a count, and says what happens next."""
import html as html_lib
import re
import shlex
import sys
from pathlib import Path
from datetime import datetime

import pytest

from assistant import failwatch
from test_renderer_in_process import load_module

NOW = datetime(2026, 11, 20, 12, 0).timestamp()  # a Friday, noon local time
HOUR = 3600


@pytest.fixture
def mod(tmp_path):
    (tmp_path / ".assistant").mkdir()
    return load_module(tmp_path)


def fail(n, text, component="self-update", start=NOW - 3600, step=60, **kw):
    for i in range(n):
        failwatch.record_failure(component, text, now=start + i * step, **kw)


def box(html):
    """(shown lines, summary, hidden lines) of the one failure box, as plain text."""
    assert html.count('data-failure="repeated"') <= 1, "one box, not a stack"
    inner = re.search(r'<span class="pulse-text">(.*)</span></div>$', html, re.S).group(1)
    details = re.search(r'<details data-context-key="repeated-failures"><summary>(.*?)</summary>(.*)</details>', inner, re.S)
    top = inner[:details.start()] if details else inner

    def split(chunk):
        return [html_lib.unescape(re.sub(r"<[^>]+>", "", part)) for part in chunk.split("<br>") if part]
    return split(top), (html_lib.unescape(details.group(1)) if details else None), split(details.group(2)) if details else []


def lines(html):
    if not html:
        return []
    shown, _summary, hidden = box(html)
    return shown + hidden


def test_nothing_shows_below_three_in_a_row(mod):
    fail(2, "boom")
    assert mod.render_failure_alerts(now=NOW) == ""
    fail(1, "boom", start=NOW - 3000)
    assert "has failed 3 times in a row" in mod.render_failure_alerts(now=NOW)


def test_a_new_failure_after_a_recovery_does_not_show_the_old_count_or_date(mod):
    fail(15, "boom", start=NOW - 4 * 86400, step=60)
    failwatch.record_ok("self-update")
    failwatch.record_failure("self-update", "boom", now=NOW - 60)
    assert mod.render_failure_alerts(now=NOW) == ""


def test_the_alert_counts_the_current_run_and_says_when_it_began(mod):
    fail(2, "boom", component="model:lessons", start=NOW - 5 * 86400, pause=False)  # an old episode of the same error
    failwatch.record_ok("model:lessons")
    start = datetime(2026, 11, 17, 9, 10).timestamp()  # the Tuesday this run began
    fail(13, "boom", component="model:lessons", start=start, step=1, pause=False)
    fail(1, "boom", component="model:lessons", start=NOW - 60, pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert alert.startswith("Lesson extraction has failed 14 times in a row since Tuesday 09:10. The error was: boom.")


def test_the_date_shows_once_the_run_is_more_than_six_days_old(mod):
    fail(3, "boom", component="dashboard-render", start=datetime(2026, 11, 13, 9, 10).timestamp(), step=1, pause=False)
    fail(1, "boom", component="dashboard-render", start=NOW - 30, pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert "since Friday 13 November 09:10." in alert
    fail(3, "other", component="model:strategist", start=datetime(2026, 11, 14, 9, 10).timestamp(), step=1, pause=False)
    fail(1, "other", component="model:strategist", start=NOW - 20, pause=False)
    assert any("since Saturday 09:10." in a for a in lines(mod.render_failure_alerts(now=NOW)))


def test_a_paused_self_update_gives_a_command_that_works_when_pasted(mod):
    fail(3, "fetch failed: fatal: couldn't find remote ref feat/x", step=1, cap=4 * HOUR)
    until = failwatch.backed_off("self-update", now=NOW)
    html = mod.render_failure_alerts(now=NOW)
    (alert,) = lines(html)
    when = datetime.fromtimestamp(until).strftime("%H:%M")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(mod.REPO / 'bin/self_update.py'))} --force"
    assert alert.endswith(f" It tries again by itself at {when}, or as soon as the branch or remote it updates from changes. "
                          f"To try now, run {command} in a terminal.")
    assert f"<code>{html_lib.escape(command)}</code>" in html and "`" not in html


def test_the_command_quotes_an_install_folder_with_a_space(mod, monkeypatch):
    monkeypatch.setattr(mod, "REPO", Path("/Users/me/My Assistant"))
    monkeypatch.setattr(mod.sys, "executable", "/opt/my python/bin/python3")
    fail(3, "boom", step=1, cap=4 * HOUR)
    html = mod.render_failure_alerts(now=NOW)
    command = "'/opt/my python/bin/python3' '/Users/me/My Assistant/bin/self_update.py' --force"
    assert f"<code>{html_lib.escape(command)}</code>" in html
    assert shlex.split(command) == ["/opt/my python/bin/python3", "/Users/me/My Assistant/bin/self_update.py", "--force"]


def test_a_paused_background_job_says_nothing_needs_doing(mod):
    fail(3, "boom", component="model:strategist", step=1)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert alert.endswith(" You don't need to do anything unless it keeps failing.")
    assert " It tries again at " in alert and "self_update" not in alert


def test_nothing_about_a_pause_when_there_is_none(mod):
    fail(4, "boom", component="dashboard-render", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert alert.endswith("The error was: boom.") and "tries again" not in alert


def test_the_next_step_sentence_goes_once_the_pause_is_over(mod):
    fail(3, "boom", component="model:triage", start=NOW - 3 * HOUR, step=1)  # paused for an hour, 2 hours ago
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert alert.endswith("The error was: boom.") and "tries again" not in alert


def test_the_error_is_short_and_clean(mod):
    fail(3, 'Error code: 529 {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}} '
            + "very long detail " * 30, pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    error = alert.split("The error was: ")[1]
    assert "{" not in error and "overloaded" not in error and error.startswith("Error code: 529 very long detail")
    assert len(error) <= 102 and error.endswith("…."), error


def test_an_error_that_is_only_json_still_says_something(mod):
    fail(3, '{"a": 1}', pause=False)
    assert "The error was: no details were given." in lines(mod.render_failure_alerts(now=NOW))[0]


def test_the_error_is_html_escaped(mod):
    fail(3, "<script>alert(1)</script> & <b>x</b>", pause=False)
    html = mod.render_failure_alerts(now=NOW)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "&amp;" in html


def test_merge_checks_share_one_line_with_session_titles_and_no_ids(mod):
    world = {"workspaces": [{"ws_ref": "workspace:7", "title": "Fix login"}, {"ref": "workspace:8", "title": "Docs <pass>"},
                            {"ws_ref": "workspace:9"}, {"title": "no ref"}]}
    fail(15, "ready_for_merge but no pr_refs on ws", component="ready-for-merge:workspace:7", pause=False)
    fail(15, "ready_for_merge but no pr_refs on ws", component="ready-for-merge:workspace:8", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world=world))
    assert alert == ("Assistant has flagged 2 sessions (Fix login, Docs <pass>) as ready to merge, but neither has a "
                     "pull request linked. Open each session and link its pull request, or tell it the work isn't ready.")
    assert "workspace" not in alert and "pr_refs" not in alert and "times" not in alert
    assert "Docs &lt;pass&gt;" in mod.render_failure_alerts(now=NOW, world=world)


def test_four_sessions_with_two_titles_give_only_the_count(mod):
    world = {"workspaces": [{"ws_ref": "workspace:0", "title": "A"}, {"ws_ref": "workspace:1", "title": "B"}]}
    for i in range(4):
        fail(3, "x", component=f"ready-for-merge:workspace:{i}", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world=world))
    assert alert.startswith("Assistant has flagged 4 sessions as ready to merge, but none of them has a pull request")
    fail(3, "x", component="ready-for-merge:workspace:9", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world={"workspaces": [
        {"ws_ref": f"workspace:{i}", "title": f"T{i}"} for i in (0, 1, 2, 3, 9)]}))
    assert "5 sessions as ready" in alert and "(" not in alert, "more than three are never named"


def test_two_sessions_one_titled_give_only_the_count(mod):
    fail(3, "x", component="ready-for-merge:workspace:0", pause=False)
    fail(3, "x", component="ready-for-merge:workspace:1", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world={"workspaces": [{"ws_ref": "workspace:0", "title": "A"}]}))
    assert alert.startswith("Assistant has flagged 2 sessions as ready to merge, but neither")


@pytest.mark.parametrize("count,expected", [(1, "1 session (Fix login) as ready to merge, but it has no pull request"),
                                            (4, "4 sessions as ready to merge, but none of them has a pull request")])
def test_merge_check_wording_for_one_and_many(mod, count, expected):
    world = {"workspaces": [{"ws_ref": "workspace:0", "title": "Fix login"}]}
    for i in range(count):
        fail(3, "x", component=f"ready-for-merge:workspace:{i}", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world=world))
    assert expected in alert
    assert ("Open the session and" if count == 1 else "Open each session and") in alert


def test_a_session_without_a_known_title_is_never_named_by_id(mod):
    fail(3, "x", component="ready-for-merge:workspace:7", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    assert alert.startswith("Assistant has flagged 1 session as ready") and "workspace" not in alert


def test_many_merge_rows_do_not_push_self_update_out(mod):
    for i in range(10):
        fail(20, "x", component=f"ready-for-merge:workspace:{i}", pause=False)
    fail(3, "fetch failed", step=1)
    text = "".join(lines(mod.render_failure_alerts(now=NOW)))
    assert "Self-update has failed" in text and "10 sessions" in text


def test_two_lines_show_and_the_rest_sit_in_one_closed_details(mod):
    for i in range(6):
        fail(3 + i, f"error {chr(97 + i)}", component=f"model:job{i}", pause=False)
    html = mod.render_failure_alerts(now=NOW)
    shown, summary, hidden = box(html)
    assert [int(re.search(r"failed (\d+) times", t).group(1)) for t in shown] == [8, 7]
    assert summary == "and 4 more that keep repeating" and len(hidden) == 4
    assert '<details data-context-key="repeated-failures">' in html, "the key lets the page reopen it after a refresh"
    assert " open" not in html.split("<details", 1)[1].split(">", 1)[0] and "<script" not in html
    assert html.count('role="alert"') == 1


def test_two_lines_say_nothing_about_more(mod):
    for i in range(2):
        fail(3, f"error {chr(97 + i)}", component=f"model:job{i}", pause=False)
    html = mod.render_failure_alerts(now=NOW)
    assert len(lines(html)) == 2 and "<details" not in html


def test_the_grouped_merge_line_counts_as_one_line(mod):
    for i in range(5):
        fail(3, "x", component=f"ready-for-merge:workspace:{i}", pause=False)
    fail(3, "a", component="model:a", pause=False)
    shown, summary, hidden = box(mod.render_failure_alerts(now=NOW))
    assert len(shown) == 2 and summary is None and hidden == []


def test_a_paused_audit_says_how_to_clear_it_but_a_model_job_that_knows_its_model_does_not(mod):
    fail(3, "boom", component="model:observer-audit", step=1)
    fail(3, "boom", component="model:strategist", step=1)
    text = {t.split(" has failed")[0]: t for t in lines(mod.render_failure_alerts(now=NOW))}
    reset = " If you've fixed the cause, delete ~/.assistant/failures.json and it tries again at its next run."
    assert text["The Observer audit"].endswith(reset)
    assert reset not in text["The strategist"]


def test_friendly_names_and_unknown_components(mod):
    fail(3, "a", component="model:lessons", pause=False)
    fail(3, "c", component="something-new", pause=False)
    fail(3, "d", component="model:newjob", pause=False)
    text = " ".join(lines(mod.render_failure_alerts(now=NOW)))
    assert "Lesson extraction has failed" in text
    assert "A background job (something-new) has failed" in text
    assert "A background job (newjob) has failed" in text and "model:newjob" not in text


def test_default_time_is_now(mod):
    fail(3, "boom", start=datetime.now().timestamp() - 100, step=1, pause=False)
    assert "Self-update has failed 3 times" in mod.render_failure_alerts()


def test_the_alert_is_on_the_page_at_the_top(tmp_path, monkeypatch):
    from test_pro_mode import render_page
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    assert "has failed" not in render_page(tmp_path)
    fail(3, "boom", start=datetime.now().timestamp() - 100, step=1, pause=False)
    page = render_page(tmp_path, workspaces=[{"ws_ref": "workspace:1", "title": "Fix login"}])
    assert page.count("Self-update has failed 3 times") == 1
    assert page.index("Self-update has failed") < page.index('<div class="tabs">')


def test_corrupt_rows_in_the_store_are_left_out_and_the_rest_show(mod, tmp_path):
    import json, os
    fail(3, "boom", component="model:triage", pause=False)
    path = Path(os.environ["ASSISTANT_FAILURES_FILE"])
    data = json.loads(path.read_text())
    data["a"] = {"component": "self-update", "streak": 5, "count": 5, "last_ts": NOW, "cap": "x"}
    data["b"] = {"component": "self-update", "streak": 4, "count": 4, "last_ts": NOW, "first_ts": None}
    data["c"] = {"component": "ready-for-merge:workspace:1", "streak": "lots", "count": 4, "last_ts": NOW}
    data["d"] = {"component": "model:lessons", "streak": 4, "count": 4, "last_ts": NOW, "backoff_until": "soon"}
    path.write_text(json.dumps(data))
    assert [t.split(" has failed")[0] for t in lines(mod.render_failure_alerts(now=NOW))] == ["Triage"]


def test_a_bad_workspace_entry_or_world_never_raises(mod):
    fail(3, "x", component="ready-for-merge:workspace:1", pause=False)
    for world in ({"workspaces": ["junk", None, 5, {"ws_ref": "workspace:1", "title": "Fix login"}]},
                  {"workspaces": "nope"}, {"workspaces": None}, "not a dict", []):
        out = mod.render_failure_alerts(now=NOW, world=world)
        assert "Assistant has flagged 1 session" in out


def test_any_other_error_means_no_box_and_the_page_still_renders(mod, monkeypatch, tmp_path):
    fail(3, "boom", component="model:triage", pause=False)
    monkeypatch.setattr(mod.failwatch, "repeated", lambda **kw: 1 / 0)
    assert mod.render_failure_alerts(now=NOW) == ""
    from test_pro_mode import render_page
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir(exist_ok=True)
    assert "Triage has failed" not in render_page(tmp_path)
