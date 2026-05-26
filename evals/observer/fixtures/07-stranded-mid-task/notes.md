# 07 — stranded mid-task → stranded with nudge_text

Rule B3. `last_turn_age_sec=2400` (40 min idle), `agent_status=idle`,
last assistant text is "Moving to spec 5" — clearly mid-task, not a
recap. The agent paused without finishing.

Expected: `stranded` with a `nudge_text` grounded in the transcript.
A reasonable nudge_text would be something like "Please continue with
spec 5 (transform-handle-rotate)."

The runner only checks that `nudge_text` is non-empty — content is
LLM-generated and we accept any reasonable continuation phrase.
