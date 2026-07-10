"""connector — the shared read-only-producer plugin base (Keel M5).

WHY THIS EXISTS: a connector's ONLY job is `source → normalized WorldEvent →
inbox`. It never classifies, decides, or writes anywhere but the inbox drop
dir and its own ~/.assistant/connectors/<name>/ state (design section 9). The
policy engine lanes; the decision queue holds; the connector produces. Keeping
that contract in ONE base library means every connector (GitHub, Gmail today;
GCal/JIRA/Slack next) is atomic-drop-correct, cursor-durable, heartbeat-visible
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

    def __init__(self, token_path, *, token_uri: str = DEFAULT_TOKEN_URI,
                 skew_sec: int = DEFAULT_TOKEN_SKEW_SEC,
                 transport: Optional[Callable[..., dict]] = None):
        self.token_path = Path(token_path)
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
        uri = tok.get("token_uri") or self._token_uri
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
# so GCal/JIRA/any future refresh-token connector reuses ONE audited code path.
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
    Google's redirect and returns (code, returned_state, redirect_uri). Binds
    127.0.0.1:0 (an EPHEMERAL free port, never a fixed one that could collide or
    be pre-claimed by another local process) and reads the OS-assigned port to
    form the redirect_uri. Never logs the request line (it carries the code)."""

    def __init__(self, *, timeout_sec: float = DEFAULT_CONSENT_TIMEOUT_SEC,
                 open_url: Optional[Callable[[str], Any]] = None):
        self._timeout = float(timeout_sec)
        self._open_url = open_url if open_url is not None else webbrowser.open

    def __call__(self, build_auth_url: Callable[[str], str], state: str) -> tuple:
        import http.server

        holder: dict = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                holder["code"] = (q.get("code") or [None])[0]
                holder["state"] = (q.get("state") or [None])[0]
                holder["error"] = (q.get("error") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h3>Authorization received.</h3>"
                    b"<p>You may close this tab and return to the terminal."
                    b"</p></body></html>")

            def log_message(self, *_a):  # SILENCE: the request line holds ?code=
                return

        # 127.0.0.1 ONLY — never 0.0.0.0; the callback (and thus the code) must
        # never be reachable off-host.
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        try:
            port = server.server_address[1]
            redirect_uri = f"http://127.0.0.1:{port}/"
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
            while "code" not in holder and "error" not in holder:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise OAuthError(
                        "timed out waiting for the OAuth redirect — re-run and "
                        "complete the consent screen within "
                        f"{int(self._timeout)}s")
                server.timeout = remaining
                server.handle_request()  # one request or the timeout, bounded
        finally:
            server.server_close()
        if holder.get("error"):
            raise OAuthError(f"authorization denied: {holder['error']}")
        return holder.get("code"), holder.get("state"), redirect_uri


def run_installed_app_flow(*, client_id: str, client_secret: str, scopes,
                           token_uri: str = DEFAULT_TOKEN_URI,
                           auth_uri: str = DEFAULT_AUTH_URI,
                           open_url: Optional[Callable[[str], Any]] = None,
                           code_getter: Optional[Callable[..., tuple]] = None,
                           exchange_transport: Optional[Callable[..., dict]] = None,
                           timeout_sec: float = DEFAULT_CONSENT_TIMEOUT_SEC,
                           now: Optional[float] = None) -> dict:
    """Run the OAuth 2.0 installed-app authorization-code flow (loopback + PKCE)
    and return a token dict ready for OAuthTokenManager.seed():
    ``{client_id, client_secret, token_uri, refresh_token, access_token,
    expiry_epoch, scopes}``.

    ``code_getter(build_auth_url, state) -> (code, returned_state, redirect_uri)``
    and ``exchange_transport(token_uri, form) -> resp_dict`` are BOTH injectable
    so a unit test drives the entire flow with no browser and no network: inject
    a code_getter returning a canned (code, state, redirect_uri) and an
    exchange_transport returning canned tokens, then assert the token dict and
    that OAuthTokenManager can load()/refresh() it. The defaults are the real
    loopback server and the base's `_urllib_token_post`.
    """
    if not client_id or not client_secret:
        raise OAuthError("client_id and client_secret are required to authorize")
    scope_str = scopes if isinstance(scopes, str) else " ".join(scopes)
    # PKCE verifier + a random state are minted here and closed over by
    # build_auth_url so the consent URL, the CSRF check and the code exchange all
    # agree on the same values.
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    def build_auth_url(redirect_uri: str) -> str:
        # access_type=offline AND prompt=consent are BOTH required to GUARANTEE
        # Google returns a refresh_token (offline alone is silently dropped on a
        # re-consent — the classic footgun) rather than only an access token.
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return auth_uri + "?" + urllib.parse.urlencode(params)

    getter = code_getter or _LoopbackCodeGetter(timeout_sec=timeout_sec,
                                                open_url=open_url)
    code, returned_state, redirect_uri = getter(build_auth_url, state)
    # CSRF: a callback whose state does not match the one we minted is not our
    # flow — abort rather than exchange an attacker-supplied code.
    if returned_state != state:
        raise OAuthError("state mismatch on the OAuth callback — possible CSRF; "
                         "aborting without exchanging the code")
    if not code:
        raise OAuthError("no authorization code returned on the callback")

    exchange = exchange_transport or _urllib_token_post
    resp = exchange(token_uri, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
    if not isinstance(resp, dict) or not resp.get("access_token"):
        raise OAuthError("token endpoint returned no access_token")
    if not resp.get("refresh_token"):
        # The classic footgun: a re-consent that returns only an access token.
        # Without a refresh_token the cache is useless the moment it expires.
        raise OAuthError(
            "token endpoint returned NO refresh_token — revoke this app's prior "
            "grant at https://myaccount.google.com/permissions and re-run "
            "(prompt=consent must issue a fresh refresh_token)")
    now = now if now is not None else time.time()
    expires_in = resp.get("expires_in")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": token_uri,
        "refresh_token": resp["refresh_token"],
        "access_token": resp["access_token"],
        "expiry_epoch": now + (int(expires_in) if isinstance(
            expires_in, (int, float)) else 3600),
        "scopes": scope_str,
    }


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
        """
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
        """
        cadence = float(self.config.get("cadence_sec", DEFAULT_CADENCE_SEC))
        i = 0
        backoff = 0.0
        while max_iterations is None or i < max_iterations:
            i += 1
            errored = True
            retry_after = None
            try:
                result = self.poll_once() or {}
                self.prune_raw()
                retry_after = result.get("retry_after_sec")
                errored = (str(result.get("status", "")) not in
                           _HEALTHY_STATUSES) or bool(retry_after)
            except Exception as e:  # noqa: BLE001 — never kill the daemon
                self._log(f"{self.name}: poll failed: {e}")
                try:
                    self.write_heartbeat(errors=[str(e)[:300]])
                except OSError:
                    pass
            if errored:
                backoff = min(MAX_BACKOFF_SEC,
                              (backoff * 2) if backoff else cadence)
                delay = max(cadence, float(retry_after or 0.0), backoff)
            else:
                backoff = 0.0
                delay = cadence
            if max_iterations is None or i < max_iterations:
                sleep(delay)
