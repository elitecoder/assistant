#!/usr/bin/env python3
"""Link your own sources to the Work queue (a Pro mode feature).

    work-queue-setup.py status                 one line per source, without signing in to anything
    work-queue-setup.py link <source> [--account EMAIL ...] [--host HOST]
    work-queue-setup.py unlink <source>        unlinking slack also deletes its browser profile and cookies
    work-queue-setup.py check                  run a read-only probe for each linked source
    work-queue-setup.py unlinked               print the sources you haven't linked, one per line
    work-queue-setup.py refresh                re-read the linked sources and rebuild the page

Sources: slack, gmail, calendar, outlook, github. Linking detects the tool,
runs a read-only probe, and saves the source in
~/.assistant/work-queue/sources.json. Nothing here prints or stores a secret:
tokens stay in your environment or in each tool's own sign-in store.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "src")]
from assistant.slack_focus.client import http_transport  # noqa: E402

from work_queue import config, refresh, tools  # noqa: E402
from work_queue.inputs import outlook, slack  # noqa: E402

LABELS = {"slack": "Slack", "gmail": "Gmail", "calendar": "Calendar", "outlook": "Outlook", "github": "GitHub"}
TOOLS = {"slack": "uv", "gmail": "gog", "calendar": "gog", "outlook": "npx", "github": "gh"}
INSTALL = {
    "uv": f"Install uv (https://docs.astral.sh/uv/), then run: uv run --no-project --with {tools.PLAYWRIGHT} python -m playwright install chromium",
    "gog": "Install the gog CLI (https://gogcli.sh, or brew install steipete/tap/gogcli), then run: "
           "gog auth credentials <client-secret.json>, then gog auth add <you@example.com>",
    "npx": "Install Node.js, which includes npx",
    "gh": "Install the GitHub CLI (https://cli.github.com/)",
}
SIGN_IN = {
    "slack": "Set SLACK_USER_TOKEN in ~/.zprofile (see the Slack focus list in docs/connectors.md), then run "
             "bin/work-queue-setup.py link slack and sign in to Slack in the window that opens",
    "gmail": "Run gog auth add you@example.com, then bin/work-queue-setup.py link gmail",
    "calendar": "Run gog auth add you@example.com, then bin/work-queue-setup.py link calendar",
    "outlook": "Run npx -y @microsoft/workiq once and finish its Microsoft 365 sign-in, then bin/work-queue-setup.py link outlook",
    "github": "Run gh auth login, then bin/work-queue-setup.py link github",
}
NO_CLIENT = "oauth client credentials missing"
CREDENTIALS = ("Run gog auth credentials <client-secret.json> first, then gog auth add <you@example.com>, "
               "then bin/work-queue-setup.py link {name}")
TRANSPORT = http_transport


class NeedsSignIn(tools.SourceError):
    pass


def google_accounts(service: str) -> list[str]:
    found = tools.run_json(["gog", "auth", "list", "-j"])
    accounts = [a["email"] for a in found.get("accounts") or [] if service in (a.get("services") or [])]
    if not accounts:
        raise NeedsSignIn(f"No Google account in gog has {service} access")
    return accounts


def detect_slack(args) -> dict:
    if shutil.which("uv") is None:
        raise tools.ToolMissing("uv isn't installed")
    token = os.environ.get("SLACK_USER_TOKEN", "")
    auth = TRANSPORT("auth.test", {}, token) if token else {"error": "SLACK_USER_TOKEN isn't set"}
    if not auth.get("ok"):
        raise NeedsSignIn(f"Slack didn't accept your user token ({auth.get('error', 'unknown error')})")
    entry = {"team_id": auth.get("enterprise_id") or auth["team_id"], "workspace_url": auth["url"].rstrip("/"),
             "close_after_days": 14}
    answer = tools.run_json(slack.browser("signin", entry, "--wait", "300"), timeout=420)
    if not answer.get("ok"):
        raise NeedsSignIn("no Slack session in the Work queue's browser window")
    return entry


def detect_gmail(args) -> dict:
    accounts = args.account or google_accounts("gmail")
    for account in accounts:
        tools.run_json(["gog", "gmail", "messages", "search", "in:inbox", "--max", "1", "-a", account, "-j"])
    return {"accounts": accounts}


def detect_calendar(args) -> dict:
    accounts = args.account or google_accounts("calendar")
    for account in accounts:
        tools.run_json(["gog", "calendar", "events", "primary", "--today", "--max", "1", "-a", account, "-j"])
    return {"accounts": accounts}


def detect_outlook(args) -> dict:
    me = outlook.one("/me?$select=mail,userPrincipalName")
    return {"address": me.get("mail") or me["userPrincipalName"]}


def gh_user(host: str | None) -> None:
    tools.run(["gh", "api", "user", "--jq", ".login"], env={"GH_HOST": host} if host else None)


def detect_github(args) -> dict:
    gh_user(args.host)
    return {"skip_authors": [], **({"host": args.host} if args.host else {})}


def probe(name: str, entry: dict) -> None:
    """A read-only check that the saved source still answers."""
    if name == "slack":
        slack.later(entry)
    elif name == "outlook":
        outlook.one("/me?$select=mail")
    elif name == "github":
        gh_user(entry.get("host"))
    else:
        DETECT[name](argparse.Namespace(account=entry["accounts"]))


DETECT = {"slack": detect_slack, "gmail": detect_gmail, "calendar": detect_calendar, "outlook": detect_outlook,
          "github": detect_github}


def describe(name: str, entry: dict) -> str:
    if name in ("gmail", "calendar"):
        return ", ".join(entry["accounts"])
    return {"slack": entry.get("workspace_url"), "outlook": entry.get("address"), "github": entry.get("host", "github.com")}[name]


def problem(name: str, exc: tools.SourceError) -> str:
    """One status line that says what's wrong and the exact fix."""
    if isinstance(exc, tools.ToolMissing):
        return f"{LABELS[name]}: tool missing. {INSTALL[TOOLS[name]]}"
    detail = " ".join(str(exc).split())
    fix = CREDENTIALS.format(name=name) if NO_CLIENT in detail.lower() else SIGN_IN[name]
    return f"{LABELS[name]}: needs sign-in ({detail}). {fix}"


def status(doc: dict) -> int:
    for name in config.SOURCES:
        if name not in doc:
            print(f"{LABELS[name]}: not linked. Run bin/work-queue-setup.py link {name}")
        elif shutil.which(TOOLS[name]) is None:
            print(f"{LABELS[name]}: tool missing. {INSTALL[TOOLS[name]]}")
        else:
            print(f"{LABELS[name]}: linked ({describe(name, doc[name])})")
    return 0


def link(doc: dict, args) -> int:
    try:
        entry = DETECT[args.source](args)
    except tools.SourceError as exc:
        print(problem(args.source, exc))
        return 1
    doc[args.source] = entry
    config.save(doc)
    print(f"{LABELS[args.source]}: linked ({describe(args.source, entry)})")
    return 0


def check(doc: dict) -> int:
    failed = 0
    for name in config.SOURCES:
        if name not in doc:
            print(f"{LABELS[name]}: not linked. Run bin/work-queue-setup.py link {name}")
            continue
        try:
            probe(name, doc[name])
        except tools.SourceError as exc:
            print(problem(name, exc))
            failed = 1
            continue
        print(f"{LABELS[name]}: linked ({describe(name, doc[name])})")
    return failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "link", "unlink", "check", "unlinked", "refresh"))
    parser.add_argument("source", nargs="?", choices=config.SOURCES)
    parser.add_argument("--account", action="append", help="a Google account for gmail or calendar; repeat for more")
    parser.add_argument("--host", help="a GitHub Enterprise host for github")
    args = parser.parse_args(argv)
    if args.action in ("link", "unlink") and not args.source:
        parser.error(f"{args.action} needs a source: {', '.join(config.SOURCES)}")
    if args.action == "refresh":
        return refresh.main()
    try:
        doc = config.load()
    except ValueError as exc:
        print(f"Can't read the Work queue settings: {exc}", file=sys.stderr)
        return 2
    if args.action == "unlink":
        found = doc.pop(args.source, None)
        config.save(doc)
        print(f"{LABELS[args.source]}: {'unlinked' if found is not None else 'not linked'}")
        if args.source == "slack" and config.browser_profile().exists():
            shutil.rmtree(config.browser_profile())
            print("Slack: removed the Work queue's browser profile and its Slack cookies")
        return 0
    if args.action == "unlinked":
        print("\n".join(n for n in config.SOURCES if n not in doc))
        return 0
    if args.action == "link":
        return link(doc, args)
    return {"status": status, "check": check}[args.action](doc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
