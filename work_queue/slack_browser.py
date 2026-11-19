"""Read and update your Slack Later list through your own signed-in browser.

Slack's Web API has no public method for Later saves, so this opens the Slack
web client in a Chromium profile that only the Work queue uses, reads the
session token from the page's own `saved.list` request, and calls the same API
the page calls. The token stays in memory: nothing here prints or stores it.

Run it with Playwright, outside the repo's project environment:

    uv run --no-project --with playwright==1.63.0 python work_queue/slack_browser.py signin|list|apply ...

    signin  open a window and wait up to --wait seconds for you to sign in
    list    print {"ok": true, "items": [...]} with your open Later saves
    apply   mark each save in --job completed (or uncompleted) and verify it

It always prints one JSON object. {"ok": false, "error": ...} means no
signed-in session, or Slack refused a call.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

TOKEN = re.compile(r'name="token"\r?\n\r?\n(xox[a-z]-[^\r\n]+)')
SIGN_IN = "Slack sign-in needed: run bin/work-queue-setup.py link slack and sign in"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_session(pw, profile: Path, client_url: str, headless: bool, wait_seconds: int):
    """Open the Later page and wait for the token. Returns (context, token or "")."""
    context = pw.chromium.launch_persistent_context(str(profile), headless=headless,
                                                    viewport={"width": 1200, "height": 850})
    page = context.pages[0] if context.pages else context.new_page()
    found: list[str] = []

    def on_request(request):
        match = TOKEN.search(request.post_data or "") if "saved.list" in request.url else None
        if match and not found:
            found.append(match.group(1))

    page.on("request", on_request)
    page.goto(client_url, wait_until="domcontentloaded", timeout=60000)
    for _ in range(wait_seconds):
        if found:
            break
        page.wait_for_timeout(1000)
    return context, (found[0] if found else "")


def call(context, api: str, token: str, method: str, form: dict) -> dict:
    return context.request.post(api + method, form={"token": token, **form}).json()


def open_saves(context, api: str, token: str) -> list[dict]:
    items, cursor = [], ""
    while True:
        page = call(context, api, token, "saved.list", {"count": "100", **({"cursor": cursor} if cursor else {})})
        if not page.get("ok"):
            raise RuntimeError(f"saved.list failed: {page.get('error', 'unknown error')}")
        items += [{k: it.get(k) for k in ("item_type", "item_id", "ts", "date_created")}
                  for it in page.get("saved_items") or []]
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return items


def apply_job(context, api: str, token: str, job: list[dict]) -> dict:
    """Mark each save, then re-read the open list to verify every change."""
    out = {}
    for j in job:
        r = call(context, api, token, "saved.update",
                 {"item_type": "message", "item_id": j["cid"], "ts": j["ts"], "date_due": "0", "mark": j["mark"]})
        out[j["key"]] = ({"status": "done" if j["mark"] == "completed" else "undone", "at": now()} if r.get("ok")
                         else {"status": "error", "error": r.get("error") or "unknown", "at": now()})
    still_open = {f"{it['item_id']}:{it['ts']}" for it in open_saves(context, api, token)}
    for j in job:
        result = out[j["key"]]
        if result["status"] != "error":
            is_open = f"{j['cid']}:{j['ts']}" in still_open
            result["verified"] = not is_open if j["mark"] == "completed" else is_open
            if not result["verified"]:
                result["error"] = "Slack accepted the change but the save still shows the old state"
    return out


def run(args) -> dict:
    with sync_playwright() as pw:
        context, token = open_session(pw, args.profile, args.client_url, args.headless, args.wait)
        try:
            if not token:
                return {"ok": False, "error": SIGN_IN}
            if args.action == "apply":
                return {"ok": True, "applied": apply_job(context, args.api_url, token, json.loads(args.job.read_text()))}
            return {"ok": True, "items": open_saves(context, args.api_url, token) if args.action == "list" else []}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            context.close()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("signin", "list", "apply"))
    parser.add_argument("--client-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--wait", type=int, default=60, help="seconds to wait for the Slack session")
    parser.add_argument("--headless", action="store_true")
    print(json.dumps(run(parser.parse_args(argv))))


if __name__ == "__main__":
    main(sys.argv[1:])
