#!/usr/bin/env python3
"""outlook.py — read-only Outlook / Microsoft 365 mail connector (Keel M5 wave 3).

WHY: Mukul's inbound mail is not only Gmail — an Outlook.com / work-M365 mailbox
is the same kind of world-event source. This connector turns newly-arrived mail
into normalized WorldEvents on the SAME policy/decision spine as Gmail, GitHub,
GCal and Slack, proving the OAuth base generalizes cleanly to a SECOND
provider (Microsoft) after wave-1 pinned it to Google only.

This is a pure PRODUCER: each newly-arrived message becomes a normalized
WorldEvent dropped into the inbox. It NEVER sends, replies, deletes, marks read
or mutates anything — every Microsoft Graph call is a read-only GET
(/me, /me/mailFolders/inbox/messages/delta). Send/draft is a future M7 concern.
A grep/AST CI test (tests/test_keel_connector_readonly.py) proves no mutation.

OAuth (design section 9 — the SAME base as Gmail/GCal, now provider="microsoft"):
  - REUSES the connector base's OAuthTokenManager + run_installed_app_flow with
    provider="microsoft": the refresh-token grant + loopback/PKCE consent flow
    are owned by the base, not forked here. token_uri/auth_uri are PINNED to the
    Microsoft allowlist (login.microsoftonline.com) — a poisoned client-secrets
    endpoint can never exfiltrate the code/secret (the wave-1 A1 fix, now
    per-provider).
  - Scopes are ``offline_access User.Read Mail.Read`` and nothing more.
    `offline_access` is how Microsoft issues a refresh_token (the analog of
    Google's access_type=offline+prompt=consent) — without it the cache would be
    useless the moment the access token expires. `User.Read` is REQUIRED for the
    GET /me owner-address lookup that drives direct/cc classification: Mail.Read
    alone does NOT grant /me, so without it every /me poll 403s and no mail ever
    escalates (D2). PKCE S256 is sent (the base flow does).
  - The `common` tenant endpoint works for BOTH personal Outlook.com AND
    work/school M365 accounts.

  AZURE APP REGISTRATION (public client — no secret):
    Register the app under Azure "Mobile and desktop applications" (a PUBLIC
    client), set the redirect URI to ``http://localhost`` (NOT 127.0.0.1 — AAD's
    port-agnostic loopback exception is scoped to `localhost`, and a 127.0.0.1
    redirect fails AADSTS50011), turn "Allow public client flows" ON, and create
    NO client secret (a public client has none; PKCE is the substitute and AAD
    rejects a presented secret with AADSTS700025). The client-secrets JSON needs
    only ``client_id``.

  - Seed the token cache once, on the owner's hardware:
        bin/connectors/outlook.py --authorize --client-secrets <that.json>
    (add --force to replace an existing cache). No secret is ever printed; the
    cache is written 0600 by the base's atomic writer. If you previously
    authorized with an older scope set, re-run with --force so the added
    User.Read scope is consented (a scope change requires re-consent — D2).

Cursor = Microsoft Graph's mail DELTA query. A first run with no cursor SEEDS:
it walks the delta pages to obtain the durable ``@odata.deltaLink`` but emits
NOTHING (never dump the whole mailbox — the same principle as Gmail's historyId
seed). Every later poll GETs the stored deltaLink, emits the new/changed
messages, and stores the fresh deltaLink the response returns.

CURSOR DISCIPLINE (wave-1 blocker): the deltaLink advances ONLY after the full
page set is consumed and every message durably dropped. A page-set that stops
early (max_pages / cap) PARKS the ``@odata.nextLink`` at a clean page boundary
(every prior page fully emitted) and does NOT advance the deltaLink — exactly
like Gmail/GCal park their page tokens. A transient inbox-drop failure mid-page
aborts WITHOUT advancing the cursor at all, so the whole walk re-runs next poll
and re-emits (deltaLinks/skiptokens are reusable; the spine dedups). A 410 Gone
on the deltaLink → the token expired → a full resync (emit-nothing reseed to a
fresh deltaLink), SURFACED in the heartbeat as a loss window (wave-1 E5/E6), not
reported as a clean seed. The 410 handling is a BOUNDED LOOP, not recursion —
one reseed per poll, so a persistently-gone delta endpoint can never blow the
stack (D4).

kind is derived MECHANICALLY from Graph metadata, DIRECT-ADDRESSED FIRST (SEC2 —
never auto-drop a message addressed straight to the owner): ANY of the owner's
addresses (mail + UPN + smtp: proxyAddresses aliases — D1) in toRecipients →
direct; in ccRecipients → cc; else Graph's own inferenceClassification=="other"
→ newsletter; else message. When the owner address set is unknown (the /me
lookup has not succeeded yet) the newsletter/direct branches are SKIPPED and the
message is a plain `message`. CRUCIALLY, Outlook's `newsletter` lane is DIGEST,
NOT drop (D1): inferenceClassification=="other" is Focused-Inbox's soft
per-user importance guess, not a deterministic bulk marker (unlike Gmail's
List-Unsubscribe header), and "Other" routinely holds wanted mail (DL traffic,
first-contact humans, Bcc'd/alias-addressed mail). So Outlook has NO hard-drop
rule — "other" mail is made VISIBLE in the digest, never silently tombstoned.

Stdlib only (urllib via the base's injectable transports — NO msal/requests/azure
libs). Both transports are dependency-injected so unit tests prove refresh-on-
expiry and delta cursor discipline with NO live network. No LLM. Never closes a
workspace.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from assistant import connector  # noqa: E402

SOURCE = "outlook"
NAME = "outlook"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# READ-ONLY scopes — this is a producer; it never sends, drafts, deletes or
# marks read. offline_access is what makes Microsoft issue a refresh_token.
# User.Read (D2) is REQUIRED for the GET /me owner-address lookup: Mail.Read
# alone does NOT grant /me, so without User.Read every /me poll 403s, the owner
# address is never learned, and EVERY message degrades to `message` (digest) —
# direct mail would never escalate. Adding this scope means the owner must re-run
# `--authorize --force` once to re-consent (see the module setup docstring).
OUTLOOK_SCOPES = ("offline_access", "User.Read", "Mail.Read")
# Minimal metadata fields — no message BODY beyond the bodyPreview snippet, and
# never a token. toRecipients/ccRecipients/inferenceClassification are needed for
# the mechanical direct/cc/newsletter classification (SEC2 direct-first). isRead
# is DELIBERATELY OMITTED (D5): it is unused downstream, and including it in the
# delta $select makes Graph re-emit a message on a mark-read/flag long after the
# 30-day dedup spine has pruned it — a needless duplicate decision.
SELECT_FIELDS = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                 "webLink,bodyPreview,inferenceClassification")
# $top bounds the delta page size well under max_events_per_poll so the cap is
# only ever reached at a page BOUNDARY (never mid-page) — which is what keeps the
# parked nextLink a fully-emitted contiguous prefix.
DELTA_START_URL = (f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta"
                   f"?$select={SELECT_FIELDS}&$top=50")


def _addrs(recips) -> set:
    """Lower-cased email addresses from a Graph recipients array."""
    out = set()
    for r in (recips or []):
        if isinstance(r, dict):
            a = ((r.get("emailAddress") or {}).get("address") or "").strip().lower()
            if a:
                out.add(a)
    return out


def _owner_set(account) -> set:
    """The owner's address(es) as a lower-cased set. Accepts a single string
    (the wave-3 shape / the golden fixtures) OR an iterable of addresses (the
    full proxyAddresses+mail+UPN set — D1): a message addressed to ANY of the
    owner's aliases counts as addressed to the owner."""
    if isinstance(account, str):
        vals = [account]
    else:
        vals = list(account or [])
    return {v.strip().lower() for v in vals
            if isinstance(v, str) and v.strip()}


def message_to_event(msg: dict, account_email="") -> dict:
    """One Microsoft Graph message (delta metadata) → one WorldEvent. Pure
    function (the replay fixtures test it directly).

    `kind` is derived MECHANICALLY from Graph's own metadata — never an LLM or a
    lane decision. DIRECT-ADDRESSED WINS FIRST (SEC2): ANY owner address in
    toRecipients → direct; in ccRecipients → cc; else inferenceClassification
    "other" → newsletter; else message. ``account_email`` is a single address
    OR the full owner alias set (proxyAddresses + mail + UPN — D1), so a message
    to an ALIAS still classifies direct and is never mis-laned as newsletter.
    When it is unknown the newsletter/direct/cc branches are skipped (→ message)
    so a direct message is never auto-droppable as newsletter before we can
    confirm it is not direct. Note the Outlook `newsletter` lane is DIGEST, not
    drop (D1): inferenceClassification=="other" is Focused-Inbox's soft
    importance guess, NOT a deterministic bulk marker, so "other" mail is made
    VISIBLE in the digest, never silently tombstoned. Policies do the laning;
    the connector only labels."""
    msg_id = str(msg.get("id") or "?")
    acct = _owner_set(account_email)
    to_addrs = _addrs(msg.get("toRecipients"))
    cc_addrs = _addrs(msg.get("ccRecipients"))
    inference = str(msg.get("inferenceClassification") or "").strip().lower()

    if acct and (to_addrs & acct):
        kind = "direct"
    elif acct and (cc_addrs & acct):
        kind = "cc"
    elif acct and inference == "other":
        kind = "newsletter"
    else:
        kind = "message"

    frm = (msg.get("from") or {}).get("emailAddress") or {}
    sender_addr = str(frm.get("address") or "").strip()
    sender_name = str(frm.get("name") or "").strip()
    if sender_name and sender_addr:
        actor = f"{sender_name} <{sender_addr}>"
    else:
        actor = sender_addr or sender_name

    ts_epoch = connector.eventspine.parse_iso(msg.get("receivedDateTime")) \
        or connector.time.time()

    subject = msg.get("subject") or "(no subject)"
    refs = {"sender": sender_addr} if sender_addr else {}
    return connector.build_world_event(
        source=SOURCE,
        kind=kind,
        external_id=f"outlook:{msg_id}",
        ts_epoch=ts_epoch,
        actor=actor,
        title=subject,
        snippet=msg.get("bodyPreview") or "",   # byte-truncated by the base
        url=msg.get("webLink") or "",           # human-viewable OWA link
        refs=refs,
    )


class OutlookConnector(connector.Connector):
    def __init__(self, *, http=None, oauth_transport=None,
                 token_manager=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._http = http or connector.urllib_transport
        # provider="microsoft": the base pins token_uri/auth_uri to the Microsoft
        # allowlist on both the consent flow AND every refresh.
        self.tokens = token_manager or connector.OAuthTokenManager(
            self.token_path(),
            provider="microsoft",
            token_uri=connector.MICROSOFT_TOKEN_URI,
            skew_sec=int(self.config.get("token_skew_sec",
                                         connector.DEFAULT_TOKEN_SKEW_SEC)),
            transport=oauth_transport)

    # ── authenticated readonly GET ──────────────────────────────────────────

    def _get(self, url: str, token: str) -> tuple:
        return self._http("GET", url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "assistant-connector-outlook",
            # D6: ask Graph for IMMUTABLE ids so a message's id survives a folder
            # move — otherwise a move-out-and-back mints a NEW id, the dedup
            # spine sees it as new, and the same human message is decided twice.
            "Prefer": 'IdType="ImmutableId"',
        })

    def _heartbeat(self, now, errors=None, event_count=None, poll_count=None,
                   status=None):
        # The explicit tri-state status the brief/panel key off. not_configured
        # (opted out) is QUIET (ok:true, errors empty); an errored poll is the
        # only alarming state. Mirrors gmail.py / gcal.py exactly.
        if status is None:
            status = "error" if errors else "ok"
        self.write_heartbeat(
            last_poll_epoch=now,
            token_expiry_epoch=self.tokens.expiry_epoch(),
            errors=errors, event_count=event_count, poll_count=poll_count,
            extra={"status": status})

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()

        # Outlook is OPTIONAL. No token cache means the owner never ran
        # `--authorize` — a CLEAN opted-out state, exactly like Gmail: one QUIET
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

        # Learn the account address once (needed for direct/cc classification).
        # A failure here is non-fatal: classification degrades to the SEC2-safe
        # `message` (never a silent newsletter drop), and the error is surfaced.
        if not cursor.get("email"):
            self._fetch_account_email(now, token, cursor, errors)

        return self._delta(now, token, cursor, errors)

    def _fetch_account_email(self, now, token, cursor, errors) -> None:
        # Needs the User.Read scope (D2) — Mail.Read alone 403s here. Fetch the
        # owner's FULL address set (D1): mail + UPN + every smtp: proxyAddresses
        # alias, so a message to an alias still classifies `direct` and is never
        # mis-laned as a droppable newsletter.
        try:
            status, _h, body = self._get(
                f"{GRAPH_BASE}/me?$select=mail,userPrincipalName,proxyAddresses",
                token)
        except Exception as e:  # noqa: BLE001
            errors.append(f"me: {str(e)[:120]}")
            return
        if status != 200:
            errors.append(f"me status {status}")
            return
        me = _safe_json(body)
        primary = str(me.get("mail") or me.get("userPrincipalName") or "").strip()
        addrs = set()
        for key in ("mail", "userPrincipalName"):
            v = str(me.get(key) or "").strip().lower()
            if v:
                addrs.add(v)
        # proxyAddresses look like ["SMTP:me@contoso.com", "smtp:alias@…", "sip:…"];
        # keep only the smtp: routing aliases (case-insensitive prefix).
        for pa in (me.get("proxyAddresses") or []):
            if isinstance(pa, str) and pa[:5].lower() == "smtp:":
                a = pa[5:].strip().lower()
                if a:
                    addrs.add(a)
        cursor["email"] = primary            # primary, for display/back-compat
        cursor["addrs"] = sorted(addrs)      # full owner alias set (JSON list)

    # ── delta walk (seed | incremental | resume; 410 full resync) ────────────

    def _delta(self, now, token, cursor, errors) -> dict:
        """Walk the Graph mail delta. Emits the new/changed messages, advancing
        the durable ``delta_link`` ONLY when the whole page set is consumed and
        dropped. A truncation PARKS ``resume_link`` at a page boundary; a
        transient drop aborts without advancing; a 410 full-resyncs (emit-nothing
        reseed) and surfaces the loss window in the heartbeat (E5)."""
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        max_pages = int(self.config.get("max_pages",
                                        connector.DEFAULT_MAX_PAGES))
        # The owner's full alias set (mail + UPN + smtp: proxyAddresses — D1),
        # falling back to the single primary address for a cursor seeded before
        # the addrs set was recorded.
        owner = cursor.get("addrs") or (
            [cursor["email"]] if cursor.get("email") else [])

        # Where to start, and whether this walk emits. A parked resume_link wins
        # (mid-walk continuation); else the idle delta_link (incremental); else a
        # fresh seed from the base delta URL (emit nothing — never dump the
        # whole mailbox).
        resume = cursor.get("resume_link")
        delta = cursor.get("delta_link")
        if resume:
            url = resume
            emitting = bool(cursor.get("resume_emitting", True))
            mode = "resume"
        elif delta:
            url = delta
            emitting = True
            mode = "incremental"
        else:
            url = DELTA_START_URL
            emitting = False
            mode = "seed"

        emitted = 0
        malformed = 0
        removed = 0
        seen_ids: set = set()
        pages = 0
        next_link = None
        new_delta = None
        truncated = False
        failed_transient = False
        # D4: a persistent 410 must be BOUNDED. We restructure to a LOOP (never
        # recurse — a mailbox-migration 410 that recurred once per GET blew the
        # stack with ~1000 live GETs). Exactly ONE reseed is allowed per poll:
        # the first 410 clears the pointers and restarts the walk from
        # DELTA_START_URL emitting nothing; a SECOND 410 in the same poll returns
        # an error status + heartbeat instead of looping again.
        reseeded = False

        while True:
            pages += 1
            try:
                status, hdrs, body = self._get(url, token)
            except Exception as e:  # noqa: BLE001 — transient network: stop, keep cursor
                errors.append(f"http: {str(e)[:200]}")
                self._heartbeat(now, errors=errors)
                return {"status": "http_error", "emitted": emitted,
                        "errors": errors}
            if status == 410:
                # The delta token expired — we lost the change window. Full
                # resync: reseed (emit NOTHING to a fresh deltaLink), SURFACING
                # the loss (E5) rather than a clean seed. Bounded to ONE reseed.
                if reseeded:
                    errors.append("410 gone AGAIN after reseed — delta endpoint "
                                  "persistently gone this poll; backing off")
                    self._heartbeat(now, errors=errors)
                    return {"status": "status_410", "emitted": emitted,
                            "errors": errors}
                errors.append("410 gone — delta token expired, full resync "
                              "(loss window)")
                reseeded = True
                url = DELTA_START_URL
                emitting = False
                mode = "seed"
                truncated = False
                next_link = None
                new_delta = None
                seen_ids = set()
                continue
            if status != 200:
                errors.append(f"delta status {status}")
                res = {"status": f"status_{status}", "emitted": emitted,
                       "errors": errors}
                if status in (429, 500, 502, 503, 504):  # OP1: honor backoff
                    ra = connector.parse_retry_after(hdrs, now)
                    if ra is not None:
                        res["retry_after_sec"] = ra
                self._heartbeat(now, errors=errors)
                return res

            data = _safe_json(body)
            items = data.get("value") or []
            # Emit the WHOLE page (atomic; there is no mid-page resume point) with
            # per-item discipline: a poison item is skip-and-count, a transient
            # inbox-drop OSError aborts the walk WITHOUT advancing the cursor.
            for it in items:
                if not isinstance(it, dict):
                    malformed += 1
                    errors.append("delta item is not an object")
                    continue
                if it.get("@removed") is not None:
                    removed += 1          # deletion tombstone — producer skips
                    continue
                mid = str(it.get("id") or "")
                if not mid:
                    malformed += 1
                    errors.append("delta item missing id")
                    continue
                if mid in seen_ids:
                    continue              # de-dup within the batch
                seen_ids.add(mid)
                if not emitting:
                    continue              # seed: walk for the deltaLink only
                try:
                    event = message_to_event(it, owner)
                except Exception as e:  # noqa: BLE001 — poison: skip-and-count
                    malformed += 1
                    errors.append(f"msg {mid[:24]}: malformed: {str(e)[:120]}")
                    continue
                try:
                    self.emit(event, raw=it, now=now)
                except OSError as e:      # transient drop: abort, keep cursor
                    errors.append(f"emit {mid[:24]}: {str(e)[:120]}")
                    failed_transient = True
                    break
                emitted += 1
            if failed_transient:
                break

            next_page = data.get("@odata.nextLink")
            page_delta = data.get("@odata.deltaLink")
            if next_page:
                # A clean page boundary: every prior page is fully emitted. Stop
                # early on the caps and PARK the nextLink (never advance the
                # deltaLink past unfetched pages).
                if pages >= max_pages or emitted >= cap:
                    next_link = next_page
                    truncated = True
                    break
                url = next_page
                continue
            new_delta = page_delta       # final page → the fresh watermark
            break

        # ── persist the cursor per the discipline above ──────────────────────
        new_cursor = dict(cursor)
        if failed_transient:
            # Do NOT advance: keep the pre-poll pointer so the walk re-runs and
            # re-emits next poll (the spine dedups the already-dropped items).
            pass
        elif truncated:
            new_cursor.pop("delta_link", None)
            new_cursor["resume_link"] = next_link
            new_cursor["resume_emitting"] = emitting
        elif new_delta:
            # Walk completed to a fresh deltaLink → advance the durable watermark.
            new_cursor.pop("resume_link", None)
            new_cursor.pop("resume_emitting", None)
            new_cursor["delta_link"] = new_delta
            new_cursor["seeded"] = True
        else:
            # Completed but the response carried no deltaLink (defensive; Graph
            # always returns one). Keep whatever pointer we had and retry.
            errors.append("delta completed without a deltaLink — will retry")

        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self._heartbeat(now, event_count=emitted,
                        poll_count=new_cursor["poll_count"],
                        errors=errors or None)
        # The poll COMPLETED (a 410 already resynced in-line; poison items were
        # skip-and-counted) → report a healthy status so run_forever holds
        # cadence, exactly like Gmail's reseed returns "seeded". The errors (410
        # loss window, poison skips) are SURFACED in the heartbeat (ok=false).
        return {"status": "ok", "emitted": emitted, "errors": errors,
                "mode": mode, "malformed": malformed, "removed": removed,
                "truncated": truncated}


def _safe_json(body) -> dict:
    try:
        data = connector.json.loads(
            body.decode("utf-8") if isinstance(body, (bytes, bytearray))
            else body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_client_secrets(path) -> tuple:
    """Parse a Microsoft OAuth client-secrets JSON → (client_id, client_secret,
    token_uri, auth_uri). Accepts the Google-style ``{"installed"/"web": {...}}``
    wrapper or a bare object, so a hand-written Azure app-registration secrets
    file works. token_uri/auth_uri default to the Microsoft `common` endpoints;
    whatever the file carries is PINNED to the Microsoft allowlist downstream.
    The secret is never logged."""
    raw = connector.json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("client-secrets JSON is not an object")
    block = raw.get("installed") or raw.get("web") or raw
    if not isinstance(block, dict):
        raise ValueError("client-secrets JSON 'installed'/'web' is malformed")
    cid = block.get("client_id")
    # D3: an Azure "Mobile and desktop applications" registration is a PUBLIC
    # client with NO secret — PKCE is the substitute and AAD rejects a presented
    # secret. So require ONLY the client_id; the secret is optional and returned
    # as None when absent (the flow then omits it from the exchange).
    if not cid:
        raise ValueError("client-secrets JSON missing client_id")
    secret = block.get("client_secret") or None
    token_uri = block.get("token_uri") or connector.MICROSOFT_TOKEN_URI
    auth_uri = block.get("auth_uri") or connector.MICROSOFT_AUTH_URI
    return cid, secret, token_uri, auth_uri


def authorize(client_secrets_path, *, force=False, open_url=None,
              code_getter=None, exchange_transport=None) -> Path:
    """The one-time consent flow that MOVES outlook from not_configured → ok.
    REUSES the base's loopback+PKCE installed-app flow with provider="microsoft"
    (the SAME code Gmail/GCal use — not a fork) for the READ-ONLY
    ``offline_access Mail.Read`` scope, seeding token.json through
    OAuthTokenManager.seed (atomic 0600). Prints NO secret. Fully unit-testable
    via the two injection points."""
    cid, secret, token_uri, auth_uri = _load_client_secrets(client_secrets_path)
    c = OutlookConnector()
    tok = connector.run_installed_app_flow(
        client_id=cid, client_secret=secret, scopes=list(OUTLOOK_SCOPES),
        provider="microsoft", token_uri=token_uri, auth_uri=auth_uri,
        open_url=open_url, code_getter=code_getter,
        exchange_transport=exchange_transport)
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
                    help="path to the Microsoft OAuth client-secrets JSON "
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

    c = OutlookConnector(dry_run=args.dry_run, record=args.record,
                         log=lambda m: print(m, file=sys.stderr))

    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0

    if not c.token_path().exists():
        print("Outlook not configured — run: bin/connectors/outlook.py "
              "--authorize --client-secrets <path> "
              "(daemon will keep re-checking)", file=sys.stderr)
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
