#!/usr/bin/env python3
"""gmail.py — read-only Gmail connector (Keel M5).

WHY: inbound email is the highest-volume world-event source, and the design
picked Gmail as the second M5 connector specifically to prove the OAuth
refresh-token flow AND a history-cursor style (vs GitHub's Last-Modified
watermark) before fanning out to GCal/JIRA/Slack.

This is a pure PRODUCER: each newly-arrived message becomes a normalized
WorldEvent dropped into the inbox. It NEVER sends, replies, deletes, marks
read, or mutates anything — the only Gmail calls are readonly GETs
(users.getProfile, users.history.list, users.messages.get with
``format=metadata``). A grep CI test proves no send/mutation API is called.

OAuth (design section 9, Gmail row — the static-~/.zprofile-token handwave is
explicitly REJECTED as the Bedrock-under-launchd 403 hazard):
  - The connector base's OAuthTokenManager owns the access-token lifecycle:
    an expired/near-expiry token is refreshed in-process via the refresh-token
    grant and the new expiry is surfaced in heartbeat.json, so a dead token is
    visible in the morning brief within one poll.
  - The refresh_token + client credentials live ONLY in the token cache file
    (~/.assistant/connectors/gmail/token.json, mode 0600), seeded once by an
    out-of-band consent flow on the owner's hardware — never in a plist/config.

Cursor = Gmail's ``historyId`` (users.history.list incremental). A first run
with no cursor SEEDS the historyId from the profile and emits nothing (never
dump the whole mailbox); a 404 (historyId too old) reseeds the same way.

Stdlib only (urllib via the base's injectable transport + OAuth transport).
Both are dependency-injected so unit tests prove refresh-on-expiry and
raw→event normalization with NO live network. No LLM.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from assistant import connector  # noqa: E402

SOURCE = "gmail"
NAME = "gmail"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
METADATA_HEADERS = ("From", "To", "Cc", "Subject", "List-Unsubscribe")
# Gmail's own category labels — objective source metadata, not a lane judgment.
NEWSLETTER_LABELS = frozenset({
    "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS"})


def _headers_map(msg: dict) -> dict:
    """Case-insensitive header name → value from a messages.get payload."""
    out = {}
    for h in ((msg.get("payload") or {}).get("headers") or []):
        if isinstance(h, dict) and h.get("name"):
            out[str(h["name"]).lower()] = h.get("value") or ""
    return out


def message_to_event(msg: dict, account_email: str = "") -> dict:
    """One Gmail message (format=metadata) → one WorldEvent. Pure function
    (the replay fixtures test it directly).

    `kind` is derived MECHANICALLY from Gmail's own metadata — never an LLM or a
    lane decision: a List-Unsubscribe header or a CATEGORY_* label → newsletter;
    else the account address in To/Cc → direct; else message. Policies do the
    laning (newsletter→drop, direct→escalate); the connector only labels."""
    msg_id = str(msg.get("id") or "?")
    headers = _headers_map(msg)
    labels = set(msg.get("labelIds") or [])
    subject = headers.get("subject") or "(no subject)"
    sender = headers.get("from") or ""
    to_cc = f"{headers.get('to','')} {headers.get('cc','')}".lower()

    if "list-unsubscribe" in headers or (labels & NEWSLETTER_LABELS):
        kind = "newsletter"
    elif account_email and account_email.lower() in to_cc:
        kind = "direct"
    else:
        kind = "message"

    internal = msg.get("internalDate")
    if isinstance(internal, str) and internal.isdigit():
        ts_epoch = int(internal) / 1000.0
    elif isinstance(internal, (int, float)):
        ts_epoch = float(internal) / 1000.0
    else:
        ts_epoch = connector.eventspine.parse_iso(headers.get("date")) \
            or connector.time.time()

    return connector.build_world_event(
        source=SOURCE,
        kind=kind,
        external_id=f"gmail:{msg_id}",
        ts_epoch=ts_epoch,
        actor=sender,
        title=subject,
        snippet=msg.get("snippet") or "",
        url=f"https://mail.google.com/mail/u/0/#all/{msg_id}",
        refs={},
    )


class GmailConnector(connector.Connector):
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
            "User-Agent": "assistant-connector-gmail",
        })

    def _heartbeat(self, now, errors=None, event_count=None, poll_count=None):
        self.write_heartbeat(
            last_poll_epoch=now,
            token_expiry_epoch=self.tokens.expiry_epoch(),
            errors=errors, event_count=event_count, poll_count=poll_count)

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()
        cursor = self.load_cursor()
        errors: list = []

        # 1. A live access token (refreshes transparently if expired). If the
        #    refresh itself fails, surface it in the heartbeat (expiry stays in
        #    the past → the brief shows the connector as token-expired) and stop.
        try:
            token = self.tokens.access_token(now)
        except connector.OAuthError as e:
            errors.append(f"oauth: {str(e)[:200]}")
            self._heartbeat(now, errors=errors)
            return {"status": "oauth_error", "emitted": 0, "errors": errors}

        # 2. First run / reseed: no durable historyId → seed from the profile
        #    and emit NOTHING (never dump the whole mailbox).
        if not cursor.get("history_id"):
            return self._seed(now, token, cursor, errors)

        # 3. Incremental history since the cursor.
        return self._incremental(now, token, cursor, errors)

    def _seed(self, now, token, cursor, errors) -> dict:
        try:
            status, _h, body = self._get(f"{API_BASE}/profile", token)
        except Exception as e:  # noqa: BLE001
            errors.append(f"http: {str(e)[:200]}")
            self._heartbeat(now, errors=errors)
            return {"status": "http_error", "emitted": 0, "errors": errors}
        if status != 200:
            errors.append(f"profile status {status}")
            self._heartbeat(now, errors=errors)
            return {"status": f"status_{status}", "emitted": 0,
                    "errors": errors}
        prof = _safe_json(body)
        new_cursor = dict(cursor)
        new_cursor["history_id"] = str(prof.get("historyId") or "")
        new_cursor["email"] = prof.get("emailAddress") or cursor.get("email", "")
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        self.save_cursor(new_cursor)
        self._heartbeat(now, event_count=0,
                        poll_count=new_cursor["poll_count"])
        return {"status": "seeded", "emitted": 0,
                "history_id": new_cursor["history_id"], "errors": errors}

    def _incremental(self, now, token, cursor, errors) -> dict:
        email = cursor.get("email") or ""
        start = cursor.get("history_id")
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        message_ids: list = []
        newest_history = start
        page_token = None
        pages = 0
        while True:
            pages += 1
            url = (f"{API_BASE}/history?startHistoryId={start}"
                   "&historyTypes=messageAdded")
            if page_token:
                url += f"&pageToken={page_token}"
            try:
                status, _h, body = self._get(url, token)
            except Exception as e:  # noqa: BLE001
                errors.append(f"http: {str(e)[:200]}")
                self._heartbeat(now, errors=errors)
                return {"status": "http_error", "emitted": 0,
                        "errors": errors}
            if status == 404:
                # historyId too old — Gmail expired it. Reseed rather than
                # backfill the entire mailbox.
                errors.append("history 404 — reseeding")
                return self._seed(now, token,
                                  {k: v for k, v in cursor.items()
                                   if k != "history_id"}, errors)
            if status != 200:
                errors.append(f"history status {status}")
                self._heartbeat(now, errors=errors)
                return {"status": f"status_{status}", "emitted": 0,
                        "errors": errors}
            data = _safe_json(body)
            if data.get("historyId"):
                newest_history = str(data["historyId"])
            for rec in (data.get("history") or []):
                for added in (rec.get("messagesAdded") or []):
                    mid = ((added.get("message") or {}).get("id"))
                    if mid:
                        message_ids.append(str(mid))
            page_token = data.get("nextPageToken")
            if not page_token or len(message_ids) >= cap or pages >= 25:
                break

        # De-dup ids within this batch (a message can appear in multiple history
        # records); the spine still dedups across batches.
        seen = set()
        ordered_ids = [m for m in message_ids
                       if not (m in seen or seen.add(m))][:cap]

        emitted = 0
        for mid in ordered_ids:
            url = (f"{API_BASE}/messages/{mid}?format=metadata"
                   + "".join(f"&metadataHeaders={h}" for h in METADATA_HEADERS))
            try:
                status, _h, body = self._get(url, token)
            except Exception as e:  # noqa: BLE001
                errors.append(f"msg {mid}: {str(e)[:120]}")
                continue
            if status != 200:
                errors.append(f"msg {mid}: status {status}")
                continue
            msg = _safe_json(body)
            event = message_to_event(msg, email)
            self.emit(event, raw=msg, now=now)
            emitted += 1

        # Advance the durable cursor ONLY after the batch is dropped.
        new_cursor = dict(cursor)
        if newest_history:
            new_cursor["history_id"] = str(newest_history)
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self._heartbeat(now, event_count=emitted,
                        poll_count=new_cursor["poll_count"], errors=errors or None)
        return {"status": "ok", "emitted": emitted,
                "history_id": new_cursor.get("history_id"), "errors": errors}


def _safe_json(body) -> dict:
    try:
        data = connector.json.loads(
            body.decode("utf-8") if isinstance(body, (bytes, bytearray))
            else body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="normalize + print, do NOT drop into the inbox")
    ap.add_argument("--record", action="store_true",
                    help="also write sanitized {raw,expected} replay fixtures")
    ap.add_argument("--once", action="store_true",
                    help="one poll then exit (default: KeepAlive loop)")
    args = ap.parse_args(argv)
    c = GmailConnector(dry_run=args.dry_run, record=args.record,
                       log=lambda m: print(m, file=sys.stderr))
    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
