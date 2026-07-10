#!/usr/bin/env python3
"""github-notifications.py — read-only GitHub notifications connector (Keel M5).

WHY: GitHub notifications (review requested, mentions, CI activity, …) are
world events Mukul must not have to poll a browser tab for. This connector is a
pure PRODUCER: it turns each notification thread into a normalized WorldEvent
and atomically drops it into the inbox. It NEVER classifies, decides, merges,
comments, or mutates anything — the policy engine lanes downstream, and a grep
CI test proves no mutation/send API is called here.

It is the first of two M5 connectors chosen to prove the contract on a
watermark cursor style (the other, Gmail, proves a history-cursor style).

Contract specifics (design section 9, GitHub row):
  - external_id  ``gh-notif:<repo>:<thread_id>:<updated_at>`` — stable across
    re-polls of the same thread state; a new comment bumps updated_at → a new
    event, which the spine dedups against prior ones by id.
  - auth         the ``gh`` CLI token (``gh auth token``) — injected in tests.
  - watermark    the ``Last-Modified`` response header replayed as
    ``If-Modified-Since``; a 304 means "nothing new", costing no event work.
  - cadence      60s default, from config (``connectors.github.cadence_sec``).

Read-only: the ONLY HTTP verb is GET against api.github.com/notifications.
Stdlib only (urllib via the base's injectable transport). No LLM.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The base lives in the repo's src/ package; this script runs standalone under
# launchd, so put src/ on the path before importing it.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from assistant import connector  # noqa: E402

SOURCE = "github"
NAME = "github"
API_URL = "https://api.github.com/notifications"

# api.github.com/repos/<owner>/<repo>/(pulls|issues)/<n> → the human html_url.
_SUBJECT_API_RE = re.compile(
    r"api\.github\.com/repos/([^/]+)/([^/]+)/(pulls|issues)/(\d+)")
# Link: <url>; rel="next" pagination (GitHub returns 50 threads/page).
_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _human_url_and_refs(repo: str, subject_url) -> tuple:
    """Map a notification subject to (human html_url, refs). GitHub's
    notification `subject.url` is the API url (…/pulls/12); downstream wants
    the clickable html_url (…/pull/12) and typed refs (repo + pr number) so the
    goal-linker can match a merged PR to its goal (N1). Unmatched subjects keep
    their original url and just carry the repo ref."""
    refs: dict = {}
    if repo and repo != "?/?":
        refs["repo"] = repo
    url = subject_url
    if isinstance(subject_url, str):
        m = _SUBJECT_API_RE.search(subject_url)
        if m:
            owner, name, kind, num = m.groups()
            path = "pull" if kind == "pulls" else "issues"
            url = f"https://github.com/{owner}/{name}/{path}/{num}"
            if kind == "pulls":
                refs["pr"] = num
    return url, refs


def _next_link(headers: dict):
    link = headers.get("Link") or headers.get("link") or ""
    m = _NEXT_LINK_RE.search(link)
    return m.group(1) if m else None


def gh_cli_token() -> str:
    """Default token provider — the `gh` CLI's stored token. Never logged, never
    archived. Injected away in unit tests."""
    r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                       timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"gh auth token failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def notification_to_event(notif: dict) -> dict:
    """One GitHub notification thread → one WorldEvent. Pure function (the
    replay fixtures test it directly). `kind` is the notification `reason`
    (review_requested / mention / ci_activity / …) — an OBJECTIVE field from
    GitHub, not a lane judgment, so policies can match it."""
    repo = ((notif.get("repository") or {}).get("full_name")) or "?/?"
    thread_id = str(notif.get("id") or "?")
    updated_at = notif.get("updated_at") or ""
    reason = notif.get("reason") or "subscribed"
    subject = notif.get("subject") or {}
    title = subject.get("title") or f"{repo} notification"
    subj_type = subject.get("type") or ""
    owner = ((notif.get("repository") or {}).get("owner") or {}).get("login")
    ts_epoch = connector.eventspine.parse_iso(updated_at)
    if ts_epoch is None:
        ts_epoch = connector.time.time()
    url, refs = _human_url_and_refs(repo, subject.get("url"))
    return connector.build_world_event(
        source=SOURCE,
        kind=reason,
        external_id=f"gh-notif:{repo}:{thread_id}:{updated_at}",
        ts_epoch=ts_epoch,
        actor=owner,
        title=title,
        snippet=f"{repo} · {reason}" + (f" · {subj_type}" if subj_type else ""),
        url=url,
        refs=refs,
    )


class GitHubNotificationsConnector(connector.Connector):
    def __init__(self, *, token_provider=None, http=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._token_provider = token_provider or gh_cli_token
        self._http = http or connector.urllib_transport

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()
        cursor = self.load_cursor()
        errors: list = []
        base_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "assistant-connector-github",
        }
        try:
            base_headers["Authorization"] = f"Bearer {self._token_provider()}"
        except Exception as e:  # noqa: BLE001
            errors.append(f"token: {str(e)[:200]}")
            self.write_heartbeat(last_poll_epoch=now, errors=errors)
            return {"status": "auth_error", "emitted": 0, "errors": errors}

        last_mod = cursor.get("last_modified")
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        max_pages = int(self.config.get("max_pages",
                                        connector.DEFAULT_MAX_PAGES))

        # ── fetch ALL pages before emitting or advancing the watermark ──────
        # E1/E3: reading only page 1 and then advancing the watermark drops
        # page 2+. Walk Link: rel="next" to the end; if we stop early (cap or
        # page cap) mark the batch TRUNCATED so the watermark is NOT advanced —
        # the remainder is re-fetched next poll (never jump past un-emitted
        # threads).
        collected: list = []
        first_last_mod = None
        truncated = False
        url = API_URL
        pages = 0
        while url:
            pages += 1
            headers = dict(base_headers)
            if pages == 1 and last_mod:
                headers["If-Modified-Since"] = last_mod
            try:
                status, resp_headers, body = self._http("GET", url,
                                                        headers=headers)
            except Exception as e:  # noqa: BLE001
                errors.append(f"http: {str(e)[:200]}")
                self.write_heartbeat(last_poll_epoch=now, errors=errors)
                return {"status": "http_error", "emitted": 0, "errors": errors}

            if pages == 1 and status == 304:  # nothing changed — cheap no-op
                self._advance_poll_count(cursor)
                self.write_heartbeat(
                    last_poll_epoch=now,
                    poll_count=cursor.get("poll_count", 0) + 1)
                return {"status": "not_modified", "emitted": 0,
                        "errors": errors}
            if status != 200:
                errors.append(f"status {status}")
                res = {"status": f"status_{status}", "emitted": 0,
                       "errors": errors}
                if status in (403, 429):  # OP1: honor the server's backoff
                    ra = connector.parse_retry_after(resp_headers, now)
                    if ra is not None:
                        res["retry_after_sec"] = ra
                self.write_heartbeat(last_poll_epoch=now, errors=errors)
                return res

            try:
                items = connector.json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                errors.append(f"parse: {str(e)[:200]}")
                self.write_heartbeat(last_poll_epoch=now, errors=errors)
                return {"status": "parse_error", "emitted": 0,
                        "errors": errors}
            if not isinstance(items, list):
                items = []
            if pages == 1:
                first_last_mod = (resp_headers.get("Last-Modified")
                                  or resp_headers.get("last-modified"))
            collected.extend(n for n in items if isinstance(n, dict))

            nxt = _next_link(resp_headers)
            if not nxt:
                break  # fully consumed — safe to advance the watermark
            if len(collected) >= cap or pages >= max_pages:
                truncated = True
                break
            url = nxt

        # ── emit (one poison thread must not wedge or lose the rest — E4) ────
        emitted = 0
        malformed = 0
        for notif in collected[:cap]:
            try:
                event = notification_to_event(notif)
            except Exception as e:  # noqa: BLE001 — upstream schema drift
                malformed += 1
                errors.append(f"malformed item: {str(e)[:120]}")
                continue
            self.emit(event, raw=notif, now=now)
            emitted += 1
        if len(collected) > cap:  # more collected than we emitted → truncated
            truncated = True

        # Advance the watermark ONLY when the full set was consumed AND emitted
        # (design connector.py: "the watermark advances ONLY after the batch it
        # covers is safely dropped"). On truncation keep the old watermark so
        # the remainder re-fetches next poll.
        new_cursor = dict(cursor)
        if not truncated and first_last_mod:
            new_cursor["last_modified"] = first_last_mod
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self.write_heartbeat(last_poll_epoch=now, event_count=emitted,
                             poll_count=new_cursor["poll_count"],
                             errors=errors or None)
        return {"status": "ok", "emitted": emitted, "errors": errors,
                "malformed": malformed, "truncated": truncated}

    def _advance_poll_count(self, cursor: dict) -> None:
        c = dict(cursor)
        c["poll_count"] = cursor.get("poll_count", 0) + 1
        self.save_cursor(c)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="normalize + print, do NOT drop into the inbox")
    ap.add_argument("--record", action="store_true",
                    help="also write sanitized {raw,expected} replay fixtures")
    ap.add_argument("--once", action="store_true",
                    help="one poll then exit (default: KeepAlive loop)")
    args = ap.parse_args(argv)
    c = GitHubNotificationsConnector(dry_run=args.dry_run, record=args.record,
                                     log=lambda m: print(m, file=sys.stderr))
    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
