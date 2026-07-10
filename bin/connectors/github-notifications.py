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
    return connector.build_world_event(
        source=SOURCE,
        kind=reason,
        external_id=f"gh-notif:{repo}:{thread_id}:{updated_at}",
        ts_epoch=ts_epoch,
        actor=owner,
        title=title,
        snippet=f"{repo} · {reason}" + (f" · {subj_type}" if subj_type else ""),
        url=subject.get("url"),
        refs={},
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
        emitted = 0
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "assistant-connector-github",
        }
        try:
            headers["Authorization"] = f"Bearer {self._token_provider()}"
        except Exception as e:  # noqa: BLE001
            errors.append(f"token: {str(e)[:200]}")
            self.write_heartbeat(last_poll_epoch=now, errors=errors)
            return {"status": "auth_error", "emitted": 0, "errors": errors}

        last_mod = cursor.get("last_modified")
        if last_mod:
            headers["If-Modified-Since"] = last_mod

        try:
            status, resp_headers, body = self._http("GET", API_URL,
                                                    headers=headers)
        except Exception as e:  # noqa: BLE001
            errors.append(f"http: {str(e)[:200]}")
            self.write_heartbeat(last_poll_epoch=now, errors=errors)
            return {"status": "http_error", "emitted": 0, "errors": errors}

        if status == 304:  # watermark says nothing changed — cheap no-op
            self.write_heartbeat(last_poll_epoch=now,
                                 poll_count=cursor.get("poll_count", 0) + 1)
            self._advance_poll_count(cursor)
            return {"status": "not_modified", "emitted": 0, "errors": errors}
        if status != 200:
            errors.append(f"status {status}")
            self.write_heartbeat(last_poll_epoch=now, errors=errors)
            return {"status": f"status_{status}", "emitted": 0,
                    "errors": errors}

        try:
            items = connector.json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            errors.append(f"parse: {str(e)[:200]}")
            self.write_heartbeat(last_poll_epoch=now, errors=errors)
            return {"status": "parse_error", "emitted": 0, "errors": errors}
        if not isinstance(items, list):
            items = []

        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        for notif in items[:cap]:
            if not isinstance(notif, dict):
                continue
            event = notification_to_event(notif)
            self.emit(event, raw=notif, now=now)
            emitted += 1

        # Advance the durable watermark ONLY after the batch is dropped — a
        # crash before this leaves the old Last-Modified so the next run
        # re-fetches (at-least-once; the spine dedups the overlap).
        new_cursor = dict(cursor)
        lm = resp_headers.get("Last-Modified") or resp_headers.get(
            "last-modified")
        if lm:
            new_cursor["last_modified"] = lm
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self.write_heartbeat(last_poll_epoch=now, event_count=emitted,
                             poll_count=new_cursor["poll_count"])
        return {"status": "ok", "emitted": emitted, "errors": errors}

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
