# Assistant unit tests

Pure-Python tests for the mechanical (non-LLM) parts of the Assistant
pipeline. No Claude calls. Fast — full suite < 5s.

## Run

```bash
cd ~/dev/assistant
python3 -m unittest discover tests -v
```

Or one suite at a time:

```bash
python3 -m unittest tests.test_purge_stale_awaiting -v
python3 -m unittest tests.test_build_ws_context -v
python3 -m unittest tests.test_no_close_workspace -v
```

## Dashboard browser checks

Run the overview against an isolated local server and fixture data:

```bash
uv run --with playwright python tests/drive_dashboard_overview.py \
  --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --output-dir /tmp/assistant-overview-browser
```

The driver measures layout at phone, tablet, and desktop widths. It exercises
search, context expansion, refresh, older-work reminders, and outdated-data gates.
Workspace focus requests are intercepted; the driver never switches a real session.
The server-side identity guard has separate coverage in `test_todo_server.py`.

## What's covered

| File | What it tests |
|---|---|
| `test_purge_stale_awaiting.py` | `bin/purge-stale-awaiting.py` drop predicates: closed workspaces, done TODOs, cmux-down safety. |
| `test_build_ws_context.py` | `bin/build-ws-context.py` mechanical signals: `agent_status`, transcript-path resolution, no PR data leaks into output, protected-workspace flag. |
| `test_no_close_workspace.py` | Regression-pin: no production code path shells out `cmux close-workspace`. |

## What's NOT covered here

The Observer's verdict logic — that's the LLM half and lives in
`evals/observer/`. Run that suite separately when you change the
Observer prompt, the ruleset, or any field in `build-ws-context.py`'s
output.
