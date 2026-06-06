"""ToolSubsystem — thin in-process wrapper over bin/tool-dispatch.py.

Per the build spec, the tool scripts under bin/tools/ stay as-is, and the
daemon calls them through the EXISTING tool-dispatch.py interface (no rewrite).
So this subsystem is not a loop — it is the daemon's single entry point for
"run a named tool", used by the `status` CLI and available to any future
in-process caller. `run()` is a no-op park: it has no background work, it just
waits for shutdown so it fits the uniform Subsystem lifecycle.
"""
from __future__ import annotations

import json
import subprocess
import sys

from . import Subsystem


class ToolSubsystem(Subsystem):
    name = "tools"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dispatches = 0

    def run(self) -> None:
        # No background work — park until shutdown so the daemon can treat
        # every subsystem uniformly (start a thread, join on stop).
        self.log.info("tools subsystem ready (dispatch on demand)")
        while not self.stop.is_set():
            if self.wait(3600):
                break

    def list_tools(self) -> list[dict]:
        """Return the tool manifest (name/description/args) via the dispatcher's
        own --list, so the daemon never re-parses tools-manifest.json itself."""
        rc, out = self._dispatch_raw(["--list"])
        if rc != 0:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []

    def dispatch(self, tool: str, args: list[str] | None = None) -> dict:
        """Run one named tool and return its parsed JSON stdout (tools emit
        clean JSON by contract). On any failure, return a structured error
        dict rather than raising — the dispatcher itself never raises."""
        self._dispatches += 1
        rc, out = self._dispatch_raw([tool, *(args or [])])
        try:
            parsed = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw": out}
        if rc != 0 and "error" not in parsed:
            parsed = {"error": f"tool {tool!r} exited {rc}", "tool": tool, **parsed}
        return parsed

    def _dispatch_raw(self, argv: list[str]) -> tuple[int, str]:
        script = self.config.tool_dispatch_script
        if not script.exists():
            return 1, json.dumps({"error": f"tool-dispatch missing at {script}"})
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *argv],
                capture_output=True, text=True, timeout=90,
            )
            return proc.returncode, proc.stdout
        except subprocess.TimeoutExpired:
            return 124, json.dumps({"error": "tool-dispatch timed out"})
        except Exception as e:  # noqa: BLE001
            return 1, json.dumps({"error": str(e)})

    def status(self) -> dict:
        return {"name": self.name, "dispatches": self._dispatches}
