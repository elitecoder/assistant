#!/usr/bin/env python3
"""jira.py — read-only JIRA connector (Keel M5 wave 2).

WHY: a ticket assigned to Mukul, an @-mention on an issue, a status flip on
something he watches — all world events that today live only in a browser tab.
This connector turns each JIRA *change* into a normalized WorldEvent and drops
it into the inbox for the policy spine to lane.

WHY THE M5w2 REWORK (this file is a rebuild, not a patch): the first cut modeled
"one event per issue snapshot", keyed by the latest changelog id, cursored by a
rendered *minute* string, paged by ``startAt``. That was broken on five
independent, silent-data-loss axes, so the model itself is replaced:

  1. WATERMARK = EPOCH MILLISECONDS (not a minute string). The cursor stores an
     integer ``watermark_ms`` and the JQL is ``updated >= <ms>``. JQL evaluates a
     bare epoch-millis literal timezone-INDEPENDENTLY, which kills TWO blockers at
     once: the old rendered-UTC watermark was compared by JIRA in the *profile*
     timezone (a Pacific user's watermark sat ~7-8h in the future → permanent
     rolling silent loss), and the old whole-minute granularity meant >cap issues
     sharing one minute could never drain (``>=`` re-fetched the same first cap
     forever). Millisecond resolution dissolves the whole-minute collisions and
     the epoch-millis literal is TZ-safe. We keep ``>=`` (overlap) and let the
     spine's stable-external_id dedup absorb the boundary re-fetch. The watermark
     advances ONLY through the contiguous successfully-emitted issue prefix
     (the wave-1 cursor-discipline invariant) and parks at the last emitted
     ``updated_ms`` on truncation.

  2. EVENTS COME FROM DELTA OBJECTS, NOT THE ISSUE SNAPSHOT. One event per NEW
     changelog history (``jira:<key>:changelog:<history_id>``) AND one per
     new/updated comment (``jira:<key>:comment:<comment_id>``). Both ids are
     native and monotonic. This structurally fixes the killer defect: a real
     JIRA comment bumps the issue's ``updated`` but appends NO changelog entry,
     so the old snapshot model reused the previous changelog id → external_id
     collision → the spine swallowed the comment and the mention lane never fired
     live. Now a comment is its own event with its own id, so a field-change poll
     followed by a comment-only poll yields TWO spine events, not one swallowed
     (the verifier's repro; see the regression fixture + test). It also fixes the
     coalescing of a multi-change poll into one "latest kind" event (each history
     is emitted) and makes re-emit idempotent (native ids dedup).

  3. MENTION DETECTION VIA accountId, NOT EMAIL. Real Cloud renders a mention as
     ``[~accountid:5b10...]`` (wiki markup) or an ADF ``{"type":"mention",
     "attrs":{"id":...}}`` node — never the bare email. The owner's accountId
     comes from ``GET /rest/api/3/myself`` and is matched against both markups.
     Email-substring matching against the body was dead on real Cloud.

  4. PAGINATION VIA nextPageToken (``/rest/api/3/search/jql``), NOT startAt. The
     old ``/rest/api/2/search`` is deprecated/removed on Cloud, and ``startAt``
     offset paging over a mutating ``updated ASC`` result skips any row that
     slides across a page boundary mid-pagination. The new endpoint is token-
     paged (we GET it — still read-only), and the watermark is anchored on the
     last emitted issue's ``updated_ms`` (keyset), so a mid-pagination update
     can never cause a skip.

  5. DEFAULT JQL SCOPED TO THE USER. The default is ``(assignee = currentUser()
     OR reporter = currentUser() OR watcher = currentUser())`` AND the
     ``updated >=`` clause — relevance, not whole-instance ingestion (which was
     both digest churn and a PII over-collection). ``JIRA_JQL`` overrides/widens
     the scope clause.

  6. HTTPS PIN. A non-``https`` JIRA_BASE_URL is rejected up front so a
     misconfigured ``http://`` base can never leak the PAT in cleartext.

Pure PRODUCER: the ONLY HTTP verb is GET (against ``/myself`` and the
``search/jql`` read endpoint). It never transitions, comments on, or edits an
issue. A grep CI test proves no mutation/send API is called.

Auth (design section 9, JIRA row): a Personal Access Token, NOT OAuth. The PAT
(plus base URL + account email for JIRA Cloud basic auth) is read from the
environment — the spawn-connector.sh launcher sources ~/.zprofile exactly like
slack-reactor, so the token never lives in a plist or this file. The credential
provider is dependency-injected (mirroring github-notifications.py's token
provider) so unit tests never touch the network or a real token. When the PAT
env var is absent the connector is a QUIET not_configured.

Stdlib only (urllib via the base's injectable transport). No LLM.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from assistant import connector  # noqa: E402

SOURCE = "jira"
NAME = "jira"

# Env var names (read via the spawn-sh ~/.zprofile source). BASE_URL like
# https://acme.atlassian.net ; EMAIL is the account for JIRA Cloud basic auth.
ENV_BASE_URL = "JIRA_BASE_URL"
ENV_EMAIL = "JIRA_EMAIL"
ENV_TOKEN = "JIRA_API_TOKEN"
ENV_JQL = "JIRA_JQL"

# SCOPE DECISION (design section 9): default to the issues that are actually
# RELEVANT to the owner — assigned to, reported by, or watched by currentUser()
# — not the entire JIRA instance. Widen/override via $JIRA_JQL.
DEFAULT_SCOPE = ("assignee = currentUser() OR reporter = currentUser() "
                 "OR watcher = currentUser()")

# The new Cloud search endpoint (token-paged). We issue it as a GET so the
# read-only guard (no POST in a connector) stays green while still migrating off
# the deprecated /rest/api/2/search + startAt.
SEARCH_PATH = "/rest/api/3/search/jql"
MYSELF_PATH = "/rest/api/3/myself"
# Fields we need to shape events + deltas. `comment` returns the issue's recent
# comments inline (the delta source for comment/mention events); `expand=changelog`
# returns the histories (the delta source for field-change events).
SEARCH_FIELDS = "summary,updated,status,assignee,reporter,priority,comment"


def jira_credentials() -> tuple:
    """Default credential provider — (base_url, email, token) from the
    environment. Raises when the PAT (or base URL) is absent so poll_once can map
    that to the QUIET not_configured state. Injected away in unit tests; the
    token is never logged or archived.

    HTTPS PIN (finding 6): a base URL whose scheme is not https is REJECTED here
    — a Basic/Bearer PAT sent over http:// leaks in cleartext, so a misconfigured
    base must fail loudly, never silently downgrade."""
    base = os.environ.get(ENV_BASE_URL, "").strip().rstrip("/")
    email = os.environ.get(ENV_EMAIL, "").strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not base or not token:
        raise RuntimeError("JIRA not configured (JIRA_BASE_URL / "
                           "JIRA_API_TOKEN absent)")
    _assert_https(base)
    return base, email, token


def _assert_https(base: str) -> None:
    """Refuse a non-https base URL (finding 6). A ValueError here surfaces as a
    poll error, never a silent PAT-over-http leak."""
    scheme = connector.urllib.parse.urlparse(base).scheme.lower()
    if scheme != "https":
        raise ValueError(
            f"JIRA_BASE_URL must be https:// (got {scheme or 'no'}-scheme) — "
            "refusing to send the PAT over a non-TLS connection")


def _basic_auth_header(email: str, token: str) -> str:
    """JIRA Cloud authenticates the REST API with Basic email:token. When no
    email is set (JIRA Server PAT) fall back to a Bearer token."""
    if email:
        raw = f"{email}:{token}".encode("utf-8")
        return "Basic " + connector.base64.b64encode(raw).decode("ascii")
    return f"Bearer {token}"


# ─── time helpers (epoch-ms watermark, TZ-safe) ──────────────────────────────

def _normalize_iso(s):
    """JIRA emits ``2026-07-10T12:00:00.000+0000`` — normalize the offset to the
    ``+00:00`` form parse_iso accepts and strip the millis it can't (py3.9's
    fromisoformat is strict about both)."""
    if not isinstance(s, str) or not s:
        return s
    v = s
    if "." in v:
        head, _, tail = v.partition(".")
        tz = ""
        for i, ch in enumerate(tail):
            if ch in "+-Z":
                tz = tail[i:]
                break
        v = head + tz
    if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":
        v = v[:-2] + ":" + v[-2:]
    return v


def _epoch_ms(iso_str):
    """A JIRA timestamp (with its explicit offset) → epoch MILLISECONDS (int) or
    None. Parsing the offset-carrying string is what makes the watermark
    timezone-safe: the instant is absolute regardless of the server's or the
    owner's profile timezone."""
    secs = connector.eventspine.parse_iso(_normalize_iso(iso_str))
    if secs is None:
        return None
    return int(round(secs * 1000))


def build_jql(watermark_ms, scope: str = "") -> str:
    """The incremental JQL. ``updated >= <epoch_ms>`` — a bare epoch-millis
    literal, which JQL evaluates timezone-INDEPENDENTLY (killing the profile-TZ
    blocker) and at millisecond resolution (killing the whole-minute >cap
    starvation). ``>=`` (not ``>``) keeps a boundary overlap the spine dedups.
    ORDER BY updated ASC makes the emitted prefix contiguous in watermark order,
    which the cursor discipline relies on. The scope clause defaults to the
    owner's issues (finding 5); $JIRA_JQL overrides it."""
    clause = (f"updated >= {int(watermark_ms)}" if watermark_ms
              else "updated >= -30d")
    scope = (scope or DEFAULT_SCOPE).strip()
    jql = f"({scope}) AND ({clause})" if scope else clause
    return jql + " ORDER BY updated ASC"


# ─── mention detection (accountId — markup AND ADF nodes) ────────────────────

def _adf_mentions_account(node, account_id: str) -> bool:
    """Recursively scan an Atlassian Document Format (v3) body for a mention node
    ``{"type":"mention","attrs":{"id":"<accountId>"}}`` targeting the owner."""
    if isinstance(node, dict):
        if node.get("type") == "mention":
            if str((node.get("attrs") or {}).get("id") or "") == account_id:
                return True
        for v in node.values():
            if _adf_mentions_account(v, account_id):
                return True
    elif isinstance(node, list):
        for it in node:
            if _adf_mentions_account(it, account_id):
                return True
    return False


def _comment_mentions_account(comment: dict, account_id: str) -> bool:
    """True iff the comment @-mentions the owner by accountId. Handles BOTH the
    v2 wiki-markup body (a string containing ``[~accountid:<id>]``) and the v3
    ADF body (a dict with mention nodes). Email-substring matching is dead on
    real Cloud, so it is deliberately NOT used here (finding 3)."""
    if not account_id:
        return False
    body = comment.get("body")
    if isinstance(body, str):
        return (f"[~accountid:{account_id}]" in body
                or f"[~{account_id}]" in body)
    if isinstance(body, dict):
        return _adf_mentions_account(body, account_id)
    return False


# ─── kind derivation (mechanical, from the delta object) ─────────────────────

def _changelog_kind(history: dict, account_id: str = "") -> str:
    """WorldEvent kind for a changelog history from its items — an OBJECTIVE
    derivation the policy engine matches (assigned→staged, status/priority→digest,
    flagged→escalate, else updated). Precedence most-actionable first."""
    items = history.get("items") if isinstance(history, dict) else None
    items = items or []
    fields_changed = set()
    assignee_to_me = False
    flagged = False
    for it in items:
        if not isinstance(it, dict):
            continue
        field = str(it.get("field") or "").lower()
        fields_changed.add(field)
        if field == "assignee":
            to_id = str(it.get("to") or "")
            if account_id and to_id == account_id:
                assignee_to_me = True
        if field in ("flagged", "impediment"):
            flagged = True
    if flagged:
        return "flagged"
    if assignee_to_me or (not account_id and "assignee" in fields_changed):
        return "assigned"
    if "status" in fields_changed:
        return "status_change"
    if "priority" in fields_changed:
        return "priority_change"
    return "updated"


# ─── issue context + pure delta → event builders (fixture-tested) ────────────

def _issue_context(issue: dict) -> dict:
    """Extract the shared per-issue facts an event needs (key, summary, status,
    priority, base url) once, so each delta event is built from the same
    snapshot."""
    key = str(issue.get("key") or "?")
    fields = issue.get("fields") or {}
    base = issue.get("_base_url") or ""
    return {
        "key": key,
        "summary": fields.get("summary") or key,
        "status": ((fields.get("status") or {}).get("name") or ""),
        "priority": ((fields.get("priority") or {}).get("name") or ""),
        "base_url": base,
        "url": f"{base}/browse/{key}" if base else "",
    }


def _snippet(ctx: dict, kind: str) -> str:
    s = f"{ctx['key']} · {kind}"
    if ctx["status"]:
        s += f" · {ctx['status']}"
    if ctx["priority"]:
        s += f" · {ctx['priority']}"
    return s


def _refs(ctx: dict) -> dict:
    refs = {"jira": ctx["key"]}
    if ctx["status"]:
        refs["status"] = ctx["status"]
    return refs


def changelog_to_event(issue: dict, history: dict, account_id: str = "") -> dict:
    """One changelog history → one WorldEvent. Pure function (replay-fixture
    tested). external_id ``jira:<key>:changelog:<history_id>`` — a native
    monotonic id, so a field change is its own event and never collides with a
    comment."""
    ctx = _issue_context(issue)
    hid = str(history.get("id") or "?")
    kind = _changelog_kind(history, account_id)
    author = ((history.get("author") or {}).get("displayName") or "")
    ts_epoch = _epoch_ms(history.get("created"))
    ts_epoch = (ts_epoch / 1000.0) if ts_epoch is not None else connector.time.time()
    return connector.build_world_event(
        source=SOURCE, kind=kind,
        external_id=f"jira:{ctx['key']}:changelog:{hid}",
        ts_epoch=ts_epoch, actor=author, title=ctx["summary"],
        snippet=_snippet(ctx, kind), url=ctx["url"], refs=_refs(ctx))


def comment_to_event(issue: dict, comment: dict, account_id: str = "") -> dict:
    """One comment → one WorldEvent. Pure function (replay-fixture tested).
    external_id ``jira:<key>:comment:<comment_id>``. kind is ``mention`` iff the
    comment @-mentions the owner by accountId (markup or ADF node), else
    ``comment`` — so a comment can never be swallowed by a changelog id, and a
    mention escalates while a plain comment digests."""
    ctx = _issue_context(issue)
    cid = str(comment.get("id") or "?")
    kind = "mention" if _comment_mentions_account(comment, account_id) else "comment"
    author = ((comment.get("author") or {}).get("displayName")
              or (comment.get("updateAuthor") or {}).get("displayName") or "")
    ts_epoch = _comment_ms(comment)
    ts_epoch = (ts_epoch / 1000.0) if ts_epoch is not None else connector.time.time()
    return connector.build_world_event(
        source=SOURCE, kind=kind,
        external_id=f"jira:{ctx['key']}:comment:{cid}",
        ts_epoch=ts_epoch, actor=author, title=ctx["summary"],
        snippet=_snippet(ctx, kind), url=ctx["url"], refs=_refs(ctx))


def _comment_ms(comment: dict):
    """A comment's watermark instant — its ``updated`` (an edit bumps it) or
    ``created``, in epoch ms."""
    return (_epoch_ms(comment.get("updated"))
            if comment.get("updated") else None) or _epoch_ms(
        comment.get("created"))


def issue_deltas(issue: dict, account_id: str = "",
                 watermark_ms: int = 0) -> list:
    """All NEW delta events for one issue since ``watermark_ms``: one per
    changelog history created at/after the watermark AND one per comment
    updated/created at/after it, ordered by their own timestamp so emission is
    monotonic. Pure function — the golden replay fixtures test it directly,
    including the field-change-then-comment → TWO events regression."""
    events = []
    fields = issue.get("fields") or {}
    hists = ((issue.get("changelog") or {}).get("histories") or [])
    if isinstance(hists, list):
        for h in hists:
            if not isinstance(h, dict):
                continue
            hms = _epoch_ms(h.get("created"))
            if hms is None or hms >= watermark_ms:
                events.append((hms if hms is not None else 0,
                               changelog_to_event(issue, h, account_id)))
    comments = ((fields.get("comment") or {}).get("comments") or [])
    if isinstance(comments, list):
        for cm in comments:
            if not isinstance(cm, dict):
                continue
            cms = _comment_ms(cm)
            if cms is None or cms >= watermark_ms:
                events.append((cms if cms is not None else 0,
                               comment_to_event(issue, cm, account_id)))
    events.sort(key=lambda t: t[0])
    return [e for _, e in events]


class JiraConnector(connector.Connector):
    def __init__(self, *, credentials_provider=None, http=None,
                 jql_scope=None, account_id=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._creds = credentials_provider or jira_credentials
        self._http = http or connector.urllib_transport
        # $JIRA_JQL overrides/widens the default currentUser() scope clause.
        self._jql_scope = (jql_scope if jql_scope is not None
                           else os.environ.get(ENV_JQL, "").strip())
        # accountId is normally discovered from /myself each poll; an injected
        # value lets a unit test skip that round-trip.
        self._account_id = account_id

    def _heartbeat_not_configured(self, now) -> None:
        self.write_heartbeat(
            last_poll_epoch=now,
            extra={"status": connector.STATE_NOT_CONFIGURED})

    def _get(self, url: str, auth: str) -> tuple:
        return self._http("GET", url, headers={
            "Authorization": auth,
            "Accept": "application/json",
            "User-Agent": "assistant-connector-jira",
        })

    def _fetch_account_id(self, base: str, auth: str, errors: list):
        """GET /rest/api/3/myself → the owner's accountId (for mention detection
        and, informationally, the profile timeZone). Best-effort: a failure just
        degrades mention detection, never crashes the poll."""
        if self._account_id is not None:
            return self._account_id
        try:
            status, _h, body = self._get(base + MYSELF_PATH, auth)
        except Exception as e:  # noqa: BLE001
            errors.append(f"myself: {str(e)[:120]}")
            return ""
        if status != 200:
            errors.append(f"myself status {status}")
            return ""
        data = _safe_json(body)
        return str(data.get("accountId") or "")

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()
        cursor = self.load_cursor()
        errors: list = []

        try:
            base, email, token = self._creds()
        except ValueError as e:  # https pin / config validation → surface it
            errors.append(str(e)[:200])
            self.write_heartbeat(last_poll_epoch=now, errors=errors,
                                 extra={"status": "error"})
            return {"status": "config_error", "emitted": 0, "errors": errors}
        except Exception:  # noqa: BLE001 — absent PAT is not_configured, quiet
            self._heartbeat_not_configured(now)
            return {"status": "not_configured", "emitted": 0, "errors": []}

        # HTTPS PIN (finding 6), enforced here too so an injected/misconfigured
        # provider can never send the PAT over http:// — validated BEFORE any
        # request goes out.
        try:
            _assert_https(base)
        except ValueError as e:
            errors.append(str(e)[:200])
            self.write_heartbeat(last_poll_epoch=now, errors=errors,
                                 extra={"status": "error"})
            return {"status": "config_error", "emitted": 0, "errors": errors}

        auth = _basic_auth_header(email, token)
        account_id = self._fetch_account_id(base, auth, errors)
        watermark_ms = int(cursor.get("watermark_ms") or 0)
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        max_pages = int(self.config.get("max_pages",
                                        connector.DEFAULT_MAX_PAGES))
        jql = build_jql(watermark_ms, self._jql_scope)

        # ── fetch ALL pages (ascending updated) via nextPageToken BEFORE
        #    emitting/advancing — no startAt (finding 4) ──────────────────────
        collected: list = []
        page_token = None
        pages = 0
        truncated = False
        while True:
            pages += 1
            params = [
                f"jql={connector.urllib.parse.quote(jql)}",
                "maxResults=100",
                f"fields={connector.urllib.parse.quote(SEARCH_FIELDS)}",
                "expand=changelog",
            ]
            if page_token:
                params.append(
                    f"nextPageToken={connector.urllib.parse.quote(page_token)}")
            url = base + SEARCH_PATH + "?" + "&".join(params)
            try:
                status, hdrs, body = self._get(url, auth)
            except Exception as e:  # noqa: BLE001
                errors.append(f"http: {str(e)[:200]}")
                self.write_heartbeat(last_poll_epoch=now, errors=errors,
                                     extra={"status": "error"})
                return {"status": "http_error", "emitted": 0, "errors": errors}
            if status != 200:
                errors.append(f"search status {status}")
                res = {"status": f"status_{status}", "emitted": 0,
                       "errors": errors}
                if status in (403, 429):
                    ra = connector.parse_retry_after(hdrs, now)
                    if ra is not None:
                        res["retry_after_sec"] = ra
                self.write_heartbeat(last_poll_epoch=now, errors=errors,
                                     extra={"status": "error"})
                return res
            data = _safe_json(body)
            issues = data.get("issues") or []
            for it in issues:
                if isinstance(it, dict):
                    it["_base_url"] = base
                    collected.append(it)
            page_token = data.get("nextPageToken")
            is_last = bool(data.get("isLast")) or not page_token
            if is_last:
                break
            if pages >= max_pages:
                truncated = True
                break

        # ── emit in ascending order; watermark advances through the CONTIGUOUS
        #    successfully-emitted ISSUE prefix only, parking at the last emitted
        #    updated_ms on truncation (findings 1 + 4) ─────────────────────────
        emitted = 0
        malformed = 0
        last_safe_ms = None
        for issue in collected:
            if emitted >= cap:
                truncated = True
                break
            try:
                iss_ms = _epoch_ms((issue.get("fields") or {}).get("updated") or "")
                deltas = issue_deltas(issue, account_id, watermark_ms)
            except Exception as e:  # noqa: BLE001 — poison issue: skip+count
                malformed += 1
                errors.append(f"issue {issue.get('key', '?')}: "
                              f"malformed: {str(e)[:120]}")
                continue
            issue_ok = True
            for event in deltas:
                if emitted >= cap:
                    truncated = True
                    issue_ok = False  # issue only partially emitted → don't advance
                    break
                try:
                    self.emit(event, raw=_sanitize_issue(issue), now=now)
                except OSError as e:
                    # Transient drop failure — STOP so the watermark parks BEFORE
                    # this issue and it re-fetches next poll (no loss).
                    errors.append(f"emit {issue.get('key', '?')}: {str(e)[:120]}")
                    issue_ok = False
                    break
                emitted += 1
            if not issue_ok:
                break
            # Whole issue emitted safely → its updated_ms is now the high-water.
            if iss_ms is not None:
                last_safe_ms = iss_ms

        new_cursor = dict(cursor)
        if last_safe_ms is not None:
            new_cursor["watermark_ms"] = last_safe_ms
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self.write_heartbeat(last_poll_epoch=now, event_count=emitted,
                             poll_count=new_cursor["poll_count"],
                             errors=errors or None,
                             extra={"status": "error" if errors else "ok"})
        return {"status": "ok", "emitted": emitted, "errors": errors,
                "malformed": malformed, "truncated": truncated,
                "watermark_ms": new_cursor.get("watermark_ms")}


def _sanitize_issue(issue: dict) -> dict:
    """Metadata-only copy for the raw archive — strips the injected base url."""
    return {k: v for k, v in issue.items() if k != "_base_url"}


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
    c = JiraConnector(dry_run=args.dry_run, record=args.record,
                      log=lambda m: print(m, file=sys.stderr))

    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0

    try:
        c._creds()
    except Exception:  # noqa: BLE001 — absent PAT is not an error, just a hint
        print("JIRA not configured — set JIRA_BASE_URL / JIRA_EMAIL / "
              "JIRA_API_TOKEN in ~/.zprofile (daemon will keep re-checking)",
              file=sys.stderr)
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
