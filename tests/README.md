# Assistant unit tests

Pure-Python tests for the mechanical (non-LLM) parts of the Assistant
pipeline. The default suite uses local fixtures; optional memory integration
tests require a separate environment.

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

## Check new code before review

| Check | Required evidence |
| --- | --- |
| Regression tests | The test fails with the original bug or a targeted broken version. |
| Test suite | Targeted tests pass, then the full applicable suite passes. |
| New-code coverage | Every changed executable Python line and branch is covered. |
| Browser coverage | Changed dashboard JavaScript has complete line and block coverage from a real browser. |
| Independent reviews | Two reviewers inspect the full change and tests; resolve blocking findings before proceeding. |
| Runtime behavior | Exercise the real path and inspect the result before recommending a merge. |

Run from the repository root. Keep installer tests isolated from the host's
services; a temporary `HOME` alone doesn't isolate `launchctl`.

```bash
REPORT="$HOME/dev/generated-docs/assistant-coverage"
mkdir -p "$REPORT"
uv run --with pytest --with pytest-cov --with coverage python -m pytest tests/ -q \
  --cov="$PWD/bin" --cov="$PWD/src" --cov-config=tests/coverage.ini \
  --cov-report="json:$REPORT/python.json"
npm --prefix tests ci --no-audit --no-fund
uv run --with playwright python tests/drive_dashboard_overview.py \
  --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --output-dir "$REPORT/browser" --coverage-dir "$REPORT/browser"
node tests/browser_coverage.cjs "$REPORT/browser/browser-v8.json" "$REPORT/browser/istanbul.json"
python3 tests/check_change_coverage.py --base main \
  --python-report "$REPORT/python.json" \
  --browser-report "$REPORT/browser/istanbul.json" \
  --output "$REPORT/changed-code.json"
uv run --with pytest python tests/mutation_smoke.py --output "$REPORT/mutations.json"
```

The last command fails below 100%. It also rejects missing reports, excluded
new code, and browser reports from a different script version. Python coverage
of an HTML string doesn't count as JavaScript coverage. Layout and CSS still
need browser measurements and visual inspection.

Coverage isn't proof of correctness. Use the two reviews to challenge the
diagnosis, side effects, missing cases, and whether tests catch the actual bug.
Keep both verdicts and the measured results with the exact commit under review.

The mutation check uses separate temporary copies. It breaks four protections
one at a time and requires the corresponding test to fail; it never edits your
working copy or sends commands to live sessions.

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
