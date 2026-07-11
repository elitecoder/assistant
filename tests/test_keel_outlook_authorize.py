"""Tests for the Outlook OAuth consent flow AND the per-provider endpoint
pinning generalization (Keel M5 wave-3).

The wave-1 A1 fix pinned token_uri/auth_uri to a GOOGLE host allowlist so a
tampered client-secrets file could not exfiltrate the auth code/secret to an
attacker endpoint. Wave-3 generalizes that to a PER-PROVIDER registry:
gmail/gcal (provider="google") pin EXACTLY as before, and outlook
(provider="microsoft") pins to login.microsoftonline.com. This module proves:

  * the Microsoft consent flow requests offline_access + Mail.Read, PKCE S256,
    state, on the `common` tenant endpoint;
  * a poisoned token_uri/auth_uri is neutralized for BOTH providers (never
    trusted from the file), and a genuine endpoint passes through;
  * OAuthTokenManager.refresh() ALSO pins the cache's token_uri per provider;
  * outlook.py --authorize seeds a 0600 cache that load()/refresh() accept;
  * Outlook is OPTIONAL: no token.json is a quiet not_configured state.

New module, unittest style, tmp $HOME. No network, no browser (both injection
points used).
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
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


ol = _load("ol_authz_test", "bin/connectors/outlook.py")
conn = ol.connector
MS_AUTH = conn.MICROSOFT_AUTH_URI
MS_TOKEN = conn.MICROSOFT_TOKEN_URI
G_AUTH = conn.DEFAULT_AUTH_URI
G_TOKEN = conn.DEFAULT_TOKEN_URI


def _canned_tokens(**over):
    d = {"access_token": "ACCESS", "refresh_token": "REFRESH",
         "expires_in": 3600}
    d.update(over)
    return d


class _CaptureGetter:
    """Injected code_getter: runs the flow with NO browser/network. Captures the
    consent URL and returns a canned (code, state, redirect_uri)."""
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


# ─── the reusable installed-app flow, provider="microsoft" ───────────────────

class MicrosoftFlowTests(unittest.TestCase):
    def test_auth_url_has_offline_access_scope_pkce_state_common_tenant(self):
        getter = _CaptureGetter()
        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES), token_uri=MS_TOKEN, auth_uri=MS_AUTH,
            code_getter=getter, exchange_transport=lambda u, f: _canned_tokens())
        self.assertTrue(getter.auth_url.startswith(MS_AUTH))
        self.assertIn("/common/", getter.auth_url)  # personal + work/school
        q = urllib.parse.parse_qs(urllib.parse.urlparse(getter.auth_url).query)
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["code_challenge_method"], ["S256"])   # PKCE
        self.assertTrue(q["code_challenge"][0])
        self.assertTrue(q["state"][0])                           # CSRF token
        # offline_access is how Microsoft issues a refresh_token.
        self.assertIn("offline_access", q["scope"][0])
        self.assertIn("Mail.Read", q["scope"][0])
        self.assertIn("User.Read", q["scope"][0])  # D2: /me needs User.Read
        # Microsoft keys off the scope, NOT Google's access_type/prompt params.
        self.assertNotIn("access_type", q)

    def test_pkce_verifier_sent_to_token_endpoint_matches_challenge(self):
        getter = _CaptureGetter()
        seen = {}

        def exchange(uri, form):
            seen.update(form)
            seen["uri"] = uri
            return _canned_tokens()

        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES), code_getter=getter,
            token_uri=MS_TOKEN, auth_uri=MS_AUTH, exchange_transport=exchange)
        self.assertEqual(seen["grant_type"], "authorization_code")
        self.assertEqual(seen["code"], "AUTHCODE")
        self.assertTrue(seen["code_verifier"])
        import base64
        import hashlib
        q = urllib.parse.parse_qs(urllib.parse.urlparse(getter.auth_url).query)
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(seen["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(q["code_challenge"][0], expect)

    def test_returned_token_dict_token_uri_pinned_microsoft(self):
        getter = _CaptureGetter()
        tok = conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES), token_uri=MS_TOKEN, auth_uri=MS_AUTH,
            code_getter=getter, exchange_transport=lambda u, f: _canned_tokens(
                expires_in=1234), now=1000.0)
        self.assertEqual(tok["token_uri"], MS_TOKEN)
        self.assertEqual(tok["refresh_token"], "REFRESH")
        self.assertEqual(tok["expiry_epoch"], 1000.0 + 1234)
        self.assertEqual(tok["scopes"], "offline_access User.Read Mail.Read")

    def test_no_refresh_token_raises_offline_access_hint(self):
        getter = _CaptureGetter()
        with self.assertRaises(conn.OAuthError) as cm:
            conn.run_installed_app_flow(
                client_id="CID", client_secret="SEC", provider="microsoft",
                scopes=list(ol.OUTLOOK_SCOPES), code_getter=getter,
                token_uri=MS_TOKEN, auth_uri=MS_AUTH,
                exchange_transport=lambda u, f: {"access_token": "A",
                                                 "expires_in": 3600})
        msg = str(cm.exception).lower()
        self.assertIn("refresh_token", msg)
        self.assertIn("offline_access", msg)  # provider-specific remediation

    def test_poisoned_token_uri_pinned_to_microsoft(self):
        getter = _CaptureGetter()
        seen = {}
        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES),
            token_uri="https://attacker.example/steal", auth_uri=MS_AUTH,
            code_getter=getter,
            exchange_transport=lambda u, f: seen.update({"uri": u})
            or _canned_tokens())
        self.assertEqual(seen["uri"], MS_TOKEN)
        self.assertNotIn("attacker", seen["uri"])

    def test_poisoned_auth_uri_pinned_to_microsoft(self):
        getter = _CaptureGetter()
        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES),
            auth_uri="https://attacker.example/auth", token_uri=MS_TOKEN,
            code_getter=getter,
            exchange_transport=lambda u, f: _canned_tokens())
        self.assertTrue(getter.auth_url.startswith(MS_AUTH))
        self.assertNotIn("attacker", getter.auth_url)

    def test_public_client_no_secret_omitted_from_exchange_and_token(self):
        # D3: a Microsoft PUBLIC client authorizes with NO client_secret — the
        # exchange POST must omit it (AAD rejects a presented secret) and the
        # returned token dict must carry none (there is nothing to store).
        getter = _CaptureGetter()
        seen = {}
        tok = conn.run_installed_app_flow(
            client_id="CID", client_secret=None, provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES), token_uri=MS_TOKEN, auth_uri=MS_AUTH,
            code_getter=getter,
            exchange_transport=lambda u, f: seen.update(f) or _canned_tokens())
        self.assertNotIn("client_secret", seen)      # never POSTed
        self.assertEqual(seen["code_verifier"], seen.get("code_verifier"))
        self.assertTrue(seen["code_verifier"])       # PKCE substitutes for it
        self.assertNotIn("client_secret", tok)       # nothing to persist

    def test_google_still_requires_client_secret(self):
        # D3: Google's token endpoint needs a secret EVEN with PKCE, so the flow
        # fails closed for a secret-less google authorize (unchanged confidential
        # behavior).
        with self.assertRaises(conn.OAuthError):
            conn.run_installed_app_flow(
                client_id="CID", client_secret=None, provider="google",
                scopes=["s"], token_uri=G_TOKEN, auth_uri=G_AUTH,
                code_getter=_CaptureGetter(),
                exchange_transport=lambda u, f: _canned_tokens())

    def test_legit_microsoft_endpoints_pass_through(self):
        getter = _CaptureGetter()
        seen = {}
        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider="microsoft",
            scopes=list(ol.OUTLOOK_SCOPES), token_uri=MS_TOKEN, auth_uri=MS_AUTH,
            code_getter=getter,
            exchange_transport=lambda u, f: seen.update({"uri": u})
            or _canned_tokens())
        self.assertEqual(seen["uri"], MS_TOKEN)
        self.assertTrue(getter.auth_url.startswith(MS_AUTH))


# ─── the per-provider pinning regression (google unchanged; both neutralized) ─

class ProviderPinningRegressionTests(unittest.TestCase):
    """The security-critical generalization: google STILL pins to Google,
    microsoft pins to Microsoft, and a poisoned token_uri is neutralized for
    BOTH — the file value is never trusted for either provider."""

    def _exchanged_uri(self, *, provider, token_uri, auth_uri):
        getter = _CaptureGetter()
        seen = {}
        conn.run_installed_app_flow(
            client_id="CID", client_secret="SEC", provider=provider,
            scopes=["s"], token_uri=token_uri, auth_uri=auth_uri,
            code_getter=getter,
            exchange_transport=lambda u, f: seen.update({"uri": u})
            or _canned_tokens())
        return seen["uri"], getter.auth_url

    def test_google_default_provider_still_pins_google(self):
        # No provider arg at all → defaults to google → identical wave-1 behavior.
        getter = _CaptureGetter()
        seen = {}
        conn.run_installed_app_flow(
            client_id="C", client_secret="S", scopes=["s"],
            token_uri="https://attacker.example/steal", auth_uri=G_AUTH,
            code_getter=getter,
            exchange_transport=lambda u, f: seen.update({"uri": u})
            or _canned_tokens())
        self.assertEqual(seen["uri"], G_TOKEN)
        self.assertTrue(getter.auth_url.startswith(G_AUTH))

    def test_poisoned_token_uri_neutralized_for_both_providers(self):
        for provider, pinned, auth in (("google", G_TOKEN, G_AUTH),
                                       ("microsoft", MS_TOKEN, MS_AUTH)):
            uri, _ = self._exchanged_uri(
                provider=provider,
                token_uri="https://attacker.example/steal", auth_uri=auth)
            self.assertEqual(uri, pinned, msg=provider)
            self.assertNotIn("attacker", uri)

    def test_legit_endpoint_passes_through_for_both_providers(self):
        for provider, token, auth in (("google", G_TOKEN, G_AUTH),
                                      ("microsoft", MS_TOKEN, MS_AUTH)):
            uri, aurl = self._exchanged_uri(
                provider=provider, token_uri=token, auth_uri=auth)
            self.assertEqual(uri, token, msg=provider)
            self.assertTrue(aurl.startswith(auth), msg=provider)

    def test_cross_provider_host_is_rejected(self):
        # A Google host offered to the MICROSOFT flow is NOT on Microsoft's
        # allowlist → pinned to Microsoft's default (and vice-versa). The
        # allowlists never bleed across providers.
        uri, aurl = self._exchanged_uri(
            provider="microsoft", token_uri=G_TOKEN, auth_uri=G_AUTH)
        self.assertEqual(uri, MS_TOKEN)
        self.assertTrue(aurl.startswith(MS_AUTH))
        uri2, aurl2 = self._exchanged_uri(
            provider="google", token_uri=MS_TOKEN, auth_uri=MS_AUTH)
        self.assertEqual(uri2, G_TOKEN)
        self.assertTrue(aurl2.startswith(G_AUTH))

    def test_http_scheme_is_pinned_to_https_default_for_both_providers(self):
        # F1: a poisoned http:// token_uri on an OTHERWISE-VALID provider host
        # must NOT survive — a cleartext POST would leak the code + PKCE verifier
        # + secret to an on-path attacker. Host-only pinning (wave-1) let this
        # through; scheme pinning closes it for BOTH providers (incl. the merged
        # gmail/gcal OAuth).
        cases = (
            ("google", "http://oauth2.googleapis.com/token", G_TOKEN, G_AUTH),
            ("microsoft",
             "http://login.microsoftonline.com/common/oauth2/v2.0/token",
             MS_TOKEN, MS_AUTH),
        )
        for provider, poisoned_http, pinned, auth in cases:
            uri, _ = self._exchanged_uri(
                provider=provider, token_uri=poisoned_http, auth_uri=auth)
            self.assertEqual(uri, pinned, msg=provider)
            self.assertTrue(uri.startswith("https://"), msg=provider)

    def test_http_scheme_auth_uri_pinned_to_https_default(self):
        # F1 for the AUTHORIZE endpoint: a poisoned http:// auth_uri on a valid
        # host phishes consent over cleartext → pinned to the https default.
        _, aurl = self._exchanged_uri(
            provider="microsoft",
            token_uri=MS_TOKEN,
            auth_uri="http://login.microsoftonline.com/common/oauth2/v2.0/authorize")
        self.assertTrue(aurl.startswith("https://"))
        self.assertTrue(aurl.startswith(MS_AUTH))

    def test_matching_host_with_tampered_port_pinned_to_default(self):
        # F3: a valid https host but a non-default PORT must not smuggle a
        # redirect to an attacker-controlled listener — reconstruct from the
        # pinned default.
        uri, _ = self._exchanged_uri(
            provider="microsoft",
            token_uri="https://login.microsoftonline.com:8443/common/oauth2/v2.0/token",
            auth_uri=MS_AUTH)
        self.assertEqual(uri, MS_TOKEN)

    def test_unknown_provider_fails_closed(self):
        # F4: a misspelled/unknown provider must RAISE (fail closed), never
        # silently fall back to Google's pinning for a Microsoft flow.
        with self.assertRaises(conn.OAuthError):
            conn._provider("microsft")
        with self.assertRaises(conn.OAuthError):
            conn.run_installed_app_flow(
                client_id="C", client_secret="S", provider="nope",
                scopes=["s"], code_getter=_CaptureGetter(),
                exchange_transport=lambda u, f: _canned_tokens())

    def test_refresh_pins_cache_token_uri_per_provider(self):
        # Defense-in-depth: even a token.json whose token_uri was later tampered
        # must refresh against the PINNED provider endpoint, for both providers.
        for provider, poisoned_default, pinned in (
                ("google", G_TOKEN, G_TOKEN),
                ("microsoft", MS_TOKEN, MS_TOKEN)):
            with TemporaryDirectory() as td:
                path = Path(td) / "token.json"
                path.write_text(json.dumps({
                    "refresh_token": "R", "client_id": "c",
                    "client_secret": "s", "access_token": "OLD",
                    "expiry_epoch": 0,
                    "token_uri": "https://attacker.example/steal"}))
                seen = {}

                def transport(uri, form):
                    seen["uri"] = uri
                    return {"access_token": "NEW", "expires_in": 3600}

                mgr = conn.OAuthTokenManager(path, provider=provider,
                                             token_uri=poisoned_default,
                                             transport=transport)
                self.assertEqual(mgr.access_token(now=100.0), "NEW")
                self.assertEqual(seen["uri"], pinned, msg=provider)
                self.assertNotIn("attacker", seen["uri"])


# ─── client-secrets JSON parsing (Microsoft) ─────────────────────────────────

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
        p = self._write({"installed": {"client_id": "cid", "client_secret": "shh",
                                       "token_uri": "https://tok",
                                       "auth_uri": "https://auth"}})
        cid, sec, tok, auth = ol._load_client_secrets(p)
        self.assertEqual((cid, sec, tok, auth),
                         ("cid", "shh", "https://tok", "https://auth"))

    def test_bare_shape_defaults_to_microsoft_uris(self):
        p = self._write({"client_id": "cid", "client_secret": "shh"})
        cid, sec, tok, auth = ol._load_client_secrets(p)
        self.assertEqual((cid, sec), ("cid", "shh"))
        self.assertEqual(tok, MS_TOKEN)   # defaults to Microsoft `common`
        self.assertEqual(auth, MS_AUTH)

    def test_missing_client_id_raises(self):
        p = self._write({"installed": {"client_secret": "only_secret"}})
        with self.assertRaises(ValueError):
            ol._load_client_secrets(p)

    def test_public_client_no_secret_is_allowed(self):
        # D3: an Azure "Mobile and desktop applications" registration is a PUBLIC
        # client with NO secret — client_id alone must parse, secret → None.
        p = self._write({"installed": {"client_id": "cid"}})
        cid, sec, tok, auth = ol._load_client_secrets(p)
        self.assertEqual(cid, "cid")
        self.assertIsNone(sec)
        self.assertEqual(tok, MS_TOKEN)
        self.assertEqual(auth, MS_AUTH)


# ─── outlook.py --authorize end-to-end + OPTIONAL not_configured ─────────────

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

    def _client_secrets(self, **over):
        p = self.home / "cs.json"
        block = {"client_id": "CID", "client_secret": "SEC"}
        block.update(over)
        p.write_text(json.dumps({"installed": block}))
        return p


class AuthorizeIntegrationTests(HomeTestCase):
    def test_authorize_seeds_cache_that_load_and_refresh_accept(self):
        getter = _CaptureGetter()
        path = ol.authorize(self._client_secrets(), code_getter=getter,
                            exchange_transport=lambda u, f: _canned_tokens())
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        c = ol.OutlookConnector()
        self.assertEqual(c.token_path(), path)
        loaded = c.tokens.load()
        self.assertEqual(loaded["refresh_token"], "REFRESH")
        self.assertEqual(loaded["scopes"], "offline_access User.Read Mail.Read")
        self.assertEqual(loaded["token_uri"], MS_TOKEN)  # pinned at seed time

    def test_authorize_poisoned_client_secrets_token_uri_pinned_microsoft(self):
        # A tampered client-secrets file with a real Microsoft auth_uri (victim
        # consents to their OWN mailbox) but an attacker token_uri: the code
        # exchange must STILL POST to Microsoft's real endpoint.
        p = self._client_secrets(auth_uri=MS_AUTH,
                                 token_uri="https://attacker.example/steal")
        seen = {}
        ol.authorize(p, code_getter=_CaptureGetter(),
                     exchange_transport=lambda u, f: seen.update({"uri": u})
                     or _canned_tokens())
        self.assertEqual(seen["uri"], MS_TOKEN)
        self.assertNotIn("attacker", seen["uri"])

    def test_authorize_refuses_clobber_then_force(self):
        ol.authorize(self._client_secrets(), code_getter=_CaptureGetter(),
                     exchange_transport=lambda u, f: _canned_tokens(
                         refresh_token="FIRST"))
        with self.assertRaises(conn.OAuthError):
            ol.authorize(self._client_secrets(), code_getter=_CaptureGetter(),
                         exchange_transport=lambda u, f: _canned_tokens())
        ol.authorize(self._client_secrets(), force=True,
                     code_getter=_CaptureGetter(),
                     exchange_transport=lambda u, f: _canned_tokens(
                         refresh_token="SECOND"))
        self.assertEqual(ol.OutlookConnector().tokens.load()["refresh_token"],
                         "SECOND")


class NotConfiguredTests(HomeTestCase):
    def _hb(self, c):
        return json.loads(c.heartbeat_path().read_text())

    def test_poll_once_without_token_is_not_configured_not_error(self):
        c = ol.OutlookConnector()
        res = c.poll_once()
        self.assertEqual(res["status"], "not_configured")
        self.assertEqual(res["emitted"], 0)
        self.assertEqual(res["errors"], [])
        hb = self._hb(c)
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])
        self.assertEqual(hb["errors"], [])

    def test_main_once_without_token_exits_clean_rc0(self):
        rc = ol.main(["--once"])
        self.assertEqual(rc, 0)
        c = ol.OutlookConnector()
        self.assertFalse(c.token_path().exists())
        self.assertEqual(self._hb(c)["status"], "not_configured")

    def test_main_run_forever_without_token_does_not_spin(self):
        # F3: default (no --once) with no token must NOT exit(0) — it enters the
        # resident loop that re-checks config on a long cadence.
        entered = {"n": 0}

        def fake_run_forever(inner_self, **_kw):
            entered["n"] += 1
            inner_self.poll_once()

        orig = ol.OutlookConnector.run_forever
        ol.OutlookConnector.run_forever = fake_run_forever
        try:
            rc = ol.main([])
        finally:
            ol.OutlookConnector.run_forever = orig
        self.assertEqual(rc, 0)
        self.assertEqual(entered["n"], 1)
        self.assertEqual(self._hb(ol.OutlookConnector())["status"],
                         "not_configured")

    def test_authorize_missing_client_secrets_arg_rc2(self):
        self.assertEqual(ol.main(["--authorize"]), 2)


class RedirectHostTests(unittest.TestCase):
    """D3c: the loopback callback server ALWAYS binds 127.0.0.1, but the
    redirect_uri it ADVERTISES uses the provider's redirect_host — `localhost`
    for Microsoft (AAD's port-agnostic loopback exception is scoped to
    `http://localhost` with NO trailing slash; a 127.0.0.1 redirect fails
    AADSTS50011) and `127.0.0.1` (with the wave-1 trailing slash) for Google."""

    def _advertised_redirect(self, redirect_host):
        holder: dict = {}
        result: dict = {}
        getter = conn._LoopbackCodeGetter(timeout_sec=10,
                                          open_url=lambda u: None,
                                          redirect_host=redirect_host)

        def build_auth_url(redirect_uri):
            holder["uri"] = redirect_uri
            return "http://example.invalid/consent"

        def run():
            try:
                result["ret"] = getter(build_auth_url, "STATE")
            except Exception as ex:  # noqa: BLE001
                result["err"] = ex

        t = threading.Thread(target=run, daemon=True)
        t.start()
        for _ in range(1000):
            if holder.get("uri"):
                break
            time.sleep(0.005)
        self.assertIn("uri", holder, "loopback server never bound")
        advertised = holder["uri"]
        # The bind is always 127.0.0.1 regardless of the advertised host; hit it
        # directly by port to complete the callback and let the thread finish.
        port = urllib.parse.urlparse(advertised).port
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?code=C&state=STATE", timeout=5).read()
        t.join(timeout=5)
        return advertised

    def test_microsoft_advertises_localhost_without_trailing_slash(self):
        uri = self._advertised_redirect("localhost")
        parsed = urllib.parse.urlparse(uri)
        self.assertEqual(parsed.hostname, "localhost")   # NOT 127.0.0.1
        self.assertEqual(parsed.path, "")                # no trailing slash
        self.assertTrue(uri.startswith("http://localhost:"))
        self.assertFalse(uri.endswith("/"))

    def test_google_advertises_127_0_0_1_with_trailing_slash_unchanged(self):
        uri = self._advertised_redirect("127.0.0.1")
        parsed = urllib.parse.urlparse(uri)
        self.assertEqual(parsed.hostname, "127.0.0.1")   # wave-1 behavior kept
        self.assertEqual(parsed.path, "/")               # trailing slash
        self.assertTrue(uri.startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
