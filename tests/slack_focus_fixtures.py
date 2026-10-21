"""A small fake Slack workspace for the Slack focus tests. No network."""
from __future__ import annotations

import json

NOW = 1_790_000_000.0
DAY = 86400


def ts(days_ago: float, n: int = 0) -> str:
    return f"{NOW - days_ago * DAY:.0f}.{n:06d}"


USERS = {"U1": "Mukul Sharma", "U2": "Aalia Habib", "U3": "Tim Beauchamp", "U4": "Deploy Bot"}
CHANNELS = {
    "C1": {"name": "team", "last_read": ts(3)},
    "C2": {"name": "prod-deployments", "last_read": ts(0)},
    "D1": {"is_im": True, "user": "U2", "last_read": ts(5)},
    "G1": {"is_mpim": True, "last_read": ts(5)},
}


def msg(user, when, text, **extra):
    return {"user": user, "ts": when, "text": text, **extra}


class FakeSlack:
    """transport(method, params, token) over a fixed set of threads."""

    def __init__(self, threads=None, history=None, search=None, auth=None):
        self.calls: list[tuple[str, dict]] = []
        self.threads = threads if threads is not None else default_threads()
        self.history = history if history is not None else default_history()
        self.search_results = search if search is not None else default_search()
        self.auth = auth or {"ok": True, "user_id": "U1", "url": "https://acme.slack.com/"}
        self.page_size = 0  # when set, DM history and replies come back in pages

    def _paged(self, msgs, params, parent_on_every_page=False):
        if not self.page_size:
            return {"messages": msgs}
        head, rest = (msgs[:1], msgs[1:]) if parent_on_every_page else ([], msgs)
        start = int(params.get("cursor") or 0)
        more = start + self.page_size < len(rest)
        return {"messages": head + rest[start:start + self.page_size],
                "response_metadata": {"next_cursor": str(start + self.page_size) if more else ""}}

    def __call__(self, method, params, token):
        self.calls.append((method, dict(params)))
        if method == "auth.test":
            return self.auth
        if method == "users.info":
            return {"user": {"real_name": USERS.get(params["user"], "")}}
        if method == "conversations.info":
            return {"channel": dict(CHANNELS.get(params["channel"], {"name": params["channel"]}))}
        if method == "usergroups.users.list":
            return {"users": ["U1"] if params["usergroup"] == "S1" else ["U9"]}
        if method == "search.messages":
            query = params["query"]
            key = next(k for k in ("from:me", "is:dm", "to:me", "<@U1>") if k in query)
            matches = self.search_results.get(key, [])
            page = params["page"]
            chunk = matches[(page - 1) * 2:page * 2]
            pages = max(1, (len(matches) + 1) // 2)
            return {"messages": {"matches": chunk, "paging": {"pages": pages}}}
        if method == "conversations.history":
            msgs = self.history.get(params["channel"], [])
            if "cursor" not in params and params["channel"] == "C1" and len(msgs) > 1:
                return {"messages": msgs[:1], "response_metadata": {"next_cursor": "next"}}
            if params.get("cursor") == "next":
                return {"messages": msgs[1:], "response_metadata": {"next_cursor": ""}}
            return self._paged(msgs, params)
        if method == "conversations.replies":
            return self._paged(self.threads.get((params["channel"], params["ts"]), []), params,
                               parent_on_every_page=True)
        raise AssertionError(f"unexpected method {method}")


def default_threads():
    return {
        # Tim asked Mukul directly in a watched channel; no reply yet.
        ("C1", ts(1)): [msg("U3", ts(1), "Who owns the path bug? <@U1> can you check?"),
                        msg("U2", ts(0.9), "I think it's walnut")],
        # Mukul answered last: waiting on others.
        ("C1", ts(2)): [msg("U2", ts(2), "Can you review <https://github.com/acme/app/pull/7|PR 7>?"),
                        msg("U1", ts(1.5), "Done, approved")],
        # A group mention of a group Mukul is in.
        ("C1", ts(2.5)): [msg("U3", ts(2.5), "<!subteam^S1|@leads> please look at the freeze plan"),
                          msg("U3", ts(2.4), "anyone?")],
        # Mukul took part and others kept going, with a question.
        ("C1", ts(4)): [msg("U1", ts(4), "Let's split the work"),
                        msg("U2", ts(3.8), "Should I take the importer? What about tests?")],
        # Unread watched-channel post with nobody addressing Mukul.
        ("C1", ts(0.5)): [msg("U3", ts(0.5), "FYI the build is green again after the fix landed")],
        # A deploy bot post in a skipped channel.
        ("C2", ts(1)): [msg("U4", ts(1), "Deployed <@U1> build 42")],
        # Mukul reacted to the last message: handled elsewhere.
        ("C1", ts(3)): [msg("U3", ts(3), "<@U1> ship it?",
                            reactions=[{"name": "+1", "users": ["U1"]}])],
        # A thread that ended before the window.
        ("C1", ts(9)): [msg("U3", ts(9), "old <@U1>")],
        # A DM thread with replies.
        ("D1", ts(1)): [msg("U2", ts(1), "thread root"), msg("U2", ts(0.8), "reply in thread")],
    }


def default_history():
    return {
        "C1": [{"ts": ts(0.5), "user": "U3", "text": "FYI"},
               {"ts": ts(4), "latest_reply": ts(3.8), "user": "U1", "text": "Let's split the work"},
               {"ts": ts(2), "latest_reply": ts(1.5), "thread_ts": ts(2), "user": "U2", "text": "review"},
               {"ts": ts(2.5), "latest_reply": ts(2.4), "user": "U3", "text": "freeze plan"},
               {"ts": ts(20), "user": "U3", "text": "too old"},
               {"ts": ts(1), "subtype": "channel_join"}],
        "D1": [msg("U2", ts(1), "thread root", reply_count=1),
               msg("U2", ts(0.7), "Any small features I can pick up?"),
               {"ts": ts(0.6), "subtype": "bot_message", "text": "ignored"}],
        "G1": [msg("U3", ts(2), "fyi all, slides are in the folder")],
    }


def _hit(cid, when, text, thread_ts=None, user="U3", **channel):
    link = f"https://acme.slack.com/archives/{cid}/p{when.replace('.', '')}"
    if thread_ts:
        link += f"?thread_ts={thread_ts}&cid={cid}"
    return {"ts": when, "text": text, "user": user, "permalink": link,
            "channel": {"id": cid, "name": channel.get("name", cid), **channel}}


def default_search():
    return {
        "<@U1>": [_hit("C1", ts(1), "<@U1> can you check?", thread_ts=ts(1)),
                  _hit("C1", ts(3), "<@U1> ship it?"),
                  _hit("C2", ts(1), "Deployed"),
                  _hit("C1", ts(9), "old <@U1>"),
                  _hit("G1", ts(2), "hi", is_mpim=True)],
        "is:dm": [_hit("D1", ts(0.7), "Any small features I can pick up?", is_im=True)],
        "to:me": [_hit("D1", ts(0.8), "reply in thread", thread_ts=ts(1), is_im=True)],
        "from:me": [
            _hit("C1", ts(1.5), "Done, approved", thread_ts=ts(2), user="U1", name="team"),
            _hit("C1", ts(2.2), "I'll send the freeze plan doc by friday", user="U1", name="team"),
            _hit("C1", ts(2.1), "I'll fix the importer in https://github.com/acme/app/pull/9 today",
                 user="U1", name="team"),
            _hit("C1", ts(2.0), "I will review https://github.com/acme/app/pull/9 again soon",
                 user="U1", name="team"),
            _hit("C1", ts(1.9), "Will merge https://github.com/acme/app/pull/8 after the tests pass",
                 user="U1", name="team"),
            _hit("D1", ts(1.8), "Should I send the plan doc tomorrow?", user="U1", is_im=True),
        ],
    }


def write_config(home, block):
    path = home / ".assistant/comms/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"llm": {}, "slack_focus": block}))
    return path


CONFIG = {
    "days": 7,
    "watched_channels": {"C1": "team"},
    "bot_authors": ["Deploy Bot"],
    "skip_channels": ["deployments"],
    "github_repos": ["acme/app"],
    "jira": {"enabled": True, "base_url": "https://jira.acme.test", "project_keys": ["ABC"]},
}
