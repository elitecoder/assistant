"""Read-only Slack Web API client for the focus board.

It never posts, reacts, or marks anything read: `call` refuses any method
outside READ_METHODS before a request leaves the machine. The token is your
xoxp- user token from $SLACK_USER_TOKEN, because search needs a user token.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

READ_METHODS = frozenset({
    "auth.test",
    "conversations.history",
    "conversations.info",
    "conversations.replies",
    "search.messages",
    "usergroups.users.list",
    "users.info",
})
# A transport is (method, params, token) -> parsed JSON. Injectable for tests.
Transport = Callable[[str, dict, str], dict]
# Errors that mean "this one thread or channel is gone or private", not that the
# pull is broken. Any other error stops the refresh, so the board is never
# silently built from a partial pull.
SKIPPABLE = frozenset({"thread_not_found", "channel_not_found", "not_in_channel", "user_not_found",
                       "user_not_visible", "no_such_subteam"})


class SlackError(RuntimeError):
    pass


def http_transport(method: str, params: dict, token: str) -> dict:
    """POST one form-encoded Web API call, retrying rate limits and flaky reads."""
    for _ in range(20):
        req = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=urllib.parse.urlencode(params).encode(),
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            time.sleep(int(err.headers.get("Retry-After", "5")) + 1)
        except (http.client.IncompleteRead, urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(3)
    raise SlackError(f"{method}: Slack didn't answer after 20 tries")


def iso(ts) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


class Client:
    def __init__(self, token: str, transport: Transport = http_transport, sleep=time.sleep):
        if not token:
            raise SlackError("Set SLACK_USER_TOKEN to a Slack user token (xoxp-).")
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._users: dict[str, str] = {}
        self._channels: dict[str, dict] = {}
        self._groups: dict[str, bool] = {}
        auth = self._transport("auth.test", {}, token)
        if not auth.get("ok"):
            raise SlackError(f"Slack refused the token: {auth.get('error', 'unknown error')}")
        self.me = auth["user_id"]
        self.workspace_url = str(auth.get("url") or "").rstrip("/")
        self.me_name = self.user_name(self.me)

    def call(self, method: str, params: dict) -> dict:
        """One read call. A `ratelimited` answer is retried; a skippable error
        returns the answer as is; any other error raises."""
        if method not in READ_METHODS:
            raise SlackError(f"{method} isn't a read method; the focus board only reads Slack")
        for _ in range(4):
            answer = self._transport(method, params, self._token)
            error = answer.get("error")
            if answer.get("ok", True) or error in SKIPPABLE:
                return answer
            if error != "ratelimited":
                raise SlackError(f"Slack {method} failed: {error or 'unknown error'}")
            self._sleep(30)
        raise SlackError(f"Slack {method} stayed rate limited; try again in a few minutes")

    def user_name(self, uid: str) -> str:
        if not uid:
            return ""
        if uid not in self._users:
            u = self.call("users.info", {"user": uid}).get("user", {})
            self._users[uid] = u.get("real_name") or u.get("name") or uid
        return self._users[uid]

    def channel(self, cid: str) -> dict:
        if cid not in self._channels:
            ch = self.call("conversations.info", {"channel": cid}).get("channel", {})
            if ch.get("is_im"):
                name, kind = f"DM {self.user_name(ch.get('user'))}", "dm"
            elif ch.get("is_mpim"):
                name, kind = "group DM", "mpim"
            else:
                name, kind = "#" + ch.get("name", cid), "channel"
            self._channels[cid] = {"name": name, "kind": kind, "last_read": float(ch.get("last_read") or 0)}
        return self._channels[cid]

    def in_group(self, gid: str) -> bool:
        if gid not in self._groups:
            users = self.call("usergroups.users.list", {"usergroup": gid}).get("users", [])
            self._groups[gid] = self.me in users
        return self._groups[gid]

    def mentions_me(self, text: str) -> str | None:
        if f"<@{self.me}>" in (text or ""):
            return "direct"
        if any(self.in_group(g) for g in re.findall(r"<!subteam\^(S[A-Z0-9]+)", text or "")):
            return "group"
        return None

    def clean(self, text: str) -> str:
        text = re.sub(r"<@([UW][A-Z0-9]+)>", lambda m: "@" + self.user_name(m.group(1)), text or "")
        text = re.sub(r"<!subteam\^[A-Z0-9]+(\|[^>]+)?>", "@group", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
        text = re.sub(r":[a-z0-9_+'-]+:", "", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    def search(self, query: str, max_pages: int = 80):
        for page in range(1, max_pages + 1):
            r = self.call("search.messages", {"query": query, "count": 100, "page": page, "sort": "timestamp"})
            messages = r.get("messages", {})
            yield from messages.get("matches", [])
            if page >= messages.get("paging", {}).get("pages", 1):
                return

    def permalink(self, cid: str, ts: str, thread_ts: str) -> str:
        link = f"{self.workspace_url}/archives/{cid}/p{ts.replace('.', '')}"
        return link + (f"?thread_ts={thread_ts}&cid={cid}" if thread_ts != ts else "")
