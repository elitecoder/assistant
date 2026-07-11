#!/usr/bin/env python3
"""jira.py — read-only JIRA connector (Keel M5 wave 2).

WHY: a ticket assigned to Mukul, an @-mention on an issue, a status flip on
something he's watching — all world events that today live only in a browser
tab. This connector turns each JIRA issue update into a normalized WorldEvent
and drops it into the inbox for the policy spine to lane.

Pure PRODUCER: the ONLY HTTP verb is GET against the JIRA REST search API. It
never transitions, comments on, or edits an issue. A grep CI test proves no
mutation API is called.

Auth (design section 9, JIRA row): a Personal Access Token, NOT OAuth. The PAT
(plus base URL + account email for JIRA Cloud basic auth) is read from the
environment — the spawn-connector.sh launcher sources ~/.zprofile exactly like
slack-reactor, so the token never lives in a plist or this file. The token
provider is dependency-injected (mirroring github-notifications.py's
gh_cli_token) so unit tests never touch the network or a real token. When the
PAT env var is absent the connector is a QUIET not_configured — the owner simply
has not connected JIRA.

Cursor = the last-seen ``updated`` watermark. Each poll runs
``updated >= "<watermark>" ORDER BY updated ASC`` (JIRA's JQL ``updated`` has
minute granularity — a strict ``>`` would drop same-minute siblings, so we use
``>=`` at the minute floor and let the event spine dedup the boundary re-fetch
by the stable external_id). The watermark advances ONLY through the contiguous
successfully-emitted prefix (the wave-1 cursor-discipline blocker): a transient
failure on issue k parks the watermark at issue k-1 so k and everything after it
re-fetch next poll; a poison issue is skip-and-counted (it can never succeed, so
it must not wedge the issues behind it) and surfaced in the heartbeat.

external_id ``jira:<key>:<changelog_id>`` — a new change on the same issue bumps
the changelog id → a new event; a re-fetch of the same change dedups.

Stdlib only (urllib via the base's injectable transport). No LLM.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
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


def jira_credentials() -> tuple:
    """Default credential provider — (base_url, email, token) from the
    environment. Raises when the PAT (or base URL) is absent so poll_once can map
    that to the QUIET not_configured state. Injected away in unit tests; the
    token is never logged or archived."""
    base = os.environ.get(ENV_BASE_URL, "").strip().rstrip("/")
    email = os.environ.get(ENV_EMAIL, "").strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not base or not token:
        raise RuntimeError("JIRA not configured (JIRA_BASE_URL / "
                           "JIRA_API_TOKEN absent)")
    return base, email, token


def _basic_auth_header(email: str, token: str) -> str:
    """JIRA Cloud authenticates the REST API with Basic email:token. When no
    email is set (JIRA Server PAT) fall back to a Bearer token."""
    if email:
        raw = f"{email}:{token}".encode("utf-8")
        return "Basic " + connector.base64.b64encode(raw).decode("ascii")
    return f"Bearer {token}"


def _fmt_watermark(epoch: float) -> str:
    """A JQL-safe minute-floored timestamp: 'yyyy-MM-dd HH:mm'. Minute
    granularity is all JIRA's ``updated`` field supports, so this is the finest
    watermark the query can honor."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def build_jql(watermark: str, extra: str = "") -> str:
    """The incremental JQL. ``>=`` (not ``>``) at the minute floor so same-minute
    siblings are never skipped; the spine dedups the boundary overlap by the
    stable external_id. ORDER BY updated ASC makes the emitted prefix contiguous
    in watermark order, which the cursor discipline relies on."""
    clause = f'updated >= "{watermark}"' if watermark else "updated >= -30d"
    if extra:
        clause = f"({clause}) AND ({extra})"
    return clause + " ORDER BY updated ASC"


# ─── mechanical kind derivation (from the changelog, never a judgment) ───────

def _latest_history(issue: dict):
    hists = ((issue.get("changelog") or {}).get("histories") or [])
    if not isinstance(hists, list) or not hists:
        return None
    # histories are returned newest-last by JIRA when ORDER-agnostic; sort by
    # id so "latest" is deterministic regardless of API ordering.
    def _hid(h):
        try:
            return int(h.get("id") or 0)
        except (TypeError, ValueError):
            return 0
    return max(hists, key=_hid)


def _changelog_id(issue: dict) -> str:
    h = _latest_history(issue)
    if h and h.get("id"):
        return str(h["id"])
    # No changelog exposed (e.g. a brand-new issue) — synthesize a stable id
    # from the updated timestamp so the external_id is still stable per state.
    updated = (issue.get("fields") or {}).get("updated") or ""
    return "u" + str(updated).replace(" ", "").replace(":", "")


def _account_mentioned(issue: dict, account: str) -> bool:
    if not account:
        return False
    acct = account.lower()
    fields = issue.get("fields") or {}
    comments = ((fields.get("comment") or {}).get("comments") or [])
    if isinstance(comments, list) and comments:
        body = str(comments[-1].get("body") or "").lower()
        if acct in body:
            return True
    return False


def derive_kind(issue: dict, account: str = "") -> str:
    """WorldEvent kind from the issue's latest changelog + comments — an
    OBJECTIVE derivation the policy engine can match (mention→escalate,
    assigned→staged, status_change/priority_change/updated→digest, …). Precedence
    is most-actionable first."""
    if _account_mentioned(issue, account):
        return "mention"
    hist = _latest_history(issue)
    items = (hist.get("items") if isinstance(hist, dict) else None) or []
    fields_changed = set()
    assignee_to_me = False
    flagged = False
    for it in items:
        if not isinstance(it, dict):
            continue
        field = str(it.get("field") or "").lower()
        fields_changed.add(field)
        if field == "assignee":
            to_str = f"{it.get('to','')} {it.get('toString','')}".lower()
            if account and account.lower() in to_str:
                assignee_to_me = True
        if field in ("flagged", "impediment"):
            flagged = True
    if flagged:
        return "flagged"
    if assignee_to_me or (account == "" and "assignee" in fields_changed):
        return "assigned"
    if "status" in fields_changed:
        return "status_change"
    if "priority" in fields_changed:
        return "priority_change"
    if "comment" in fields_changed:
        return "comment"
    return "updated"


def issue_to_event(issue: dict, account: str = "") -> dict:
    """One JIRA issue (fields + changelog) → one WorldEvent. Pure function (the
    replay fixtures test it directly)."""
    key = str(issue.get("key") or "?")
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or key
    updated = fields.get("updated") or ""
    reporter = ((fields.get("assignee") or {}).get("displayName")
                or (fields.get("reporter") or {}).get("displayName") or "")
    status_name = ((fields.get("status") or {}).get("name") or "")
    priority = ((fields.get("priority") or {}).get("name") or "")
    changelog_id = _changelog_id(issue)
    kind = derive_kind(issue, account)
    ts_epoch = connector.eventspine.parse_iso(_normalize_iso(updated))
    if ts_epoch is None:
        ts_epoch = connector.time.time()
    base = issue.get("_base_url") or ""
    url = f"{base}/browse/{key}" if base else ""
    refs = {"jira": key}
    if status_name:
        refs["status"] = status_name
    snippet = f"{key} · {kind}" + (f" · {status_name}" if status_name else "")
    if priority:
        snippet += f" · {priority}"
    return connector.build_world_event(
        source=SOURCE,
        kind=kind,
        external_id=f"jira:{key}:{changelog_id}",
        ts_epoch=ts_epoch,
        actor=reporter,
        title=summary,
        snippet=snippet,
        url=url,
        refs=refs,
    )


def _normalize_iso(s):
    """JIRA emits ``2026-07-10T12:00:00.000+0000`` — normalize the offset to
    ``+00:00`` form parse_iso accepts, and strip millis it can't."""
    if not isinstance(s, str) or not s:
        return s
    v = s
    if "." in v:
        head, _, tail = v.partition(".")
        # keep any trailing timezone after the millis
        tz = ""
        for i, ch in enumerate(tail):
            if ch in "+-Z":
                tz = tail[i:]
                break
        v = head + tz
    if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":
        v = v[:-2] + ":" + v[-2:]
    return v


class JiraConnector(connector.Connector):
    def __init__(self, *, credentials_provider=None, http=None,
                 jql_extra=None, account=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._creds = credentials_provider or jira_credentials
        self._http = http or connector.urllib_transport
        self._jql_extra = (jql_extra if jql_extra is not None
                           else os.environ.get("JIRA_JQL", "").strip())
        self._account = (account if account is not None
                         else os.environ.get(ENV_EMAIL, "").strip())

    def _heartbeat_not_configured(self, now) -> None:
        self.write_heartbeat(
            last_poll_epoch=now,
            extra={"status": connector.STATE_NOT_CONFIGURED})

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()
        cursor = self.load_cursor()
        errors: list = []

        try:
            base, email, token = self._creds()
        except Exception:  # noqa: BLE001 — absent PAT is not_configured, quiet
            self._heartbeat_not_configured(now)
            return {"status": "not_configured", "emitted": 0, "errors": []}

        auth = _basic_auth_header(email, token)
        account = self._account or email
        watermark = cursor.get("watermark") or ""
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        max_pages = int(self.config.get("max_pages",
                                        connector.DEFAULT_MAX_PAGES))
        jql = build_jql(watermark, self._jql_extra)

        # ── fetch ALL pages (ascending updated) BEFORE emitting/advancing ────
        collected: list = []
        start_at = 0
        pages = 0
        truncated = False
        while True:
            pages += 1
            url = (f"{base}/rest/api/2/search"
                   f"?jql={connector.urllib.parse.quote(jql)}"
                   f"&startAt={start_at}&maxResults=100"
                   "&expand=changelog"
                   "&fields=summary,updated,status,assignee,reporter,"
                   "priority,comment")
            try:
                status, hdrs, body = self._http("GET", url, headers={
                    "Authorization": auth,
                    "Accept": "application/json",
                    "User-Agent": "assistant-connector-jira",
                })
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
            total = data.get("total")
            start_at += len(issues)
            if not issues or (isinstance(total, int) and start_at >= total):
                break
            if pages >= max_pages or len(collected) >= cap:
                truncated = True
                break

        # ── emit in ascending order; watermark advances through the CONTIGUOUS
        #    successfully-emitted prefix only (E1/E2/E4) ───────────────────────
        emitted = 0
        malformed = 0
        last_safe_epoch = None
        for issue in collected:
            if emitted >= cap:
                truncated = True
                break
            try:
                event = issue_to_event(issue, account)
            except Exception as e:  # noqa: BLE001 — poison: skip+count, no wedge
                malformed += 1
                errors.append(f"issue {issue.get('key','?')}: "
                              f"malformed: {str(e)[:120]}")
                continue
            try:
                self.emit(event, raw=_sanitize_issue(issue), now=now)
            except OSError as e:
                # A drop failure is transient — STOP here so the watermark parks
                # before this issue and it re-fetches next poll (no loss).
                errors.append(f"emit {issue.get('key','?')}: {str(e)[:120]}")
                break
            emitted += 1
            ue = connector.eventspine.parse_iso(
                _normalize_iso((issue.get("fields") or {}).get("updated") or ""))
            if ue is not None:
                last_safe_epoch = ue

        new_cursor = dict(cursor)
        if last_safe_epoch is not None:
            new_cursor["watermark"] = _fmt_watermark(last_safe_epoch)
            new_cursor["watermark_epoch"] = last_safe_epoch
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self.write_heartbeat(last_poll_epoch=now, event_count=emitted,
                             poll_count=new_cursor["poll_count"],
                             errors=errors or None,
                             extra={"status": "error" if errors else "ok"})
        return {"status": "ok", "emitted": emitted, "errors": errors,
                "malformed": malformed, "truncated": truncated,
                "watermark": new_cursor.get("watermark")}


def _sanitize_issue(issue: dict) -> dict:
    """Metadata-only copy for the raw archive — strips the injected base url and
    keeps no secret (there is none in the issue payload anyway)."""
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
