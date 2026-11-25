# Assistant unit tests

Pure-Python tests for the mechanical (non-LLM) parts of the Assistant
pipeline. The default suite uses local fixtures; optional memory integration
tests require a separate environment.

## Run

```bash
cd ~/dev/assistant
uv run --with pytest --with pytest-cov --with coverage --with playwright==1.63.0 python -m pytest tests -q
```

Or one file at a time:

```bash
uv run --with pytest python -m pytest tests/test_purge_stale_awaiting.py -q
uv run --with pytest python -m pytest tests/test_no_close_workspace.py -q
```

**Run tests with `pytest`, not `unittest`.** `tests/conftest.py` moves `HOME` to a throwaway folder before
anything is imported, because many scripts under `bin/` work out the home folder once, at import. `python3 -m
unittest` doesn't load `conftest.py`, so it gets no such protection and uses your real home. A test that needs a
home of its own still sets one with `monkeypatch`. The protection covers `pytest` only: a one-off script, or a
server you start by hand, uses whatever `HOME` you give it, so give it a temporary one. There is no switch to
turn this off. To debug against your real setup, copy `~/.assistant` into a scratch folder and point the code at
the copy.

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
| Browser coverage | Changed dashboard and Work queue page JavaScript has complete line and block coverage from a real browser. |
| Independent reviews | Two reviewers inspect the full change and tests; resolve blocking findings before proceeding. |
| Runtime behavior | Exercise the real path and inspect the result before recommending a merge. |

Run from the repository root. Keep installer tests isolated from the host's
services; a temporary `HOME` alone doesn't isolate `launchctl`.

```bash
REPORT="$HOME/dev/generated-docs/assistant-coverage"
mkdir -p "$REPORT"
npm --prefix tests ci --no-audit --no-fund
WQ_BROWSER_COVERAGE="$REPORT/work-queue/browser-v8.json" \
  uv run --with pytest --with pytest-cov --with coverage --with playwright==1.63.0 \
  python tests/run_python_coverage.py --output-dir "$REPORT" --pytest-args -q
node tests/browser_coverage.cjs "$REPORT/work-queue/browser-v8.json" "$REPORT/work-queue/istanbul.json"
uv run --with playwright python tests/drive_dashboard_overview.py \
  --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --output-dir "$REPORT/browser" --coverage-dir "$REPORT/browser"
node tests/browser_coverage.cjs "$REPORT/browser/browser-v8.json" "$REPORT/browser/istanbul.json"
node --test --experimental-test-coverage --test-reporter=lcov \
  --test-reporter-destination="$REPORT/reactor.lcov" slack-reactor/test/*.test.js
uv run --with coverage python tests/check_change_coverage.py --base main \
  --python-report "$REPORT/python.json" \
  --reactor-lcov "$REPORT/reactor.lcov" \
  --browser-report "$REPORT/browser/istanbul.json" \
  --work-queue-browser-report "$REPORT/work-queue/istanbul.json" \
  --output "$REPORT/changed-code.json"
uv run --with pytest python tests/mutation_smoke.py --output "$REPORT/mutations.json"
```

The coverage command fails below 100%. It also rejects missing or stale reports,
excluded new code, and browser reports from a different script version. Source
fingerprints are captured before and after the test run; changed source invalidates
the measurement. Changed Python continuation lines map to their executable
statement, rather than disappearing from the count. Python coverage
of an HTML string doesn't count as JavaScript coverage. Layout and CSS still
need browser measurements and visual inspection.

The slack reactor's Node source (`slack-reactor/src/*.js`) is measured by Node's
own runner, and `--reactor-lcov` hands the report to the gate, which checks every
changed line and branch and rejects a report older than the source. It needs
Node 22 or newer, and the output folder must already exist:

```bash
REPORT="$HOME/dev/generated-docs/assistant-coverage"
mkdir -p "$REPORT"
node --test --experimental-test-coverage --test-reporter=lcov \
  --test-reporter-destination="$REPORT/reactor.lcov" slack-reactor/test/*.test.js
```

The current coverage adapters cover Python under `bin/`, `hooks/`, `install/`,
`src/`, and `work_queue/`, the dashboard's embedded JavaScript, and the Work
queue page's script (`JS` in `work_queue/page.py`). With `$WQ_BROWSER_COVERAGE`
set, `test_work_queue_browser.py` records that script's coverage in Chromium
during the Python coverage run. `tests/coverage.ini` traces greenlets, because Playwright's
synchronous API runs your code in them, so the coverage run needs Playwright
(which brings `greenlet`) installed. Changes to other executable code fail until
an adapter is added; they aren't silently treated as covered.

The checked-in workflow runs these checks for pull requests and pushes to
`main`. To prevent merges that bypass them, require **Tests and 100% new-code
coverage** in your branch protection settings. This change doesn't edit those
repository settings or automate the two independent reviews.

Coverage isn't proof of correctness. Use the two reviews to challenge the
diagnosis, side effects, missing cases, and whether tests catch the actual bug.
Keep both verdicts and the measured results with the exact commit under review.

The mutation check uses separate temporary copies. It breaks each protection
in `CHECKS` one at a time and requires the corresponding test to fail; it never edits your
working copy or sends commands to live sessions. `--shard i/n` runs every n-th
check from the i-th; the workflow runs shards 1/4 through 4/4 in parallel Linux jobs, four checks at a time in each
(`--workers 4`), and `test_mutation_shards.py` proves the shards together run each check exactly once. Checks whose
test needs a Mac (`MAC_ONLY_TESTS` in `tests/mutation_smoke.py`: tests that save through the Slack task-list lock,
which takes a macOS-only flag) are left out of the shards off a Mac; the coverage job, on macOS, runs them with
`--mac-only`.

**Add the checks for a change in a file of its own**, `tests/mutation_checks/<what-the-change-is>.py`, which
defines `CHECKS` as a tuple of `(name, file, original, broken, test)` entries. The harness adds every file in that
folder to its list, in file-name order. Two changes in progress then never edit the same lines of the harness
(they still share the changelog); when every change appended to the one list in `tests/mutation_smoke.py`, each
pull request conflicted with the next, and one merge joined two entries into one.

- **A check that weakens a guard runs with the guard really weakened.** If the guard is what keeps a test away
  from something real (your home folder, a live session, a service), the test the check names must put a
  stand-in in that thing's place, so the broken version can only reach the stand-in. On 2026-11-24 a check that
  loosened the installer tests' home guard wrote three files into a developer's real home.
- Don't add new entries to the list in `tests/mutation_smoke.py`, and don't move old ones. When you change a line
  an existing entry quotes, edit that entry where it lives, whether in that list or in a file in the folder.
- Every `.py` file in the folder must define `CHECKS`; helpers go elsewhere. A file whose name starts with `.` or
  `_` isn't read.
- A list of one check needs a comma after it: `CHECKS = ((...),)`.
- An empty `broken` deletes the original.

Try the checks you just wrote before you push. Each takes from a second to about half a minute, depending on its
test. The text must match the name's capital letters:

```bash
uv run --with pytest python tests/mutation_smoke.py --name "part of your check's name" --output "$REPORT/mine.json"
```

The harness refuses a list it can't trust as soon as it loads: an entry that isn't five strings, one with no name,
file, original or test, one whose `broken` equals its original, and two entries with one name. For a mistake in a
file in the folder it names the file and the entry within it. A name used twice in different files is reported by
name only, so search for it. `test_mutation_shards.py` also checks, in seconds, that every entry's snippet is in its file
exactly once and that pytest collects its test, so a stale entry doesn't wait for a shard to fail. It doesn't run
the mutations; `--name` and the shards do.

## What's covered

| File | What it tests |
|---|---|
| `test_purge_stale_awaiting.py` | `bin/purge-stale-awaiting.py` drop predicates: closed workspaces, done TODOs, cmux-down safety. |
| `test_build_ws_context.py` | `bin/build-ws-context.py` mechanical signals: `agent_status`, transcript-path resolution, no PR data leaks into output, protected-workspace flag. |
| `test_no_close_workspace.py` | Regression-pin: no production code path shells out `cmux close-workspace`. |
| `test_work_queue_*.py` | The Work queue: server routes and apply, each source against fake `gh`, `gog`, `npx`, and `uv`, `sources.json` driving the refresh, the setup CLI, the installer in its sandbox, and the Pro mode gate. |
| `test_work_queue_triage.py` | The Work queue's model triage with the model faked at its seam: the row schema, the injection fence and secret scrub, the cache, Start here and marks, Since last night counts, budget, deadline, and failure notes, the default mode gate, and the real call against a fake `claude` in the temporary `HOME`. |

The `wq` fixture in `work_queue_fakes.py` replaces triage's model call with one
that fails, and clears every variable that could route a call to a real model.
A test that needs answers passes the fake `Model` from `work_queue_fakes.py` to
`refresh.run(model=...)` or `triage.run`. A test of the real call puts
`fake_claude` in the temporary `HOME`, which answers every kind of prompt and
logs each call.

The Work queue's Slack and page checks drive a real Chromium. They skip unless
Playwright and its Chromium are installed, so run the suite with
`--with playwright==1.63.0` (the version the coverage workflow pins) and run
`python -m playwright install chromium` once to include them.

## What's NOT covered here

The Observer's verdict logic — that's the LLM half and lives in
`evals/observer/`. Run that suite separately when you change the
Observer prompt, the ruleset, or any field in `build-ws-context.py`'s
output.
