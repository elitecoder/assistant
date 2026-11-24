"""Check each Ready for you item on every refresh, and clear the ones whose follow-up already happened.

A done TODO you sent from the page, and haven't marked Done, clears when:

- pr-merged / pr-closed   every pull request its result or detail names is merged, or merged or closed
- you-commented           after it finished, you commented on or reviewed one of those pull requests
- you-replied             after it finished, you posted in its source Slack thread

Clearing writes the mark the Done button writes, plus `auto` and `why`, so the page can say why and undo it. Each
cleared TODO is recorded in ready-checks.json, so an item you bring back never clears again. A blocked TODO never
clears and never restarts: when its reason names a GitHub repository it couldn't reach, and `gh api repos/OWNER/NAME`
now works, it gets a note that the blocker looks gone. A check that fails leaves its item as it was.
"""
from __future__ import annotations

import re
import traceback
import urllib.parse
from datetime import datetime, timezone

from assistant import records
from assistant.slack_focus.client import Client, SlackError

from work_queue import config, todo_result, tools

READY_MARK = "https://work-queue.local/ready/"
GH_TIMEOUT = 30
PR_URL = re.compile(r"https://([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")
REPO_URL = re.compile(r"https://([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\s.,;:)]|$)")
REPO_NAMED = re.compile(r"\b(?:access to|repo|repository)\s+`?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)`?(?=[\s.,;:)]|$)",
                        re.I)
NO_ACCESS = re.compile(r"\baccess\b|permission|\b40[34]\b|not found|can'?t (?:read|see|reach)|cannot (?:read|see)|"
                       r"denied|forbidden", re.I)
SLACK_PATH = re.compile(r"/archives/([CDG][A-Z0-9]{8,11})/p(\d{10})(\d{6})")
FAILURES = (tools.SourceError, SlackError, OSError, ValueError, KeyError, TypeError)


def checks_path():
    return config.data_dir() / "ready-checks.json"


def marks_path():
    return config.data_dir() / "marks.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def when(stamp) -> datetime | None:
    """An ISO time as an aware datetime; None when it's missing or can't be read."""
    try:
        found = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def items() -> list[dict]:
    """The TODOs Ready for you lists: sent from the page, done or blocked, and not marked Done."""
    queued = config.read_json(config.data_dir() / "queued.json", {})
    marks = config.read_json(marks_path(), {})
    ids = {q.get("id") for q in queued.values() if isinstance(q, dict)} if isinstance(queued, dict) else set()
    doc = todo_result.read_store()
    return [i for i in [*doc.get("items", []), *doc.get("completed", [])]
            if i.get("id") in ids and i.get("status") in ("done", "blocked") and READY_MARK + i["id"] not in marks]


def gh_hosts(doc: dict) -> set[str]:
    """github.com and the GitHub host you linked in sources.json."""
    host = (doc.get("github") or {}).get("host") if isinstance(doc.get("github"), dict) else None
    return {"github.com", *([host] if isinstance(host, str) and host else [])}


def gh_env(host: str) -> dict | None:
    return None if host == "github.com" else {"GH_HOST": host}


def pr_urls(item: dict, hosts: set[str]) -> list[str]:
    """Each pull request the agent's result names, once, in order. A result that names none falls back to the
    TODO's detail and link, which name the row it came from."""
    def named(text: str) -> list[str]:
        found = [f"https://{h}/{o}/{r}/pull/{n}" for h, o, r, n in PR_URL.findall(text) if h in hosts]
        return list(dict.fromkeys(found))
    return named(todo_result.result_of(item)) or named(f"{item.get('detail') or ''}\n{item.get('url') or ''}")


def short_pr(url: str) -> str:
    _, owner, name, number = PR_URL.fullmatch(url).groups()
    return f"{owner}/{name}#{number}"


class GitHub:
    """One gh call per pull request and one for your login per host, per refresh. A failed call is None."""

    def __init__(self):
        self.prs: dict[str, dict | None] = {}
        self.logins: dict[str, str | None] = {}

    def pr(self, url: str) -> dict | None:
        if url not in self.prs:
            host = PR_URL.fullmatch(url).group(1)
            self.prs[url] = self.read(["gh", "pr", "view", url, "--json", "state,comments,reviews"], host)
        return self.prs[url]

    def login(self, host: str) -> str | None:
        if host not in self.logins:
            found = self.read(["gh", "api", "user"], host)
            self.logins[host] = found.get("login") or None if isinstance(found, dict) else None
        return self.logins[host]

    def read(self, cmd: list[str], host: str):
        try:
            return tools.run_json(cmd, timeout=GH_TIMEOUT, env=gh_env(host))
        except tools.SourceError as exc:
            print(f"Ready for you: {' '.join(cmd[:3])} failed: {exc}")
            return None

    def can_read(self, host: str, repo: str) -> bool:
        return self.read(["gh", "api", f"repos/{repo}"], host) is not None


def landed(urls: list[str], gh: GitHub) -> tuple[str, str] | None:
    """("pr-merged" or "pr-closed", why) when every pull request is merged or closed; None otherwise."""
    states = [(gh.pr(u) or {}).get("state") for u in urls]
    if not urls or any(s not in ("MERGED", "CLOSED") for s in states):
        return None
    names = ", ".join(short_pr(u) for u in urls)
    if all(s == "MERGED" for s in states):
        return "pr-merged", f"{names} {'was' if len(urls) == 1 else 'were'} merged."
    return "pr-closed", f"{names} {'was' if len(urls) == 1 else 'were'} merged or closed."


def you_commented(urls: list[str], gh: GitHub, after: datetime) -> tuple[str, str] | None:
    for url in urls:
        found = gh.pr(url)
        me = gh.login(PR_URL.fullmatch(url).group(1)) if found else None
        if not me:
            continue
        posts = [(c, "createdAt") for c in found.get("comments") or []] + \
                [(r, "submittedAt") for r in found.get("reviews") or []]
        if any((p.get("author") or {}).get("login") == me and (t := when(p.get(field))) and t > after
               for p, field in posts):
            return "you-commented", f"You commented on or reviewed {short_pr(url)} after it finished."
    return None


def slack_thread(link: str) -> tuple[str, str] | None:
    """The channel and thread root of a Slack message link; None for any other link."""
    parts = urllib.parse.urlsplit(link or "")
    found = SLACK_PATH.fullmatch(parts.path) if parts.scheme == "https" and (parts.hostname or "").endswith(".slack.com") \
        else None
    if not found:
        return None
    root = urllib.parse.parse_qs(parts.query).get("thread_ts", [f"{found.group(2)}.{found.group(3)}"])[0]
    return found.group(1), root


def you_replied(item: dict, slack, after: datetime) -> tuple[str, str] | None:
    thread = slack_thread(item.get("url") or "")
    client = slack() if thread else None
    if client is None:
        return None
    cid, root = thread
    found = client.call("conversations.replies", {"channel": cid, "ts": root, "oldest": f"{after.timestamp():.6f}",
                                                  "limit": 200})
    if any(m.get("user") == client.me and float(m.get("ts") or 0) > after.timestamp()
           for m in found.get("messages") or []):
        return "you-replied", "You replied in its Slack thread after it finished."
    return None


def pr_list(urls: list[str]) -> str:
    """Pull requests in plain words: "o/r#1", "o/r#1 and #2", or "o/r#1, #2, and x/y#3". A pull request in the same
    repository as the one before it shows only its number."""
    names, repo = [], None
    for url in urls:
        name = short_pr(url)
        here, number = name.split("#")
        names.append(f"#{number}" if here == repo else name)
        repo = here
    return names[0] if len(names) == 1 else (" and ".join(names) if len(names) == 2 else
                                             f"{', '.join(names[:-1])}, and {names[-1]}")


def pending(urls: list[str], gh: GitHub) -> str:
    """What would clear the item, in plain words, when its pull requests are still open; "" otherwise. It names what
    would happen, not who it waits on: the open pull request may be the agent's, or someone else's it reviewed."""
    waiting = [u for u in urls if (gh.pr(u) or {}).get("state") == "OPEN"]
    if not waiting:
        return ""
    one = len(waiting) == 1
    return (f"Clears when {pr_list(waiting)} {'merges or closes' if one else 'merge or close'}, or once you comment on "
            f"or review {'it' if one else 'them'}.")


def blocked_repos(reason: str, hosts: set[str]) -> list[tuple[str, str]]:
    """The (host, OWNER/NAME) repositories a blocked reason says it couldn't reach; [] unless it says so."""
    if not NO_ACCESS.search(reason or ""):
        return []
    found = [(h, f"{o}/{r}") for h, o, r in REPO_URL.findall(reason) if h in hosts]
    found += [("github.com", f"{o}/{r}") for o, r in REPO_NAMED.findall(reason)]
    return list(dict.fromkeys(found))


def unblocked(item: dict, gh: GitHub, hosts: set[str]) -> str:
    repos = blocked_repos(item.get("statusReason") or "", hosts)
    if repos and all(gh.can_read(host, repo) for host, repo in repos):
        names = ", ".join(repo for _, repo in repos)
        return f"The blocker looks gone: GitHub can read {names} now, so you can resume {item['id']}."
    return ""


def guarded(check, *args):
    """A check's answer; None when it fails, so a failure leaves the item showing."""
    try:
        return check(*args)
    except FAILURES as exc:
        print(f"Ready for you: a check failed: {type(exc).__name__}: {exc}")
        return None


def slack_client(transport, token: str):
    """A function that builds the Slack client once, on first use; it returns None when Slack can't be read."""
    made: list = []

    def get():
        if not made:
            made.append(guarded(Client, token, transport) if token else None)
        return made[0]
    return get


def check(doc: dict, transport, token: str) -> dict:
    """Check every Ready for you item, clear the finished ones, and save what was found. Returns the saved checks."""
    hosts, gh, slack = gh_hosts(doc), GitHub(), slack_client(transport, token)
    saved = config.read_json(checks_path(), {})
    saved = saved if isinstance(saved, dict) else {}
    out = {k: v for k, v in saved.items() if isinstance(v, dict) and v.get("cleared")}
    cleared: dict[str, dict] = {}
    for item in items():
        td = item["id"]
        if td in out:
            continue
        if item["status"] == "blocked":
            if note := guarded(unblocked, item, gh, hosts):
                out[td] = {"note": note, "at": now_iso()}
                records.check(item, "unblocked")
            continue
        urls = pr_urls(item, hosts)
        after = when(item.get("resultAt") or item.get("statusUpdatedAt"))
        found = guarded(landed, urls, gh) or (after and (guarded(you_commented, urls, gh, after)
                                                         or guarded(you_replied, item, slack, after)))
        if found:
            out[td] = {"cleared": found[0], "why": found[1], "at": now_iso()}
            cleared[td] = out[td]
            records.check(item, found[0])
        elif wait := guarded(pending, urls, gh):
            out[td] = {"pending": wait, "at": now_iso()}
    if cleared:
        with config.marks_lock():
            marks = config.read_json(marks_path(), {})
            for td, c in cleared.items():
                marks.setdefault(READY_MARK + td, {"state": "done", "until": 0, "at": c["at"], "auto": c["cleared"],
                                                   "why": c["why"]})
            config.write_json(marks_path(), marks)
    config.write_json(checks_path(), out)
    return out


def run(doc: dict, transport, token: str) -> dict:
    """check(), except that a bug in it never holds back the refresh: the traceback goes to the log."""
    try:
        return check(doc, transport, token)
    except Exception:  # noqa: BLE001 — Ready for you checks never stop the refresh; the traceback goes to the log
        traceback.print_exc()
        return {}
