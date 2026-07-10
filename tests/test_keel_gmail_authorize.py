"""Tests for the one-time OAuth consent flow that SEEDS the Gmail token cache.

The wave-1 gap: OAuthTokenManager.refresh() needs a token.json holding a
refresh_token, but nothing created it — the "out-of-band consent flow" was
documented, never built. This proves the flow (connector.run_installed_app_flow
+ OAuthTokenManager.seed + gmail.py --authorize) end-to-end with ZERO network
and ZERO browser, via the two injection points (code_getter, exchange_transport)
plus a captured build_auth_url. Also proves Gmail is OPTIONAL: no token.json is a
quiet not_configured state, never a crash/alert.

New module (sorts before test_keel_gmail_connector), unittest style, tmp $HOME.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gm = _load("gm_authz_test", "bin/connectors/gmail.py")
conn = gm.connector
READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def _canned_tokens(**over):
    d = {"access_token": "ACCESS", "refresh_token": "REFRESH",
         "expires_in": 3600}
    d.update(over)
    return d


class _CaptureGetter:
    """An injected code_getter that runs the flow with NO browser/network: it
    calls build_auth_url (capturing the consent URL for assertions) and returns a
    canned (code, state, redirect_uri). `state_override` forces a CSRF mismatch.
    """
    def __init__(self, *, code="AUTHCODE", state_override=None,
                 redirect_uri="http://127.0.0.1:54321/"):
        self.code = code
        self.state_override = state_override
        self.redirect_uri = redirect_uri
        self.auth_url = None

    def __call__(self, build_auth_url, state):
        self.auth_url = build_auth_url(self.redirect_uri)
        returned_state = self.state_override if self.state_override is not None \
            else state
        return self.code, returned_state, self.redirect_uri


# ─── the reusable installed-app flow (base) ──────────────────────────────────

class FlowTests(unittest.TestCase):
    def test_auth_url_has_offline_consent_pkce_state_and_readonly_scope(self):
        getter = _CaptureGetter()

        def exchange(uri, form):
            return _canned_tokens()

        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", scopes=[READONLY],
            code_getter=getter, exchange_transport=exchange)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(getter.auth_url).query)
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["access_type"], ["offline"])   # guarantees...
        self.assertEqual(q["prompt"], ["consent"])        # ...a refresh_token
        self.assertEqual(q["code_challenge_method"], ["S256"])  # PKCE
        self.assertTrue(q["code_challenge"][0])
        self.assertTrue(q["state"][0])                    # CSRF token present
        self.assertEqual(q["scope"], [READONLY])          # read-only only
        self.assertEqual(q["client_id"], ["CID"])
        self.assertTrue(q["redirect_uri"][0].startswith("http://127.0.0.1:"))

    def test_pkce_verifier_sent_to_token_endpoint_matches_challenge(self):
        getter = _CaptureGetter()
        seen = {}

        def exchange(uri, form):
            seen.update(form)
            return _canned_tokens()

        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", scopes=[READONLY],
            code_getter=getter, exchange_transport=exchange)
        # The exchange carries the verifier + the same redirect_uri + the code.
        self.assertEqual(seen["grant_type"], "authorization_code")
        self.assertEqual(seen["code"], "AUTHCODE")
        self.assertEqual(seen["redirect_uri"], getter.redirect_uri)
        self.assertTrue(seen["code_verifier"])
        # S256: challenge in the URL is base64url(sha256(verifier)), no padding.
        import base64
        import hashlib
        q = urllib.parse.parse_qs(urllib.parse.urlparse(getter.auth_url).query)
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(seen["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(q["code_challenge"][0], expect)

    def test_state_mismatch_is_rejected_before_exchange(self):
        getter = _CaptureGetter(state_override="ATTACKER")
        exchanged = {"called": False}

        def exchange(uri, form):
            exchanged["called"] = True
            return _canned_tokens()

        with self.assertRaises(conn.OAuthError) as cm:
            conn.run_installed_app_flow(
                client_id="CID", client_secret="SEC", scopes=[READONLY],
                code_getter=getter, exchange_transport=exchange)
        self.assertIn("state mismatch", str(cm.exception).lower())
        self.assertFalse(exchanged["called"])  # never exchanged the code

    def test_no_refresh_token_raises_clear_error(self):
        getter = _CaptureGetter()

        def exchange(uri, form):
            return {"access_token": "A", "expires_in": 3600}  # NO refresh_token

        with self.assertRaises(conn.OAuthError) as cm:
            conn.run_installed_app_flow(
                client_id="CID", client_secret="SEC", scopes=[READONLY],
                code_getter=getter, exchange_transport=exchange)
        msg = str(cm.exception).lower()
        self.assertIn("refresh_token", msg)
        self.assertIn("revoke", msg)  # actionable: revoke prior grant + retry

    def test_returned_token_dict_shape_and_expiry(self):
        getter = _CaptureGetter()

        def exchange(uri, form):
            return _canned_tokens(expires_in=1234)

        tok = conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", scopes=[READONLY],
            token_uri="https://tok/endpoint", code_getter=getter,
            exchange_transport=exchange, now=1000.0)
        self.assertEqual(tok["client_id"], "CID")
        self.assertEqual(tok["client_secret"], "SEC")
        self.assertEqual(tok["token_uri"], "https://tok/endpoint")
        self.assertEqual(tok["refresh_token"], "REFRESH")
        self.assertEqual(tok["access_token"], "ACCESS")
        self.assertEqual(tok["expiry_epoch"], 1000.0 + 1234)
        self.assertEqual(tok["scopes"], READONLY)


# ─── seed via the base's atomic 0600 writer ──────────────────────────────────

class SeedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sub" / "token.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _mgr(self):
        return conn.OAuthTokenManager(self.path)

    def test_seed_writes_0600_and_load_accepts(self):
        tok = {"client_id": "C", "client_secret": "S",
               "token_uri": conn.DEFAULT_TOKEN_URI, "refresh_token": "R",
               "access_token": "A", "expiry_epoch": 5000, "scopes": READONLY}
        self._mgr().seed(tok)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        loaded = self._mgr().load()
        self.assertEqual(loaded["refresh_token"], "R")

    def test_seed_then_refresh_uses_the_seeded_cache(self):
        tok = {"client_id": "C", "client_secret": "S",
               "token_uri": conn.DEFAULT_TOKEN_URI, "refresh_token": "R",
               "access_token": "OLD", "expiry_epoch": 0, "scopes": READONLY}
        self._mgr().seed(tok)
        seen = {}

        def transport(uri, form):
            seen.update(form)
            return {"access_token": "NEW", "expires_in": 3600}

        mgr = conn.OAuthTokenManager(self.path, transport=transport)
        fresh = mgr.access_token(now=100.0)  # expired → refreshes
        self.assertEqual(fresh, "NEW")
        self.assertEqual(seen["grant_type"], "refresh_token")
        self.assertEqual(seen["refresh_token"], "R")

    def test_seed_refuses_clobber_without_force(self):
        tok = {"refresh_token": "R1", "client_id": "C", "client_secret": "S"}
        self._mgr().seed(tok)
        with self.assertRaises(conn.OAuthError):
            self._mgr().seed({"refresh_token": "R2"})
        self.assertEqual(self._mgr().load()["refresh_token"], "R1")  # untouched

    def test_seed_force_overwrites(self):
        self._mgr().seed({"refresh_token": "R1"})
        self._mgr().seed({"refresh_token": "R2"}, force=True)
        self.assertEqual(self._mgr().load()["refresh_token"], "R2")

    def test_seed_without_refresh_token_raises(self):
        with self.assertRaises(conn.OAuthError):
            self._mgr().seed({"access_token": "A"})  # no refresh_token


# ─── client-secrets JSON parsing (installed + web shapes) ────────────────────

class ClientSecretsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, obj):
        p = self.dir / "cs.json"
        p.write_text(json.dumps(obj))
        return p

    def test_installed_shape(self):
        p = self._write({"installed": {
            "client_id": "cid.apps", "client_secret": "shh",
            "token_uri": "https://tok", "auth_uri": "https://auth"}})
        cid, sec, tok, auth = gm._load_client_secrets(p)
        self.assertEqual((cid, sec, tok, auth),
                         ("cid.apps", "shh", "https://tok", "https://auth"))

    def test_web_shape_and_default_uris(self):
        p = self._write({"web": {"client_id": "wid", "client_secret": "wsec"}})
        cid, sec, tok, auth = gm._load_client_secrets(p)
        self.assertEqual((cid, sec), ("wid", "wsec"))
        self.assertEqual(tok, conn.DEFAULT_TOKEN_URI)   # falls back to defaults
        self.assertEqual(auth, conn.DEFAULT_AUTH_URI)

    def test_missing_fields_raise(self):
        p = self._write({"installed": {"client_id": "only_id"}})
        with self.assertRaises(ValueError):
            gm._load_client_secrets(p)


# ─── gmail.py --authorize end-to-end + OPTIONAL not_configured ───────────────

class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old is not None:
            os.environ["HOME"] = self._old
        self._tmp.cleanup()

    def _client_secrets(self):
        p = self.home / "cs.json"
        p.write_text(json.dumps({"installed": {
            "client_id": "CID", "client_secret": "SEC"}}))
        return p


class AuthorizeIntegrationTests(HomeTestCase):
    def test_authorize_seeds_a_cache_that_load_and_refresh_accept(self):
        getter = _CaptureGetter()

        def exchange(uri, form):
            return _canned_tokens()

        path = gm.authorize(self._client_secrets(), code_getter=getter,
                            exchange_transport=exchange)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # The connector's own token manager can now load + refresh it.
        c = gm.GmailConnector()
        self.assertEqual(c.token_path(), path)
        loaded = c.tokens.load()
        self.assertEqual(loaded["refresh_token"], "REFRESH")
        self.assertEqual(loaded["scopes"], READONLY)

    def test_authorize_refuses_clobber_then_force_overwrites(self):
        getter = _CaptureGetter()
        gm.authorize(self._client_secrets(),
                     code_getter=getter,
                     exchange_transport=lambda u, f: _canned_tokens(
                         refresh_token="FIRST"))
        with self.assertRaises(conn.OAuthError):
            gm.authorize(self._client_secrets(), code_getter=getter,
                         exchange_transport=lambda u, f: _canned_tokens())
        gm.authorize(self._client_secrets(), force=True, code_getter=getter,
                     exchange_transport=lambda u, f: _canned_tokens(
                         refresh_token="SECOND"))
        self.assertEqual(gm.GmailConnector().tokens.load()["refresh_token"],
                         "SECOND")

    def test_no_secret_in_authorize_output(self):
        # The success message must never echo a token or the client_secret.
        path = gm.authorize(
            self._client_secrets(), code_getter=_CaptureGetter(),
            exchange_transport=lambda u, f: _canned_tokens())
        self.assertIn("token.json", str(path))
        # (the value is written to disk 0600; nothing returns/prints a secret)


class NotConfiguredTests(HomeTestCase):
    def _read_heartbeat(self, c):
        return json.loads(c.heartbeat_path().read_text())

    def test_poll_once_without_token_is_not_configured_not_error(self):
        c = gm.GmailConnector()
        res = c.poll_once()
        self.assertEqual(res["status"], "not_configured")
        self.assertEqual(res["emitted"], 0)
        self.assertEqual(res["errors"], [])
        hb = self._read_heartbeat(c)
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])              # NOT an error state
        self.assertEqual(hb["errors"], [])

    def test_main_once_without_token_exits_clean_rc0(self):
        # --once with no token cache: quiet not_configured, clean exit, no crash.
        rc = gm.main(["--once"])
        self.assertEqual(rc, 0)
        c = gm.GmailConnector()
        self.assertFalse(c.token_path().exists())      # nothing seeded
        self.assertEqual(self._read_heartbeat(c)["status"], "not_configured")

    def test_main_run_forever_default_without_token_does_not_spin(self):
        # Default (no --once) must NOT enter run_forever to retry a human-only
        # consent; it short-circuits to not_configured + rc 0.
        rc = gm.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self._read_heartbeat(gm.GmailConnector())["status"],
            "not_configured")

    def test_authorize_missing_client_secrets_arg_rc2(self):
        self.assertEqual(gm.main(["--authorize"]), 2)

    def test_configured_poll_heartbeat_status_ok(self):
        # After a token exists, a poll heartbeat carries status ok (not
        # not_configured) — the three states are distinct.
        gm.authorize(self._client_secrets(), code_getter=_CaptureGetter(),
                     exchange_transport=lambda u, f: _canned_tokens())

        def http(method, url, headers=None, data=None):
            assert method == "GET"
            return (200, {}, b'{"emailAddress":"me@x.com","historyId":"42"}')

        c = gm.GmailConnector(http=http,
                              oauth_transport=lambda u, f: {
                                  "access_token": "A", "expires_in": 3600})
        res = c.poll_once()
        self.assertEqual(res["status"], "seeded")
        self.assertEqual(self._read_heartbeat(c)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
