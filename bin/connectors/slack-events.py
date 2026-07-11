#!/usr/bin/env python3
"""slack-events.py — read-only Slack events connector (Keel M5 wave 2).

WHY: an @-mention of the bot or a DM in Slack is a world event Mukul must not
have to sit in Slack to catch. This connector NORMALIZES those into WorldEvents
in the inbox so they flow through the same policy/decision spine as email and
GitHub.

HOW EVENTS ARRIVE (design section 9, Slack row — "rides the existing Bolt app's
app_mention/DM handlers"): the existing Node slack-reactor already runs a Bolt
Socket-Mode app. It gains two READ-ONLY handlers (app_mention + message) that
atomically SPOOL each raw event payload into
``~/.assistant/connectors/slack/spool/``. This Python connector is the consumer:
each poll it reads the spool, normalizes each raw event via the pure
slack_event_to_event(), atomically drops the WorldEvent into the inbox, archives
the raw payload, and removes the spooled file. The emoji→TODO reactor and the
never-postMessage rule are UNTOUCHED — this connector is a pure producer and,
like every connector, a grep CI test proves it issues no chat.post / send call.

not_configured: the Slack app is "wired" iff its bot token is present in the
environment (the spawn-sh sources ~/.zprofile exactly like slack-reactor). When
it is absent the connector is a QUIET not_configured — the owner has not
connected Slack. The wiring check is dependency-injected so tests never need a
real token (mirroring github's token provider / jira's PAT provider).

external_id ``slack:<channel>:<event_ts>`` — the message ts is unique per
channel, so a re-spool of the same event dedups to one WorldEvent downstream.

Cursor = the last processed ``event_ts`` watermark, advanced ONLY through the
contiguous successfully-emitted prefix: a transient drop failure on one spooled
event stops the batch (leaving that file + everything after for the next poll),
while a poison payload is skip-and-counted (removed so it cannot wedge the queue)
and surfaced in the heartbeat.

Stdlib only. No HTTP, no LLM — the connector never calls Slack; the Bolt app
feeds it via the spool.
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

SOURCE = "slack"
NAME = "slack"
# The bot token env var the Bolt app requires — its presence means Slack is
# wired. We NEVER read its value here (no secret handling); presence is enough.
ENV_WIRED = "SLACK_BOT_TOKEN"


def slack_is_wired() -> bool:
    """Default wiring check: is the Slack Bolt app configured? True iff its bot
    token is present in the environment (sourced from ~/.zprofile by the
    launcher). Injected away in unit tests."""
    return bool(os.environ.get(ENV_WIRED, "").strip())


def _archive_permalink(channel: str, ts: str) -> str:
    """Best-effort Slack deep link when the Bolt handler did not capture a
    permalink: https://slack.com/archives/<channel>/p<ts-without-dot>."""
    if not channel or not ts:
        return ""
    p = "p" + str(ts).replace(".", "")
    return f"https://slack.com/archives/{channel}/{p}"


def slack_event_to_event(payload: dict) -> dict:
    """One spooled Slack event → one WorldEvent. Pure function (the replay
    fixtures test it directly).

    kind is derived MECHANICALLY from Slack's own event metadata, never a lane
    judgment: an ``app_mention`` type → ``app_mention``; a ``message`` whose
    ``channel_type`` is ``im`` → ``dm``; any other channel message → ``message``
    (channel-noise). Policies lane them (app_mention/dm→escalate,
    message→digest)."""
    ev = payload.get("event") if isinstance(payload.get("event"), dict) \
        else payload
    etype = str(ev.get("type") or "")
    channel_type = str(ev.get("channel_type") or "")
    channel = str(ev.get("channel") or ev.get("channel_id") or "?")
    ts = str(ev.get("ts") or ev.get("event_ts") or "")
    user = str(ev.get("user") or ev.get("username") or "")
    text = ev.get("text") or ""

    if etype == "app_mention":
        kind = "app_mention"
    elif channel_type == "im":
        kind = "dm"
    else:
        kind = "message"

    ts_epoch = None
    try:
        ts_epoch = float(ts) if ts else None
    except (TypeError, ValueError):
        ts_epoch = None
    if ts_epoch is None:
        ts_epoch = connector.time.time()

    title = _title_from_text(text) or f"Slack {kind}"
    url = payload.get("permalink") or ev.get("permalink") \
        or _archive_permalink(channel, ts)
    refs = {"channel": channel, "slack_ts": ts}
    thread = ev.get("thread_ts")
    if thread:
        refs["thread_ts"] = str(thread)
    if user:
        refs["slack_user"] = user
    return connector.build_world_event(
        source=SOURCE,
        kind=kind,
        external_id=f"slack:{channel}:{ts}",
        ts_epoch=ts_epoch,
        actor=user,
        title=title,
        snippet=text,
        url=url,
        refs=refs,
    )


def _title_from_text(text) -> str:
    s = " ".join(str(text or "").split())
    return (s[:78] + "…") if len(s) > 80 else s


class SlackEventsConnector(connector.Connector):
    def __init__(self, *, wired_check=None, **kw):
        super().__init__(NAME, SOURCE, **kw)
        self._wired_check = wired_check or slack_is_wired

    def spool_dir(self) -> Path:
        return self.dir() / "spool"

    def _heartbeat_not_configured(self, now) -> None:
        self.write_heartbeat(
            last_poll_epoch=now,
            extra={"status": connector.STATE_NOT_CONFIGURED})

    def _spooled_files(self):
        d = self.spool_dir()
        if not d.is_dir():
            return []
        # Sort by event_ts embedded in the filename when present, else by name —
        # a stable ascending order so the contiguous-prefix watermark is
        # meaningful. Ignore in-flight tmp files.
        files = [p for p in d.iterdir()
                 if p.is_file() and p.suffix == ".json"
                 and not p.name.startswith(".")]
        return sorted(files, key=lambda p: p.name)

    def poll_once(self, now=None) -> dict:
        now = now if now is not None else connector.time.time()

        # Slack is OPTIONAL. If the Bolt app isn't wired there is nothing to
        # consume — a clean opted-out state, exactly like Gmail/GitHub/JIRA.
        if not self._wired_check():
            self._heartbeat_not_configured(now)
            return {"status": "not_configured", "emitted": 0, "errors": []}

        cursor = self.load_cursor()
        errors: list = []
        cap = int(self.config.get("max_events_per_poll",
                                  connector.DEFAULT_MAX_EVENTS_PER_POLL))
        emitted = 0
        malformed = 0
        last_ts = cursor.get("watermark_ts") or ""
        truncated = False

        for path in self._spooled_files():
            if emitted >= cap:
                truncated = True
                break
            try:
                payload = connector.json.loads(path.read_text())
            except (OSError, ValueError) as e:
                # Unreadable/corrupt spool file — poison. Remove it so it cannot
                # wedge the queue, count it, surface it. (Not a transient error.)
                malformed += 1
                errors.append(f"spool {path.name}: unreadable: {str(e)[:100]}")
                self._consume(path)
                continue
            try:
                event = slack_event_to_event(payload)
            except Exception as e:  # noqa: BLE001 — poison payload: skip+count
                malformed += 1
                errors.append(f"spool {path.name}: malformed: {str(e)[:100]}")
                self._consume(path)
                continue
            try:
                self.emit(event, raw=payload, now=now)
            except OSError as e:
                # Transient drop failure — STOP (leave this file + the rest for
                # the next poll) so the watermark never advances past an
                # un-emitted event.
                errors.append(f"emit {path.name}: {str(e)[:100]}")
                truncated = True
                break
            # Only remove the spool file AFTER a successful drop (at-least-once:
            # a crash between drop and unlink re-drops, the spine dedups).
            self._consume(path)
            emitted += 1
            ext_ts = str((event.get("refs") or {}).get("slack_ts") or "")
            if ext_ts:
                last_ts = ext_ts

        new_cursor = dict(cursor)
        new_cursor["watermark_ts"] = last_ts
        new_cursor["poll_count"] = cursor.get("poll_count", 0) + 1
        new_cursor["last_emitted"] = emitted
        self.save_cursor(new_cursor)
        self.write_heartbeat(last_poll_epoch=now, event_count=emitted,
                             poll_count=new_cursor["poll_count"],
                             errors=errors or None,
                             extra={"status": "error" if errors else "ok"})
        return {"status": "ok", "emitted": emitted, "errors": errors,
                "malformed": malformed, "truncated": truncated}

    def _consume(self, path: Path) -> None:
        """Remove a processed spool file. In --dry-run this is a NO-OP so the
        read-only inspection is side-effect-free (D1): a dry run must never
        destroy the very events it inspects — the base's emit() already skips the
        drop/archive under dry_run, and this guards the spool unlink too."""
        if self.dry_run:
            return
        try:
            path.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="normalize + print, do NOT drop or consume the spool")
    ap.add_argument("--record", action="store_true",
                    help="also write sanitized {raw,expected} replay fixtures")
    ap.add_argument("--once", action="store_true",
                    help="one poll then exit (default: KeepAlive loop)")
    args = ap.parse_args(argv)

    c = SlackEventsConnector(dry_run=args.dry_run, record=args.record,
                             log=lambda m: print(m, file=sys.stderr))

    if args.once or args.dry_run:
        result = c.poll_once()
        print(connector.json.dumps(result), file=sys.stderr)
        return 0

    if not c._wired_check():
        print("Slack not configured — wire the Slack app (SLACK_BOT_TOKEN in "
              "~/.zprofile) (daemon will keep re-checking)", file=sys.stderr)
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
