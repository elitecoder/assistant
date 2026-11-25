"""The default mode's way of typing, and the doorway to Pro mode's record of what is typed.

Everything that types into an agent session from Assistant's own code asks one question first: is this Pro mode?
In Pro mode the typing goes through `assistant.session_effects` (the ledger: an intent on disk before the first
keystroke, a result after). In the default mode it goes through `Keys` below, which does what typing always did:
text, a pause, Enter, and nothing recorded or refused.

This module uses the standard library only, and loads `assistant.mode`, `session_effects` (and through it
`effects` and `pointers`) only when it needs them and only inside a `try`, so the default mode never depends on
those loading: a failure to load any of them is the default mode, and the services that type keep running.

`effects` stands in for `assistant.session_effects` in each of those services:
- `effects.active()` asks the mode and loads nothing.
- Any other name loads `session_effects` the first time it is asked for. If that fails in Pro mode it is said
  loudly, once (in the log), and every use then refuses: `begin`, `Keys` and `type_line` raise `Unavailable` (which
  is what `effects.Refused` then names, with code `unrecorded`), so nothing is typed without its record; `writable()`
  is False, `finish` and `trace_of` do nothing.
- In the default mode `effects.Refused` is a class nothing raises, and nothing is loaded.

The calls that type are named here and in `session_effects.py` and nowhere else (`tests/test_session_typing_fence.py`).
"""
from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("assistant.session_gate")

UNRECORDED = ("Assistant couldn't record what it types, so it typed nothing. "
              "Check that your disk isn't full and that ~/.assistant/effects can be written to.")


def active() -> bool:
    """Pro mode. A mode that can't be read, or `assistant.mode` failing to load, is the default mode."""
    try:
        from assistant import mode  # noqa: PLC0415
        return bool(mode.is_pro())
    except Exception:  # noqa: BLE001 - never let the mode lookup change what a typing does
        return False


class Unavailable(Exception):
    """What stands in for `session_effects.Refused` when Pro mode can't load `session_effects`."""

    def __init__(self, code: str = "unrecorded", message: str = UNRECORDED, short: str | None = None):
        super().__init__(message)
        self.code = code
        self.short = short or message


class _NothingRaised(Exception):
    """What `effects.Refused` names in the default mode: nothing is recorded there, so nothing raises it."""


def _refuse(*_a, **_k):
    raise Unavailable()


class _Effects:
    """`session_effects`, loaded the first time Pro mode needs it (see the module's docstring)."""

    def __init__(self):
        self._module = None
        self._said = False

    @staticmethod
    def active() -> bool:
        return active()

    def _load(self):
        if self._module is None:
            try:
                from assistant import session_effects  # noqa: PLC0415
                self._module = session_effects
            except Exception as exc:  # noqa: BLE001
                if not self._said:
                    self._said = True
                    log.error("session_effects can't be loaded (%s: %s): in Pro mode nothing will be typed, "
                              "because it can't be recorded", type(exc).__name__, exc)
        return self._module

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "Refused" and self._module is None and not active():
            return _NothingRaised
        module = self._load()
        if module is not None:
            return getattr(module, name)
        return {"Refused": Unavailable, "UNRECORDED": UNRECORDED, "INVALID": UNRECORDED,
                "writable": lambda: False, "finish": lambda *a, **k: None,
                "trace_of": lambda *a, **k: ""}.get(name, _refuse)


effects = _Effects()


class Keys:
    """The keystrokes of a typing begun in the default mode: as they always were. `rpc(method, params)` answers
    with the reply, or None when cmux refused or didn't answer in time (a `TimeoutError` is read as that, so the
    typing carries on as it always did); the Enter follows the text whatever cmux said about it."""

    def __init__(self, rpc, surface_id: str):
        self.rpc, self.surface_id = rpc, surface_id
        self.confirmed = None  # nothing here records or checks, so there is nothing to confirm

    def outcome(self) -> str:
        """Always `done`: nothing is recorded, and what cmux answered never stopped a typing."""
        return "done"

    def finish(self) -> None:
        """Nothing to close: nothing was recorded."""

    def _go(self, method: str, params: dict) -> bool:
        try:
            return self.rpc(method, {"surface_id": self.surface_id, **params}) is not None
        except TimeoutError:
            return False

    def text(self, text: str) -> bool:
        return self._go("surface.send_text", {"text": text})

    def key(self, key: str) -> bool:
        return self._go("surface.send_key", {"key": key})

    def carriage_return(self) -> bool:
        """Enter as a character, for a terminal that ignores the key."""
        return self.text("\r")

    def line(self, text: str, enter: str = "key", pause: float = 0.0, sleep=None) -> bool:
        """Type `text`, then Enter (`enter` is "key", "cr" or "" for none). Returns whether cmux took the text."""
        took = self.text(text)
        if enter:
            if pause:
                (sleep or time.sleep)(pause)
            self.key("enter") if enter == "key" else self.carriage_return()
        return took


def run_now_legacy(cmux_bin: str, ws_ref: str, text: str) -> None:
    """What the dashboard's "run now" has always typed in the default mode, exactly: `cmux send` to the workspace's
    active surface, then Enter."""
    subprocess.run([cmux_bin, "send", "--workspace", ws_ref, text], check=False, timeout=4, capture_output=True)
    subprocess.run([cmux_bin, "send-key", "--workspace", ws_ref, "Enter"], check=False, timeout=4, capture_output=True)
