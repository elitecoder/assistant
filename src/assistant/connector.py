"""connector — the shared read-only-producer plugin base (Keel M5).

WHY THIS EXISTS: a connector's ONLY job is `source → normalized WorldEvent →
inbox`. It never classifies, decides, or writes anywhere but the inbox drop
dir and its own ~/.assistant/connectors/<name>/ state (design section 9). The
policy engine lanes; the decision queue holds; the connector produces. Keeping
that contract in ONE base library means every connector (GitHub, Gmail today;
GCal/Slack next) is atomic-drop-correct, cursor-durable, heartbeat-visible
and OAuth-safe by construction instead of by each author's diligence.

The base enforces the mandatory contract (design section 9):

  (1) ATOMIC DROP. `emit()` writes the WorldEvent to a tmp file in the inbox
      then `os.replace`s it to `evt-<source>-<stamp>.json` — the exact
      tmp+rename idiom the whole repo uses so the event-spine consumer never
      reads a half-written file.
  (2) NORMALIZED SCHEMA WITH A STABLE external_id. `build_world_event()` calls
      eventspine._base_event DIRECTLY (not a copy) so a connector's drop is
      byte-identical to what the consumer would mint itself — the dedup key
      sha256(source:external_id) matches on both sides, so at-least-once
      delivery collapses to exactly-one decision downstream.
  (3) DURABLE CURSOR + consumer-side dedup. `load_cursor`/`save_cursor` persist
      a watermark atomically. We rely on the spine's dedup index for
      exactly-once (at-least-once here is SAFE and deliberate); the connector
      adds NO second dedup — a double-drop is the spine's job to collapse, and
      a crash mid-batch simply re-emits from the last durable cursor.
  (4) RAW ARCHIVE (7-day retention). `archive_raw()` keeps the source payload
      under raw/<day>/ so the Drafter has context and a schema change can
      re-normalize history. Retention is pruned each poll. Secrets are NEVER
      archived — the caller passes already-metadata-only payloads and the
      OAuth token cache lives in a separate file that is never touched here.
  (5) HEARTBEAT. `write_heartbeat()` records last_poll, token_expiry and errors
      into heartbeat.json; world-scanner joins it into world.json and the
      morning brief's health section renders it — so a dead connector or an
      expiring token surfaces within one morning (design section 4/9).
  (6) --dry-run / --record. dry-run emits nothing (prints); record writes the
      sanitized real event + raw into evals/connectors/<name>/fixtures/ so
      replay fixtures exist from day one.
  (7) OAUTH REFRESH is OWNED HERE (OAuthTokenManager), not handed to a static
      ~/.zprofile token — the design explicitly rejects that (the documented
      Bedrock-under-launchd 403 hazard: an expired access token 403s silently).
      The refresh-token grant is a plain form POST, so it is pure stdlib
      (urllib); the transport is dependency-injected so unit tests prove
      refresh-on-expiry against a mock with NO live network.

Cadences/caps come from ~/.assistant/comms/config.json's `connectors` block
(never hardcoded — the module constants are only the fallback defaults).

Connectors are INDEPENDENT KeepAlive daemons; they must NEVER poll inside the
pulse's time budget. `run_forever()` is their own loop. Paths are computed
per-call so a test pointing $HOME at a tmp dir sees fresh paths. Pure stdlib,
no LLM, never closes a workspace.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

from . import eventspine

# ─── fallback defaults (all overridable via config; never the sole source) ───

DEFAULT_CADENCE_SEC = 60
DEFAULT_MAX_EVENTS_PER_POLL = 200
# Max upstream pages to walk per poll before stopping early. A stop-early is a
# TRUNCATION: the connector must then re-fetch the remainder next poll rather
# than advance its cursor past the un-emitted tail (E1/E3).
DEFAULT_MAX_PAGES = 10
DEFAULT_RAW_RETENTION_DAYS = 7
# A poll older than cadence * this factor (floored at MIN) marks the connector
# stale in the brief health section. Written INTO the heartbeat so consumers
# (brief, world-scanner) never need to know a connector's cadence.
DEFAULT_STALE_FACTOR = 6
MIN_STALE_AFTER_SEC = 900
# Refresh an OAuth access token this many seconds BEFORE it actually expires,
# so an in-flight poll never races the expiry boundary.
DEFAULT_TOKEN_SKEW_SEC = 300
# F3: how long an UNCONFIGURED daemon sleeps between config re-checks. Long
# enough that an optional connector nobody set up never becomes a hot respawn
# loop, short enough that a later `--authorize` / `gh auth login` is picked up
# automatically without a manual relaunch. Config-overridable.
DEFAULT_NOT_CONFIGURED_RECHECK_SEC = 300
# Default OAuth token endpoint (Google). Overridable per connector / per token
# cache so the same base serves any refresh-token provider.
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

HTTP_TIMEOUT_SEC = 30
# Cap every response body read so a hostile/broken upstream cannot exhaust
# memory with an unbounded stream (OP2). Metadata reads are tiny; 8 MiB is a
# generous ceiling that never truncates a legitimate notifications/history page.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Bounded exponential backoff ceiling for run_forever when a poll reports an
# error / rate-limit (OP1) — never hammer the fixed cadence under a 429/5xx.
MAX_BACKOFF_SEC = 900
# Poll statuses that mean "healthy, keep the normal cadence"; anything else
# triggers backoff. A poll that skipped a poison item still returns "ok".
_HEALTHY_STATUSES = frozenset({"ok", "not_modified", "seeded"})

# ─── the canonical connector tri-state (defined in ONE place) ────────────────
#
# WHY ONE PLACE: three components must agree on what "connected" means — the
# world-scanner (joins heartbeats into world.json), the morning brief's health
# section, and the dashboard Connections panel. Each used to re-implement the
# stale/expired/opted-out logic, which drifted (a not_configured connector read
# as a stale WARNING). Now every consumer derives from classify_connector() so
# the three states can never diverge.
#
#   not_configured — the connector's prerequisite is absent (Gmail: no
#                    token.json; GitHub: `gh auth token` fails). The owner has
#                    simply not opted in. This is QUIET: available, never an
#                    error/alert, and its last_poll is NEVER allowed to rot into
#                    "stale" (the daemon writes one beat and exits by design).
#   ok             — configured AND polling healthily (fresh last_poll, no
#                    errors, token not expired).
#   error          — configured but FAILING: an errored poll, a stale last_poll,
#                    or an expired/expiring OAuth token. The only alarming state.
STATE_NOT_CONFIGURED = "not_configured"
STATE_OK = "ok"
STATE_ERROR = "error"

# The wave-1 connectors, so the Connections panel can enumerate connectors that
# have NEVER run (no heartbeat file at all) and show them as "available, not
# connected" rather than hiding them. Each carries a human display name and the
# one-line "how to connect" hint the panel shows for the not_configured state.
# A new connector is added here the day it ships so it appears in the panel even
# before its first poll.
KNOWN_CONNECTORS = (
    {"name": "github", "display": "GitHub notifications",
     "hint": "run: gh auth login"},
    {"name": "gmail", "display": "Gmail",
     "hint": "run: bin/connectors/gmail.py --authorize --client-secrets <path>"},
    # ─── M5 wave 2 ───
    {"name": "gcal", "display": "Google Calendar",
     "hint": "run: bin/connectors/gcal.py --authorize --client-secrets <path>"},
    {"name": "slack", "display": "Slack",
     "hint": "wire the Slack app (SLACK_BOT_TOKEN in ~/.zprofile)"},
    # ─── M5 wave 3 ───
    {"name": "outlook", "display": "Outlook mail",
     "hint": "run: bin/connectors/outlook.py --authorize --client-secrets <path>"},
)


def classify_connector(hb: Optional[dict], now: float) -> dict:
    """Derive the canonical tri-state + health facts for ONE connector from its
    heartbeat dict (or None when no heartbeat file exists at all). This is the
    SINGLE place the three states are decided — world-scanner, the brief and the
    dashboard all call it, so they never drift.

    Rules:
      * No heartbeat file (fresh install, the daemon never ran) → not_configured
        (available, NOT a stale/error alarm).
      * heartbeat status == "not_configured" (opted out; the connector wrote one
        quiet beat then exited) → not_configured, and its aging last_poll is
        DELIBERATELY ignored: an opted-out connector must never rot into a
        "stale" warning just because time passed.
      * otherwise the connector is CONFIGURED and its liveness is re-derived from
        the heartbeat facts: error iff the beat carried errors, or last_poll is
        stale, or the OAuth token has expired; else ok. (The stored ok/status is
        intentionally NOT trusted for a configured connector — a once-ok beat
        that has since gone stale must surface as error.)
    """
    if not isinstance(hb, dict):
        return {
            "status": STATE_NOT_CONFIGURED,
            "source": None,
            "last_poll": None,
            "last_poll_epoch": None,
            "age_sec": None,
            "stale": False,
            "token_expiry": None,
            "token_expired": False,
            "errors": [],
            "ok": False,
        }
    last = hb.get("last_poll_epoch")
    last = last if isinstance(last, (int, float)) else None
    age = int(now - last) if last is not None else None
    if hb.get("status") == STATE_NOT_CONFIGURED:
        return {
            "status": STATE_NOT_CONFIGURED,
            "source": hb.get("source"),
            "last_poll": hb.get("last_poll"),
            "last_poll_epoch": last,
            "age_sec": age,
            "stale": False,          # opted-out: staleness is meaningless
            "token_expiry": hb.get("token_expiry"),
            "token_expired": False,
            "errors": [],
            "ok": False,
        }
    # F1/F4: a heartbeat field of the WRONG TYPE is corruption evidence — never a
    # value to trust, and never a reason to crash. Before this, a non-numeric
    # stale_after_sec (e.g. "900") raised TypeError in the `age > stale_after`
    # compare, and BOTH unfenced call sites (world-scanner + brief) then failed
    # WHOLE: world.json was never written (the 30s dashboard snapshot froze) and
    # the morning brief never built — one bad heartbeat took down everything,
    # violating M3's one-bad-row contract. A non-list `errors` (e.g. {"a":1})
    # likewise blew up the Connections panel's errs[:3]. Coerce each defensively;
    # a malformed field degrades THIS connector to error — never raises, and
    # never silently reads healthy.
    malformed: list = []
    sa = hb.get("stale_after_sec")
    if isinstance(sa, bool) or not isinstance(sa, (int, float)) or sa <= 0:
        if sa is not None:
            malformed.append("stale_after_sec")
        stale_after = MIN_STALE_AFTER_SEC
    else:
        stale_after = sa
    raw_errors = hb.get("errors")
    if isinstance(raw_errors, list):
        errors = [str(x) for x in raw_errors]
    elif raw_errors in (None, ""):
        errors = []
    else:  # a wrong-typed errors field (dict/str/int) — coerce, never crash
        errors = [str(raw_errors)]
        malformed.append("errors")
    if malformed:
        errors = errors + [
            f"malformed heartbeat field(s): {', '.join(malformed)}"]
    stale = age is None or age > stale_after
    texp = hb.get("token_expiry_epoch")
    token_expired = (isinstance(texp, (int, float)) and not isinstance(texp, bool)
                     and now >= texp)
    # errors present ⇒ not ok (write_heartbeat already ties ok=not errors; making
    # it explicit here means a hand-written / corrupt beat carrying errors can
    # never read healthy, and a malformed field always surfaces as error).
    ok = (bool(hb.get("ok", True)) and not stale and not token_expired
          and not errors)
    return {
        "status": STATE_OK if ok else STATE_ERROR,
        "source": hb.get("source"),
        "last_poll": hb.get("last_poll"),
        "last_poll_epoch": last,
        "age_sec": age,
        "stale": stale,
        "token_expiry": hb.get("token_expiry"),
        "token_expired": bool(token_expired),
        "errors": errors,
        "ok": ok,
    }


def _corrupt_connector_view(now: float) -> dict:
    """The classify verdict for a connector whose heartbeat file EXISTS but is
    unreadable/corrupt: error (configured-but-broken), never not_configured. A
    connector that has written a heartbeat before has RUN — a file that is now
    garbage means it broke, not that the owner opted out (F5)."""
    return {
        "status": STATE_ERROR,
        "source": None,
        "last_poll": None,
        "last_poll_epoch": None,
        "age_sec": None,
        "stale": False,
        "token_expiry": None,
        "token_expired": False,
        "errors": ["heartbeat unreadable or corrupt — configured but broken"],
        "ok": False,
    }


def read_and_classify(heartbeat_path, now: float) -> dict:
    """Read ONE connector's heartbeat file at `heartbeat_path` and classify it,
    distinguishing an ABSENT heartbeat (the daemon never ran → not_configured,
    quiet) from a PRESENT-but-corrupt one (it ran before and the file is now
    unreadable/malformed → error, configured-but-broken).

    WHY THIS EXISTS: world-scanner AND the brief must classify an identical
    on-disk input identically (F5). Before this, a corrupt heartbeat masqueraded
    as opted-out in world.json (world-scanner mapped the empty read to
    not_configured) while the brief DROPPED the connector entirely — the two
    consumers disagreed on the very same broken file. Both now call THIS, so a
    corrupt beat is error on both sides and only a genuinely absent heartbeat
    (+ no other config evidence) is not_configured."""
    p = Path(heartbeat_path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return classify_connector(None, now)  # genuinely absent → not_configured
    except OSError:
        return _corrupt_connector_view(now)   # present but unreadable → error
    try:
        hb = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _corrupt_connector_view(now)   # present but corrupt → error
    if not isinstance(hb, dict):
        return _corrupt_connector_view(now)
    return classify_connector(hb, now)


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def _repo() -> Path:
    # src/assistant/connector.py → parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def connectors_root() -> Path:
    return _home() / ".assistant" / "connectors"


def config_path() -> Path:
    return _home() / ".assistant" / "comms" / "config.json"


def fixtures_dir(name: str) -> Path:
    return _repo() / "evals" / "connectors" / name / "fixtures"


def load_connector_config(name: str) -> dict:
    """Merge config.json's `connectors._defaults` then `connectors.<name>` over
    the module fallback defaults. A missing/broken config yields the defaults —
    a connector must run with no config at all (never hardcoded, never
    required)."""
    cfg = {
        "cadence_sec": DEFAULT_CADENCE_SEC,
        "max_events_per_poll": DEFAULT_MAX_EVENTS_PER_POLL,
        "max_pages": DEFAULT_MAX_PAGES,
        "raw_retention_days": DEFAULT_RAW_RETENTION_DAYS,
        "stale_factor": DEFAULT_STALE_FACTOR,
        "token_skew_sec": DEFAULT_TOKEN_SKEW_SEC,
        "not_configured_recheck_sec": DEFAULT_NOT_CONFIGURED_RECHECK_SEC,
    }
    try:
        raw = json.loads(config_path().read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    block = raw.get("connectors")
    if not isinstance(block, dict):
        return cfg
    for key in ("_defaults", name):
        sub = block.get(key)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if k in cfg and isinstance(v, (int, float)):
                    cfg[k] = v
    return cfg


# ─── normalization (the ONE place a WorldEvent is shaped) ────────────────────

def build_world_event(*, source: str, kind: str, external_id: str,
                      ts_epoch: float, actor=None, title: str = "",
                      snippet: str = "", url=None, refs=None) -> dict:
    """Build a world-event/1 dict IDENTICAL to what the spine consumer mints.

    Delegates to eventspine._base_event so the schema, field order, id formula
    (sha256(source:external_id)), title cap (200) and snippet cap (2048) are
    shared code, not a drifting copy — the whole point of matching the consumer
    byte-for-byte is that a producer drop and a consumer re-mint dedup to the
    same id."""
    return eventspine._base_event(
        ts_epoch=ts_epoch, source=source, kind=kind, external_id=external_id,
        actor=actor, title=title, snippet=snippet, url=url, refs=refs or {})


# ─── HTTP transport (injectable — tests never touch the network) ─────────────

class HttpError(Exception):
    """A non-2xx/304 HTTP status from an injected or urllib transport."""
    def __init__(self, status: int, url: str, body: bytes = b""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


def urllib_transport(method: str, url: str, *, headers: Optional[dict] = None,
                     data: Optional[bytes] = None,
                     timeout: int = HTTP_TIMEOUT_SEC) -> tuple:
    """Default transport: (status, headers-dict, body-bytes). 304 and 4xx/5xx
    do NOT raise here — urllib raises HTTPError which we convert to a normal
    return so callers branch on status (a 304/404 is control flow, not an
    exception). Only reached in a live run; unit tests inject their own."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        return (e.code, dict(e.headers.items() if e.headers else {}),
                e.read(MAX_RESPONSE_BYTES))


def parse_retry_after(headers: dict, now: float) -> Optional[float]:
    """Seconds to wait before retrying, from a rate-limited response's
    ``Retry-After`` (delta-seconds OR an HTTP-date) or GitHub's
    ``X-RateLimit-Reset`` (epoch seconds). Returns None when neither is present
    or parseable. Used to honor the server's backoff instead of hammering the
    fixed cadence (OP1)."""
    if not isinstance(headers, dict):
        return None
    ci = {str(k).lower(): v for k, v in headers.items()}
    ra = ci.get("retry-after")
    if ra is not None:
        s = str(ra).strip()
        if s.isdigit():
            return float(s)
        try:  # HTTP-date form
            import email.utils
            dt = email.utils.parsedate_to_datetime(s)
            if dt is not None:
                return max(0.0, dt.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            pass
    reset = ci.get("x-ratelimit-reset")
    if reset is not None and str(reset).strip().isdigit():
        return max(0.0, float(str(reset).strip()) - now)
    return None


# ─── OAuth refresh-token flow (owned by the base, injectable transport) ──────

class OAuthError(Exception):
    pass


def _urllib_token_post(token_uri: str, form: dict,
                       timeout: int = HTTP_TIMEOUT_SEC) -> dict:
    """Default OAuth token-endpoint transport: form-POST → JSON dict. A refresh
    is nothing but this one call, so no OAuth library is required (stdlib-only
    constraint honored). Injected away in every unit test."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        token_uri, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    # EVERY refresh failure must become an OAuthError (O1). Network-down under
    # launchd — URLError/timeout — is the ROUTINE failure, and if it escaped
    # here it would blow past poll_once's `except OAuthError`, nulling the
    # token-expiry signal (and crashing --once with no heartbeat). A malformed
    # body (JSONDecodeError ⊂ ValueError) is likewise wrapped.
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_RESPONSE_BYTES)  # OP2: bounded read
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")[:300]
        raise OAuthError(f"token endpoint {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OAuthError(f"token endpoint unreachable: {str(e)[:200]}") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise OAuthError(f"token endpoint bad response: {str(e)[:200]}") from e


class OAuthTokenManager:
    """Owns the access-token lifecycle for a connector (design section 9,
    Gmail row). The refresh_token + client credentials live in the token cache
    file (seeded once by an out-of-band consent flow on the owner's hardware —
    NEVER in a plist, a config, or ~/.zprofile). `access_token()` transparently
    refreshes an expired/near-expired token via the injected transport and
    persists the new access token + expiry atomically.

    The transport is a callable ``(token_uri, form_dict) -> resp_dict`` — a unit
    test injects a fake that returns a canned ``{access_token, expires_in}`` and
    records the call, so refresh-on-expiry is proven with zero network. This is
    the same injectable-transport testability the codebase uses for its LLM
    callers.
    """

    def __init__(self, token_path, *, provider: str = "google",
                 token_uri: str = DEFAULT_TOKEN_URI,
                 skew_sec: int = DEFAULT_TOKEN_SKEW_SEC,
                 transport: Optional[Callable[..., dict]] = None):
        self.token_path = Path(token_path)
        self.provider = provider
        self._token_uri = token_uri
        self.skew_sec = skew_sec
        self._transport = transport or _urllib_token_post

    def load(self) -> dict:
        try:
            tok = json.loads(self.token_path.read_text())
        except FileNotFoundError as e:
            raise OAuthError(f"no token cache at {self.token_path} — run the "
                             "one-time consent flow to seed it") from e
        except (OSError, json.JSONDecodeError, ValueError) as e:
            raise OAuthError(f"token cache unreadable: {e}") from e
        if not isinstance(tok, dict) or not tok.get("refresh_token"):
            raise OAuthError("token cache missing refresh_token")
        return tok

    def _save(self, tok: dict) -> None:
        import secrets
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the tmp 0600 ATOMICALLY via os.open — never the write_text-then
        # -chmod window that leaves the secret group/other-readable (0644) until
        # the chmod lands, and a leftover 0644 .tmp if the chmod ever fails
        # (SEC3a). The tmp name is per-writer unique (pid+rand) so two
        # concurrent refreshes never share or clobber one tmp (SEC3b). 0o600 is
        # umask-proof: umask can only clear bits, and 0600 has none to clear.
        tmp = self.token_path.parent / (
            f".{self.token_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        data = json.dumps(tok, indent=2, sort_keys=True)
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, self.token_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def expiry_epoch(self) -> Optional[float]:
        try:
            tok = self.load()
        except OAuthError:
            return None
        v = tok.get("expiry_epoch")
        return float(v) if isinstance(v, (int, float)) else None

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        exp = self.expiry_epoch()
        # No expiry recorded → treat as expired so the next call refreshes.
        return exp is None or now >= (exp - self.skew_sec)

    def refresh(self, now: Optional[float] = None) -> dict:
        """POST the refresh_token grant, persist the new access token + expiry.
        The provider usually omits a new refresh_token — we keep the old one."""
        now = now if now is not None else time.time()
        tok = self.load()
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        }
        if tok.get("client_id"):
            form["client_id"] = tok["client_id"]
        if tok.get("client_secret"):
            form["client_secret"] = tok["client_secret"]
        # A1 defense-in-depth: the token cache's own token_uri is ALSO pinned to
        # this provider's allowlist before the refresh POST — a token.json whose
        # token_uri was later tampered to an attacker host must not exfiltrate the
        # refresh_token grant (client_secret + refresh_token). The seed already
        # stored a pinned value; this makes a subsequent tamper a no-op too.
        uri = _pin_endpoint(tok.get("token_uri") or self._token_uri,
                            provider=self.provider, kind="token")
        resp = self._transport(uri, form)
        if not isinstance(resp, dict) or not resp.get("access_token"):
            raise OAuthError("token endpoint returned no access_token")
        new = dict(tok)
        new["access_token"] = resp["access_token"]
        expires_in = resp.get("expires_in")
        new["expiry_epoch"] = now + (int(expires_in) if isinstance(
            expires_in, (int, float)) else 3600)
        if resp.get("refresh_token"):  # provider rotated it — keep the new one
            new["refresh_token"] = resp["refresh_token"]
        self._save(new)
        return new

    def access_token(self, now: Optional[float] = None) -> str:
        """A live access token, refreshing first if it is expired/near-expiry.
        This is the ONLY entry point a connector calls — expiry handling is the
        base's job, not each connector's."""
        now = now if now is not None else time.time()
        tok = self.load()
        if not tok.get("access_token") or self.is_expired(now):
            tok = self.refresh(now)
        return tok["access_token"]

    def seed(self, token_dict: dict, *, force: bool = False) -> Path:
        """Persist a FRESHLY-AUTHORIZED token cache (from the one-time consent
        flow) through the very same atomic os.open(0600) writer the refresh path
        uses — there is deliberately NO second secret-writing code path, so the
        SEC3a/SEC3b hardening applies to the initial seed too. Refuses to
        clobber an existing cache unless ``force``: a stray re-consent must never
        silently overwrite (and thereby destroy) a working refresh_token."""
        if not isinstance(token_dict, dict) or not token_dict.get("refresh_token"):
            raise OAuthError("cannot seed a token cache without a refresh_token")
        if self.token_path.exists() and not force:
            raise OAuthError(
                f"token cache already exists at {self.token_path} — pass "
                "force=True to overwrite (this REPLACES the stored refresh_token)")
        self._save(token_dict)
        return self.token_path


# ─── one-time installed-app consent flow (seeds the token cache) ─────────────
#
# WHY THIS LIVES IN THE BASE: OAuthTokenManager.refresh() can only run once a
# token.json holding a refresh_token exists, but NOTHING in the repo created
# that file — the Gmail connector documented "seeded once by an out-of-band
# consent flow" that did not exist. This is that flow, owned by the base (design
# section 9, Gmail row: "Full OAuth refresh-token flow owned by connector base")
# so GCal/any future refresh-token connector reuses ONE audited code path.
#
# It is Google's loopback "installed app" pattern: bind an ephemeral 127.0.0.1
# port, send the browser to the consent screen with that loopback redirect_uri,
# receive ?code= on the callback, and exchange it for {refresh,access}_token.
# Both side-effect boundaries — the browser (open_url / code_getter) and the
# token exchange (exchange_transport) — are dependency-injected, so the whole
# flow is unit-provable with NO browser and NO network, exactly like the refresh
# path's injectable transport. Stdlib only (urllib/http.server/webbrowser/
# hashlib/base64/secrets); no google-auth.

DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
# How long the loopback server waits for the human to finish consent before it
# gives up, so an abandoned flow can never wedge a terminal forever.
DEFAULT_CONSENT_TIMEOUT_SEC = 180
# Per-ACCEPTED-connection read timeout on the loopback callback server (A3). The
# listening-socket select is bounded by server.timeout, but WITHOUT this a slow
# local client that connects and dribbles (or never sends) bytes hangs
# handle_request past the consent deadline. Capped by the remaining consent
# budget so a stuck client can never wedge the flow beyond it.
DEFAULT_LOOPBACK_READ_TIMEOUT_SEC = 30

# A1: the ONLY OAuth endpoints we will ever talk to, keyed PER PROVIDER.
# token_uri/auth_uri read from a user-supplied client-secrets (or token) file are
# NOT trusted — a poisoned token_uri would redirect the code-exchange POST (which
# carries the authorization code + PKCE verifier + client_id + client_secret) to
# an attacker who then redeems the victim's refresh_token for durable mailbox
# access. A legitimate desktop/native client ALWAYS uses its provider's own
# endpoints, so any host not on THAT provider's allowlist is silently replaced
# with the pinned provider default (see _pin_endpoint). The wave-1 fix pinned
# Google only; this generalizes it so gmail/gcal (provider="google") pin exactly
# as before and outlook (provider="microsoft") pins to Microsoft — the file value
# is never trusted for EITHER provider.
#
# Each provider entry carries: the auth/token host allowlists (separated so an
# auth endpoint can never masquerade as a token endpoint or vice-versa), the
# pinned default auth/token URIs, the extra authorization-request params needed
# to guarantee a refresh_token, and a provider-specific remediation hint for the
# "no refresh_token" footgun.
_OAUTH_PROVIDERS = {
    "google": {
        "auth_hosts": frozenset({"accounts.google.com"}),
        "token_hosts": frozenset({"oauth2.googleapis.com"}),
        "default_auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "default_token_uri": "https://oauth2.googleapis.com/token",
        # Google's installed-app registration is a CONFIDENTIAL client: its token
        # endpoint requires client_secret EVEN WITH PKCE, so a secret is
        # mandatory here (D3).
        "requires_client_secret": True,
        # Google's loopback redirect accepts a 127.0.0.1 host (its docs use it);
        # keep it EXACTLY as wave-1 shipped so gmail/gcal are unchanged (D3c).
        "redirect_host": "127.0.0.1",
        # access_type=offline AND prompt=consent are BOTH required to GUARANTEE
        # Google returns a refresh_token (offline alone is silently dropped on a
        # re-consent — the classic footgun) rather than only an access token.
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
        "revoke_hint": ("revoke this app's prior grant at "
                        "https://myaccount.google.com/permissions and re-run "
                        "(prompt=consent must issue a fresh refresh_token)"),
    },
    "microsoft": {
        # Microsoft's `common` tenant (personal Outlook.com AND work/school
        # M365) uses login.microsoftonline.com for BOTH authorize and token.
        "auth_hosts": frozenset({"login.microsoftonline.com"}),
        "token_hosts": frozenset({"login.microsoftonline.com"}),
        "default_auth_uri":
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "default_token_uri":
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Azure "Mobile and desktop applications" is a PUBLIC client — NO secret;
        # PKCE is the substitute and AAD REJECTS a presented secret
        # (AADSTS700025). So a secret is optional (D3).
        "requires_client_secret": False,
        # AAD's port-agnostic loopback exception is scoped to `http://localhost`,
        # NOT `http://127.0.0.1` — a 127.0.0.1 redirect fails AADSTS50011. The
        # loopback server still BINDS 127.0.0.1, but the redirect_uri it
        # ADVERTISES to AAD must use the `localhost` host (D3c).
        "redirect_host": "localhost",
        # Microsoft issues a refresh_token when the `offline_access` SCOPE is
        # requested (the analog of Google's access_type=offline+prompt=consent);
        # that lives in the scope list, so no extra auth-request param is needed.
        "extra_auth_params": {},
        "revoke_hint": ("ensure the 'offline_access' scope is requested, revoke "
                        "the app's prior consent for this account, and re-run "
                        "so a fresh refresh_token is issued"),
    },
}

# Microsoft pinned endpoint constants, exported so the Outlook connector can seed
# its token cache / build its authorize call against the same pinned values.
MICROSOFT_AUTH_URI = _OAUTH_PROVIDERS["microsoft"]["default_auth_uri"]
MICROSOFT_TOKEN_URI = _OAUTH_PROVIDERS["microsoft"]["default_token_uri"]


def _provider(provider: Optional[str]) -> dict:
    """The provider registry entry. Defaults to Google ONLY for the literal
    default (provider is None / the caller passed nothing), so every existing
    call site behaves exactly as before. An UNKNOWN, non-empty provider key is a
    programming error (a registry/call-site drift) and FAILS CLOSED with a raise
    rather than silently falling back to Google's weaker-for-Microsoft pinning
    (F4) — a misspelled provider must never silently pin a Microsoft flow to
    Google's endpoints (or vice-versa)."""
    key = provider or "google"
    try:
        return _OAUTH_PROVIDERS[key]
    except KeyError:
        raise OAuthError(
            f"unknown OAuth provider {provider!r} — known providers: "
            + ", ".join(sorted(_OAUTH_PROVIDERS)))


def _pin_endpoint(uri: Optional[str], *, provider: str, kind: str) -> str:
    """Return the pinned provider default UNLESS `uri` is EXACTLY an https URL on
    THIS provider's allowlisted host for THIS endpoint kind ('auth' or 'token')
    at the expected path — otherwise the pinned default (A1 + F1 + F3).

    This neutralizes a poisoned token_uri/auth_uri from an untrusted
    client-secrets/token file without aborting the flow (a real provider client
    already uses these exact endpoints). Three things are checked, all of which
    must hold, or the file value is discarded for the pinned default:

      * scheme == "https" (F1): a poisoned ``http://<valid-host>/…`` must NOT
        survive — a cleartext authorize/refresh POST would leak the code +
        PKCE verifier + client_secret/refresh_token to an on-path attacker.
        Host-only pinning (the wave-1 shape) let this through for BOTH providers,
        so this tightening also closes the hole LIVE in the merged gmail/gcal
        OAuth.
      * host on THIS provider's allowlist for THIS kind (auth vs token kept
        separate so a token host can never satisfy an auth check).
      * path equals the pinned default's path AND no explicit port (F3): a
        matching host must not smuggle a tampered path/port/query/fragment —
        we RECONSTRUCT from the pinned default rather than pass the file's URL
        through, so only the exact canonical endpoint is ever contacted."""
    prov = _provider(provider)
    hosts = prov["auth_hosts"] if kind == "auth" else prov["token_hosts"]
    default = prov["default_auth_uri"] if kind == "auth" \
        else prov["default_token_uri"]
    try:
        parts = urllib.parse.urlparse(uri or "")
        host = (parts.hostname or "").lower()
        port = parts.port
    except (ValueError, TypeError):
        return default
    expected_path = urllib.parse.urlparse(default).path
    if (parts.scheme == "https" and host in hosts
            and parts.path == expected_path and port is None):
        # Genuine, canonical endpoint — but return the pinned default anyway so
        # no query/fragment or other file-controlled fragment is ever honored.
        return default
    return default


def _pkce_pair() -> tuple:
    """A PKCE (RFC 7636) code_verifier + S256 code_challenge. Google's installed
    apps support PKCE and reviewers expect it: it binds the authorization code
    to THIS client so an intercepted code on the loopback interface is useless
    without the verifier. base64url, no padding (the spec forbids '=')."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _LoopbackCodeGetter:
    """Default `code_getter`: run a one-shot loopback HTTP server that receives
    the provider's redirect and returns (code, returned_state, redirect_uri).
    Binds 127.0.0.1:0 (an EPHEMERAL free port, never a fixed one that could
    collide or be pre-claimed by another local process) and reads the OS-assigned
    port to form the redirect_uri. Never logs the request line (it carries the
    code).

    The socket ALWAYS binds 127.0.0.1 (the callback must never be reachable
    off-host), but the redirect_uri it ADVERTISES to the provider uses
    ``redirect_host`` (D3c): Google accepts ``127.0.0.1`` and keeps the wave-1
    ``http://127.0.0.1:{port}/`` form byte-for-byte; Microsoft/AAD's
    port-agnostic loopback exception is scoped to ``http://localhost`` (a
    127.0.0.1 redirect fails AADSTS50011), and its registered redirect is
    ``http://localhost`` with no path — so localhost advertises
    ``http://localhost:{port}`` with NO trailing slash to avoid a path
    mismatch."""

    def __init__(self, *, timeout_sec: float = DEFAULT_CONSENT_TIMEOUT_SEC,
                 open_url: Optional[Callable[[str], Any]] = None,
                 redirect_host: str = "127.0.0.1"):
        self._timeout = float(timeout_sec)
        self._open_url = open_url if open_url is not None else webbrowser.open
        self._redirect_host = redirect_host or "127.0.0.1"

    def __call__(self, build_auth_url: Callable[[str], str], state: str) -> tuple:
        import http.server

        holder: dict = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            # A3: a per-connection read timeout. StreamRequestHandler applies
            # this to the accepted socket, so a client that connects and dribbles
            # (or sends nothing) is dropped instead of hanging handle_request past
            # the consent deadline. Re-set each loop below to the remaining budget.
            timeout = DEFAULT_LOOPBACK_READ_TIMEOUT_SEC

            def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (q.get("code") or [None])[0]
                err = (q.get("error") or [None])[0]
                # A2: ONLY a callback actually carrying a code or an error is
                # terminal. A stray GET (favicon/prefetch/local probe) with
                # neither must NOT record code=None — the wait loop would then
                # trip the state check and abort with a spurious "possible CSRF"
                # (a trivial local DoS of setup). Answer 204 and keep waiting for
                # the real redirect.
                if code or err:
                    holder["code"] = code
                    holder["error"] = err
                    holder["state"] = (q.get("state") or [None])[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h3>Authorization received.</h3>"
                        b"<p>You may close this tab and return to the terminal."
                        b"</p></body></html>")
                else:
                    self.send_response(204)
                    self.end_headers()

            def log_message(self, *_a):  # SILENCE: the request line holds ?code=
                return

        # 127.0.0.1 ONLY — never 0.0.0.0; the callback (and thus the code) must
        # never be reachable off-host.
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        # A read timeout (or any socket error) on an accepted connection must not
        # spill a traceback to stderr — it carries no code, so silence it.
        server.handle_error = lambda *_a: None
        try:
            port = server.server_address[1]
            # Bind is 127.0.0.1; advertise the provider's redirect_host. Google
            # (127.0.0.1) keeps the wave-1 trailing slash; localhost (Microsoft)
            # omits it so it matches AAD's registered `http://localhost` exactly
            # aside from the loopback-exempt port (D3c).
            if self._redirect_host == "127.0.0.1":
                redirect_uri = f"http://{self._redirect_host}:{port}/"
            else:
                redirect_uri = f"http://{self._redirect_host}:{port}"
            auth_url = build_auth_url(redirect_uri)
            # ALWAYS print the URL to stderr so a headless/SSH user (no browser
            # to auto-open) can copy-paste it. This is the URL, never a secret.
            print("Open this URL in a browser to authorize (must be the "
                  "account owner):\n" + auth_url, file=sys.stderr)
            try:
                self._open_url(auth_url)
            except Exception:  # noqa: BLE001 — headless: printing already covered
                pass
            deadline = time.time() + self._timeout
            # A2: gate on a non-None VALUE (not key presence) so only a real
            # code/error ends the wait.
            while not holder.get("code") and not holder.get("error"):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise OAuthError(
                        "timed out waiting for the OAuth redirect — re-run and "
                        "complete the consent screen within "
                        f"{int(self._timeout)}s")
                server.timeout = remaining
                # A3: bound the accepted-connection read to the remaining budget
                # (capped at the default) so a stuck client cannot wedge the flow
                # past the consent deadline.
                _Handler.timeout = min(remaining, DEFAULT_LOOPBACK_READ_TIMEOUT_SEC)
                server.handle_request()  # one request or the timeout, bounded
        finally:
            server.server_close()
        if holder.get("error"):
            raise OAuthError(f"authorization denied: {holder['error']}")
        return holder.get("code"), holder.get("state"), redirect_uri


def run_installed_app_flow(*, client_id: str,
                           client_secret: Optional[str] = None, scopes,
                           provider: str = "google",
                           token_uri: str = DEFAULT_TOKEN_URI,
                           auth_uri: str = DEFAULT_AUTH_URI,
                           open_url: Optional[Callable[[str], Any]] = None,
                           code_getter: Optional[Callable[..., tuple]] = None,
                           exchange_transport: Optional[Callable[..., dict]] = None,
                           timeout_sec: float = DEFAULT_CONSENT_TIMEOUT_SEC,
                           now: Optional[float] = None) -> dict:
    """Run the OAuth 2.0 installed-app authorization-code flow (loopback + PKCE)
    and return a token dict ready for OAuthTokenManager.seed():
    ``{client_id, token_uri, refresh_token, access_token, expiry_epoch, scopes}``
    (plus ``client_secret`` ONLY for a confidential client — a Microsoft public
    client has none, so it is omitted from both the exchange POST and the token,
    per D3).

    ``code_getter(build_auth_url, state) -> (code, returned_state, redirect_uri)``
    and ``exchange_transport(token_uri, form) -> resp_dict`` are BOTH injectable
    so a unit test drives the entire flow with no browser and no network: inject
    a code_getter returning a canned (code, state, redirect_uri) and an
    exchange_transport returning canned tokens, then assert the token dict and
    that OAuthTokenManager can load()/refresh() it. The defaults are the real
    loopback server and the base's `_urllib_token_post`.
    """
    prov = _provider(provider)
    # D3: client_secret is REQUIRED for a confidential client (Google's installed
    # apps ship a secret and Google's token endpoint demands it even WITH PKCE),
    # but a Microsoft Azure "Mobile and desktop applications" registration is a
    # PUBLIC client with NO secret — PKCE is the substitute. Presenting a secret
    # to a public client is REJECTED by AAD (AADSTS700025). So: require a secret
    # only when the provider marks itself confidential; otherwise it is optional
    # and, when absent, omitted from the exchange POST and the returned token.
    if not client_id:
        raise OAuthError("client_id is required to authorize")
    if prov.get("requires_client_secret") and not client_secret:
        raise OAuthError(
            f"client_secret is required to authorize provider {provider!r}")
    # A1: PIN the endpoints to THIS provider's allowlist. Whatever token_uri/
    # auth_uri the caller passed through from the client-secrets file is only
    # honored if it is a genuine host FOR THIS PROVIDER; otherwise the pinned
    # provider default is used. This is the single chokepoint for BOTH the
    # consent redirect (auth_uri — a poisoned one phishes the victim's consent)
    # and the code exchange (token_uri — a poisoned one exfiltrates the code +
    # verifier + client_secret). The file value is never trusted for gmail/gcal
    # (google) OR outlook (microsoft).
    auth_uri = _pin_endpoint(auth_uri, provider=provider, kind="auth")
    token_uri = _pin_endpoint(token_uri, provider=provider, kind="token")
    scope_str = scopes if isinstance(scopes, str) else " ".join(scopes)
    # PKCE verifier + a random state are minted here and closed over by
    # build_auth_url so the consent URL, the CSRF check and the code exchange all
    # agree on the same values.
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    def build_auth_url(redirect_uri: str) -> str:
        # The provider's extra_auth_params guarantee a refresh_token is issued:
        # Google needs access_type=offline + prompt=consent (offline alone is
        # silently dropped on a re-consent — the classic footgun); Microsoft
        # needs none here because it keys off the `offline_access` scope instead.
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        params.update(prov.get("extra_auth_params") or {})
        return auth_uri + "?" + urllib.parse.urlencode(params)

    getter = code_getter or _LoopbackCodeGetter(
        timeout_sec=timeout_sec, open_url=open_url,
        redirect_host=prov.get("redirect_host", "127.0.0.1"))
    code, returned_state, redirect_uri = getter(build_auth_url, state)
    # CSRF: a callback whose state does not match the one we minted is not our
    # flow — abort rather than exchange an attacker-supplied code.
    if returned_state != state:
        raise OAuthError("state mismatch on the OAuth callback — possible CSRF; "
                         "aborting without exchanging the code")
    if not code:
        raise OAuthError("no authorization code returned on the callback")

    exchange = exchange_transport or _urllib_token_post
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:  # public client (no secret) omits it — AAD rejects one
        form["client_secret"] = client_secret
    resp = exchange(token_uri, form)
    if not isinstance(resp, dict) or not resp.get("access_token"):
        raise OAuthError("token endpoint returned no access_token")
    if not resp.get("refresh_token"):
        # The classic footgun: a re-consent that returns only an access token.
        # Without a refresh_token the cache is useless the moment it expires.
        # The remediation is provider-specific (Google: revoke the prior grant;
        # Microsoft: ensure offline_access is requested).
        raise OAuthError(
            "token endpoint returned NO refresh_token — "
            + prov["revoke_hint"])
    now = now if now is not None else time.time()
    expires_in = resp.get("expires_in")
    tok = {
        "client_id": client_id,
        "token_uri": token_uri,
        "refresh_token": resp["refresh_token"],
        "access_token": resp["access_token"],
        "expiry_epoch": now + (int(expires_in) if isinstance(
            expires_in, (int, float)) else 3600),
        "scopes": scope_str,
    }
    if client_secret:  # public client stores NO secret (there is none)
        tok["client_secret"] = client_secret
    return tok


# ─── the connector base ──────────────────────────────────────────────────────

class Connector:
    """Base for a read-only producer plugin. A subclass implements exactly one
    method, ``poll_once(now)``, which fetches new source items since the durable
    cursor and, for each, calls ``self.emit(event, raw=...)``; it returns a
    small summary dict. Everything else — atomic drop, raw archive + retention,
    heartbeat, dry-run/record, cursor persistence, the KeepAlive loop — is here.

    Subclasses MUST NOT call any mutation/send API. A grep CI test enforces it.
    """

    def __init__(self, name: str, source: str, *,
                 config: Optional[dict] = None,
                 dry_run: bool = False, record: bool = False,
                 log: Optional[Callable[[str], None]] = None):
        self.name = name
        self.source = source
        self.config = config if config is not None else load_connector_config(name)
        self.dry_run = dry_run
        self.record = record
        self._log = log or (lambda msg: None)

    # ── paths (per-call so a tmp $HOME is honored) ──────────────────────────

    def dir(self) -> Path:
        return connectors_root() / self.name

    def cursor_path(self) -> Path:
        return self.dir() / "cursor.json"

    def heartbeat_path(self) -> Path:
        return self.dir() / "heartbeat.json"

    def token_path(self) -> Path:
        return self.dir() / "token.json"

    def raw_dir(self) -> Path:
        return self.dir() / "raw"

    def inbox_dir(self) -> Path:
        return eventspine.inbox_dir()

    # ── durable cursor ──────────────────────────────────────────────────────

    def load_cursor(self) -> dict:
        try:
            data = json.loads(self.cursor_path().read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_cursor(self, cursor: dict) -> None:
        """Atomic tmp+replace. The watermark advances ONLY after the batch it
        covers is safely dropped — a crash mid-batch leaves the old cursor, so
        the next run re-emits (at-least-once; the spine dedups).

        In --dry-run this is a NO-OP: a debug/inspection run must never advance
        the durable cursor (that would destroy the very events it is used to
        investigate — D1)."""
        if self.dry_run:
            return
        p = self.cursor_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True))
        os.replace(tmp, p)

    # ── raw archive (7-day retention, secrets never stored) ─────────────────

    def archive_raw(self, external_id: str, payload: Any,
                    now: Optional[float] = None) -> Path:
        """Persist the source payload under raw/<day>/ for Drafter context and
        schema re-normalization. `payload` must already be metadata-only — this
        method never sees a token (the OAuth cache is a separate file)."""
        now = now if now is not None else time.time()
        day = eventspine.utc_iso(now)[:10]
        day_dir = self.raw_dir() / day
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = external_id.replace("/", "_").replace(":", "_")[:180]
        dst = day_dir / f"{safe}.json"
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  default=str))
        os.replace(tmp, dst)
        return dst

    def prune_raw(self, now: Optional[float] = None) -> int:
        """Delete raw/<YYYY-MM-DD>/ dirs older than the configured retention
        (default 7d). Lexicographic date-name compare, same as the spine's
        raw pruner. Returns the number of day-dirs removed."""
        import re
        import shutil
        now = now if now is not None else time.time()
        root = self.raw_dir()
        if not root.is_dir():
            return 0
        keep_days = int(self.config.get("raw_retention_days",
                                        DEFAULT_RAW_RETENTION_DAYS))
        cutoff = eventspine.utc_iso(now - keep_days * 86400)[:10]
        day_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        removed = 0
        for d in root.iterdir():
            if d.is_dir() and day_re.match(d.name) and d.name < cutoff:
                try:
                    shutil.rmtree(str(d))
                    removed += 1
                except OSError:
                    pass
        return removed

    # ── emit (atomic drop) ──────────────────────────────────────────────────

    def emit(self, event: dict, *, raw: Any = None,
             now: Optional[float] = None) -> Optional[Path]:
        """Archive the raw payload (if any) then atomically drop the WorldEvent
        into the inbox as ``evt-<source>-<stamp>.json``. Returns the drop path,
        or None in --dry-run (which prints instead). --record additionally
        writes a sanitized {raw, expected} fixture."""
        now = now if now is not None else time.time()
        ext = event.get("external_id") or event.get("id") or "unknown"
        # The raw archive is a real side effect (writes raw/<day>/…). In
        # --dry-run we skip it so a dry run is side-effect-free except printing
        # (D1) — the raw archive ran BEFORE the dry_run guard before this fix.
        if raw is not None and not self.dry_run:
            try:
                event = dict(event)
                event["raw_path"] = str(self.archive_raw(ext, raw, now))
            except OSError as e:
                self._log(f"{self.name}: raw archive failed for {ext}: {e}")
        if self.record:
            self._record_fixture(event, raw)
        if self.dry_run:
            print(json.dumps({"dry_run": True, "event": event},
                             ensure_ascii=False))
            return None
        return self._atomic_drop(event, now)

    def _atomic_drop(self, event: dict, now: float) -> Path:
        inbox = self.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        # Stamp = ns clock + short id hash → unique, sortable, collision-free
        # even for two events emitted in the same wall-clock second.
        stamp = f"{int(now * 1000)}-{str(event.get('id') or '')[:12]}"
        name = f"evt-{self.source}-{stamp}.json"
        dst = inbox / name
        tmp = inbox / f".{name}.tmp"
        tmp.write_text(json.dumps(event, ensure_ascii=False))
        os.replace(tmp, dst)
        return dst

    def _record_fixture(self, event: dict, raw: Any) -> None:
        """--record: write a {raw, expected} golden fixture so replay coverage
        exists from day one. `expected` is the normalized event minus volatile
        fields (id/raw_path are recomputed on replay)."""
        d = fixtures_dir(self.name)
        d.mkdir(parents=True, exist_ok=True)
        expected = {k: v for k, v in event.items()
                    if k not in ("id", "raw_path", "epoch")}
        stem = (event.get("external_id") or "rec").replace("/", "_").replace(
            ":", "_")[:120]
        out = d / f"rec-{stem}.json"
        out.write_text(json.dumps({"raw": raw, "expected": expected},
                                  ensure_ascii=False, indent=2, default=str))

    # ── heartbeat ───────────────────────────────────────────────────────────

    def write_heartbeat(self, *, last_poll_epoch: Optional[float] = None,
                        token_expiry_epoch: Optional[float] = None,
                        errors: Optional[list] = None,
                        poll_count: Optional[int] = None,
                        event_count: Optional[int] = None,
                        extra: Optional[dict] = None) -> Path:
        """Write heartbeat.json (last_poll, token_expiry, errors). world-scanner
        joins it into world.json and the brief health section renders it, so a
        stale last_poll or a past token_expiry is visible within one morning.
        `stale_after_sec` is written IN so consumers need not know the cadence.

        In --dry-run this is a NO-OP (finding 16): a debug/inspection run must
        never refresh last_poll. A dry run that stamped the heartbeat would mask a
        DEAD daemon as healthy — the brief would read a fresh last_poll from a
        run that dropped nothing and advanced nothing. Guarding it here (in the
        base) covers every connector and every call site — the not_configured,
        error and ok beats alike — so no caller can accidentally re-introduce the
        side effect. The other dry-run guarantees already hold: emit() skips the
        drop + raw archive, save_cursor() is a no-op, and each connector's spool/
        reminder side effects are individually dry-run-guarded."""
        if self.dry_run:
            return self.heartbeat_path()
        now = last_poll_epoch if last_poll_epoch is not None else time.time()
        cadence = int(self.config.get("cadence_sec", DEFAULT_CADENCE_SEC))
        factor = int(self.config.get("stale_factor", DEFAULT_STALE_FACTOR))
        hb = {
            "connector": self.name,
            "source": self.source,
            "last_poll": eventspine.utc_iso(now),
            "last_poll_epoch": int(now),
            "cadence_sec": cadence,
            "stale_after_sec": max(MIN_STALE_AFTER_SEC, cadence * factor),
            "errors": list(errors or []),
            "ok": not errors,
        }
        if token_expiry_epoch is not None:
            hb["token_expiry"] = eventspine.utc_iso(token_expiry_epoch)
            hb["token_expiry_epoch"] = int(token_expiry_epoch)
        else:
            hb["token_expiry"] = None
            hb["token_expiry_epoch"] = None
        if poll_count is not None:
            hb["poll_count"] = poll_count
        if event_count is not None:
            hb["event_count"] = event_count
        if extra:
            hb.update(extra)
        p = self.heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hb, indent=2, sort_keys=True))
        os.replace(tmp, p)
        return p

    # ── the daemon loop ─────────────────────────────────────────────────────

    def poll_once(self, now: Optional[float] = None) -> dict:  # pragma: no cover
        raise NotImplementedError("subclass must implement poll_once")

    def run_forever(self, *, max_iterations: Optional[int] = None,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        """KeepAlive loop: poll, prune raw, sleep the configured cadence.
        NEVER runs inside the pulse — this is the connector's own process. A
        poll exception is logged into the heartbeat and the loop continues
        (a transient API blip must not kill the daemon). `max_iterations` and
        an injected `sleep` make the loop unit-testable without real waiting.

        Under a rate-limit / error the loop backs off instead of hammering the
        fixed cadence (OP1): it honors a ``retry_after_sec`` the poll surfaces
        from Retry-After/X-RateLimit-Reset, and otherwise applies a bounded
        exponential backoff, resetting to the cadence on the first healthy poll.

        F3: an OPTIONAL connector nobody set up (poll_once returns
        ``not_configured``) is NOT an error and must NOT hot-respawn. The old
        code exit(0)'d here, which under the shipped KeepAlive=<true/> +
        ThrottleInterval=10 plists respawned the daemon every ~10s (~8600 log
        lines/day). Instead we stay resident and re-check config on a LONG
        ``not_configured_recheck_sec`` cadence — so running ``--authorize`` /
        ``gh auth login`` later is picked up AUTOMATICALLY (the next poll returns
        ok) with no manual relaunch, and no hot spin in the meantime.
        """
        cadence = float(self.config.get("cadence_sec", DEFAULT_CADENCE_SEC))
        nc_recheck = float(self.config.get(
            "not_configured_recheck_sec", DEFAULT_NOT_CONFIGURED_RECHECK_SEC))
        i = 0
        backoff = 0.0
        while max_iterations is None or i < max_iterations:
            i += 1
            errored = True
            not_configured = False
            retry_after = None
            try:
                result = self.poll_once() or {}
                self.prune_raw()
                retry_after = result.get("retry_after_sec")
                status = str(result.get("status", ""))
                not_configured = status == STATE_NOT_CONFIGURED
                errored = ((status not in _HEALTHY_STATUSES) or
                           bool(retry_after)) and not not_configured
            except Exception as e:  # noqa: BLE001 — never kill the daemon
                self._log(f"{self.name}: poll failed: {e}")
                try:
                    self.write_heartbeat(errors=[str(e)[:300]])
                except OSError:
                    pass
            if not_configured:
                # Quiet, long re-check — never an error backoff, never a hot spin.
                backoff = 0.0
                delay = nc_recheck
            elif errored:
                backoff = min(MAX_BACKOFF_SEC,
                              (backoff * 2) if backoff else cadence)
                delay = max(cadence, float(retry_after or 0.0), backoff)
            else:
                backoff = 0.0
                delay = cadence
            if max_iterations is None or i < max_iterations:
                sleep(delay)
