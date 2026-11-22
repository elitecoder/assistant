"""Each Work queue source reads with your own sign-in, through a fake tool here, and sorts by rules."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from assistant.slack_focus.client import Client
from work_queue import tools, triage
from work_queue.inputs import gcal, github, gmail, outlook, slack
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

NOW = 1_800_000_000.0
DAY = 86400.0
ENTRY = {"team_id": "E1", "workspace_url": "https://acme.slack.com/", "close_after_days": 14}


def ts(days_ago: float) -> str:
    return f"{NOW - days_ago * DAY:.6f}"


def msg(t: str, user: str, text: str, **extra) -> dict:
    return {"ts": t, "user": user, "text": text, **extra}


class FakeSlack:
    """Answers the read-only Web API calls the context step makes."""

    def __init__(self):
        self.channels = {"C1": {"name": "eng"}, "D1": {"is_im": True, "user": "U2"}, "G1": {"is_mpim": True}}
        self.threads: dict[tuple, list] = {}
        self.history: dict[str, list] = {"C1": [], "D1": [], "G1": []}

    def __call__(self, method, params, token):
        if method == "auth.test":
            return {"ok": True, "user_id": "U1", "url": "https://acme.slack.com/"}
        if method == "users.info":
            return {"ok": True, "user": {"real_name": {"U1": "Me", "U2": "Ana", "U3": "Bo"}[params["user"]]}}
        if method == "conversations.info":
            return {"ok": True, "channel": self.channels[params["channel"]]}
        if method == "conversations.replies":
            found = self.threads.get((params["channel"], params["ts"])) or [
                m for (cid, root), thread in self.threads.items() for m in thread
                if cid == params["channel"] and m["ts"] == params["ts"] and root != m["ts"]]
            return {"ok": True, "messages": found} if found else {"ok": False, "error": "thread_not_found"}
        found = [m for m in self.history[params["channel"]]
                 if float(m["ts"]) >= float(params.get("oldest", 0))
                 and (float(m["ts"]) <= float(params["latest"]) if params.get("inclusive") else
                      float(m["ts"]) < float(params.get("latest", 9e12)))]
        return {"ok": True, "messages": sorted(found, key=lambda m: -float(m["ts"]))[:params["limit"]]}


@pytest.fixture
def fake_slack(wq):
    fs = FakeSlack()
    saves = {}

    def save(cid, t):
        saves[f"{cid}:{t}"] = {"item_type": "message", "item_id": cid, "ts": t}

    # last: a channel thread where you wrote the last reply 30 days ago
    root = ts(40)
    fs.threads[("C1", root)] = [msg(root, "U2", "Can you look at the deploy?", reply_count=2, thread_ts=root),
                                msg(ts(35), "U3", "Bump", thread_ts=root), msg(ts(30), "U1", "Done, shipped.", thread_ts=root)]
    save("C1", root)
    # question and moved: you saved a reply inside a thread
    r2 = ts(50)
    fs.threads[("C1", r2)] = [msg(r2, "U2", "Plan for Q3\nsecond line", reply_count=3, thread_ts=r2),
                              msg(ts(49), "U3", "Draft is up", thread_ts=r2), msg(ts(48), "U1", "Looks good", thread_ts=r2),
                              msg(ts(45), "U2", "Who owns step 2?", thread_ts=r2)]
    save("C1", ts(49))
    r3 = ts(60)
    fs.threads[("C1", r3)] = [msg(r3, "U2", "Retro notes", reply_count=2, thread_ts=r3),
                              msg(ts(59), "U1", "Added mine", thread_ts=r3), msg(ts(58), "U3", "Thanks all", thread_ts=r3)]
    save("C1", r3)
    # fyi, stale, ownnote, open: single channel messages
    fs.history["C1"] += [msg(ts(20), "U2", "FYI the wiki moved"), msg(ts(21), "U2", "<@U1> can you approve?"),
                         msg(ts(22), "U1", "note to self: rotate keys"), msg(ts(3), "U2", "new ask"),
                         msg(ts(23), "", "deploy finished", username="deploybot")]
    for days in (20, 21, 22, 3, 23):
        save("C1", ts(days))
    # promise: a DM where your reply in the next two days promised something
    fs.history["D1"] += [msg(ts(30), "U2", "Could you send the numbers?"), msg(ts(29.5), "U1", "I'll send them Monday"),
                         msg(ts(25), "U2", "outside the window")]
    save("D1", ts(30))
    # promise in a group DM with no replies, and a message Slack can't show
    fs.history["G1"] += [msg(ts(40), "U1", "I will write it up")]
    save("G1", ts(40))
    save("G1", ts(41))
    items = list(saves.values()) + [{"item_type": "file", "item_id": "F1", "ts": "1"}]
    fake_tool(wq.bin, "uv", [("slack_browser.py list", {"ok": True, "items": items})])
    return Client("xoxp-test", fs)


def test_slack_rules_sort_every_later_save(fake_slack, wq):
    rows = {r["key"]: r for r in slack.rows(ENTRY, fake_slack, NOW)}
    group = {k: r["group"] for k, r in rows.items()}
    assert group == {
        f"C1:{ts(40)}": "last", f"C1:{ts(49)}": "question", f"C1:{ts(60)}": "moved",
        f"C1:{ts(20)}": "fyi", f"C1:{ts(21)}": "stale", f"C1:{ts(22)}": "ownnote", f"C1:{ts(3)}": "open",
        f"C1:{ts(23)}": "fyi", f"D1:{ts(30)}": "promise", f"G1:{ts(40)}": "promise", f"G1:{ts(41)}": "unread"}
    last = rows[f"C1:{ts(40)}"]
    assert last["title"] == "#eng: Can you look at the deploy?" and last["reason"].endswith("Quiet for 30 days.")
    assert last["evidence"].startswith("Me, ") and last["evidence"].endswith("“Done, shipped.”")
    assert last["link"] == f"https://acme.slack.com/archives/C1/p{ts(40).replace('.', '')}"
    reply = rows[f"C1:{ts(49)}"]
    assert reply["title"] == "#eng: Plan for Q3" and f"thread_ts={ts(50)}" in reply["link"]
    assert reply["ctx"]["root"]["text"].startswith("Plan for Q3") and reply["ctx"]["after_n"] == 2
    dm = rows[f"D1:{ts(30)}"]
    assert dm["where"] == "DM Ana" and [m["text"] for m in dm["ctx"]["after"]] == ["I'll send them Monday"]
    assert "Closing it drops the promise" in dm["reason"]
    assert rows[f"C1:{ts(23)}"]["who"] == "deploybot"
    assert rows[f"G1:{ts(41)}"]["title"] == "group DM: saved message" and rows[f"G1:{ts(41)}"]["reason"] == \
        "Slack can't show this message anymore"
    assert rows[f"C1:{ts(3)}"]["reason"] == "" and rows[f"C1:{ts(21)}"]["ctx"]["msg"]["to_me"]
    assert "--headless" in calls(wq.bin, "uv")[0] and "https://app.slack.com/client/E1/later" in calls(wq.bin, "uv")[0]
    assert "--api-url https://acme.slack.com/api/" in calls(wq.bin, "uv")[0]


def test_slack_question_needs_a_question_mark_or_a_mention(fake_slack):
    rec = {"after": [{"user": "U2", "text": "ping <@U1>", "ts": ts(30), "to_me": True}], "msg": {"user": "U2", "text": "x",
           "ts": ts(31), "to_me": False}, "me_after": True, "kind": "channel"}
    assert slack.classify(rec, "U1", NOW, 14)[0] == "question"
    assert slack.classify(rec, "U1", NOW, 31)[0] == "open"


def test_a_failed_later_list_stops_the_slack_read(wq):
    fake_tool(wq.bin, "uv", [("list", {"ok": False, "error": "Slack sign-in needed"})])
    with pytest.raises(tools.SourceError, match="Slack sign-in needed"):
        slack.later(ENTRY)
    fake_tool(wq.bin, "uv", [("list", {"ok": False})])
    with pytest.raises(tools.SourceError, match="Slack Later list failed"):
        slack.later(ENTRY)


# ─── Gmail ───────────────────────────────────────────────────────────────────

ACCT = "me@example.com"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gthread(subject, frm, labels, body, date_ms, **headers):
    hs = [{"name": "Subject", "value": subject}, {"name": "From", "value": frm}, {"name": "To", "value": ACCT}]
    hs += [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]
    return {"thread": {"messages": [{"labelIds": labels, "internalDate": str(date_ms), "snippet": "snip &amp; more",
                                     "payload": {"headers": hs, **body}}]}}


def gmail_fakes(wq, more=False):
    now_ms = int(datetime.now().timestamp() * 1000)
    old_ms = int(datetime(2020, 3, 4).timestamp() * 1000)
    inbox = [{"id": f"m{n}", "threadId": f"t{n}", "labels": ["INBOX"]} for n in range(1, 8)]
    inbox[0]["labels"].append("UNREAD")
    fake_tool(wq.bin, "gog", [
        ("search in:inbox", {"messages": inbox, "has_more": more}),
        ("search is:unread -in:inbox", {"messages": [{"id": "m1b", "threadId": "t1"}, {"id": "m9", "threadId": "t9",
                                                                                         "labels": ["UNREAD"]}]}),
        ("thread get t1", gthread("Lunch?", "Ana <ana@friends.com>", ["INBOX", "UNREAD"],
                                  {"mimeType": "text/plain", "body": {"data": b64("Are you free?\n\n\nThursday")}}, now_ms)),
        ("thread get t2", gthread("Photos", "Bo <bo@friends.com>", ["INBOX"],
                                  {"mimeType": "multipart/mixed", "parts": [{"mimeType": "image/png"}, {
                                      "mimeType": "text/html", "body": {"data": b64("<style>x{}</style><p>Look</p><br>here")}}]}, old_ms)),
        ("thread get t3", gthread("Read later", f"Me <{ACCT}>", ["INBOX"],
                                  {"mimeType": "multipart/alternative", "parts": [{"mimeType": "multipart/related", "parts": [
                                      {"mimeType": "text/plain", "body": {"data": b64("a link")}}]}]}, now_ms)),
        ("thread get t4", gthread("Weekly digest", "News <news@list.example>", ["INBOX", "CATEGORY_UPDATES"],
                                  {"mimeType": "text/html", "body": {"data": b64("<div>Top stories</div>")}}, now_ms,
                                  list_unsubscribe="<mailto:u@list.example>, <https://list.example/u?id=1>",
                                  list_unsubscribe_post="List-Unsubscribe=One-Click")),
        ("thread get t5", gthread("Your order has shipped", "Shop <orders@shop.example>", ["INBOX", "UNREAD"],
                                  {"mimeType": "text/plain"}, now_ms)),
        ("thread get t6", gthread("Big sale", "Promo <list@promo.example>", ["INBOX", "CATEGORY_PROMOTIONS", "STARRED"],
                                  {}, now_ms, list_unsubscribe="<https://promo.example/u>")),
        ("thread get t7", gthread("Flash sale", "deals@promo.example", ["INBOX", "CATEGORY_PROMOTIONS"], {}, now_ms)),
        ("thread get t9", gthread("Your alert", "Bot <alerts@bot.example>", ["UNREAD"], {}, now_ms)),
    ])
    return old_ms


def test_gmail_rules_sort_each_conversation_and_build_trash_and_unsubscribe_data(wq):
    gmail_fakes(wq)
    rows = {r["key"]: r for r in gmail.rows(ACCT)}
    by_tid = {r["link"].rsplit("/", 1)[1]: r for r in rows.values()}
    assert {t: r["group"] for t, r in by_tid.items()} == {
        "t1": "act", "t2": "unsorted", "t3": "self", "t4": "news", "t5": "unread", "t6": "news", "t7": "note",
        "t9": "unread"}
    assert by_tid["t1"]["msg_ids"] == ["m1"] and by_tid["t9"]["msg_ids"] == ["m9"] and by_tid["t1"]["body"] == "Are you free?\nThursday"
    assert by_tid["t1"]["from"] == "Ana" and by_tid["t1"]["unread"] and by_tid["t1"]["to"] == [ACCT]
    assert "Look" in by_tid["t2"]["body"] and "x{}" not in by_tid["t2"]["body"] and by_tid["t2"]["day"] == "Mar 4, 2020"
    assert by_tid["t3"]["body"] == "a link"
    assert by_tid["t4"]["unsub_url"] == "https://list.example/u?id=1" and by_tid["t6"]["unsub_url"] == ""
    assert by_tid["t5"]["body"] == "snip & more" and by_tid["t5"]["reason"].startswith("You never opened it")
    assert by_tid["t9"]["link"] == f"https://mail.google.com/mail/?authuser={ACCT}#all/t9"
    assert all(r["tid"] == t for t, r in by_tid.items()), "each row names its thread, for Archive and Mark read"
    assert [t for t, r in by_tid.items() if r["archived"]] == ["t9"], "only mail outside the inbox reads as archived"
    assert by_tid["t7"]["from"] == "deals@promo.example" and by_tid["t6"]["from"] == "Promo"
    assert all(f"-a {ACCT} -j" in c for c in calls(wq.bin, "gog"))


def test_gmail_reuses_unchanged_conversations_and_refuses_a_truncated_read(wq):
    gmail_fakes(wq)
    gmail.rows(ACCT)
    first = len([c for c in calls(wq.bin, "gog") if "thread get" in c])
    assert first == 8
    gmail.rows(ACCT)
    assert len([c for c in calls(wq.bin, "gog") if "thread get" in c]) == first
    gmail_fakes(wq, more=True)
    with pytest.raises(tools.SourceError, match="more than 1000 messages"):
        gmail.rows(ACCT)


# ─── Outlook ─────────────────────────────────────────────────────────────────

ME = "me@corp.example"
TODAY = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


def om(mid, subject, sender, to=(ME,), conv="c0", days=1, **extra):
    return {"id": mid, "subject": subject, "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
            "toRecipients": [{"emailAddress": {"name": t.split("@")[0], "address": t}} for t in to],
            "ccRecipients": [{"emailAddress": {"name": "Cy", "address": "cy@corp.example"}}],
            "receivedDateTime": (TODAY - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conversationId": conv, "webLink": f"https://outlook.office.com/{mid}", "bodyPreview": "preview", **extra}


def outlook_fakes(wq, failing_body=False):
    filler = [om(f"f{n}", "Old note", "ana@corp.example", to=("x@corp.example",), days=90, isRead=True) for n in range(97)]
    inbox = [om("m1", "Can you review?", "ana@corp.example", conv="c1"),
             om("m2", "Re: plan", "bo@corp.example", conv="c2"),
             om("m3", "[repo] PR merged", "notifications@github.com"),
             om("m4", "Accepted: Sync", "ana@corp.example"),
             om("m5", "Newsletter", "news@corp.example", inferenceClassification="other"),
             om("m6", "Last quarter", "bo@corp.example", to=("x@corp.example",), days=60, isRead=True),
             om("m7", "Hi", "ana@corp.example", to=("x@corp.example",), isRead=False)] + filler
    body_ok = {"uniqueBody": {"contentType": "html", "content": "<p>Please&nbsp;review</p><br>  thanks"}}
    fake_tool(wq.bin, "npx", [
        ("receivedDateTime%20lt%20", {"value": inbox[100:]}),
        ("mailFolders/inbox/messages", {"value": inbox[:100]}),
        ("sentitems", {"value": [{"conversationId": "c2"}]}),
        ("-u /me/messages/m1?", json.dumps(body_ok) + "\n" + "\n".join(
            json.dumps({"body": {"contentType": "text", "content": f"text {n}"}}) for n in (2, 3, 4))),
        ("-u /me/messages/m5?", {"results": [{"statusCode": 200, "data": {"body": {"content": "five"}}},
                                             {"statusCode": 429, "error": "throttled"},
                                             {"statusCode": 200, "data": {}},
                                             {"statusCode": 200, "data": {"body": {"content": "f0"}}}]}),
        ("-u /me/messages/m6?", {"results": [{"statusCode": 429}]} if failing_body else
         {"results": [{"statusCode": 200, "data": {"body": {"content": "six"}}}]}),
        ("-u /me/messages/f", "\n".join(json.dumps({"body": {"content": "filler"}}) for _ in range(4))),
    ])
    return inbox


def test_outlook_rules_sort_the_inbox_and_retry_throttled_bodies(wq):
    outlook_fakes(wq)
    waits = []
    rows = {r["link"].rsplit("/", 1)[1]: r for r in outlook.rows({"address": ME}, TODAY, waits.append)}
    assert [rows[m]["group"] for m in ("m1", "m2", "m3", "m4", "m5", "m6", "m7")] == \
        ["act", "read", "gh", "cal", "bulk", "old", "read"]
    assert rows["m1"]["body"] == "Please review\nthanks" and rows["m2"]["body"] == "text 2"
    assert (rows["m5"]["body"], rows["m6"]["body"], rows["m7"]["body"], rows["f0"]["body"]) == ("five", "six", "preview", "f0")
    assert rows["m6"]["reason"].startswith("You've kept this since") and rows["m1"]["cc"] == ["Cy"]
    assert waits == [2]
    assert len(rows) == 104 and rows["m7"]["unread"]
    assert any("receivedDateTime%20lt%20" in c for c in calls(wq.bin, "npx"))
    assert any("sentDateTime%20ge%20" in c for c in calls(wq.bin, "npx"))


def test_outlook_reuses_bodies_and_fails_loudly(wq):
    outlook_fakes(wq)
    outlook.rows({"address": ME}, TODAY, lambda s: None)
    before = len([c for c in calls(wq.bin, "npx") if "/me/messages/" in c])
    outlook.rows({"address": ME}, TODAY, lambda s: None)
    assert len([c for c in calls(wq.bin, "npx") if "/me/messages/" in c]) == before
    (wq.data / "work/outlook.json").unlink()
    outlook_fakes(wq, failing_body=True)
    with pytest.raises(tools.SourceError, match="couldn't read 1 email bodies"):
        outlook.rows({"address": ME}, TODAY, lambda s: None)
    fake_tool(wq.bin, "npx", [("inbox", {"results": [{"statusCode": 403, "error": "Forbidden"}]})])
    with pytest.raises(tools.SourceError, match="Work IQ couldn't read /me/mailFolders/inbox/messages: Forbidden"):
        outlook.rows({"address": ME}, TODAY)
    fake_tool(wq.bin, "npx", [("inbox", '{"value": []}{"value": []}')])
    with pytest.raises(tools.SourceError, match="returned 2 answers for 1 requests"):
        outlook.rows({"address": ME}, TODAY)


# ─── GitHub and Calendar ─────────────────────────────────────────────────────

def test_github_lists_review_requests_and_skips_authors(wq):
    pr = {"number": 7, "title": "Fix it", "url": "https://github.com/o/r/pull/7", "repository": {"nameWithOwner": "o/r"},
          "author": {"login": "ana"}, "createdAt": "2026-11-02T10:00:00Z", "isDraft": True}
    fake_tool(wq.bin, "gh", [("search prs", [pr, {**pr, "number": 8, "author": {"login": "bot"}}])])
    rows = github.rows({"skip_authors": ["bot"]})
    assert rows == [{"key": pr["url"], "link": pr["url"], "draft": True, "title": "o/r#7: Fix it",
                     "meta": "Review requested · ana · opened Nov 2"}]
    assert "--review-requested=@me --state=open" in calls(wq.bin, "gh")[0]
    github.rows({"host": "ghe.example.com"})
    assert (wq.bin / "gh.env").read_text().splitlines() == ["", "ghe.example.com"]


def test_calendar_lists_todays_events(wq):
    fake_tool(wq.bin, "gog", [("calendar events", {"events": [
        {"summary": "Standup", "start": {"dateTime": "2026-11-18T17:00:00Z"}, "htmlLink": "https://calendar.google.com/e1",
         "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"start": {"date": "2026-11-18"}},
        {"summary": "Gone", "status": "cancelled", "start": {"date": "2026-11-18"}}]})])
    rows = gcal.rows(ACCT)
    assert [(r["title"], r["declined"], r["link"]) for r in rows] == [
        ("Standup", True, "https://calendar.google.com/e1"), ("(no title)", False, "")]
    assert rows[1]["when"] == "All day" and rows[0]["when"].endswith(("AM", "PM"))
    assert calls(wq.bin, "gog") == [f"calendar events primary --today --max 50 -a {ACCT} -j"]


def test_tool_failures_say_what_failed(wq):
    with pytest.raises(tools.ToolMissing, match="gh isn't installed"):
        tools.run(["gh", "auth", "status"])
    fake_tool(wq.bin, "gh", [("auth", {"exit": 4, "stderr": "not logged in"})])
    with pytest.raises(tools.SourceError, match="gh auth status failed: not logged in"):
        tools.run(["gh", "auth", "status"])
    fake_tool(wq.bin, "gh", [("auth", "not json")])
    with pytest.raises(tools.SourceError, match="didn't return JSON"):
        tools.run_json(["gh", "auth", "status"])
    fake_tool(wq.bin, "slow", [])
    (wq.bin / "slow").write_text("#!/bin/sh\nsleep 5\n")
    with pytest.raises(tools.SourceError, match="didn't answer within 1 seconds"):
        tools.run(["slow"], timeout=1)


def test_a_slack_title_shows_as_written_and_the_model_gets_it_without_secrets(wq):
    fs = FakeSlack()
    fs.history["C1"].append(msg(ts(3), "U2", "x" * 80 + " ghp_" + "a" * 30))
    fake_tool(wq.bin, "uv", [("slack_browser.py list", {"ok": True, "items": [
        {"item_type": "message", "item_id": "C1", "ts": ts(3)}]})])
    row = slack.rows(ENTRY, Client("xoxp-test", fs), NOW)[0]
    assert row["title"] == "#eng: " + "x" * 80 + " ghp_" + "a" * 14 + "…", "the page shows the title as written"
    sent = triage.open_items({"slack": [row]})[0]["title"]
    assert sent == "#eng: " + "x" * 80 + " [removed]", "the model's copy loses the token before it's cut"


def test_a_long_tool_error_keeps_its_start_and_its_last_line():
    traceback = ("Traceback (most recent call last):\n" + "".join(f'  File "/x/mod{n}.py", line {n}, in f\n    call()\n'
                                                                   for n in range(20))
                 + "playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at /x/chrome")
    flat = tools.one_line(traceback)
    assert "\n" not in flat and flat.startswith("Traceback (most recent call last): File")
    assert flat.endswith("playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at /x/chrome")
    assert " … " in flat and len(flat) <= 300
    short = "Error: the client ID JSON is missing.\n  try again"
    assert tools.one_line(short) == "Error: the client ID JSON is missing. try again"


def test_a_long_last_line_keeps_only_its_last_150_characters():
    last = "".join(str(n % 10) for n in range(400))
    flat = tools.one_line("Error: the first sentence.\n" + "x " * 200 + "\n" + last)
    assert flat.startswith("Error: the first sentence. x x") and flat.endswith(" … " + last[-150:])
    assert len(flat) <= 300 and last[:-150] not in flat
