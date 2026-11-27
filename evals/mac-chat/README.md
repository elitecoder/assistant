# Mac chat eval

Asks Assistant for Mac the operator's real Slack questions on several model setups, so you can compare accuracy and
cost before changing the default model, effort, or instructions.

```bash
python3 evals/mac-chat/run.py mac/.build/release/AssistantBar haiku haiku-high sonnet
```

Questions and reference answers live in `~/.assistant/evals/mac-chat/refs.json`, outside the repo, because they quote
private conversations. Grade each answer 0 to 3 against the reference facts, and status answers against
`AssistantBar --print`. Grades from the run that set the defaults are in `~/.assistant/evals/mac-chat/grades.md`.

Results on 12 questions, November 2026 (of 36):

| Setup | Score | Status (of 15) | Lookups (of 21) | Cost |
|---|---|---|---|---|
| Haiku | 19 | 10 | 9 | $0.15 |
| Haiku, high effort | 25 | 11 | 14 | $0.31 |
| Sonnet | 21 | 8 | 13 | $1.77 |
| Haiku, high effort, current instructions | 24 | 15 | 9 | $0.13 |
