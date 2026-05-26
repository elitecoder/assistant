# 08 — active workspace → active

Rule B4 (default). `last_turn_age_sec=45`, `agent_status=working` (a
tool_use is in flight, the e2e run command). Agent is mid-work,
nothing for the user to do.

Expected: `active`. No actions taken, no awaiting card emitted.
