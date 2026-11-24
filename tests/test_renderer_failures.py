"""The dashboard shows a failure that keeps repeating once, with a count, and says what happens next."""
import html as html_lib
import re
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


def lines(html):
    return [html_lib.unescape(t) for t in re.findall(r'<span class="pulse-text">(.*?)</span>', html)]


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


def test_a_paused_self_update_says_when_it_retries_and_how_to_try_now(mod):
    fail(3, "fetch failed: fatal: couldn't find remote ref feat/x", step=1, cap=4 * HOUR)
    until = failwatch.backed_off("self-update", now=NOW)
    (alert,) = lines(mod.render_failure_alerts(now=NOW))
    when = datetime.fromtimestamp(until).strftime("%H:%M")
    assert alert.endswith(f" It tries again by itself at {when}. To try now, run "
                          f"`{mod.REPO}/bin/self_update.py --force` in a terminal.")
    assert "paused" not in alert


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
    assert alert == ("Assistant has flagged 2 sessions (Fix login, Docs <pass>) as ready to merge 30 times, but neither has a "
                     "pull request linked. Open each session and link its pull request, or tell it the work isn't ready.")
    assert "workspace" not in alert and "pr_refs" not in alert
    assert "Docs &lt;pass&gt;" in mod.render_failure_alerts(now=NOW, world=world)


@pytest.mark.parametrize("count,expected", [(1, "1 session (Fix login) as ready to merge 3 times, but it has no pull request"),
                                            (4, "4 sessions as ready to merge 12 times, but none of them has a pull request")])
def test_merge_check_wording_for_one_and_many(mod, count, expected):
    world = {"workspaces": [{"ws_ref": "workspace:0", "title": "Fix login"}]}
    for i in range(count):
        fail(3, "x", component=f"ready-for-merge:workspace:{i}", pause=False)
    (alert,) = lines(mod.render_failure_alerts(now=NOW, world=world))
    assert expected in alert and "Open the session and" in alert if count == 1 else "Open each session and" in alert


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


def test_at_most_three_lines_then_how_many_more(mod):
    for i in range(6):
        fail(3 + i, f"error {chr(97 + i)}", component=f"model:job{i}", pause=False)
    out = lines(mod.render_failure_alerts(now=NOW))
    assert len(out) == 4
    assert [int(re.search(r"failed (\d+) times", t).group(1)) for t in out[:3]] == [8, 7, 6]
    assert out[3] == "and 3 more failures that keep repeating."


def test_exactly_three_lines_say_nothing_about_more(mod):
    for i in range(3):
        fail(3, f"error {chr(97 + i)}", component=f"model:job{i}", pause=False)
    assert len(lines(mod.render_failure_alerts(now=NOW))) == 3


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
