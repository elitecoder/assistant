#!/usr/bin/env python3
"""gcal.py — read-only Google Calendar connector (Keel M5 wave 2).

WHY: an upcoming meeting Mukul forgot is a world event just like an email or a
GitHub mention. This connector turns the calendar into a source of
``event_upcoming`` reminders that flow through the same policy/decision spine as
everything else — so "you have a 1:1 in an hour" is a laned decision, not a
push notification the design forbids.

This is a pure PRODUCER: it emits normalized WorldEvents and NEVER writes to the
calendar. The only Google calls are read-only GETs (events.list). A grep CI test
proves no send/mutation API is called.

OAuth (design section 9, GCal row — "same OAuth base as Gmail"):
  - The connector REUSES the base's OAuthTokenManager + run_installed_app_flow
    EXACTLY as gmail.py does — the refresh-token flow is owned by the base, not
    forked here. Scope is calendar.readonly and nothing more.
  - Seed the token cache once, on the owner's hardware:
        bin/connectors/gcal.py --authorize --client-secrets <that.json>
    (add --force to replace an existing cache). No secret is ever printed.

Cursor = Calendar's ``syncToken`` (events.list incremental). A first run with no
token does a bounded full list of FUTURE events (populating the upcoming store),
captures the ``nextSyncToken`` and emits reminders only when a window is due. A
410 GONE (the syncToken expired) triggers a bounded full-resync — the same
shape as Gmail's 404 reseed, and, per the wave-1 E5 lesson, SURFACED in the
heartbeat (ok=false + a "410" error) rather than reported as a clean seed.

Reminders (CRITICAL — the once-per-window dedup):
  Each upcoming event fires ``event_upcoming`` at T-24h and T-1h before its
  start. A 60s poll would otherwise re-emit the same reminder every minute, so
  each time-based emission carries its OWN stable dedup key
  ``gcal-upcoming:<event_id>:<start>:<window>`` (window ∈ {24h, 1h}) which is
  the emitted WorldEvent's external_id. The connector ALSO records fired keys in
  its durable cursor (``emitted_reminders``) so a window is produced exactly
  ONCE at the producer even before the spine dedups — proven by
  test_keel_gcal_connector.ReminderDedupTests.

Stdlib only (urllib via the base's injectable transports). No LLM.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from assistant import connector  # noqa: E402

SOURCE = "gcal"
NAME = "gcal"
API_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# READ-ONLY scope only — a producer never writes the calendar back.
GCAL_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# The reminder windows, as (label, lead-seconds). Ordered widest→tightest.
REMINDER_WINDOWS = (("24h", 24 * 3600), ("1h", 3600))
# Keep at most this many fired-reminder keys / upcoming events in the cursor so
# a long-lived daemon never grows the cursor unbounded (both are pruned once an
# event's start is in the past, but a defensive cap bounds pathological cases).
MAX_TRACKED = 2000


def _event_start(ev: dict):
    """(start_iso_string, start_epoch|None) for a calendar event. Handles both
    timed events (start.dateTime, RFC3339 w/ offset) and all-day events
    (start.date, a bare YYYY-MM-DD treated as UTC midnight). The raw string is
    part of the external_id, so it must be stable across polls."""
    start = ev.get("start") or {}
    dt = start.get("dateTime")
    if isinstance(dt, str) and dt:
        return dt, connector.eventspine.parse_iso(dt)
    d = start.get("date")
    if isinstance(d, str) and d:
        return d, connector.eventspine.parse_iso(d + "T00:00:00Z")
    return "", None


def event_to_reminder(ev: dict, window: str, *, now=None) -> dict:
    """One calendar event + a window label → one ``event_upcoming`` WorldEvent.
    Pure function (the replay fixtures test it directly). The WorldEvent's ts is
    the event START (deterministic, not poll-time), its external_id is the
    stable once-per-window dedup key, and refs carry the typed calendar
    reference + window so a policy can lane per horizon (24h→digest, 1h→staged).
    """
    event_id = str(ev.get("id") or "?")
    start_str, start_epoch = _event_start(ev)
    summary = ev.get("summary") or "(no title)"
    location = ev.get("location") or ""
    organizer = ((ev.get("organizer") or {}).get("email")
                 or (ev.get("creator") or {}).get("email") or "")
    url = ev.get("htmlLink") or ""
    ts_epoch = start_epoch if start_epoch is not None else (
        now if now is not None else connector.time.time())
    refs = {"window": window,
            "gcal": f"gcal:{event_id}:{start_str}",
            "event_id": event_id}
    if location:
        refs["location"] = location
    lead = "in 24h" if window == "24h" else "in 1h"
    return connector.build_world_event(
        source=SOURCE,
        kind="event_upcoming",
        external_id=f"gcal-upcoming:{event_id}:{start_str}:{window}",
        ts_epoch=ts_epoch,
        actor=organizer,
        title=summary,
        snippet=f"starts {start_str} ({lead})"
                + (f" · {location}" if location else ""),
        url=url,
        refs=refs,
    )


class GCalConnector(connector.Connector):
    def __init__(self, *, http=None, oauth_transport=None,
                 token_manager=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._http = http or connector.urllib_transport
        self.tokens = token_manager or connector.OAuthTokenManager(
            self.token_path(),
            skew_sec=int(self.config.get("token_skew_sec",
                                         connector.DEFAULT_TOKEN_SKEW_SEC)),
            transport=oauth_transport)

    # ── authenticated readonly GET ──────────────────────────────────────────

    def _get(self, url: str, token: str) -> tuple:
        return self._http("GET", url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "assistant-connector-gcal",
        })

    def _heartbeat(self, now, errors=None, event_count=None, poll_count=None,
                   status=None):
        # The explicit tri-state status the brief/panel key off. not_configured
        # (opted out) is QUIET (ok:true, errors empty); an errored poll is the
        # only alarming state. Mirrors gmail.py exactly.
        if status is None:
            status = "error" if errors else "ok"
        self.write_heartbeat(
            last_poll_epoch=now,
            token_expiry_epoch=self.tokens.expiry_epoch(),
            errors=errors, event_count=event_count, poll_count=poll_count,
            extra={"status": status})

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()

        # GCal is OPTIONAL — no token cache means the owner never ran
        # `--authorize`. A clean opted-out state, exactly like Gmail: one QUIET
        # not_configured heartbeat, never a crash-loop or alert.
        if not self.token_path().exists():
            self._heartbeat(now, status="not_configured")
            return {"status": "not_configured", "emitted": 0, "errors": []}

        cursor = self.load_cursor()
        errors: list = []

        try:
            token = self.tokens.access_token(now)
        except connector.OAuthError as e:
            errors.append(f"oauth: {str(e)[:200]}")
            self._heartbeat(now, errors=errors)
            return {"status": "oauth_error", "emitted": 0, "errors": errors}

        # 1. Sync the upcoming-events store from the calendar (syncToken
        #    incremental, or a bounded full list on first run / 410 GONE).
        sync = self._sync(now, token, cursor, errors)
        if sync.get("fatal"):
            return sync["result"]
        cursor = sync["cursor"]

        # 2. Fire any reminder windows that have come due, ONCE each.
        emitted = self._fire_reminders(now, cursor, errors)

        # 3. Prune past events / fired keys, persist the cursor, heartbeat.
        self._prune_cursor(now, cursor)
        cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        cursor["last_emitted"] = emitted
        self.save_cursor(cursor)
        self._heartbeat(now, event_count=emitted,
                        poll_count=cursor["poll_count"],
                        errors=errors or None,
                        status=("error" if errors else "ok"))
        # The poll itself COMPLETED (a 410 already resynced in-line; a poison
        # item was skip-and-counted) — report a healthy status so run_forever
        # holds cadence, exactly like Gmail's reseed returns "seeded". The
        # errors (410 loss window, poison skips) are SURFACED in the heartbeat
        # (ok=false), never swallowed.
        res = {"status": "ok", "emitted": emitted,
               "errors": errors, "sync": sync.get("mode"),
               "upcoming": len(cursor.get("upcoming") or {})}
        if sync.get("retry_after_sec") is not None:
            res["retry_after_sec"] = sync["retry_after_sec"]
        return res

    # ── sync (syncToken incremental; bounded full list; 410 resync) ──────────

    def _sync(self, now, token, cursor, errors) -> dict:
        """Walk events.list, updating cursor['upcoming']. Advances syncToken
        ONLY when the whole sync is consumed to a nextSyncToken; a truncation
        parks a pageToken so the remainder is fetched next poll (never advance
        past an un-fetched page). A 410 clears the syncToken and full-resyncs,
        SURFACED in the heartbeat (E5)."""
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        max_pages = int(self.config.get("max_pages",
                                        connector.DEFAULT_MAX_PAGES))
        upcoming = dict(cursor.get("upcoming") or {})
        sync_token = cursor.get("sync_token")
        page_token = cursor.get("page_token")
        mode = "incremental" if sync_token else "full"
        # time_min anchors a full list to future events only (never backfill the
        # whole calendar history). Kept stable across a paginated full sync.
        time_min = cursor.get("time_min") or connector.eventspine.utc_iso(now)

        pages = 0
        applied = 0
        next_sync = None
        truncated = False
        while True:
            pages += 1
            # Google's list pagination requires the continuation request to carry
            # the SAME query params as the initial one (finding 15) — a bare
            # pageToken with the timeMin/orderBy (or syncToken) dropped yields
            # undefined results / a 400 on page 2+. So the base params are ALWAYS
            # emitted, and pageToken is ADDED for a continuation (never replaces
            # them): incremental → syncToken (+pageToken); full → orderBy+timeMin
            # (+pageToken).
            params = ["singleEvents=true", "maxResults=250",
                      "showDeleted=true"]
            if sync_token:
                params.append(f"syncToken={connector.urllib.parse.quote(sync_token)}")
            else:
                params.append("orderBy=startTime")
                params.append(f"timeMin={connector.urllib.parse.quote(time_min)}")
            if page_token:
                params.append(f"pageToken={connector.urllib.parse.quote(page_token)}")
            url = API_BASE + "?" + "&".join(params)
            try:
                status, hdrs, body = self._get(url, token)
            except Exception as e:  # noqa: BLE001
                errors.append(f"http: {str(e)[:200]}")
                self._heartbeat(now, errors=errors)
                return {"fatal": True,
                        "result": {"status": "http_error", "emitted": 0,
                                   "errors": errors}}
            if status == 410:
                # syncToken expired — full resync from scratch. This is a real
                # loss window; surface it (E5) rather than pretend a clean seed.
                errors.append("410 gone — syncToken expired, full resync")
                cursor["sync_token"] = None
                cursor["page_token"] = None
                cursor["upcoming"] = {}
                cursor["time_min"] = connector.eventspine.utc_iso(now)
                return self._sync(now, token,
                                  {k: v for k, v in cursor.items()},
                                  errors)
            if status != 200:
                errors.append(f"events.list status {status}")
                res = {"status": f"status_{status}", "emitted": 0,
                       "errors": errors}
                ra = None
                if status in (403, 429):
                    ra = connector.parse_retry_after(hdrs, now)
                    if ra is not None:
                        res["retry_after_sec"] = ra
                self._heartbeat(now, errors=errors)
                return {"fatal": True, "result": res, "retry_after_sec": ra}
            data = _safe_json(body)
            for item in (data.get("items") or []):
                try:
                    self._apply_item(item, upcoming, now)
                    applied += 1
                except Exception as e:  # noqa: BLE001 — poison event: skip+count
                    errors.append(f"event {item.get('id','?')}: "
                                  f"malformed: {str(e)[:120]}")
            next_page = data.get("nextPageToken")
            next_sync = data.get("nextSyncToken") or next_sync
            if next_page:
                if pages >= max_pages or len(upcoming) >= cap * 4:
                    # Stop early — PARK the pageToken so the remainder is fetched
                    # next poll; do NOT advance the syncToken past unfetched
                    # pages.
                    page_token = next_page
                    truncated = True
                    break
                page_token = next_page
                continue
            page_token = None
            break

        cursor["upcoming"] = upcoming
        cursor["time_min"] = time_min
        if truncated:
            cursor["page_token"] = page_token  # resume mid-sync next poll
        else:
            cursor["page_token"] = None
            if next_sync:
                cursor["sync_token"] = next_sync  # advance ONLY when consumed
        return {"cursor": cursor, "mode": mode, "applied": applied,
                "truncated": truncated}

    def _apply_item(self, item: dict, upcoming: dict, now) -> None:
        """Merge one calendar item into the upcoming store. Cancelled events are
        removed; past events are ignored (reminders only look forward)."""
        eid = str(item.get("id") or "")
        if not eid:
            return
        if item.get("status") == "cancelled":
            upcoming.pop(eid, None)
            return
        start_str, start_epoch = _event_start(item)
        if start_epoch is None:
            return
        upcoming[eid] = {
            "id": eid,
            "start": start_str,
            "start_epoch": start_epoch,
            "summary": item.get("summary") or "(no title)",
            "location": item.get("location") or "",
            "htmlLink": item.get("htmlLink") or "",
            "organizer": ((item.get("organizer") or {}).get("email")
                          or (item.get("creator") or {}).get("email") or ""),
        }

    # ── reminders (once-per-window, durable dedup) ───────────────────────────

    def _fire_reminders(self, now, cursor, errors=None) -> int:
        """Emit ``event_upcoming`` for each (event, window) whose lead time has
        arrived and whose dedup key has not already been fired. Records the key
        so the same window is never emitted twice — even across 60s polls.

        A per-item OSError on the drop is caught and PARKED (finding 14) — like
        slack, a transient inbox-drop failure must not abort the whole poll.
        The key is recorded ONLY after a successful emit, so a parked reminder
        re-fires next poll (no loss); we stop the pass on the first failure so a
        wedged inbox doesn't spin the full set."""
        upcoming = cursor.get("upcoming") or {}
        fired = set(cursor.get("emitted_reminders") or [])
        emitted = 0
        parked = False
        for eid, ev in sorted(upcoming.items()):
            if parked:
                break
            start_epoch = ev.get("start_epoch")
            if not isinstance(start_epoch, (int, float)):
                continue
            if now >= start_epoch:
                continue  # event already started — no more reminders
            start_str = ev.get("start") or ""
            for window, lead in REMINDER_WINDOWS:
                key = f"gcal-upcoming:{eid}:{start_str}:{window}"
                if key in fired:
                    continue
                if now >= start_epoch - lead:
                    try:
                        self.emit(self._reminder_event(ev, window, now), raw=ev,
                                  now=now)
                    except OSError as e:
                        if errors is not None:
                            errors.append(f"emit {key}: {str(e)[:120]}")
                        parked = True
                        break
                    fired.add(key)
                    emitted += 1
        # Persist fired keys (bounded); save_cursor is a no-op in --dry-run so a
        # dry run never poisons the real dedup set.
        cursor["emitted_reminders"] = sorted(fired)[-MAX_TRACKED:]
        return emitted

    def _reminder_event(self, ev: dict, window: str, now) -> dict:
        """Build the reminder WorldEvent from the stored upcoming-event dict via
        the pure event_to_reminder (reconstruct the minimal calendar shape)."""
        cal = {"id": ev.get("id"), "summary": ev.get("summary"),
               "location": ev.get("location"),
               "htmlLink": ev.get("htmlLink"),
               "organizer": {"email": ev.get("organizer")},
               "start": ({"dateTime": ev.get("start")}
                         if "T" in str(ev.get("start"))
                         else {"date": ev.get("start")})}
        return event_to_reminder(cal, window, now=now)

    def _prune_cursor(self, now, cursor) -> None:
        """Drop events whose start is in the past (with a small grace), and prune
        fired-reminder keys by whether the EVENT'S START has passed — NOT by
        membership in the `upcoming` set (finding 13).

        WHY not upcoming-membership: after a 410 + truncated resync, `upcoming`
        is rebuilt from scratch and may be PARTIAL (events on not-yet-fetched
        pages are absent). Dropping a fired key just because its event is not
        currently in `upcoming` would forget that the reminder already fired, so
        the next poll (once the event is re-fetched) would RE-FIRE it. Keying the
        prune on the start time encoded in the key makes the dedup survive a
        partial resync: a fired key is forgotten only once its event has actually
        started. An unparseable key is kept conservatively (never re-fire); the
        MAX_TRACKED cap bounds the set either way."""
        upcoming = cursor.get("upcoming") or {}
        grace = 3600
        keep = {eid: ev for eid, ev in upcoming.items()
                if isinstance(ev.get("start_epoch"), (int, float))
                and ev["start_epoch"] > now - grace}
        cursor["upcoming"] = keep
        fired = []
        for k in (cursor.get("emitted_reminders") or []):
            st = _fired_key_start_epoch(k)
            if st is not None and st <= now - grace:
                continue  # the event has started — safe to forget this key
            fired.append(k)
        cursor["emitted_reminders"] = fired[-MAX_TRACKED:]


def _fired_key_start_epoch(key: str):
    """Extract the event START (epoch seconds) encoded in a fired-reminder key
    ``gcal-upcoming:<event_id>:<start>:<window>`` — or None if it can't be
    parsed. Used to prune fired keys by start-passed, not upcoming-membership
    (finding 13). Google event ids carry no ':' so a single split isolates the
    id; the start segment (which DOES contain ':') is everything up to the
    trailing window label."""
    prefix = "gcal-upcoming:"
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix):]
    body, sep, _window = rest.rpartition(":")
    if not sep:
        return None
    _eid, sep2, start_str = body.partition(":")
    if not sep2 or not start_str:
        return None
    iso = start_str if "T" in start_str else start_str + "T00:00:00Z"
    return connector.eventspine.parse_iso(iso)


def _safe_json(body) -> dict:
    try:
        data = connector.json.loads(
            body.decode("utf-8") if isinstance(body, (bytes, bytearray))
            else body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_client_secrets(path) -> tuple:
    """Parse a Google OAuth client-secrets JSON → (client_id, client_secret,
    token_uri, auth_uri). Identical shape-handling to gmail.py's loader (the
    Cloud Console file is the same format); the secret is never logged."""
    raw = connector.json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("client-secrets JSON is not an object")
    block = raw.get("installed") or raw.get("web") or raw
    if not isinstance(block, dict):
        raise ValueError("client-secrets JSON 'installed'/'web' is malformed")
    cid = block.get("client_id")
    secret = block.get("client_secret")
    if not cid or not secret:
        raise ValueError("client-secrets JSON missing client_id/client_secret")
    token_uri = block.get("token_uri") or connector.DEFAULT_TOKEN_URI
    auth_uri = block.get("auth_uri") or connector.DEFAULT_AUTH_URI
    return cid, secret, token_uri, auth_uri


def authorize(client_secrets_path, *, force=False, open_url=None,
              code_getter=None, exchange_transport=None) -> Path:
    """The one-time consent flow that MOVES gcal from not_configured → ok. Reuses
    the base's loopback+PKCE installed-app flow (the SAME code Gmail uses — not a
    fork) for the READ-ONLY calendar scope, seeding token.json through
    OAuthTokenManager.seed (atomic 0600). Prints NO secret. Fully unit-testable
    via the two injection points."""
    cid, secret, token_uri, auth_uri = _load_client_secrets(client_secrets_path)
    c = GCalConnector()
    tok = connector.run_installed_app_flow(
        client_id=cid, client_secret=secret, scopes=[GCAL_READONLY_SCOPE],
        token_uri=token_uri, auth_uri=auth_uri, open_url=open_url,
        code_getter=code_getter, exchange_transport=exchange_transport)
    return c.tokens.seed(tok, force=force)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="normalize + print, do NOT drop into the inbox")
    ap.add_argument("--record", action="store_true",
                    help="also write sanitized {raw,expected} replay fixtures")
    ap.add_argument("--once", action="store_true",
                    help="one poll then exit (default: KeepAlive loop)")
    ap.add_argument("--authorize", action="store_true",
                    help="run the one-time OAuth consent flow to seed "
                         "token.json, then exit (needs --client-secrets)")
    ap.add_argument("--client-secrets",
                    help="path to the Google Desktop OAuth client-secrets JSON "
                         "(used with --authorize)")
    ap.add_argument("--force", action="store_true",
                    help="with --authorize, overwrite an existing token.json "
                         "(REPLACES the stored refresh_token)")
    args = ap.parse_args(argv)

    if args.authorize:
        if not args.client_secrets:
            print("--authorize requires --client-secrets <path>",
                  file=sys.stderr)
            return 2
        try:
            path = authorize(args.client_secrets, force=args.force)
        except (connector.OAuthError, OSError, ValueError) as e:
            print(f"authorization failed: {e}", file=sys.stderr)
            return 1
        print(f"authorized — token cache seeded at {path}", file=sys.stderr)
        return 0

    c = GCalConnector(dry_run=args.dry_run, record=args.record,
                      log=lambda m: print(m, file=sys.stderr))

    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0

    if not c.token_path().exists():
        print("Google Calendar not configured — run: bin/connectors/gcal.py "
              "--authorize --client-secrets <path> "
              "(daemon will keep re-checking)", file=sys.stderr)
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
