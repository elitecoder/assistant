"""slack_focus — the Slack focus board: which Slack threads need your action.

Ported from the standalone focus-board tool. The pipeline is read-only and
mostly mechanical:

  collect  → pull your mentions, DMs, and watched channels; bucket every thread
  triage   → map buckets to lanes, resolve threads whose linked PRs or tickets
             closed, pick out your own promises
  judge    → one bounded model call over the surfaced candidates only
  board    → the view the dashboard's Slack tab renders

Your settings live in the `slack_focus` block of ~/.assistant/comms/config.json.
The Slack user token comes from $SLACK_USER_TOKEN, never from config.
"""
