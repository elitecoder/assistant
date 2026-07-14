"""CommsSubsystem — outbound Slack: ledger broadcasts + heartbeat paging.

The in-process-daemon variant of the two mechanical, no-LLM loops from
bin/comms-listen.py (ledger watcher + heartbeat pager). It does NOT carry
comms-listen.py's inbound warm-session reply loop NOR its lesson-proposal
delivery loop — both stay in comms-listen.py; the single-process daemon is
additive and must not double-poll the same Slack channel as the standalone
daemon. (Proposal delivery is coupled to the inbound loop: confirmation happens
via the warm session's `y`/`n` reply flow, so whichever daemon owns inbound must
own proposal delivery too — migrate the two together, sharing proposals.cursor,
never split across processes or they race on the high-water mark.)

Two cooperating jobs, each on its own thread under the shared shutdown Event:

  1. Ledger watcher — poll actions-ledger.jsonl via LedgerReader.read_new();
     broadcast each new entry through slack.send(), suppressing routine noise
     (noop / emit-card / self-update-skip / lesson-proposal), exactly as
     comms-listen.py does. Each sent broadcast is mirrored into
     conversation.jsonl as an out turn.

  2. Heartbeat pager — every heartbeat_check_sec, read Assistant's pulse
     heartbeat.json; if stale (age > stale_heartbeat_sec) or status is bad,
     send a templated urgent page, deduped to heartbeat_dedup_sec.

When Slack isn't configured (no token / no target / target not allowlisted),
both jobs still run their read loops but skip the actual send — so an
unconfigured box exercises the watch logic without egress. The send itself is
gated by slack.send() against config.allowed_targets.
"""
from __future__ import annotations

import json
import threading
import time

from . import Subsystem
from .. import conversation, ledger, slack


class CommsSubsystem(Subsystem):
    name = "comms"

    def __init__(self, *args, send_enabled: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        # Caller can force-disable egress (--dry-run); otherwise we send only
        # when Slack is actually configured AND the target is allowlisted.
        self._send_enabled = send_enabled and self.config.has_slack
        self._reader = ledger.LedgerReader(
            self.config.ledger_path, self.config.ledger_cursor_path)
        self._last_alert = 0
        self._broadcasts = 0
        self._pages = 0
        self._threads: list[threading.Thread] = []

    def run(self) -> None:
        self.log.info("comms subsystem started (send_enabled=%s)", self._send_enabled)
        self._reader.initialize_cursor_if_missing()
        self._threads = [
            threading.Thread(target=self._ledger_loop, name="comms-ledger", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="comms-heartbeat", daemon=True),
        ]
        for t in self._threads:
            t.start()
        while not self.stop.is_set():
            if self.wait(1):
                break
        for t in self._threads:
            t.join(timeout=3)
        self.log.info("comms subsystem stopped (%d broadcast(s), %d page(s))",
                      self._broadcasts, self._pages)

    # ── ledger watcher ────────────────────────────────────────────────────

    def _ledger_loop(self) -> None:
        while not self.stop.is_set():
            try:
                entries = self._reader.read_new()
            except Exception as e:  # noqa: BLE001
                self.log.warning("ledger read error: %s", e)
                entries = []
            for entry in entries:
                if self.stop.is_set():
                    break
                self._broadcast_entry(entry)
            if self.stop.wait(self.config.ledger_poll_sec):
                break

    def _broadcast_entry(self, entry: dict) -> None:
        """Apply the suppression rules, then send + mirror. Mirrors
        comms-listen.py's ledger_loop suppression set exactly."""
        if entry.get("outcome") == "skipped":
            return  # no work happened
        kind = entry.get("kind", "")
        key = entry.get("key", "")
        if kind in ("noop", "emit-card"):
            self.log.debug("suppressed routine broadcast kind=%s key=%s", kind, key)
            return
        if kind == "self-update" and "skip" in key:
            self.log.debug("suppressed self-update-skip broadcast key=%s", key)
            return
        if kind in ("lesson-proposal", "lesson_proposal") or key.startswith("lesson-proposal"):
            self.log.debug("suppressed lesson-proposal broadcast key=%s", key)
            return

        body = slack.fmt_action_line(entry)
        if not self._send_enabled:
            self.log.info("would broadcast key=%s (send disabled)", key)
            return
        target = self.config.target
        try:
            result = slack.send(body, target, token=self.config.bot_token,
                                allowed=self.config.allowed_targets, kind="action")
        except RuntimeError as e:
            self.log.warning("ledger broadcast failed key=%s target=%s: %s",
                             key, target, str(e)[:160])
            return
        msg_ts = result.get("ts")
        channel = result.get("channel", target)
        if msg_ts:
            try:
                conversation.append_turn(
                    self.config.conversation_path, str(channel), str(msg_ts),
                    "out", body, kind="action")
            except Exception as e:  # noqa: BLE001 — mirror is best-effort
                self.log.debug("conversation mirror failed: %s", e)
        self._broadcasts += 1
        self.log.info("broadcast key=%s", key)

    # ── heartbeat pager ───────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            self._check_heartbeat()
            if self.stop.wait(self.config.heartbeat_check_sec):
                break

    def _check_heartbeat(self) -> None:
        hb = self._read_heartbeat()
        last_ts = int(hb.get("last_pulse_ts") or 0)
        if last_ts <= 0:
            return  # no heartbeat yet — nothing to judge
        age = int(time.time()) - last_ts
        stale = age > self.config.stale_heartbeat_sec
        bad = hb.get("status") in {"frozen", "stale_world", "respawn-requested"}
        now = int(time.time())
        if (stale or bad) and now - self._last_alert >= self.config.heartbeat_dedup_sec:
            body = slack.fmt_heartbeat_alert(hb, age)
            if self._send_enabled:
                try:
                    slack.send(body, self.config.target, token=self.config.bot_token,
                               allowed=self.config.allowed_targets, kind="urgent")
                except RuntimeError as e:
                    self.log.warning("heartbeat page failed target=%s: %s",
                                     self.config.target, str(e)[:160])
            else:
                self.log.info("would page: heartbeat stale age=%ss (send disabled)", age)
            self._last_alert = now
            self._pages += 1
            self.log.warning("heartbeat-stale page age=%ss", age)
        elif not (stale or bad):
            self._last_alert = 0  # healthy → re-arm

    def _read_heartbeat(self) -> dict:
        p = self.config.heartbeat_path
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def status(self) -> dict:
        return {
            "name": self.name,
            "send_enabled": self._send_enabled,
            "broadcasts": self._broadcasts,
            "pages": self._pages,
            "cursor": self._reader.read_cursor(),
        }
