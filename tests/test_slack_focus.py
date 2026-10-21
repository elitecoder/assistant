"""Tests for src/assistant/slack_focus — the Slack tab's pipeline, end to end
over a fake Slack workspace (tests/slack_focus_fixtures.py). No network, no
model, no gh: every outside call goes through an injected fake."""
from __future__ import annotations

import base64
import io
import json
import subprocess
import urllib.error
from datetime import datetime

import pytest

from assistant.slack_focus import board, client, collect, judge, settings, store, triage
from slack_focus_fixtures import CONFIG, NOW, FakeSlack, ts, write_config


@pytest.fixture
def cfg():
    return settings.parse(CONFIG)


@pytest.fixture
def slack():
    return client.Client("xoxp-test", FakeSlack())


def gh_runner(states=None, merged=None, fail=False):
    """A fake subprocess.run for gh: `pr view` answers from `states`,
    `pr list` answers with `merged`."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if fail:
            raise OSError("no gh")
        if cmd[1:3] == ["pr", "view"]:
            state = (states or {}).get(cmd[3])
            if state is None:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": state}) if state != "junk" else "{", "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(merged or []), "")

    run.calls = calls
    return run


def by_text(threads, text):
    return next(t for t in threads if text in t["first"]["text"])


# ─── settings ────────────────────────────────────────────────────────────────

def test_parse_reads_every_user_setting():
    s = settings.parse(CONFIG | {"title": "My board", "github_hosts": ["git.corp.example"]})
    assert s.configured and s.days == 7 and s.title == "My board"
    assert s.watched_channels == {"C1": "team"}
    assert s.bot_authors == ("Deploy Bot",) and s.skip_channels == ("deployments",)
    assert s.github_repos == ("acme/app",) and s.github_hosts == ("git.corp.example",)
    assert s.jira.enabled and s.jira.base_url == "https://jira.acme.test" and s.jira.project_keys == ("ABC",)
    assert (s.jira.token_env, s.jira.email_env) == ("JIRA_API_TOKEN", "")


def test_parse_falls_back_per_field_on_bad_types():
    s = settings.parse({"days": "7", "title": " ", "watched_channels": ["C1"], "bot_authors": "bot",
                        "jira": "yes", "github_hosts": []})
    assert s.configured
    assert (s.days, s.title, s.watched_channels, s.bot_authors) == (7, "Slack focus", {}, ())
    assert s.github_hosts == ("github.com",) and not s.jira.enabled
    assert settings.parse({"days": True}).days == 7 and settings.parse({"days": 99}).days == 7
    assert settings.parse({"days": 3}).days == 3
    assert not settings.parse(None).configured


def test_load_reads_comms_config(tmp_path):
    assert not settings.load(tmp_path).configured
    write_config(tmp_path, CONFIG)
    assert settings.load(tmp_path).watched_channels == {"C1": "team"}
    (tmp_path / ".assistant/comms/config.json").write_text("[1]")
    assert not settings.load(tmp_path).configured
    (tmp_path / ".assistant/comms/config.json").write_text("{bad")
    assert not settings.load(tmp_path).configured


# ─── client ──────────────────────────────────────────────────────────────────

def test_client_refuses_any_non_read_method(slack):
    with pytest.raises(client.SlackError, match="only reads Slack"):
        slack.call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert "chat.postMessage" not in [m for m, _ in slack._transport.calls]


def test_client_needs_a_token_slack_accepts():
    with pytest.raises(client.SlackError, match="SLACK_USER_TOKEN"):
        client.Client("", FakeSlack())
    with pytest.raises(client.SlackError, match="invalid_auth"):
        client.Client("xoxp-bad", FakeSlack(auth={"ok": False, "error": "invalid_auth"}))
    with pytest.raises(client.SlackError, match="unknown error"):
        client.Client("xoxp-bad", FakeSlack(auth={"ok": False}))


def test_client_identity_and_helpers(slack):
    assert (slack.me, slack.me_name, slack.workspace_url) == ("U1", "Mukul Sharma", "https://acme.slack.com")
    assert slack.user_name("") == ""
    assert slack.channel("D1") == {"name": "DM Aalia Habib", "kind": "dm", "last_read": float(ts(5))}
    assert slack.channel("G1")["kind"] == "mpim" and slack.channel("C1")["name"] == "#team"
    assert slack.mentions_me("hey <@U1>") == "direct"
    assert slack.mentions_me("<!subteam^S1|@leads> look") == "group"
    assert slack.mentions_me("<!subteam^S2|@other> look") is None
    assert slack.clean("<@U2> see <https://x.test|the doc> :tada:  now") == "@Aalia Habib see the doc now"
    assert slack.clean("<!subteam^S1|@leads> hi") == "@group hi"
    assert slack.permalink("C1", "1.000002", "1.000002") == "https://acme.slack.com/archives/C1/p1000002"
    assert slack.permalink("C1", "2.0", "1.0").endswith("?thread_ts=1.0&cid=C1")


def test_client_raises_on_slack_errors_but_skips_gone_threads():
    answers = {"conversations.replies": [{"ok": False, "error": "thread_not_found"}],
               "search.messages": [{"ok": False, "error": "ratelimited"}, {"ok": True, "messages": {}}],
               "conversations.history": [{"ok": False, "error": "missing_scope"}]}
    fake = FakeSlack()

    def transport(method, params, token):
        queue = answers.get(method)
        return queue.pop(0) if queue else fake(method, params, token)

    sleeps = []
    sl = client.Client("t", transport, sleep=sleeps.append)
    answers["users.info"] = [{"ok": False}]
    assert sl.call("conversations.replies", {})["error"] == "thread_not_found"
    assert sl.call("search.messages", {}) == {"ok": True, "messages": {}} and sleeps == [30]
    with pytest.raises(client.SlackError, match="conversations.history failed: missing_scope"):
        sl.call("conversations.history", {})
    with pytest.raises(client.SlackError, match="users.info failed: unknown error"):
        sl.call("users.info", {"user": "U7"})
    answers["search.messages"] = [{"ok": False, "error": "ratelimited"}] * 4
    with pytest.raises(client.SlackError, match="stayed rate limited"):
        sl.call("search.messages", {})
    assert sleeps == [30] * 5


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_transport_retries_rate_limits_and_flaky_reads(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    answers = [urllib.error.HTTPError("u", 429, "slow", {"Retry-After": "2"}, None),
               urllib.error.URLError("reset"),
               _Resp(b'{"ok": true}')]
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), req.data))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    assert client.http_transport("auth.test", {"a": 1}, "xoxp-t") == {"ok": True}
    assert sleeps == [3, 3] and seen[0] == ("https://slack.com/api/auth.test", "Bearer xoxp-t", b"a=1")


def test_http_transport_raises_other_http_errors_and_gives_up(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    def forbidden(req, timeout):
        raise urllib.error.HTTPError("u", 403, "no", {}, None)

    monkeypatch.setattr(client.urllib.request, "urlopen", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        client.http_transport("auth.test", {}, "t")

    def down(req, timeout):
        raise TimeoutError()

    monkeypatch.setattr(client.urllib.request, "urlopen", down)
    with pytest.raises(client.SlackError, match="20 tries"):
        client.http_transport("auth.test", {}, "t")


# ─── collect ─────────────────────────────────────────────────────────────────

def test_collect_buckets_every_thread(slack, cfg):
    progress = []
    doc, mine = collect.collect(slack, cfg, now=NOW, progress=lambda i, n: progress.append((i, n)))
    threads = doc["threads"]
    assert doc["me"] == "U1" and doc["me_name"] == "Mukul Sharma" and doc["days"] == 7
    assert [t["id"] for t in threads] == list(range(1, len(threads) + 1))
    assert [t["last_activity"] for t in threads] == sorted((t["last_activity"] for t in threads), reverse=True)
    buckets = {t["first"]["text"][:20]: t["bucket"] for t in threads}
    assert buckets == {
        "FYI the build is gre": "unread_watched",
        "thread root": "ball_mine_dm",
        "Who owns the path bu": "ball_mine_mention",
        "Deployed @Mukul Shar": "bot",
        "Can you review PR 7?": "waiting_on_others",
        "fyi all, slides are ": "ball_mine_dm",
        "@group please look a": "group_mention",
        "@Mukul Sharma ship i": "replied_elsewhere",
        "Let's split the work": "thread_moved_on",
    }
    dm = next(t for t in threads if t["kind"] == "dm" and t["messages"] == 2 and t["first"]["text"] == "thread root"
              and "Any small" in t["latest"][-1]["text"])
    assert dm["permalink"].startswith("https://acme.slack.com/archives/D1/")
    assert not any(t["first"]["text"] == "old @Mukul Sharma" for t in threads)
    assert progress and progress[0][0] == 1
    assert len(mine) == 6 and mine[0]["cid"] == "C1" and mine[-1]["dm"]


def test_collect_skips_empty_dms_and_deleted_threads(cfg):
    search = {"is:dm": [{"ts": ts(1), "text": "x", "permalink": "https://acme.slack.com/archives/D1/p1",
                         "channel": {"id": "D1"}}],
              "<@U1>": [{"ts": ts(1), "text": "x", "channel": {"id": "C1"},
                         "permalink": f"https://acme.slack.com/archives/C1/p1?thread_ts={ts(2)}&cid=C1"}]}
    sl = client.Client("t", FakeSlack(threads={}, history={}, search=search))
    doc, _ = collect.collect(sl, cfg, now=NOW)
    assert doc["threads"] == []
    assert ("conversations.replies", {"channel": "C1", "ts": ts(2), "limit": 200}) in sl._transport.calls


def test_search_stops_at_its_page_limit():
    fake = FakeSlack(search={"<@U1>": [{"ts": str(n), "text": "x", "channel": {"id": "C1"}} for n in range(6)]})
    sl = client.Client("t", fake)
    assert len(list(sl.search("<@U1>", max_pages=1))) == 2
    assert [p["page"] for m, p in fake.calls if m == "search.messages"] == [1]


def test_bucket_rule_order(cfg):
    base = {"last_author": "Aalia Habib", "channel": "#team", "last_is_me": False, "i_posted_after_in_channel": False,
            "i_reacted_to_last": False, "kind": "channel", "mentioned_after_my_last_reply": None,
            "i_participated": False, "from_others_after_my_last": 0, "unread": False, "watched": False}
    assert collect.bucket(base, cfg) == "read_fyi"
    assert collect.bucket(base | {"unread": True}, cfg) == "unread_other"
    assert collect.bucket(base | {"channel": "#x-deployments"}, cfg) == "bot"
    assert collect.bucket(base | {"i_posted_after_in_channel": True}, cfg) == "replied_elsewhere"


def test_my_later_post_in_the_thread_counts_as_replied(cfg):
    threads = {("C1", ts(1)): [{"user": "U3", "ts": ts(1), "text": "<@U1> can you check?"}]}
    search = {"<@U1>": [{"ts": ts(1), "text": "x", "permalink": "https://acme.slack.com/archives/C1/p1",
                         "channel": {"id": "C1"}}],
              "from:me": [{"ts": ts(0.9), "text": "yes", "channel": {"id": "C1", "name": "team"},
                           "permalink": f"https://acme.slack.com/archives/C1/p2?thread_ts={ts(1)}&cid=C1"}]}
    sl = client.Client("t", FakeSlack(threads=threads, history={}, search=search))
    doc, _ = collect.collect(sl, settings.parse({}), now=NOW)
    assert [t["bucket"] for t in doc["threads"]] == ["replied_elsewhere"]


def test_subscribed_thread_uses_its_own_read_marker():
    threads = {("C1", ts(1)): [{"user": "U3", "ts": ts(1), "text": "news", "subscribed": True,
                                "last_read": ts(0.5)}]}
    search = {"<@U1>": [{"ts": ts(1), "text": "x", "permalink": "https://acme.slack.com/archives/C1/p1",
                         "channel": {"id": "C1"}}]}
    sl = client.Client("t", FakeSlack(threads=threads, history={}, search=search))
    doc, _ = collect.collect(sl, settings.parse({"watched_channels": {"C1": "team"}}), now=NOW)
    assert doc["threads"][0]["unread"] is False and doc["threads"][0]["bucket"] == "read_fyi"


def test_collect_defaults_to_the_current_time(cfg):
    sl = client.Client("t", FakeSlack(threads={}, history={}, search={}))
    doc, mine = collect.collect(sl, cfg)
    assert doc["threads"] == [] and mine == []
    assert abs(datetime.fromisoformat(doc["generated"]).timestamp() - __import__("time").time()) < 60


# ─── triage ──────────────────────────────────────────────────────────────────

def _collected(slack, cfg):
    return collect.collect(slack, cfg, now=NOW)


def test_triage_lanes_promises_and_resolution(slack, cfg):
    doc, mine = _collected(slack, cfg)
    resolver = triage.Resolver(cfg, run=gh_runner({"8": "MERGED", "9": "OPEN"}), env={})
    focus = triage.triage(doc, mine, cfg, resolver)
    lanes = [(i["lane"], i["text"][:25]) for i in focus["active"]]
    assert lanes[0][0] == "Do first"
    assert ("Do first", "Any small features I can ") in lanes
    assert ("Do first", "I think it's walnut") in lanes
    assert ("This week", "fyi all, slides are in th") in lanes  # a quiet group DM waits
    assert ("This week", "anyone?") in lanes
    assert ("Optional", "FYI the build is green ag") in lanes
    promises = [i["text"] for i in focus["active"] if i["lane"] == "Promise"]
    # Newest first; the second message about PR 9 is a repeat, and a question isn't a promise.
    assert promises == ["I'll fix the importer in https://github.com/acme/app/pull/9 today",
                        "I'll send the freeze plan doc by friday"]
    assert [h["why"] for h in focus["handled"]] == ["linked PR merged or closed"]
    assert focus["handled"][0]["text"].startswith("Will merge")
    assert focus["me_name"] == "Mukul Sharma" and focus["counts"]["threads"] == len(doc["threads"])
    order = [i["lane"] for i in focus["active"]]
    assert order == sorted(order, key=triage.LANE_ORDER.index)


def test_triage_resolves_threads_by_pr_or_ticket(cfg):
    def thread(text, bucket="ball_mine_mention"):
        return {"id": 1, "bucket": bucket, "channel": "#team", "kind": "channel", "permalink": "https://a/1",
                "last_author": "Tim", "mentioned_after_my_last_reply": "direct", "last_activity": "2026-10-01T00:00:00",
                "first": {"author": "Tim", "text": text, "at": "x"}, "latest": []}

    resolver = triage.Resolver(cfg, run=gh_runner({"7": "CLOSED"}), env={"JIRA_API_TOKEN": "tok"},
                               urlopen=lambda req, timeout: _Resp(json.dumps(
                                   {"fields": {"status": {"statusCategory": {"key": "done"}}}}).encode()))
    doc = {"days": 7, "me_name": "Mukul Sharma", "threads": [
        thread("see github.com/acme/app/pull/7 please"),
        thread("ABC-12 is the ticket"),
        thread("nothing linked here"),
        thread("ignored lane", bucket="read_fyi")]}
    focus = triage.triage(doc, [], cfg, resolver)
    assert [h["why"] for h in focus["handled"]] == ["linked PR merged or closed", "linked ticket done"]
    assert [i["text"] for i in focus["active"]] == ["nothing linked here"]
    no_jira = settings.parse(CONFIG | {"jira": {"enabled": False, "project_keys": ["ABC"]}})
    focus = triage.triage(doc, [], no_jira, triage.Resolver(no_jira, run=gh_runner({"7": "OPEN"}), env={}))
    assert focus["handled"] == [] and len(focus["active"]) == 3


def test_promises_in_dms_name_the_conversation_kind(cfg):
    mine = [{"cid": "D1", "ts": ts(1), "cname": "U0ABC", "dm": True, "permalink": "https://a/1",
             "text": "I'll send the plan doc to you tomorrow"},
            {"cid": "G1", "ts": ts(2), "cname": "mpdm-a--b-1", "dm": True, "permalink": "https://a/2",
             "text": "I'll share the review notes next week"},
            {"cid": "C1", "ts": ts(3), "cname": None, "dm": False, "permalink": "https://a/3",
             "text": "I'll fix the flaky test in the pipeline"}]
    focus = triage.triage({"days": 7, "me_name": "Mukul Sharma", "threads": []}, mine, cfg,
                          triage.Resolver(cfg, run=gh_runner(), env={}))
    assert [i["channel"] for i in focus["active"]] == ["DM", "group DM", "#"]


def test_noise_hints_never_drop():
    me = "Mukul Sharma"
    t = {"bucket": "ball_mine_mention", "first": {"text": "TL;DR of standup"}, "latest": []}
    assert triage.noise(t, me) == "digest or standup post"
    t = {"bucket": "thread_moved_on", "first": {"text": "x"}, "latest": [{"text": "ok then", "author": "Tim"}]}
    assert triage.noise(t, me) == "discussion, no open question"
    t = {"bucket": "ball_mine_dm", "first": {"text": "x"}, "latest": [{"text": "thanks", "author": "Tim"}]}
    assert triage.noise(t, me) == "trailed off (short ack)"
    t["latest"] = [{"text": "thanks", "author": me}]
    assert triage.noise(t, me) is None


def test_regexes_use_configured_hosts_and_keys():
    assert triage.pr_regex(["git.corp.example"]).findall("https://git.corp.example/o/r/pull/3") == \
        [("git.corp.example", "o/r", "3")]
    assert triage.jira_regex([]) is None
    assert triage.jira_regex(["ABC", "XY"]).findall("ABC-1 and XY-22 not UTF-8") == ["ABC-1", "XY-22"]


def test_resolver_pr_state_cache_and_failures(cfg):
    run = gh_runner({"1": "MERGED", "2": "OPEN", "3": "junk"})
    r = triage.Resolver(cfg, run=run, env={})
    assert r.pr_closed("github.com", "o/r", "1") and r.pr_closed("github.com", "o/r", "1")
    assert len(run.calls) == 1 and run.calls[0][-3:] == ["o/r", "--json", "state"]
    assert not r.pr_closed("github.com", "o/r", "2")
    assert not r.pr_closed("github.com", "o/r", "3")
    assert not r.pr_closed("git.corp.example", "o/r", "4")
    assert run.calls[-1][5] == "git.corp.example/o/r"
    assert triage.Resolver(cfg, run=gh_runner(fail=True), env={}).gh(["x"]) is None


def test_resolver_jira_auth_and_failures(cfg, monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        if "BAD" in req.full_url:
            raise urllib.error.URLError("down")
        return _Resp(json.dumps({"fields": {"status": {"statusCategory": {"key": "indeterminate"}}}}).encode())

    cloud = settings.parse(CONFIG | {"jira": CONFIG["jira"] | {"email_env": "JIRA_EMAIL"}})
    r = triage.Resolver(cloud, env={"JIRA_API_TOKEN": "tok", "JIRA_EMAIL": "me@acme.test"}, urlopen=urlopen)
    assert not r.jira_done("ABC-1") and not r.jira_done("ABC-1") and len(seen) == 1
    assert seen[0] == ("https://jira.acme.test/rest/api/2/issue/ABC-1?fields=status",
                       "Basic " + base64.b64encode(b"me@acme.test:tok").decode())
    assert not r.jira_done("BAD-1")
    env_base = settings.parse(CONFIG | {"jira": {"enabled": True, "project_keys": ["ABC"]}})
    r = triage.Resolver(env_base, env={"JIRA_BASE_URL": "https://env.acme.test/", "JIRA_API_TOKEN": "t",
                                       "JIRA_EMAIL": "someone-else@acme.test"}, urlopen=urlopen)
    r.jira_done("ABC-2")
    assert seen[-1] == ("https://env.acme.test/rest/api/2/issue/ABC-2?fields=status", "Bearer t")
    assert not triage.Resolver(cfg, env={}, urlopen=urlopen).jira_done("ABC-3")  # no token: no request
    assert len(seen) == 3


# ─── judge ───────────────────────────────────────────────────────────────────

def _focus_and_threads(slack, cfg):
    doc, mine = _collected(slack, cfg)
    focus = triage.triage(doc, mine, cfg, triage.Resolver(cfg, run=gh_runner({"8": "OPEN", "9": "OPEN"}), env={}))
    return focus, {t["id"]: t for t in doc["threads"]}


def _answer(focus, lanes):
    """A model answer: keep candidate i with lanes[i]; drop the rest."""
    return json.dumps([{"id": i, "lane": lane, "title": f"title {i}", "why": "because", "effort": "Quick"}
                       for i, lane in lanes.items()])


def test_judge_applies_lanes_and_caches(slack, cfg):
    focus, threads = _focus_and_threads(slack, cfg)
    n = len(focus["active"])
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return "Here you go:\n" + _answer(focus, {0: "Do first", 1: "Read", 2: "Done", 3: "This week", 4: "weird"})

    judged, cache = judge.judge(focus, threads, ["app#5 Fix importer"], None, model)
    assert "Mukul Sharma" in prompts[0] and "app#5 Fix importer" in prompts[0]
    assert sorted(i["text"] for i in judged["active"]) == ["title 0", "title 3", "title 4"]
    kept_weird = next(i for i in judged["active"] if i["text"] == "title 4")
    assert kept_weird["lane"] == focus["active"][4]["lane"]  # an unknown lane keeps triage's
    assert [i["text"] for i in judged["read"]] == ["title 1"]
    assert judged["handled"][-1] == {"text": "title 2", "channel": focus["active"][2]["channel"],
                                     "link": focus["active"][2]["link"], "why": "because"}
    assert len(judged["judge_dropped"]) == n - 5
    assert judged["judge"] == {"ran": True, "respond": 3, "read": 1, "dropped": n - 5, "cached": 0, "rejudged": n}
    assert focus["active"][0]["text"] != "title 0"  # the input isn't mutated

    again, cache2 = judge.judge(focus, threads, ["app#5 Fix importer"], cache, lambda p: pytest.fail("re-judged"))
    assert again["judge"]["cached"] == n and again["active"] == judged["active"]
    assert cache2 == cache

    calls = []
    judge.judge(focus, threads, ["app#6 other"], cache, lambda p: calls.append(p) or _answer(focus, {0: "Do first"}))
    assert calls  # a new merged PR invalidates every cached verdict


def test_judge_rejudges_a_row_whose_text_changed(slack, cfg):
    focus, threads = _focus_and_threads(slack, cfg)
    _, cache = judge.judge(focus, threads, [], None, lambda p: _answer(focus, {0: "Do first"}))
    changed = json.loads(json.dumps(focus))
    changed["active"][0]["text"] = "a brand new reply"
    prompts = []
    judged, _ = judge.judge(changed, threads, [], cache, lambda p: prompts.append(p) or _answer(changed, {0: "Read"}))
    assert len(prompts) == 1 and "a brand new reply" in prompts[0]
    assert judged["judge"]["rejudged"] == 1 and judged["read"][0]["text"] == "title 0"


def test_judge_keeps_raw_rows_when_the_model_fails(slack, cfg):
    focus, threads = _focus_and_threads(slack, cfg)
    for answer in ("", "no json here", "[{bad json]", "[]"):
        judged, cache = judge.judge(focus, threads, [], {"keep": 1}, lambda p: answer)
        assert judged["judge"]["ran"] is False and judged["active"] == focus["active"]
        assert cache == {"keep": 1}
    judged, cache = judge.judge(focus, threads, [], None, lambda p: "")
    assert cache == {}


def test_judge_effort_and_missing_fields(slack, cfg):
    focus, threads = _focus_and_threads(slack, cfg)
    answer = json.dumps([{"id": 0, "lane": "Do first", "title": "t", "effort": "Huge", "why": 7},
                         {"id": 1}, {"id": 2, "title": 42}, {"id": 3, "title": "  "}, "junk",
                         {"id": 4, "lane": "Done", "title": "done", "why": ["x"]}])
    judged, _ = judge.judge(focus, threads, [], None, lambda p: answer)
    assert [i["text"] for i in judged["active"]] == ["t"]
    assert judged["active"][0]["effort"] == "" and judged["active"][0]["why"] == ""
    assert judged["handled"][-1]["why"] == "already done"


def test_usable_cache_rejects_mismatches():
    sig = "s"
    good = {"done_evidence": ["a"], "rubric": sig, "verdicts": {
        "x": "DROP", "y": {"lane": "Read", "title": "t"}, "z": 3, "n": {"title": 5}, "b": {"title": " "}}}
    assert judge.usable_cache(good, ["a"], sig) == {"x": "DROP", "y": {"lane": "Read", "title": "t"}}
    assert judge.usable_cache(good, ["b"], sig) == {}
    assert judge.usable_cache(good, ["a"], "other") == {}
    assert judge.usable_cache(good | {"verdicts": []}, ["a"], sig) == {}
    assert judge.usable_cache([], ["a"], sig) == {}


def test_done_evidence_lists_merged_prs(cfg):
    run = gh_runner(merged=[{"number": 5, "title": "Fix importer"}])
    r = triage.Resolver(cfg, run=run, env={})
    assert judge.done_evidence(cfg, r, 7, datetime(2026, 10, 20)) == ["app#5 Fix importer"]
    assert "merged:>=2026-10-11" in run.calls[0]
    bad = triage.Resolver(cfg, run=lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "{oops", ""), env={})
    assert judge.done_evidence(cfg, bad, 7, datetime(2026, 10, 20)) == []
    failed = triage.Resolver(cfg, run=lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "err"), env={})
    assert judge.done_evidence(cfg, failed, 7, datetime(2026, 10, 20)) == []


# ─── store ───────────────────────────────────────────────────────────────────

def test_fingerprint_changes_with_a_new_reply():
    thread = {"first": {"text": "a", "at": "1"}, "latest": [{"text": "b", "at": "2"}]}
    assert store.signature(thread) == "b|2"
    assert store.signature({"first": {"text": "a", "at": "1"}, "latest": []}) == "a|1"
    assert store.signature(None) == ""
    item = {"link": "https://x/1", "sig": store.signature(thread)}
    fp = store.fingerprint(item)
    assert len(fp) == 64 and fp == store.fingerprint(dict(item))
    thread["latest"].append({"text": "c", "at": "3"})
    assert store.fingerprint(dict(item, sig=store.signature(thread))) != fp
    assert store.fingerprint({"link": "https://x/1"}) == store.fingerprint({"link": "https://x/1", "sig": ""})


def test_fingerprint_matches_the_focus_board_formula():
    """A focus-board done.json keeps working: same link, same last message, same key."""
    import hashlib

    link = "https://acme.slack.com/archives/C1/p1"
    old = hashlib.sha256((link + "||" + "thanks!|2026-10-20T10:00").encode()).hexdigest()
    assert store.fingerprint({"link": link, "sig": "thanks!|2026-10-20T10:00"}) == old


def test_done_marks_and_json_io(tmp_path):
    store.set_done(tmp_path, "a" * 64, True)
    store.set_done(tmp_path, "b" * 64, True)
    store.set_done(tmp_path, "a" * 64, False)
    marks = store.read_json(store.base_dir(tmp_path) / "done.json")
    assert list(marks) == ["b" * 64]
    (store.base_dir(tmp_path) / "done.json").write_text("[]")
    store.set_done(tmp_path, "c" * 64, True)
    assert list(store.read_json(store.base_dir(tmp_path) / "done.json")) == ["c" * 64]
    assert store.read_json(tmp_path / "missing.json", "d") == "d"
    assert [p.name for p in store.base_dir(tmp_path).iterdir() if p.name.startswith(".")] == []


def test_write_json_leaves_no_temp_file_on_failure(tmp_path):
    with pytest.raises(TypeError):
        store.write_json(tmp_path / "x.json", {"bad": object()})
    assert list(tmp_path.iterdir()) == []


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "x.lock"
    with store.locked(path) as first:
        assert first
        with store.locked(path, blocking=False) as second:
            assert second is False
        with store.locked(path, blocking=False, wait=0.2) as waited:
            assert waited is False
    with store.locked(path, blocking=False) as again:
        assert again


def test_lock_wait_takes_a_lock_released_meanwhile(tmp_path):
    import threading

    path = tmp_path / "x.lock"
    holding, release = threading.Event(), threading.Event()

    def hold():
        with store.locked(path):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(5)
    threading.Timer(0.2, release.set).start()
    with store.locked(path, blocking=False, wait=3) as got:
        assert got
    t.join(5)


def test_state_is_live_only_while_a_run_holds_the_lock(tmp_path):
    root = store.base_dir(tmp_path)
    assert store.read_state(tmp_path) == {}
    started = "2026-10-20T10:00:00+00:00"
    at = datetime.fromisoformat(started).timestamp()
    store.write_state(tmp_path, running=True, started=started, phase="Pulling Slack")
    assert store.read_state(tmp_path, now=at + 30)["running"] is True  # still starting up
    with store.locked(root / "refresh.lock"):
        assert store.read_state(tmp_path, now=at + 3600)["running"] is True
    state = store.read_state(tmp_path, now=at + 61)
    assert (state["running"], state["ok"], state["phase"]) == (False, False, None)
    assert "stopped before it finished" in state["error"]
    store.write_state(tmp_path, started=None)
    assert store.read_state(tmp_path)["running"] is False
    store.write_state(tmp_path, started="garbage", error="Slack refused the token")
    assert store.read_state(tmp_path)["error"] == "Slack refused the token"
    (root / "state.json").write_text("[]")
    assert store.write_state(tmp_path, ok=True) == {"ok": True}
    (root / "state.json").write_text("[]")
    assert store.read_state(tmp_path) == {}


# ─── board ───────────────────────────────────────────────────────────────────

def test_board_splits_open_and_checked_off(tmp_path, slack, cfg):
    write_config(tmp_path, CONFIG | {"title": "Mine"})
    view = board.load(tmp_path)
    assert view["configured"] and view["focus"] is None and view["title"] == "Mine"
    focus, threads = _focus_and_threads(slack, cfg)
    root = store.base_dir(tmp_path)
    store.write_json(root / "focus.json", focus)
    first = focus["active"][0]
    assert first["sig"] == store.signature(threads[first["thread_id"]])
    store.set_done(tmp_path, store.fingerprint(first), True)
    # A newer pull renumbers threads.json; the check-off still holds.
    store.write_json(root / "threads.json", {"threads": [dict(t, id=t["id"] + 100) for t in threads.values()]})
    view = board.load(tmp_path)
    assert [d["text"] for d in view["done_by_you"]] == [first["text"]] and view["done_by_you"][0]["done_at"]
    names = [name for name, *_ in view["lanes"]]
    assert names == ["Do first", "Promise", "This week", "Optional"]
    assert view["do_first"] == sum(i["lane"] == "Do first" for i in focus["active"]) - 1
    assert view["judged"] is False and view["handled"] == focus["handled"]


def test_board_ignores_malformed_files(tmp_path):
    root = store.base_dir(tmp_path)
    store.write_json(root / "focus.json", {"active": "nope"})
    assert board.load(tmp_path)["focus"] is None
    store.write_json(root / "focus.json", {"active": [{"lane": "Do first", "link": "https://x", "thread_id": 1}]})
    store.write_json(root / "threads.json", [])
    store.write_json(root / "done.json", [])
    view = board.load(tmp_path)
    assert view["do_first"] == 1 and view["done_by_you"] == [] and not view["configured"]


def test_package_never_names_a_write_method():
    """The only Slack calls are reads; no module even names a write method."""
    import pathlib

    src = pathlib.Path(client.__file__).parent
    text = "".join(p.read_text() for p in src.glob("*.py"))
    import re

    assert not re.search(r"""["'](chat|reactions|files|pins|reminders|stars)\.\w+|conversations\.(mark|join|leave)""",
                         text)
