"""Dashboard Connections panel (Keel M5): the optional-connector tri-state
surface rendered by bin/render-assistant-page.py.

The panel lists EVERY known connector with its state — Connected (ok),
Available/not connected (not_configured), Needs attention (error) — from the
SAME world.json `connectors` block the health section derives from. This proves:
  * all three states render distinctly from a fixture world.json;
  * a fresh install (nothing configured) renders every connector as
    "available, not connected" — an honest empty state, not blank, not an error;
  * connector-derived strings (name, error text) are html.escape(quote=True)'d —
    an XSS payload in a connector error/name never reaches the page verbatim;
  * a panel-build failure degrades to a small message (the page still renders).

New module (unittest style); loads the renderer by path like the sibling
renderer tests. No live network.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/render-assistant-page.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("renderer_conn_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConnectionsPanelTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        home = Path(self._tmp_obj.name)
        (home / ".claude/cache").mkdir(parents=True)
        self.mod = load_module(home)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_renders_all_three_states(self):
        world = {"connectors": {
            "gmail": {"status": "ok", "age_sec": 42, "last_poll": "T",
                      "token_expiry": "2026-06-01T00:00:00Z"},
            "github": {"status": "not_configured"},
            "jira": {"status": "error", "stale": True, "errors": ["boom"]},
        }}
        html, n = self.mod.render_connections_panel(world)
        self.assertEqual(n, 1)                       # only gmail connected
        self.assertIn("Connected", html)
        self.assertIn("Available", html)             # github not_configured
        self.assertIn("Needs attention", html)       # jira error
        self.assertIn("conn-dot ok", html)
        self.assertIn("conn-dot available", html)
        self.assertIn("conn-dot attention", html)
        # ok connector shows relative last-poll + token expiry.
        self.assertIn("last poll 42s", html)
        self.assertIn("2026-06-01T00:00:00Z", html)

    def test_known_connector_shows_hint_when_not_configured(self):
        # github has no world entry at all → enumerated from the registry as
        # available, with its how-to-connect hint.
        html, n = self.mod.render_connections_panel({"connectors": {}})
        self.assertIn("gh auth login", html)                 # github hint
        self.assertIn("--authorize", html)                   # gmail hint

    def test_fresh_install_all_available_no_error(self):
        html, n = self.mod.render_connections_panel({"connectors": {}})
        self.assertEqual(n, 0)
        self.assertIn("GitHub notifications", html)
        self.assertIn("Gmail", html)
        # Honest empty state: available, never styled as error/attention.
        self.assertIn("conn-dot available", html)
        self.assertNotIn("conn-dot attention", html)
        self.assertNotIn("Needs attention", html)

    def test_xss_in_error_is_escaped(self):
        payload = '<script>alert(1)</script>'
        world = {"connectors": {
            "gmail": {"status": "error", "errors": [payload]}}}
        html, n = self.mod.render_connections_panel(world)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_xss_in_connector_name_is_escaped(self):
        world = {"connectors": {
            '<img src=x onerror=alert(1)>': {"status": "error",
                                             "errors": ["x"]}}}
        html, n = self.mod.render_connections_panel(world)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_build_failure_degrades_to_message(self):
        # world is None → world.get raises inside; the fence degrades to a small
        # message tuple instead of propagating (the page still renders).
        html, n = self.mod.render_connections_panel(None)
        self.assertEqual(n, 0)
        self.assertIn("Connections unavailable", html)
        self.assertIn("empty", html)


if __name__ == "__main__":
    unittest.main()
