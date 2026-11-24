# Changelog

All notable changes to the Assistant daemon are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is carried in `pyproject.toml` and `src/assistant/__init__.py`
(`__version__`); keep the two in sync when bumping.

## [Unreleased]

### Security
- Check every request that changes something on the dashboard server the same
  way, with no exceptions. Before, a web page open in your browser could make
  your machine start a TODO's agent session (`/dispatch-now`), type a
  proposal's text into a session (`/proposal/<id>/run-now`), switch to a
  workspace, turn a task's automatic start on or off, remove a task, or add a
  note to one, because those six routes skipped the cross-site check. A page
  sandboxed inside any website could also reach the routes that did check,
  because a missing or `null` origin was let through, and any website could
  show the dashboard in a frame and act through it. Now:
  - The dashboard answers only at `127.0.0.1` or `localhost`, on any port, so
    a forwarded port works and a page that points another name at your
    machine is refused.
  - A change from the dashboard page you opened is let through, under either
    `127.0.0.1` or `localhost` on the same port. One from any other page is
    refused, including a page on `localhost` at another port.
  - The dashboard page can't be shown in a frame, read, or embedded by another
    site.
  - Anything with no origin, such as the dashboard opened as a local file or a
    command-line call, has to send the token in `~/.assistant/api-token` as
    the `X-Assistant-Token` header, and is refused with a sentence saying so
    otherwise. The `/goal` skill sends it for you; to do it by hand, put the
    header on standard input (`curl -H @-`) so it stays off the command line.
    The token is created the first time it's needed. Only you can read it
    and the saved dashboard page that now holds it.
  - If you forward the dashboard's port through another local server or
    tunnel, every page that address serves is treated as the dashboard's own
    page. The saved page holds your token and always talks to
    `http://127.0.0.1:9876`, so don't share it.
  - This stops web pages open in your browser. It does not stop other
    programs running as you on this Mac, which can read the token file and
    the saved page.
  - A dashboard tab you left open before this update keeps working. Opened as
    a local file, only **Refresh now** and the signal that you looked at the
    Notifications tab reach the server, and they now say when they are
    refused.
  - Two page rebuilds at once no longer share one temporary file, so neither
    can crash or garble the page, and a problem with the token file no longer
    stops the page from being built.

### Added
- Show a failure that keeps repeating as one red line at the top of the
  dashboard, with how many times in a row it has happened, since when, a short
  error, and what happens next, all in one box that shows two lines and keeps
  the rest under "and N more that keep repeating". It covers self-update,
  background model calls, and the page refresh after an edit. Sessions flagged
  as ready to merge with no pull request linked share one line, which clears
  when each has a pull request, is no longer ready, or closes. The self-update
  line gives a command you can paste, with the Python the services use. A background job that fails the same
  way three times in a row stops calling the model, and so stops spending,
  for an hour, then two, up to 24 hours. Self-update pauses for 4 hours at
  most. It, and the model jobs that know their model beforehand, try again at
  once if the remote, branch, model, or provider changes. Anything you
  ask for, such as a Work queue or Slack tab refresh, is never paused. A pause
  ends by itself, and the line clears when the next attempt works. To clear a
  pause sooner, run `self_update.py --force`, or delete
  `~/.assistant/failures.json`, which forgets every count. If the page itself
  can't be rebuilt, it can't show the line until a rebuild works. The file
  holds short error samples, with tokens, keys, and passwords removed, that can
  include a branch name or a model id. Counts stay out of the actions ledger.
- Record every Work queue Gmail change (**Archive**, **Mark read**, moving a
  conversation back to your inbox, and Gmail Trash from **Apply approvals**)
  and every unsubscribe in a new append-only record, before it runs and again
  when it ends, in `~/.assistant/effects/effects.jsonl`. An action Assistant
  can't record doesn't run, and the page says "Assistant couldn't record this
  action, so it didn't run it. Check that your disk isn't full, then try
  again". A second press while the first is still working is turned down. An
  unsubscribe that went through is never sent again, and neither is one that
  was sent and never answered: the row says "Assistant sent this but didn't
  hear back, so it may have gone through. Approve it again to send it once
  more". An **Undo** that doesn't happen, because the record refused it or
  Gmail did, leaves the row in Trash and says why on the row and on the
  last-run line, and the next **Apply approvals** tries it again. The record
  holds ids only, never a subject, a sender, message text or an unsubscribe
  link, and only you can read it. See `docs/effects.md`.
- Record every Slack Later save the Work queue marks completed or reopens in
  the same record, before it acts and again when it ends. That covers a row you
  send to an agent and **Apply approvals**, **Undo** included. A save the record
  can't write, or turns down, is left out of that run and the rest still go
  through. A reopen the record refuses leaves the save done and its row says
  why ("Undo not done: ..."); the next **Apply approvals** tries it again. A save
  counts as done when Slack accepted the change, and as verified when your Later
  list, read again, shows it. When Slack answers with an error, the save is
  recorded as not done. When the browser run stopped after it started, so a save
  may have changed, it is recorded as unknown, never as not done. The record
  holds only the channel id, the message time and a short code for the row,
  never the message, its link or a name. See `docs/effects.md`.
- Try a Slack save again by itself when marking it done after you send its row
  to an agent didn't work (Slack was busy, answered with an error, needed you to
  sign in, or the record turned it down). A sent row leaves the page, so before
  this the save stayed open in Slack with no sign. Now it is tried again after 1,
  5 and 15 minutes, and a restart keeps the saves still waiting. If the last try
  fails, one line under the page title says how many saves couldn't be marked,
  why, and what to do ("Mark it done in Slack yourself", or sign in again). The
  line goes away when the save is marked or Slack no longer has it.
- Clear finished work from the Work queue's **Ready for you** box on each
  refresh. A done item clears when every pull request its result names is
  merged or closed, or when you commented on or reviewed one of them, or
  posted in its Slack thread, after it finished. Clearing writes the **Done**
  mark with the reason, **Show cleared** lists the last 7 days' cleared items
  with **Undo**, and an item you bring back never clears again. An item with
  an open pull request says what would clear it. A blocked item never clears;
  when its reason names a GitHub repository it couldn't reach and `gh` can
  read it now, it says the blocker looks gone. A failed check leaves the item
  showing and never stops the refresh. `/api/state` adds `checks`. The server
  and the refresh now change `marks.json` under one file lock, so a mark one
  of them writes is never lost to the other.
- Give each Work queue row a short action title from the model, such as
  "Review Ana's pull request #42 on the parser", and use it as the headline
  on the Open list and Start here, with the row's own text under it. A row
  without a usable title shows its own text as before, and **Send to Agent**
  still sends the row's own text.
- Group the Work queue's Open list by the model's lanes: **Do first**,
  **Promise**, **This week**, and **Optional**, in that order, each row naming
  its source. Rows the model didn't sort follow under **Not sorted yet**,
  grouped by source. The triage cache is asked again once, since the prompt
  changed.
- Turn the Observer off with `{"observer": {"enabled": false}}` in
  `~/.assistant/config.json`. The pulse then sends no workspace to it and
  each workspace keeps its last verdict.
- Show finished agent work in a **Ready for you** box at the top of the Work
  queue's Start here tab, or of the Open list when there's no Start here. Each
  TODO you sent from the page that's done or blocked shows its title, id, and
  result or blocked reason, with **Done** to hide it. `/api/state` now lists
  each sent row's TODO under `queued`: id, title, status, result, reason,
  finish time, and workspace. The page reads it again every 20 seconds while
  a sent TODO is still open or still listed, without redrawing a note you're
  typing. A blocked Start here item shows **Sent td-NNN**, turned off.
- Add **Open workspace** to each Ready for you item: `POST /api/focus` brings
  the TODO's cmux workspace to the front and selects it. It refuses a closed
  workspace and a workspace ref that cmux reused for another workspace, whose
  title doesn't start with the TODO's id.
- Merge the Work queue's Outlook, Gmail, and Unsubscribe tabs into one
  **Email** tab with a collapsible part for each. A part with email that
  needs you starts open, and old links and saved tabs land on the right part.
- Add email actions to every Outlook and Gmail row on the Email tab: **Send
  to Agent** and **Snooze**, and on Gmail, **Archive** while it's in the
  inbox and **Mark read** while it's unread. `POST /api/gmail` changes the
  whole conversation, so mail that arrived since the last refresh changes
  too, then reads every message's labels back and reports success only when
  Gmail confirms it. **Show hidden email** lists hidden email with **Undo**,
  and Undo on an archived email moves it back to the inbox. Hiding keys on
  the email's own id, so it survives a refresh.
- Add a note box to Send to Agent on Open list rows, Start here items, and
  email rows. The note goes in the TODO above the untrusted row block,
  outside its markers, labeled as the sender's own instruction. Cmd+Enter or
  Ctrl+Enter sends and Esc cancels; a failed send keeps the note, and a row
  that was already sent says the note wasn't added.
- Serve files inside `~/dev/generated-docs` read-only at `GET /files/<path>`,
  so `file://` links in an agent's result open from the page. Nothing outside
  that folder is served, through `..` or a symbolic link either, Markdown is
  served as text, and each file is sandboxed. Links in a result keep a
  trailing period out, and only `http`, `https`, and `file` links are made.

### Fixed
- Some services could fail without a word because they ran under the system's
  older Python. They now use the same Python as the rest of Assistant.
- Trim oversized logs. Every five minutes the Assistant now checks its logs:
  the ones in `~/.assistant/logs`, the `.log` files in `~/.assistant`, the
  comms listener log, the workspace watcher log, and the to-do server's two
  output files. A log over 20 MB is cut to its last 5 MB; the older lines are
  deleted and there is no backup copy. This frees the space the repeated lines
  took; one log had reached 1.6 GB and another 870 MB. Services keep writing
  without a restart, and a line written during a trim can be lost. Nothing is
  deleted the day you update: when `~/.assistant/config.json` has no `logs`
  entry, the first time a log is found over the limit the Assistant only shows
  a card saying which logs it will trim and when, and waits 24 hours. To keep
  your logs, or to change the limits, add a `logs` entry alongside your
  existing keys; do not replace the file. For example,
  `{"mode": "pro", "logs": {"cap_mb": 0}}` turns trimming off, and
  `"cap_mb"` and `"keep_mb"` are in megabytes. Check the file with
  `python3 -m json.tool ~/.assistant/config.json`. A `logs` setting that can't
  be read, such as a number in quotes, turns trimming off and the log says why.
- Stop the workspace watcher from restarting its cmux event stream in a tight
  loop. When cmux refused the connection, the watcher started a new stream
  the instant the last one ended and logged every try. It now waits 2 seconds
  after a failed stream, doubling up to 30, and starts over after a stream
  that stayed up for a minute. It logs a repeated failure once, then once per
  wait step with the attempt count, and says when the stream recovers. cmux's
  own error text no longer goes to the launchd error log; the watcher logs its
  last line instead. The workspace numberer likewise logs a repeated cmux
  error once, then once every 5 minutes with a count, and says when cmux
  answers again.
- **If the Slack reactor ran any earlier version, rotate your Slack bot token
  now: its log file holds the old one.**

  1. In the Slack app's settings, under OAuth & Permissions, reinstall the app
     to get a new bot token.
  2. If the old log also shows an `xapp-` token, regenerate the app-level
     token under Basic Information, App-Level Tokens.
  3. Update `SLACK_BOT_TOKEN` (and `SLACK_APP_TOKEN`) in `~/.zprofile`.
  4. Restart the reactor with this command:
     `launchctl kickstart -k gui/$(id -u)/com.assistant.slack-reactor`
  5. Confirm the old token no longer works. Call `auth.test` with it, for
     example with this command, and expect `invalid_auth` or `token_revoked`:
     `curl -s -X POST -H "Authorization: Bearer <old token>" https://slack.com/api/auth.test`
  6. Delete or empty `~/.assistant/logs/slack-reactor.launchd.err`, and any
     copy of it in a backup.

  What changed: the Slack reactor's error log held the bot token again and
  again, because a failed Slack request printed its whole request, headers and
  body included. From now on the reactor replaces token-shaped text with
  `<redacted>` in what it prints and in its crash output. A crash writes one
  line with the reason, the network error underneath and a few stack lines,
  then exits so it restarts as before. The pulse, the comms listener, the cmux
  watcher, the workspace watcher and numberer, the world scanner, the to-do
  server, the connectors and the background service do the same for Slack,
  GitHub, Google, AWS and model keys, JWTs, `Bearer`, `Authorization` and
  cookie values, passwords and `token=` style fields, and an uncaught error in
  any of them prints its traceback the same way. This does not clean old lines
  in any log.

  The limits: this is pattern matching, not a guarantee. Scripts that only
  `print`, output written around the logger, and a token split across two
  writes are not covered.
- Stop a Work queue test from failing at random. It read the refresh log the instant the refresh
  reported it had finished, a moment before the last lines were written, and failed about one run in
  four. It now waits for the line it checks.
- Keep the limit of 5 busy sessions working with the Observer off. Assistant
  counted busy sessions from the Observer's checks, so with the Observer off,
  which is now the default, it counted none and could start up to 30 sessions.
  It now counts them itself from cmux and each session's transcript. **The 5
  includes sessions you are using yourself**, so a task set to start
  automatically, or a Work queue row you sent to an agent, now waits while 5
  sessions are busy. A session is busy when it had a turn in the last 10
  minutes or has a tool call open. A tool call still open after 2 hours is
  treated as a session waiting on you, not a busy one. A plain terminal and an
  agent that has exited aren't counted; a session whose transcript can't be
  read is. The limit is fixed at 5; there is no setting for it. The **Tasks**
  tab's "Dispatch capped" card now says in plain words which limit is holding
  things and names the busy sessions. When Assistant can't read the open
  workspaces or the sessions, it starts nothing that time and shows a
  "Dispatch paused" card, which clears by itself once it can.
- Stop the comms daemon from leaking duplicate `assistant-comms (warm)`
  workspaces. Its liveness check, close guard, and orphan scan listed
  workspaces with a bare `cmux list-workspaces`, which shows only the focused
  window. A live warm session in another window looked gone, so the daemon
  spawned a second one and skipped closing the first. All three now list every
  window (`list-windows`, then `list-workspaces --window` per window), and a
  window that fails to list makes the check unknown rather than gone. The
  close step now logs `already gone` for a session that really ended, instead
  of calling it "not a warm workspace (ref reissued?)".
- List pull requests from only the repositories you choose. Add `repos` to
  the `github` entry in `sources.json`, or run `bin/work-queue-setup.py link
  github --repo OWNER/NAME`, repeated for each repository. Without it, the
  Work queue still lists every pull request that asks for your review.
- Show today's Outlook calendar on the Work queue's Today tab when Outlook is
  linked, in the same rows as Google Calendar. It reads your local day
  through Work IQ, leaves out cancelled events, and marks the ones you
  declined. A failed read shows as its own **Outlook calendar** error, and the
  other sources still show. Set `"calendar": false` on the `outlook` entry to
  turn it off.
- Never count a Gmail read-back that failed as verified. Apply's Trash and
  restore checks treated a failed or unreadable `gog gmail get` as a message
  without labels, so a restore read back as verified.
- Leave hidden email out of **Approve all**, the approved count, the Outlook
  search, and **Apply approvals**: email you snoozed, archived, sent to an
  agent, or marked done or dropped.
- Answer a Work queue request that fails unexpectedly, for example on a TODO
  store that isn't valid JSON, with a JSON error instead of dropping it.
- Run each mutation check in a temporary folder whose path has no `__`. A
  random folder name with `__` in it failed the installer tests' baseline,
  which read it as a plist placeholder left unreplaced.
- Run the mutation checks in three CI shards instead of two, so each shard
  finishes within its 60-minute limit with this release's added checks.
- Keep the Work queue's sent-save worker running after a failure. An
  unexpected error in a batch, such as a malformed result or a job file that
  can't be written, stopped the background thread but left it marked as
  running, so later sent Slack saves waited until the server restarted. Now
  that batch records an error for each of its saves, the traceback goes to
  the log, and the worker goes on; a save sent during a run goes in the next
  run, and `queue_save` starts the worker outside its lock.
- Build the fake AWS access key IDs in the secret-scrubber test from two parts, so GitHub secret scanning no longer flags them. The test values are unchanged.

### Changed
- Keep each change's mutation checks in a file of its own under `tests/mutation_checks/`. Every pull request
  used to add its checks at the same place in one long list, so each conflicted with the next, and one merge
  joined two checks into one that nothing reported. The harness now also refuses a malformed or duplicated
  check when it loads, and a quick test finds a check whose quoted line or test has gone.
- An existing Pro install stops creating decisions, saving the morning brief,
  and planning for your goals after this update; to turn each back on, add
  `"jobs": {"decisions": true, "brief": true, "planner": true}` (just the ones
  you want) to `~/.assistant/config.json`, next to `"mode"`.
  Write `true` without quotes; it takes effect at Assistant's next check. Behind
  this is five months on the maintainer's machine: none of 49,193
  decisions was accepted, rejected, edited, or snoozed, 7 of 103 briefs were
  opened, and the planner acted twice and no goal recorded progress. With
  decisions off, new GitHub and session events are skipped, so turning them on
  later starts fresh, not from a backlog. Decisions that are already open still
  age out, staged ones after 72 hours and digest ones after 24, and urgent ones
  stay until you answer them and still show under **Awaiting your input**. The
  **Completed actions** and **Other updates** lists get no new entries while
  decisions are off. With the brief off, no brief is saved and the Notifications
  tab is a live summary; decisions no longer move to the digest for going
  unseen. With the planner off, nothing acts on your goals or records their
  progress, though they stay viewable and editable. The lesson extractor no
  longer proposes lessons from repeated successful actions unless you set
  `ASSISTANT_LESSON_LEDGER_PASS=1`; the part that finds corrections in your
  sessions is unchanged. The Notifications tab and **Settings and background
  services** say which of these are off, what to add to turn each on, and when
  a value you wrote is ignored. See
  [Configuration](docs/configuration.md#background-jobs-that-are-off-by-default).
- Make the mutation checks faster without dropping any. Each test's baseline now runs once per run, not
  once per check that names it (510 checks name 315 tests), each shard runs three checks at a time
  (`--workers 3`), and the workflow runs four shards, up from three. The slowest shard had reached 51 of
  its 60 minutes. Four shards plus the coverage job is five macOS jobs, the most that run at once here, so
  no job waits in a queue.
- The Observer is now off by default. It runs only when
  `~/.assistant/config.json` has `{"observer": {"enabled": true}}`, with the
  JSON value `true`, and the pulse has `ASSISTANT_WATCHER=1`. A missing file,
  unreadable JSON, a missing key, or any other value leaves it off. Existing
  installs lose the Observer and its audit after this update unless you add
  the setting. With it off, the pulse still dispatches autoDispatch TODOs,
  clears stale cards, and updates itself, and each workspace keeps its last
  verdict. `assistant-doctor.py`, the dashboard's **Settings and background
  services**, and the installer's summary say whether it's on and how to turn
  it on. See [Configuration](docs/configuration.md#observer).
- A lesson no longer needs a bolded trigger line above its rule.
  `assistant-curator.py write --rule R` stores the marker and the rule
  alone, and `list`, `rm`, `audit`, and the duplicate-slug check now see a
  block with no bold line, which they skipped before. `--trigger` still
  writes the bolded line, and existing blocks read as before. `list` shows a
  rule's first line when a lesson has no trigger. `propose_lesson`, the
  `propose_memory` agent tool, and the lesson extractor's drafts need only a
  rule, and the `/lesson` skill and the operating guide show the rule-only
  form. `write` keeps a blank line before the new lesson when a hand edit
  left the section without one, and a marker with no text under it is now
  listed and removable.
- Send a Work queue Start here item to an agent from the list itself. Each
  item that isn't blocked has Send to Agent next to Done, which sends its row
  for a first pass through the row's own send path, so the TODO, the done
  mark, the Slack save completion, and the removal of both the item and its
  row work exactly as for the row. A blocked item, whose row was already
  sent, has no Send button and stays on the list.
- Send any open Work queue row to an agent, alone or many at once. Send to
  Agent shows on every open row, not only the ones the model called `agent`;
  that call is now an "Agent can finish" hint. `POST /api/todo` takes a
  string `mode`, saved on the TODO as `workQueueMode`. `assist`, the default
  and the mode of a row's own button, asks for a first pass that leaves
  nothing other people can see except a branch of the agent's own and at most
  one draft PR, with the Slack replies, ticket text, and PR comments as
  drafts in its result and what's done and what's left. `handsoff` keeps the
  old finish-it-yourself instructions. Any other value is refused. The new
  Send to agents tab lists every open row across sources, "Agent can finish"
  ones first, with checkboxes, Select all, a Hands-off checkbox that's off by
  default, and a "Send N to agents" button whose confirm dialog names the
  mode; it sends one at a time, stays off until the batch is done, and lists
  any that failed. A sent row leaves the page at once and stays off it while
  its TODO exists, so the page no longer shows a sent row's Queued, Working,
  Blocked, or Done status: the TODO's status and result show on the Tasks
  tab of the Assistant dashboard, and its session on the Sessions tab once
  it starts. `/api/state` returns `sent` in place of `queued`. Start here
  marks a blocked TODO's row with a flag, not by its reason text.
- Tell a first-pass agent not to ship. When the pulse starts a TODO whose
  `workQueueMode` is `assist`, its prompt ends with first-pass limits, written
  to override the routing rules before them. They're instructions the
  dispatcher adds to the prompt, not a block: nothing outside the agent
  enforces them. They allow only the agent's own branch, local commits, a
  push of that branch, and at most one draft PR (`gh pr create --draft`)
  whose title and body @mention no one; no
  Jira, no Slack posts or reactions, no PR comments, approvals, or change
  requests, no ready-for-review, and no push to a branch it didn't create.
  The limits say to run `/architect-ffp:archffp` with `--no-ship` and to stop
  `/architect-ffp:archself` after its touch-set plan. Hands-off prompts are
  unchanged. A Work queue TODO's title and link appear in the prompt only
  inside its untrusted-row fence, and each TODO's fence markers carry a
  random code, so a row's text can't close the fence or come before the
  safety rules. A sent row's link is cut to one line of at most 1,000
  characters.
- Mark a sent Slack save completed in your Later list, in either mode, in the
  background with no browser window, and check that it left the list. The
  send never waits for it. One background worker marks every save sent so
  far in one browser run, and the result is saved in `applied.json` like an
  Apply result. A file lock keeps the worker, Apply, and the refresh from
  using the Slack browser profile at the same time; Apply and the refresh
  wait up to 90 seconds for it, then say to try again later. Apply now merges
  its results into `applied.json` instead of writing back the copy it read
  first. A save Slack no longer has (`saved_not_found`) is recorded as gone:
  neither a send nor Apply asks Slack about it again, and Apply counts it as
  settled, not approved. Any other Slack error stays an error, so Apply tries
  it again. Only a permalink on a `*.slack.com` host counts as a Slack save,
  and a saved `queued.json` or `applied.json` in the wrong shape never stops
  a send or the state the page reads.
- Raise the quality job's time limit from 20 to 45 minutes. The Work queue adds 55 mutation checks, and the mutation step alone now runs longer than 11 minutes on the CI runner.
- Raise the quality job's time limit from 45 to 60 minutes. Work queue triage and the source-link fixes add mutation checks (390 in all now, up from 130 before the Work queue), and CI measured 44:56 for the whole job at commit 963642d, with 34:29 of it in the mutation step.
- Run the mutation checks in two shards at once, in their own jobs. `tests/mutation_smoke.py --shard i/n` runs every n-th check from the i-th, so the shards together run every check exactly once, and each shard uploads its own evidence. A shard with no checks is refused rather than passing. The required check, **Tests and 100% new-code coverage**, is now a job that passes only when the tests, the coverage gate, and both shards passed; a skipped or cancelled job fails it. CI run 44676541835 took 18:48 in all, with 109 and 108 checks in the two shards, every mutant rejected. There are now 390 checks, up from 130 before the Work queue; sending rows to agents adds 62 of them, the sent-save worker fix adds 8, and Start here's Send to Agent adds 7.

### Added
- The Work queue, a Pro mode page at `http://127.0.0.1:8851/` that merges your
  Slack Later saves, pull requests that ask for your review, Outlook, Gmail,
  and today's calendar into one list you clear. It moved in from a standalone
  tool, with everything personal removed. See
  [docs/work-queue.md](docs/work-queue.md).
  - `work_queue/` holds the server, the refresh, the page, and one module per
    source in `work_queue/inputs/`. Paths come from `$HOME` and `$WQ_DATA`, and
    your sources come from `~/.assistant/work-queue/sources.json`, which holds
    no secrets.
  - Rules sort every row. The standalone tool's hand-written "start here" list,
    change notes, drafted replies, and per-row advice are gone; model triage
    replaces them (below).
  - Model triage, in Pro mode only (`work_queue/triage.py`). After the refresh
    writes the rule-based page, one batched call per 30 new or changed open
    rows (up to 120 a refresh) returns a one-line why, a disposition (`you`,
    `agent`, `handoff`, or `drop`), an effort, and, for rows that need a reply,
    a draft to copy. A second call ranks the 5 to 8 rows on **Start here**, and
    a third summarizes the **Since last night** box, whose new, gone, and
    changed counts per source come from code. Verdicts are cached in
    `$WQ_DATA/triage-cache.json` by a hash of each row's content, together
    with the rows they were asked about with: an unchanged row isn't sent
    again, and when a row changes or leaves, the rest of its call is asked
    again, so one row's text can't keep swaying its neighbors. A row the model
    doesn't answer about is shown without a verdict and asked again the next
    day, and the rest of triage goes on. With the Claude provider, the model
    is Sonnet 5.5, the `balanced` tier in `src/assistant/model_tiers.py`, and
    `"triage": {"model": ...}` in `sources.json` changes it; Droid, Copilot,
    and local models use their own model settings. `bin/assistant-llm.py set
    <provider> --feature work_queue` picks the provider. Each call is metered
    in the cost ledger as `caller: work_queue`, and reserves its estimated cost
    under the Strategist's lock against two daily budgets: all of Assistant's
    model spend (`dailyCostCeilingUsd`) and the Work queue's own
    (`triage.dailyUsd`, default 3.00 USD). Triage stops once spend has reached
    a budget; one call can still go over it. A model command that isn't
    installed books no cost, and a timeout stops every process the command
    started, within the call's own time limit.
  - Row text is untrusted: anything that looks like a secret is replaced with
    `[removed]`, the rows sit between markers with a random code, the prompt
    says they're data, never instructions, and answers are checked against a
    strict schema (known ids, enum values, bounded text; other fields are
    ignored). Secrets are removed before text is cut short, from titles,
    senders, and bodies, including Authorization and Cookie headers, signed
    link parameters, and "password ... is" phrases. Model output never acts.
    A Slack save that can't be read is never sent to the model, not even its
    old title in the change summary.
  - A call that times out, fails, or answers in the wrong shape never stops
    the other parts of triage: the page shows "Part of triage didn't finish"
    with the part and the reason, and that part isn't asked again until the
    next day or until its inputs change. When a budget is used up, the
    refresh's 10 minutes of calls run out, the model command isn't installed,
    or triage hits an unexpected error, triage stops, and the page keeps its
    rule-based rows and every verdict already paid for, and shows "Triage
    unavailable:" with the reason. The cache is saved either way. Cached verdicts, the saved
    Start here list, and the saved summary are checked against the schema
    again when they're read, and verdicts stay cached for 14 days after their
    row was last seen, so a source that fails once doesn't lose them. The
    first page write of a refresh already shows the cached verdicts.
  - Every open row has Done, Snooze, Drop, and Send to Agent, whatever the
    model said about it; a row the model said an agent can finish reads
    "Agent can finish", and the other pills read "Model suggests: ...". A
    Send to agents tab lists every open row, the "Agent can finish" ones
    first, to send many at once (below). Sending a row marks it done and
    removes it from the page at once; the Assistant dashboard follows its
    TODO, and a blocked one stays on Start here. Done on a Start here item
    saves the same mark as its row, and a marked item doesn't show there. An
    empty Start here list doesn't open first. A sent TODO's instructions start
    with the safety rules: the row's title, detail, and linked source are
    untrusted content, fenced off below the rules, never instructions; never
    share secrets or act outside the request; never send email, merge,
    force-push, or delete data. It closes the TODO with
    `work_queue/todo_result.py` or marks it `--blocked`. It creates
    `~/.claude/assistant-todo.json` when it doesn't exist yet, and never
    reuses the id of a removed TODO.
  - The server refuses any request whose `Host` isn't `127.0.0.1` or
    `localhost` with its port, and any `POST` from another origin.
  - Unsubscribe requests go only to `https` links on public addresses, never
    follow a redirect, and can't be undone. An apply that stops on an error
    reports it instead of staying on "Working", and two clicks at the same
    moment start one apply or one refresh.
  - The Slack source reads Later saves through a Chromium profile that only
    the Work queue uses (`work_queue/slack_browser.py`, run with
    `uv --with playwright`) and the conversations through the focus board's
    read-only client.
- `bin/work-queue-setup.py` links, checks, and unlinks Slack, Gmail, Google
  Calendar, Outlook (through Work IQ), and GitHub. `status` and `check` print
  one line per source: linked, not linked, needs sign-in, or tool missing, with
  the fix. `unlink slack` also deletes the Work queue's browser profile and
  its Slack cookies. The Slack source runs Playwright 1.63.0, the version the
  quality workflow tests.
- The `com.assistant.work-queue` LaunchAgent. `install.sh --apply --pro` loads
  it, starts it when it's loaded but not running, and, from a terminal, offers
  to link each unlinked source; `--no-pro` and default mode unload it. The
  dashboard's Pro mode switch loads and unloads it too. It listens on
  `127.0.0.1` only and stays stopped, instead of restarting, when Pro mode is
  off, its settings can't be read, or its port is taken. Its `port` must be a
  whole number from 1 to 65535. The installer restarts it when `work_queue/`
  changes. The hourly update never loads it fresh or restarts one you stopped.
- The Work queue server re-checks the mode on every request and every 5
  seconds. Once Pro mode is off, it answers `403` and exits cleanly.
- The Work queue server starts listening without a reverse DNS lookup of
  `127.0.0.1`. On GitHub's macOS runners, that lookup held the server's
  start past 30 seconds.
- A **Work queue** tab on the dashboard in Pro mode links to the page and says
  when it last refreshed. A bad Work queue setting shows on the tab instead of
  breaking the dashboard.
- The change-coverage gate measures `work_queue/`, and the Work queue page's
  script needs full line and block coverage from a real browser, like the
  dashboard's (`--work-queue-browser-report`). Python coverage now traces
  greenlets, so code that runs under Playwright's synchronous API is measured.
- `tests/mutation_smoke.py` counts a mutant whose test runs past 90 seconds as
  rejected and marks it `rejected (timed out)`. A baseline that runs that long
  stops the run with the check's name and test. Each check records its time.
  The private-address unsubscribe test refuses every outbound connection, so
  its mutant fails at once instead of waiting on two private addresses.

### Removed
- Stop running git from Assistant. The attention tab's preview server ran
  plain `git status --porcelain` in about a dozen checkouts every minute, and
  killed runs left stale `index.lock` files that blocked commits in other
  sessions. The attention tab's fix is on its own branch.
  - `build-ws-context.py` no longer runs `git status` or `git log @{u}..` in
    each workspace's folder. The `cwd_dirty` and `cwd_unpushed` fields are
    gone from the workspace state, the session check's fingerprint, and its
    prompt. The "no transcript, empty screen, stale" rule no longer needs a
    dirty folder, and `ready_for_cleanup` no longer checks for a clean one.
  - The session check's PR lookup reads the branch and repo from the
    checkout's `.git` files and asks `gh` by repo and branch, so `gh` no
    longer runs git in each workspace.
  - The stale-card purge looks up a PR only from a full PR link.
  - `meta_gate.py detect` takes changed files with `--paths` only; `--repo`
    and `--base` are gone.
  - `tests/test_no_git.py` fails when any Python, shell, JavaScript, Swift, or
    LaunchAgent file under `bin/`, `src/`, `hooks/`, `slack-reactor/`,
    `mac/Sources/`, `githooks/`, `launchagents/`, or `scripts/` runs git. It
    allows an exact count of git lines in the self-update and its commit
    guard (until their replacement lands), the doctor's git check, and the
    lessons tool's one `git add`.

### Changed
- `/cleanup` keeps the branch when it has commits its upstream lacks, or, with
  no upstream, commits on no remote.

### Fixed
- Make the Work queue's source-link messages accurate. Every status line is
  one line of at most 300 characters, with line breaks folded: a longer tool
  error keeps its start and up to the last 150 characters of its last line,
  which holds a traceback's error. A failed link during install no longer adds
  a second "fix the line above" warning. When gog reports missing OAuth client credentials, the
  fix says to run `gog auth credentials <client-secret.json>` before
  `gog auth add`, and a missing gog points to https://gogcli.sh. Linking Slack
  reports a missing `uv` before it asks for a token. The page shows the last
  refresh in the viewer's local time, and the Slack browser helper answers any
  unexpected error as one JSON line instead of a traceback.
- Make Work queue triage hold up against bad answers and bad files. Every
  model call now fails the same way: a timeout, a failed model command, or an
  answer in the wrong shape, such as a refusal, is a call failure that never
  stops the other parts, and only a used-up budget, the refresh's time limit,
  or a missing model command stops triage. A failed call about rows skips its
  rows for the day, so it isn't paid for again each refresh; a failed Start
  here list or summary is saved, empty, for the day, and the page names the
  part that failed. A row that makes the model time out no longer blocks the
  other rows, Start here, or the summary. After Start here and the summary, a
  failed call about rows is split in halves to find the rows that break it: at
  most 6 more calls, and only while the Work queue's budget covers the next
  one, so two bad rows in a small queue still leave the rest with verdicts. An
  answer about 2 or more rows with usable verdicts for fewer than half of them
  counts as the wrong shape, and so does an answer nested too deep to read,
  whose `RecursionError` got past the failure handling and stopped triage. A
  bad call that comes before a stop is still skipped for the day. Rows you
  marked done or dropped aren't triaged, and rows asked again after their day
  go in an order that changes each day, so splitting can isolate a few
  poisoned rows over several days instead of skipping most of a small queue
  each day. A failed call is booked at least at its reserved estimate, and a
  call that ran out of time as if it wrote 150 output tokens a second for its
  whole time limit. A reservation expects 220 output tokens a row, close to
  the measured call's 210. A saved cache, snapshot, or reservation file in the
  wrong shape is treated as empty, and the refresh still writes the page;
  a reservation dated in the future doesn't count. A blocked row leads Start
  here, even one the model called drop. Two refreshes at once keep each other's verdicts. The scrubber, now
  `work_queue/scrubber.py`, runs in linear time on any input (the tests time
  each pattern on 1 MB of repeated text with no cut, including repeated
  `eyJ`), and cuts text to 8,000 characters, and titles and senders to 1,000,
  first. It catches "password ... is: X", removing a password phrase to the
  end of its line, `pwd=`, passphrases, quoted values with spaces,
  `key: value` pairs whose key names a secret, `pw:` and `pass:` values,
  GitLab tokens, tokens in reset and verify links and in link parameters such
  as `t=`, one-time codes such as "Use code 482913 to verify", "Enter 482913
  to continue", and "code: 482-913", and card numbers that pass the Luhn
  check, and it leaves ordinary text such as "error code 1234", "Secret Santa
  is: Bob", "Tests pass: 4197", and Google Docs links alone. Slack titles show
  as written; only the model's copy is scrubbed, before it's cut, in the row
  calls and in the Since last night summary, and the names in a Slack thread
  are scrubbed and cut to 100 characters too. A reservation counts the model
  command's own prompt and the expected answer, scaled to match a real call's
  cost, and a daily budget that isn't a finite number of zero or more means the
  default, now 3.00 USD.
- Run the Work queue only on Python 3.11 or newer, the version
  `pyproject.toml` requires. `install.sh` used the first `python3` it found,
  which could be macOS's 3.9 at `/usr/bin/python3`, where the Work queue
  server can't import, and launchd restarted it every minute from login, even
  in default mode. `install.sh` now picks the first `python3` of 3.11 or newer
  from Homebrew, `/usr/local/bin`, then `/usr/bin`, or from the interpreters
  `ASSISTANT_PYTHON` lists, separated by colons. When there isn't one, the
  other services keep the `python3` found, and the Work queue's LaunchAgent
  isn't installed: one an earlier install left is stopped and removed, and
  the installer says how to fix it. The dashboard's Pro mode switch doesn't
  start the Work queue when its plist's `python3` is older than 3.11, doesn't
  run, or can't be read, and says why. On an older Python, `work_queue.server`
  exits cleanly with a message before it imports anything else. The quality
  workflow installs Python 3.11, so the tests that run on it run in CI.
- Keep the live install updating after a squash merge. The self-update refused
  to update while local main had commits GitHub's main lacked, even when a
  squash merge had already brought every change in. It now checks whether
  merging local main into GitHub's would change anything; if not, it moves
  main to GitHub's commit with `git reset --keep`. Real unsent work still
  blocks the update.
- Restart the always-on services after a pull that changes `bin/`. The comms
  listener, workspace watcher, Slack reactor, session-context watcher, and
  workspace numberer loaded their code once at start, so a merge left them on
  the old code until a crash or a reinstall. The self-update now runs
  `install.sh --apply` for any `bin/` or `slack-reactor/src/` change, and the installer restarts each
  of those services that's running when its code changed. Services you've
  stopped stay stopped.

### Added
- Pick the shell alias Claude sessions start with:
  `bin/assistant-llm.py claude-alias <name>` saves `llm.claude.alias` in
  `~/.assistant/comms/config.json`, and `claude-alias claude` restores the
  default. Task sessions, `/spawn-claude-workspace`, and the Slack session
  start the alias in the workspace's own shell, so cmux's Claude wrapper still
  records the session. When that shell doesn't define the alias, the session
  says so and starts with `claude`, and `assistant-doctor.py` warns. The
  command refuses a name that isn't one shell word, that's a shell keyword
  such as `if` or `exec`, that names Copilot or Droid, or that your login zsh
  doesn't know as an alias, function, or command. A hand-edited config with a
  name that isn't one word, a keyword, or another agent's name falls back to
  `claude`; one that names a shell builtin such as `echo` starts no session,
  and `assistant-doctor.py` warns. A live Slack session restarts on its next
  message after the alias changes, and it takes on the alias's own folders
  and permission mode; the Assistant's model choice still wins. Background model
  jobs still run the Claude program directly. A restore replays cmux's saved
  command, which skips the alias unless you declare the alias's script in
  cmux's `agents.launchers` setting.
- Refuse commits on the live install's main. The self-update links
  `githooks/refuse-main-commit` as the `pre-commit` and `pre-merge-commit`
  hooks, and never replaces a hook that's already there. Commit on a branch
  and merge through a PR.

### Changed
- The session check (`ASSISTANT_WATCHER=1`) now runs in default mode too. In
  default mode its results only add cards: it never merges, sends `/cleanup`,
  or nudges a session. The other opt-in jobs still need Pro mode.
- The session check now reads each conversation as plain text: the first typed
  request plus the latest turns, with attachments, hook output, and thinking
  removed. Background-task notices, messages from other sessions, command
  output, and conversation summaries are labeled SYSTEM, so they don't read
  as requests from you. It used to send a fixed 128 KB slice of the raw log,
  which was mostly clutter and sometimes held no conversation at all. Prompts
  are about a third of their old size.
- Session results on the Sessions tab now stay for up to 6 hours while the
  session doesn't change, instead of expiring after 10 minutes.

### Added
- `OBSERVER_EVERY_MIN` sets how many minutes pass before a changed session is
  checked again. The default, 0, checks on every run. A failed check, a new
  session, or a reused workspace number is always checked on the next run.
- `ASSISTANT_OBSERVER_HOLD_ACTIONS=1` keeps session-check results to cards in
  Pro mode too.
- Run any model job on a local OpenAI-compatible server (llama-server,
  Ollama, or LM Studio) with `assistant-llm.py set local`. Set the server with
  `llm.local.url` and `llm.local.model` in `~/.assistant/comms/config.json`,
  or with `ASSISTANT_LOCAL_LLM_URL` and `<JOB>_LOCAL_MODEL`.
- `evals/observer/replay.py` replays past Sonnet checks with the new
  conversation slice and scores other models against them.

### Added
- GitHub Copilot CLI is a third coding agent beside Claude Code and Factory
  Droid. Run `bin/assistant-llm.py set copilot`, and task sessions, the Slack
  reply session, resumes, `/spawn-claude-workspace`, and every background model
  job start Copilot instead of Claude. The dashboard and the session check read
  Copilot sessions too. Set the model with `llm.copilot.model` or
  `COPILOT_MODEL`.
- `assistant-llm.py set copilot` and the installer add cmux's Copilot hooks
  as `~/.copilot/hooks/cmux.json`, so Copilot sessions show on the dashboard
  and can be restored. `cmux hooks copilot install` can't do this itself: it
  edits `config.json`, which Copilot 1.0.109 manages and marks with comments.
  The hooks are added only when you pick Copilot, and restore relaunches
  Copilot sessions only then, so Copilot sessions you start by hand stay yours.
- The doctor checks that Copilot starts (`copilot --version`) and that its
  cmux hooks are in place whenever you pick Copilot for anything.

### Changed
- `bin/agent_session.py` is now the only place that starts a coding agent.
  `tests/test_agent_launch_seam.py` fails when any Python, shell, JavaScript,
  launchd, workflow, or skill file builds a `claude`, `copilot`, or `droid`
  command line. It reads code structure, so comments and prose don't count.
- Background jobs run Copilot's standalone build instead of its npm loader,
  which needs Node and fails under launchd. Copilot jobs run in their own
  folder, and Copilot's errors reach the failure log.
- Resuming a Copilot session keeps the flags it started with; it no longer
  adds `--allow-all` to sessions you started without it.
- Assistant answers Copilot's folder-trust question with "Yes, and remember
  this folder", so a restored session doesn't stop at the question again.
- The spending ledger reads Copilot's token counts. Copilot jobs used to be
  booked as failed with a cost estimated from their much longer output.
- Copilot gets only the extra folders that exist; it refuses to start when
  one is missing.
- Assistant reads Copilot sessions from `$COPILOT_HOME` when you set it.
- The Slack reply session on Copilot clears when Copilot reports half its
  context used, not when its log reaches 2 MB.
- A Claude pane that shows Copilot's banner or status bar in its output is
  no longer taken for a Copilot pane, and a busy Copilot pane is recognized.
- FFP to-dos still go to Claude when you pick Copilot, because they run the
  `/architect-ffp:archffp` Claude Code plugin. A Copilot session is told to
  stop and say so if a to-do turns out to be FFP work. Droid dispatch is
  unchanged.
- The dashboard's latest-reply text and the merge flow's send check read
  Copilot conversations.
- Background Copilot jobs run at the configured thinking effort instead of
  Copilot's saved default.
- `assistant-llm.py set <agent>` names any job still pinned to another
  provider, and `--feature comms` picks the agent for the Slack session alone.
- A working Copilot pane is recognized on every spinner frame and while it
  retries.
- Copilot's `✗ Execution failed` banner and `session.error` events count as a
  stopped session; the dashboard shows a Copilot tool call as working; inbox
  snippets drop Copilot's footer; the watcher titles a Copilot session by its
  own name.
- Interactive Copilot sessions start with `--no-ask-user`, so questions come
  as text Assistant can read, not a form.
- A running Slack session moves to the newly picked agent on the next
  message after `assistant-llm.py set <agent>`, and a Copilot one restarts
  when its model changes. A picked Copilot that can't start runs as Claude;
  `set copilot` warns when that's the case.
- Background Copilot jobs retry once without `--reasoning-effort` when the
  model refuses it on stderr (Copilot's `auto` model does), and the
  Observer's excerpt of a Copilot session shows tool results and errors,
  compaction summaries, notices, and aborts.
- Copilot subagent turns and automatic messages (memory, skills, discovered
  instructions) no longer read as the user's own turns in the Observer
  excerpt, the lesson extractor, or any other transcript reader.
- `evals/observer/replay.py --provider copilot` runs the candidate models on
  Copilot.
- `llm.copilot.reasoning_effort` sets Copilot's thinking effort on background
  jobs; without it Copilot keeps using the shared Droid setting.
- A Copilot tool you stop with Esc, or that was running when the session
  ended, no longer keeps the session marked as working; the dashboard finds
  Copilot's last reply behind long runs of its internal records. `assistant-comms-setup.sh` installs cmux's
  Copilot hooks when Copilot is picked, and the doctor skips the Claude
  sign-in check when the Slack session runs another agent.
- Copilot sessions Assistant starts, restores, or brings back after a crash
  get your rules from `~/.claude/CLAUDE.md` through
  `~/.assistant/copilot-instructions`, linked as a Copilot instructions file;
  your own Copilot setup isn't touched. A session cmux itself restores on app
  restart replays cmux's saved command, which doesn't carry them.
- The notification watcher reads cmux's `cmux-feed-v1:` session ids, so a
  ping again carries the agent's last message for Claude and Copilot (the
  format change in cmux had broken this since October).
- Spending and Slack-session context reads go through `bin/agent_session.py`,
  and the launch check also flags Claude-only transcript and usage parsing.
- Task sessions now answer the first-launch folder-trust question whenever it
  appears during boot, not only in one check 2 seconds after launch. Copilot
  shows it about 4 seconds in, so the old check missed it and the session sat
  waiting.
- `/spawn-claude-workspace` takes its launch command, readiness check, trust
  prompt, and submission check from `bin/agent_session.py`. It still defaults
  to Droid, takes `engine=copilot`, stops with a message when the engine isn't
  installed, and checks only conversations written since the spawn.
- The session check (`ASSISTANT_WATCHER=1`) now runs in default mode too.
  The other opt-in jobs still need Pro mode.
- The session check now reads each conversation as plain text: the first
  request plus the latest turns, with attachments, hook output, and thinking
  removed. It used to send a fixed 128 KB slice of the raw log, which was
  mostly clutter and sometimes held no conversation at all. Prompts are about
  a third of their old size.
- Session results on the Sessions tab now stay for up to 6 hours while the
  session doesn't change, instead of expiring after 10 minutes.

### Added
- `OBSERVER_EVERY_MIN` sets how many minutes pass before a changed session is
  checked again. The default, 0, checks on every run.
- `evals/observer/replay.py` replays past Sonnet checks with the new
  conversation slice and scores other models against them.

### Removed
- Remove the dollars-per-day boxes from the Brief tab and the background-work
  stats. The model-heavy background jobs that drove that cost are off.

### Changed
- The Brief tab now rebuilds from your current notifications, completed
  actions, and service status every time the dashboard updates, instead of
  showing the copy saved at 7 AM. A **Refresh now** button rebuilds it on the
  spot. If the live build fails, the tab falls back to the saved copy.
- Turn off the daily Sonnet call that writes the morning brief's wording. Its
  summary never showed on the dashboard, and its advice showed only when the
  live queue couldn't be read. The Brief tab still builds without a model. Set
  `ASSISTANT_BRIEF_NARRATOR=1` to turn it back on.
- Turn off the watcher (the Observer) by default, along with its Opus
  double-check. It sent 40–75K tokens per check, about 870 checks a day. The
  dashboard's **Waiting on you** tab covers its view without a model. Set
  `ASSISTANT_WATCHER=1` to turn it back on.
- Turn off the lessons extractor and the strategist by default. Their output
  went unread: 689 lesson proposals sat pending, and the strategist's notes
  covered 9 items 119 times. Set `ASSISTANT_LESSON_EXTRACTOR=1` or
  `ASSISTANT_STRATEGIST=1` to turn either back on. With the strategist off, the
  goals planner uses its built-in step templates.

### Fixed
- Keep idle sessions on the Sessions tab. When a transcript's timestamp changed
  without new text, the session context watcher never noticed, so the session
  stayed under Sessions not checked yet. On one Mac, 19 of 26 sessions were
  stuck there. The watcher now rereads a transcript when its timestamp changes.

### Added
- Add Pro mode. Default mode keeps the Sessions, Session details, Slack, and
  Tasks tabs, Slack replies, Slack emoji to task, auto-started tasks, and the
  `/todo`, `/cleanup`, and `/spawn-claude-workspace` skills. Pro mode adds
  notifications and triage, goals and the planner, the Notifications,
  Decisions, Background work, and Connected apps tabs, and the `/goal`,
  `/attend`, `/back-off`, and `/lesson` skills. The strategist, lessons
  extractor, and brief narrator also need Pro mode, and connectors
  pause before their next check while it's off. Alerts that arrive while Pro
  mode is off are skipped, not saved for later. Switch modes in the dashboard's
  **Settings and background services**, or with `./install.sh --apply --pro`
  and `--no-pro`. The mode is saved as `"mode"` in `~/.assistant/config.json`.
  If that file can't be read, Assistant uses default mode and the dashboard
  shows the error next to the switch.
- **Upgrading:** every existing install stays in Pro mode, because earlier
  versions installed the Pro skills for everyone. Default mode is for new
  installs and for anyone who switches. An automatic update never removes
  skills. When the code of the dashboard server or a connector changes, an
  update restarts the ones that are running, so the switch works right away.
  If a restart fails, the dashboard says so and the hourly update retries.
- Install workspace numbers by default. The installer no longer asks and keeps
  a remembered "no". An automatic update never starts them if they aren't
  running. Install with `--no-ws-numberer` to turn them off and stop a running
  copy.

### Removed
- Remove the dollars-per-day boxes from the Brief tab and the background-work
  stats. The model-heavy background jobs that drove that cost are off.

### Changed
- The Brief tab now rebuilds from your current notifications, completed
  actions, and service status every time the dashboard updates, instead of
  showing the copy saved at 7 AM. A **Refresh now** button rebuilds it on the
  spot. If the live build fails, the tab falls back to the saved copy.
- Turn off the daily Sonnet call that writes the morning brief's wording. Its
  summary never showed on the dashboard, and its advice showed only when the
  live queue couldn't be read. The Brief tab still builds without a model. Set
  `ASSISTANT_BRIEF_NARRATOR=1` to turn it back on.
- Turn off the watcher (the Observer) by default, along with its Opus
  double-check. It sent 40–75K tokens per check, about 870 checks a day. The
  dashboard's **Waiting on you** tab covers its view without a model. Set
  `ASSISTANT_WATCHER=1` to turn it back on.
- Turn off the lessons extractor and the strategist by default. Their output
  went unread: 689 lesson proposals sat pending, and the strategist's notes
  covered 9 items 119 times. Set `ASSISTANT_LESSON_EXTRACTOR=1` or
  `ASSISTANT_STRATEGIST=1` to turn either back on. With the strategist off, the
  goals planner uses its built-in step templates.

### Fixed
- Keep idle sessions on the Sessions tab. When a transcript's timestamp changed
  without new text, the session context watcher never noticed, so the session
  stayed under Sessions not checked yet. On one Mac, 19 of 26 sessions were
  stuck there. The watcher now rereads a transcript when its timestamp changes.

### Added
- Add a **Slack** tab to the dashboard, ported from the standalone focus-board
  tool. It lists the Slack threads where the next move is yours, in Do first,
  Promise, This week, and Optional, plus Done by you, Just read, Set aside, and
  Auto-resolved.
  - **Refresh from Slack** POSTs `/slack-focus/refresh` to `todo-server.py`,
    which starts `bin/slack-focus.py` in the background under zsh with
    `~/.zprofile` sourced, so `$SLACK_USER_TOKEN` and `gh` resolve under
    launchd. The page polls `/slack-focus/status` and shows each step. A second
    click while a refresh runs starts nothing.
  - **Done** and **Undo** POST `/slack-focus/done` and `/slack-focus/undone`.
    A checked-off row comes back when its thread gets a new reply or edit.
  - The pipeline lives in `src/assistant/slack_focus/`. Collect and triage are
    rules only; the judge is one model call over the surfaced candidates,
    tools off, metered as `caller="slack_focus"`, with verdicts cached by
    content. If the model fails, the raw rows show. It uses the balanced
    model tier with one attempt; the focus-board ran your `claude` alias and
    retried once. `bin/assistant-llm.py` routes it as the `slack_judge`
    feature.
  - The Slack client refuses any method outside a read allowlist before a
    request leaves the machine, and a test checks that no module names a
    common Slack write method.
  - Everything about the user is in the new `slack_focus` block of
    `~/.assistant/comms/config.json`: watched channels, bot names, skipped
    channel names, GitHub repos and hosts, and Jira projects. The Slack ID,
    name, and workspace come from the token. Jira project keys and GitHub
    hosts used to be hard-coded.
  - Files live in `~/.assistant/slack-focus/`.
  - Each row stores its thread's latest message, so a check-off keeps its
    key while a new pull renumbers threads. The key formula matches the
    focus-board's, so its `done.json` carries over.
  - A refresh counts as running only while it holds `refresh.lock`, so a
    crash or a reused process ID can't leave the button stuck.
  - Any Slack error other than a deleted or private thread or channel stops
    the refresh with that error; `ratelimited` answers are retried. The
    focus-board carried on with a partial pull. A refresh stops after 20
    minutes.
  - The Slack routes refuse `Origin: null`, which the other routes allow
    for file:// pages.
  - Times on the tab show in local time.
  - Promises made in DMs read "DM" or "group DM" instead of a channel ID.
  - Jira uses a bearer token unless `email_env` names the env var with your
    account email; the focus-board always looked for `$JIRA_EMAIL`.
  - A new reply in a DM thread that started before the window now shows up.
    The focus-board dropped it.
  - A message counts as answered only when you posted after it. The
    focus-board also counted a message of yours from up to 60 seconds
    before, which hid quick DM asks.
  - A direct @-mention of you outranks an earlier group mention in the same
    thread.
  - DMs and threads are read to the end. The focus-board stopped at 200
    messages.
  - A model answer of `[]` sets every new row aside as noise. The
    focus-board treated it as a failure and showed raw rows.
  - A promise the model moves to Do first ranks below direct asks instead
    of above every row.
  - A refresh error never includes the Slack token.
  - If a refresh launch hangs before it starts, the next **Refresh from
    Slack** stops it and starts a new one.
  - The unused low-signal hints are gone.
  - A `config.json` that isn't valid JSON shows that on the tab, with the
    line, instead of the setup steps next to the last list.
  - "Last pulled" shows when Slack was last pulled, also after a
    `--triage` run.
- A row's text now quotes the other person's latest message, not yours. The
  focus-board compared author names with the label "you", which never
  matched a real name.

### Changed
- Read agent sessions from cmux first, with mukul-memory's registry as a
  fallback. cmux 0.64.25 records agent sessions itself, and
  `cmux sessions list --json --all` prints those records without the cmux
  socket. The new `bin/cmux_sessions.py` is the one place that runs it. It
  waits up to 15 seconds, and when cmux can't answer it returns no records and
  writes one stderr line. `world-scanner.py`, `session-context-watcher.py`,
  `build-ws-context.py`, `transcript-tail.py --closed-cwd`, and
  `cmux-restore-sessions` read it instead of `~/.cmux-session-ledger.jsonl`.
  - cmux misses a session whose SessionStart never reached it. On 2026-10-19
    that was ee4ee641 in workspace:27. For such sessions every reader also
    takes the row from `~/.claude/cmux-registry.json`, written by mukul-memory's
    `cmux-registry.py` hook.
  - `world.json` `_meta.session_record_fallback` counts the sessions that came
    from that fallback, and the scanner log shows `fallback_hits=N`. Retire the
    fallback after a soak period with zero hits.
  - A registry row with a live pid must still be a `claude` or `droid` process.
    This drops sessions like aea1111b, whose recorded pid now belongs to another
    program.
  - On live state the scanner verifies the same 10 sessions as before. Only
    the aea1111b ghost drops.
  - Only the session cmux marks active on a surface counts as running there, so
    a session left behind by `/clear` in the same process no longer looks live.
  - Output fields keep their names, and `registry_live_pid` and
    `session_start_registry` keep their values. Some values now come from
    different sources:
    - **First recorded** comes from cmux's `started_at`, and from a registry
      row's SessionStart time for fallback sessions. A session restored into
      another workspace shows its original start, not when it arrived there.
    - A session's `cwd` is its launch directory, and `ts` is the record's
      latest update.
    - `transcript-tail.py --closed-cwd` picks the most recently active session.
    - Sessions of agents the fleet doesn't run, such as Copilot, are left out.
- Make `hooks/cmux-auto-resume.py` forward reliably. It's what makes cmux
  record agents started without cmux's Claude wrapper, such as an absolute
  `~/.local/bin/claude` like the warm comms session, and the scheduled fleet
  spawns.
  - The hook now starts a detached worker and returns at once.
  - The worker checks each `cmux hooks <provider> session-start` exit code.
    It then confirms the record with `cmux sessions list --session <id>`.
  - It retries after 0, 2, 5, 15, and 30 seconds.
  - A session cmux still lacks gets one `cmux-session-forward-fail` entry in
    `~/.assistant/actions-ledger.jsonl`.
  - Before, one try was made, with a 5-second timeout and its errors ignored.
- `install/patch-settings.py` registers `cmux-auto-resume.py` on SessionStart in
  Claude settings and Factory hooks when it's missing. A fresh machine used to
  link the hook without registering it. The file is written without escaping
  non-ASCII text.
- `cmux-restore-sessions` finds a session for a panel with no resume binding in
  cmux's records, then in the registry. The ledger is no longer used. Native
  `cmux restore --surface` needs a binding, so this CLI stays.
- Measure Python under `install/` and `hooks/` in the new-code coverage gate,
  so changes there can pass it.

### Removed
- Retire `hooks/cmux-session-ledger.py`. For this release it's a no-op, so a
  registration that outlives `git pull` still exits 0.
  `install/patch-settings.py` now removes its SessionStart and SessionEnd
  registrations from `~/.claude/settings.json` and `~/.factory/hooks.json`
  instead of adding them. Delete the stub and its install.sh link next release.
  `~/.cmux-session-ledger.jsonl` stays on disk, but nothing writes to it any
  more. mukul-memory's `cmux-registry.py` registration is left alone.

### Rollback
- Revert the merge commit, then run `install.sh --apply`. That restores the
  readers on registry and ledger, `cmux-restore-sessions` on the ledger, and
  the single fire-and-forget forward. The base `patch-settings.py` also
  re-registers the ledger hooks on SessionStart and SessionEnd, and the ledger
  file resumes from where it stopped.
- The `cmux-auto-resume.py` registration this release added on a fresh machine
  stays, because the base installer never removes hooks. It's harmless: the
  base hook makes the same single forward.
- Ledger events between deploy and rollback are lost, so **First recorded**
  for sessions started in that window is unknown until they restart.

### Added
- Add the `cmux-ws-numberer` LaunchAgent as an opt-in feature. It adds a `[N]`
  suffix to cmux workspace titles and gives each workspace an unused sidebar
  color. Enable it with `install.sh --apply --with-ws-numberer`, answer the
  prompt, or use `--with-all`. Its label is now `com.assistant.cmux-ws-numberer`;
  the installer removes the old `com.mukuls.cmux-ws-numberer` plist. Its log
  moves to `~/.assistant/logs/cmux-ws-numberer.log`, which it creates if
  missing, and rotates at 5 MB with two old copies.
- Let changes to `install.sh` pass the new-code coverage gate when a targeted
  mutation proves them. No line-coverage tool can measure it safely here.
  Each changed hunk that holds code needs a check in `tests/mutation_smoke.py`
  that breaks one of its lines. The gate fails on any unproven hunk, and the
  mutation step fails unless a test rejects each break and the broken script
  still parses. A check proves a hunk only when every line it breaks is changed
  code in that hunk. One check is enough for a whole hunk, so add more for
  large hunks. Review new checks like any test, because a check written to
  dodge the gate can still pass it. Hunks that only
  delete lines need no check. Other shell scripts still fail the gate.
- Check every self-update before it lands: read each runtime file the fetched
  commits add or change straight from git, scan it for leftover merge conflict
  markers, and parse it if it's Python. If a file fails, refuse the update
  before anything is stashed or merged, and record the failure on the
  dashboard. Skip that commit quietly until the remote moves, with a reminder
  once a day. Updates now fast-forward to exactly the commit that was checked.
- Run the pulse through a pre-flight (`bin/run-pulse.py`) that parses `pulse.py`
  and the modules it loads at startup before each start. If one won't parse,
  the pre-flight skips the run and exits cleanly instead of crashing. The
  dashboard shows the error at the top of the page until a pulse runs again, and
  an actions-ledger entry reaches Slack for a new error, then once a day. New installs
  get this right away. Existing machines pick it up after a reboot, a logout, or
  a manual reload of the pulse LaunchAgent, since self-update defers that reload.
- Enforce 100% changed-code coverage with separate Python and real-browser reports.
  Add missing failure-path tests and repeatable mutation checks for key protections.
  Bind reports to measured sources and correctly map multiline Python and JavaScript changes.
- Show **Go to session** on collapsed cards so you can focus a workspace
  without opening its details. Existing identity and freshness checks still apply.
- Allow manual close-out review for plan-only work when its exact saved document
  still matches the reviewed hash; keep implementation explicitly separate.
- Preserve actionable transcript context and outstanding question choices.
  Display reviewed, conversation-bound return notes with specific next steps.
- Group live GitHub alerts into review topics, show three first, and keep
  individual alerts available in history. Surface stale saved requests separately.
- Add approval-bound backlog cleanup with record fingerprints, backups, and a
  private audit receipt that cannot trigger outgoing notification broadcasts.
- Add an attention overview with compact task cards, expandable return notes,
  and reminders to finish older pending work before starting another task.

### Changed
- Move the `balanced` model tier from Sonnet 4.6 to Sonnet 5.5 on Bedrock, the
  Anthropic API, and Vertex. The warm Slack session, the Observer, the brief
  narrator, and the strategist use this tier. Restart
  `com.assistant.assistant-comms` to pick it up; the restarted daemon replaces
  a warm session on the old model before its next Slack message. The Observer
  and fleet evals now default to this tier's model, and their qualification
  hashes include `model_tiers.py`. Cost estimates price Sonnet 5 models at $2
  and $10 per million input and output tokens.

### Fixed
- Bring the warm Slack session up again on Claude Code 2.1.303 and cmux 0.65.
  Claude Code now writes several hundred KB of records right after each prompt,
  so the check that read only the last 256 KB of the transcript never saw the
  boot prompt. Every spawn was abandoned, and cmux 0.65 refused to close the
  abandoned workspace, so each retry left another one open. The check now
  searches the whole transcript, and the daemon retries the close with
  `--force` when cmux asks to confirm, still only for its own warm workspace.
- Stop the dashboard from crashing on sessions with no working folder. cmux
  0.65 lists a `restore_record` before the `resume_binding`, so
  `world-scanner.py` read the folder from a record that names it
  `working_directory` and saved no folder for those sessions. The renderer then
  failed on every run. The scanner now reads either name, and the open-sessions
  list shows a session even when its folder is unknown.
- Stop the Slack daemon from following a headless run's copy of a Slack
  message. The proofgate check's `claude -p` run records the warm session's
  prompts as queued prompts with no entrypoint, so the daemon could bind to
  that transcript and watch it for the reply. A queued prompt now counts only
  in a transcript whose first entrypoint is interactive.
- Match each terminal to its session through the `resume_binding`, which agent
  hooks keep current, before the `restore_record` that cmux 0.65 lists first.
  The restore record is a snapshot from when cmux restored the surface, so
  `world-scanner.py` and `build-ws-context.py` could name an old session.
- Stop two time-of-day test failures. The Observer audit tests failed in the
  last minute of a two-hour window, and five Strategist tests that count calls
  per UTC day failed in the last minutes of the day. Each test now starts its clock at the
  beginning of its window, so calls a few minutes later stay in the same one.
  The audit tests also set their own window length.
- Keep a Strategist test from writing a row to the real
  `~/.assistant/actions-ledger.jsonl`. It now runs with a temporary home.
- Name workspaces in Slack by the same number the sidebar shows. Workspace
  pings and action lines named the workspace by its `workspace:N` ref, which no
  longer matches the `[N]` in the title. `comms-listen` now looks up sidebar
  positions once per pass that sends something, and the footer shows `[4]`
  instead of `workspace:1000010013`. Only titles that already end in that
  `[N]` count, so a machine without `cmux-ws-numberer` keeps showing refs.
  Ledger keys stay raw so they still match `actions-ledger.jsonl`, and
  `conversation.jsonl` keeps the refs. The warm comms prompt now uses the
  sidebar number and resolves it to a ref right before acting.
- Number workspaces by sidebar position. cmux 0.65 numbers workspace refs from
  1000010000, so `cmux-ws-numberer` was adding suffixes like `[1000010009]`.
  The suffix is now the workspace's 0-based position in its window, the number
  `--workspace <index>` accepts. It follows reorders within one poll. Renames
  and colors still target the stable ref, and a cmux that reports no index
  gets colors without a suffix. The `spawn-claude-workspace` skill now says the
  bracket number isn't the ref.
- Stop the pulse tests from self-updating the checkout they run in. The tests
  that run the pulse's `main()` called the real self-update, so a checkout
  behind its remote got pulled. When the pull touched installer files, it also
  ran `install.sh --apply` with the test's temporary home. That reloaded this
  machine's daemons from a scratch copy. It also failed a push-to-`main` CI run
  whenever `main` moved before the run finished. The test loaders in
  `test_pulse.py` and `test_observer_skip.py` now stub it out, and a
  `conftest.py` guard fails any test that still reaches it.
- Keep a killed git from blocking self-updates. The pulse's timeout used to
  SIGKILL a context check mid-`git status`, leaving `.git/index.lock` behind;
  on 2026-09-28 that blocked every self-update, and seven repos on this machine
  held such locks. Timeouts now send SIGTERM first (git removes its locks) and
  still SIGKILL the group after 3 seconds, the background `git status` and
  `git log` calls take no index lock at all, and self-update removes an
  index.lock older than 10 minutes when no process has it open and no git
  process is working in the repo. Each removal is recorded in the ledger.
- Ignore Claude Code's `.claude/worktrees/` and the stray `.claude/.assistant/`
  marker, which made the tree look dirty and set off daily auto-stashes.
- Ping about an idle workspace only when it needs you: the agent's last lines
  ask you something, a permission or plan prompt is up, or the session stopped
  on an API error. Claude's idle alert fires about a minute after every turn,
  and about 75% of recent turns ended with a status update instead.
- Match the watcher's patterns ("CI red", "PR opened", "tests passed") against
  what the agent said this turn, not 50 lines of scrollback, so old tool output
  no longer triggers them. Name workspaces cmux left as "Terminal" by Claude's own
  session title, or the folder and branch.
- Mute the "stranded", "awaiting-review", and "emit-card" word lists by default
  (168 pings in two weeks, mostly on status prose), and stop a muted pattern
  from hiding a real one in the same message.
- Stop closing a healthy warm comms session whenever cmux is slow to answer.
  A refused or timed-out check now leaves the session alone; only cmux saying
  the workspace doesn't exist, or a successful workspace list without it,
  counts as gone. Short blips get two more looks a few seconds apart.
- Submit prompts to the warm session reliably. The daemon now presses Enter by
  writing a carriage return, because `surface.send_key enter` can leave the
  prompt typed but unsent on a workspace that was never shown. Every boot
  prompt and Slack message is confirmed in the transcript, with up to two more
  Enter presses when the text is still in the prompt box. A session whose boot
  prompt never lands is closed instead of being declared ready, and the session
  is bound to the transcript that recorded its prompt, never the newest file.
- Keep inbound Slack messages until the warm session confirms it received them.
  They're recorded on arrival, retried every 30 seconds for up to 3 hours, sent
  together once the session is back, and you get one "I'll answer as soon as
  it's back" note per outage. Before, a message that arrived while no session
  was up was dropped for good.
- Cut the automatic Slack feed. The heartbeat pages once per outage and posts
  once when it recovers, instead of every 30 minutes. Housekeeping ledger
  entries stay in the brief, and a ledger pass posts at most 5 updates plus one
  summary line. A workspace gets at most one ping per 15 minutes unless it asks
  a real question.
- Write Slack posts in plain language: what happened first, with the workspace
  title and the agent's own question or last message, and refs in a trailing
  line. Screen snippets drop Claude's spinner, prompt, and status lines, and the
  on-disk pattern bank inherits the built-in "CI green" mute it was missing.
- Point tests at a cmux binary that doesn't exist, so a test that misses a stub
  can't drive the real cmux.
- Pin the clock in the review-topic focus test so it stops failing once its fixture alert is more than 4 days old.
- Download browser-check dependencies publicly so CI doesn't require Adobe's internal network.
- Invalidate return notes after completed tool traffic; require review before reusing older notes.
- Block close-out for unverified terminals, and safely show incomplete question choices.
- Keep installer tests away from real terminal hooks, and count isolated Python
  subprocesses in coverage. Update regression checks for the new observation
  metadata and plain-language dashboard labels.
- Rewrite dashboard labels, status messages, and instructions in plain language
  using Google's documentation style. Preserve original questions and technical
  records in expanded details, and add a wording check for every session state.
- Resolve full session filenames directly instead of scanning every transcript
  filename for prefix matches on each collector pass.
- Keep ordinary updates and unverified sessions out of the human decision queue.
  Preserve response endings instead of dropping next steps after 800 characters.
- Separate cmux workspace counts from GitHub notifications. Sessions stay the
  default view, notification counts stay inside their inbox, and expired
  workspace counts show as unverified.
- Show snapshot age independently from page age. Suppress completion suggestions
  when data is outdated, and preserve expanded context while you read.
- Bind context and workspace-opening requests to stable identities. Distinguish
  failed observations from evidence that work is progressing.
- Track outstanding tool calls across long transcripts without nudging active
  sessions to close. Preserve separate live bindings when sessions are resumed twice.
- Isolate installer tests from host service registrations and terminal hooks.

## [0.7.4] - 2026-09-16

### Fixed
- **Warm comms session respawns on a backend toggle instead of freezing its
  spawn-time model id**: the warm session is kept alive by ref across daemon
  restarts, so its `--model` id was fixed at spawn. After a
  `claude-backend bedrock|sub` switch, a session spawned on Bedrock kept running
  `us.anthropic.claude-sonnet-4-6[1m]` even though the box moved to direct
  Anthropic (observed 2026-09-16 — a stale Bedrock sonnet id on a non-Bedrock
  box). `spawn_session` now records the resolved model id in `session.json`, and
  `ensure_warm_session` respawns the session when that id no longer matches the
  current backend's resolution. A pre-upgrade session with no recorded id
  respawns once onto the tracked id; a droid session (no such id) is unaffected.

## [0.7.3] - 2026-09-16

### Fixed
- **Warm comms session no longer leaks cmux workspaces until cmux dies**: the
  warm-session watchdog (`bin/comms-listen.py`) spawned a fresh cmux workspace
  every ~minute when `claude` never reached its ready marker, and
  `spawn_session` (`bin/comms_session.py`) returned without closing the failed
  workspace. On 2026-09-14 that leaked ~200 orphaned workspaces (refs climbing
  to 340) and killed cmux. `spawn_session` now closes the specific workspace it
  created on every post-creation failure path (no ref, no surface, never ready,
  and the new-workspace-timeout partial ref), and `watchdog_loop` backs off
  exponentially (60s doubling to 30m, resetting on recovery) so a persistent
  boot failure can't hammer cmux.
- **Warm-workspace orphan sweep is now instance-scoped**: `reconcile_warm_workspaces`'
  title-scan pass matched every `assistant-comms (warm)` workspace machine-wide,
  so a failing comms instance would close a SECOND instance's healthy warm
  session (distinct `COMMS_HOME` — a second box or a live-validation spawn).
  Each warm workspace's cmux name now carries a stable per-instance tag
  (`warm_workspace_title`), and the sweep filters on it — it still closes this
  instance's own orphans but can never reach another instance's session.

## [0.7.2] - 2026-09-05

### Fixed
- **Warm comms session no longer assumes Bedrock**: `bin/comms_session.py`
  hardcoded a Bedrock-shaped model id (`us.anthropic.claude-sonnet-4-6[1m]`)
  for the warm session it spawns, bypassing `model_tiers` (the module every
  other spawn site in this repo already resolves its model id through). If
  the operator's backend wasn't Bedrock, the spawned session got handed an id
  its own backend would reject. The model id and backend now resolve fresh on
  every spawn (not frozen at daemon import) from the same `model_tiers`
  resolution, and the launch command explicitly prefixes
  `CLAUDE_CODE_USE_BEDROCK=<0|1>` — unless the model is an explicit
  `COMMS_MODEL` pin, in which case the flag is left alone rather than risk
  contradicting an operator's deliberate override. AWS auth vars are
  deliberately NOT baked into the command (that would put secrets in the
  process's argv); they still reach the child via the pane's own login-shell
  `~/.zprofile` sourcing.
- **`model_tiers.model_for(long_context=True)` no longer drops 1M context on
  direct Anthropic**: it previously appended the `[1m]` suffix only on
  Bedrock, on the mistaken assumption that `[1m]` was Bedrock-specific.
  Verified false against the operator's own non-Bedrock `~/.zprofile` alias
  (`claude --model "claude-opus-4-8[1m]"` with Bedrock off). This was latent
  in every `long_context=True` caller (`pulse.py`'s Observer + Strategist
  models); it became an active regression the moment `comms_session.py`
  started routing through `model_tiers` above, silently breaking the warm
  session's 50%-of-1M-token `/clear` threshold on a non-Bedrock box.

## [0.7.1] - 2026-08-04

### Fixed
- **Working-override gate in pulse.py**: when `agent_status == "working"`
  (a `tool_use` is in flight with no matching `tool_result`), the Observer's
  verdict is now deterministically forced to `active` before it reaches
  `execute_verdict`. Previously, the transcript JSONL's mtime went stale during
  long tool executions (builds, test suites) because the file isn't appended to
  until the tool returns. The Observer prompt *told* the LLM to treat
  `agent_status=working` as `active`, but an LLM hint is not a gate — when
  `last_turn_age_sec > 1800` the LLM could still emit `stranded`, nudging a
  workspace that was mid-execution. The new `apply_working_override()` function
  makes the override mechanical, with a safety valve at
  `WORKING_OVERRIDE_MAX_AGE_SEC` (7200s / 2h): past that age a tool "in flight"
  is probably hung, so the Observer's verdict stands and the workspace can
  still be rescued.

## [0.7.0] - 2026-07-25

Completes Factory Droid provider parity across the assistant. Droid was
previously wired only into the headless (`llm_runner`) and dispatch
(`agent_session`) paths; the comms warm-session subsystem, the doctor, and setup
were still Claude-only, so setting `llm.provider: droid` left the warm session
spawning Claude and reading only Claude-schema transcripts.

### One-knob provider selection (G4)
- `agent_session.dispatch_agent` now falls back to the global `llm.provider` knob
  (precedence: `ASSISTANT_DISPATCH_AGENT` env → `dispatch.agent` → `llm.provider`
  → `claude`), so a single `llm.provider: droid` drives BOTH headless calls and
  spawns.
- New `agent_session.warm_agent()` resolves the comms warm-session provider:
  `ASSISTANT_COMMS_AGENT` env → `llm.features.comms.provider` (comms-only pin) →
  `llm.provider` → `claude`.

### Comms warm-session Droid parity (G1+G2+G3)
- `comms_session.spawn_session` picks the provider via `warm_agent()` and launches
  per-agent: Claude keeps its exact byte-identical launch (Sonnet, scoped
  `--add-dir`s); Droid uses `agent_session.launch_command(DROID)` with `--cwd`.
  Readiness and the first-launch trust prompt route through
  `agent_session.ready_re`/`trust_marker`.
- Transcript reads are schema-agnostic: `project_dir_for_cwd`/`newest_transcript`
  resolve the per-agent root (`~/.factory/sessions` for Droid) via
  `agent_session.confirm_dir`, and `last_assistant_text` uses
  `agent_session.record_role` (Droid `type=="message"`/`message.role`). The
  chosen provider is persisted in `session.json` (`agent`) so post-restart reads
  use the right root + schema.
- Context management is provider-aware: Droid transcripts carry no usage/token
  block, so `should_clear` uses an on-disk transcript-size proxy
  (`COMMS_DROID_CLEAR_BYTES`, ~2 MB default), and `clear_session` performs a
  lossless RESPAWN for Droid (close + spawn) instead of the Claude `/clear`. The
  `comms-listen` daemon threads the persisted session agent through every
  transcript-root / context call.

### Doctor + setup (G7, G5)
- `assistant-doctor.py` adds droid binary + `droid-glm-settings.json` checks
  (mirroring `agent_session.agent_available`), run when any seam resolves to
  droid and skipped for a Claude-only box.
- `assistant-comms-setup.sh` persists `llm.provider` into config.json
  (`ASSISTANT_LLM_PROVIDER` env, else preserved, else `claude`), merged without
  disturbing the Slack send-gate invariant.

### Cleanup
- Removed the dead `PROJECTS_DIR` constant from `session-context-watcher.py`
  (transcript discovery already flows through the Droid-aware world-scanner).

## [0.6.0] - 2026-07-14

Unifies the productization line (0.3.1–0.5.1) with the lesson-proposal delivery
loop (developed in parallel), and integrates Slack comms + productization onto
the Keel `main` line (connectors M5, gated-outbound M7, model-tiers M8). The
proposal-delivery details are under "Proposal delivery" below.

### Integrated onto Keel main
- **Slack comms sits alongside Keel as a confined pull feed** — it is NOT routed
  through the decision spine (`outbound-dispatch.py`) or the interrupt-gate. A
  reply is *solicited* (the inbound message authorizes it) and every post goes
  only to the operator's own 1:1 channel, which the send-gate (`allowed_targets`)
  refuses to leave — so comms touches no governed push surface and needs no
  chokepoint. This is a DIFFERENT action from the code-forbidden
  `slack.reply.send` (posting into a shared human channel), which stays banned.
- **CI send-fence allowlist** (`test_no_rogue_notifications.py`): `slack-send.py`
  and `src/assistant/slack.py` are consciously added to the push ALLOWLIST — the
  fence bans the `chat.postMessage` *string*, not the confined behavior, and the
  confinement is proven by `test_slack_send.py` (HTTP is never called for a
  non-allowlisted target). `assistant-doctor.py`'s scope table was reworded so it
  no longer carries the literal API string (it names no real send site).

### Fixed
- **Connector plists were author-locked** (P1 bug-class, pre-existing on main):
  the six Keel connector plists (github/gcal/gmail/slack/outlook) and
  `machine-config-sync` still carried a literal `/Users/mukuls` (or the legacy
  `/Users/<user>/` placeholder + a hardcoded interpreter), which no `sed` scheme
  substituted — they would have pointed at the author's home / wrong arch on any
  other machine. Tokenized all of them to `__HOME__`/`__REPO__`/`__PYTHON__`/
  `__PATH__`, matching the rest of the fleet; surfaced by
  `test_plist_portability.py` at merge time.

## [0.5.1] - 2026-07-07

### Fixed
- **Fresh-machine install crash** in `install/patch-settings.py`: it
  unconditionally `shutil.copy2`'d `~/.claude/settings.json` to a backup, but on
  a machine that has never run Claude Code that file is absent →
  `FileNotFoundError`, aborting the install (and the `bash <(curl…)` bootstrap)
  right after the hooks step. Now it backs up only if the file exists, else
  creates the parent dir and writes fresh. Caught by a real interactive
  fresh-install run in a sandboxed cmux workspace (the earlier
  ASSISTANT_SELF_UPDATE portability test had dodged this path).

### Testing
- `test_patch_settings.py` (4): fresh-machine create (no crash, no spurious
  backup), idempotent re-run (no duplicate hooks), and preservation of existing
  user settings (with backup).
- Validated the v0.5.0 interactive opt-in prompt **live** in a fresh sandboxed
  cmux workspace: prompt fired on a real TTY, answered memory=y/crash-resume=n,
  state persisted, re-run honored it with no re-prompt; the piped/headless path
  correctly skipped the prompt and persisted nothing.

## [0.5.0] - 2026-07-07

### Added
- **Interactive feature discovery.** On an interactive `install.sh --apply` (a
  real terminal, incl. the `bash <(curl …)` bootstrap), each undecided opt-in
  feature is now OFFERED with a one-line explanation + `[y/N]` prompt — so users
  discover memory-sync / crash-resume without reading docs to learn the `--with`
  flags exist. Answers persist in `~/.assistant/feature-opt-in`, so re-runs
  never re-ask. Precedence per feature (first match wins): explicit `--with`
  flag → remembered answer → already-running daemon (adopted) → prompt →
  headless default-OFF.
  - **Never hangs the headless paths** (the cardinal rule): the prompt fires
    only when `--apply` AND stdin is a TTY AND not `ASSISTANT_SELF_UPDATE` AND
    no explicit flag AND undecided. The pulse self-update, `curl | bash`, and CI
    each fail ≥2 of those terms and fall through to default-OFF. `read -t 60`
    bounds a walk-away; a bare `read` is never used (set -e would abort on EOF).
  - A **timeout** leaves the feature undecided (re-asked next run) — only an
    explicit decline (`n`/Enter/EOF) is remembered as "no", so a walk-away can't
    silently suppress future discovery.
  - Changing your mind: re-run with the `--with` flag (beats a remembered "no"),
    or delete the line from `~/.assistant/feature-opt-in` to be re-asked. An
    already-running daemon is adopted, never torn out.

### Testing
- `test_install_optin_prompt.py` (8 tests): proves the headless paths
  (self-update, non-tty --apply, dry-run) never prompt or hang, pins the
  answer→persist mapping (y/yes→yes; n/Enter/EOF→no), and confirms an explicit
  flag suppresses the prompt.

## [0.4.0] - 2026-07-07

### Added
- **Opt-in daemon tiers.** A bare `install.sh --apply` now installs **CORE only**
  — the fleet loop + dashboard (pulse, world-scanner, session-context-watcher,
  assistant-page, todo-server). Feature daemons are off by default and enabled
  per-flag: `--with-memory` (cross-machine memory sync — only useful on >1
  machine), `--with-crash-resume` (auto-resume crashed cmux workspaces),
  `--with-all`. Slack comms + slack-reactor remain token-gated (copied,
  hand-loaded after setup — never auto-loaded even with `--with-all`). Flags
  only affect a fresh install's loaded set; an already-running feature daemon is
  never torn out, and every feature plist is always copied so it can be enabled
  later. Rationale: a usage analysis showed memory-sync had pulled once ever and
  workspace-watcher fired 3× ever — real features, but not everyone's problem,
  so they shouldn't be forced on. Documented in ONBOARDING.md + install.sh --help.

### Changed
- **assistant-page render cadence 15s → 30s** to match world-scanner's output
  rate — the renderer's only input (world.json) changes every 30s, so rendering
  at 15s made ~half the renders byte-identical no-ops. The dashboard's own 15s
  `<meta>` auto-refresh keeps the browser view as fresh as before. (Not made
  WatchPaths-event-driven: world.json is written non-atomically, so a vnode
  event could fire mid-write — a cadence match is the safe fix.)

### Testing
- `test_install_tiers.py` (5 tests): drives `install.sh` dry-run and asserts
  CORE loads by default, feature daemons are copy-no-load until their `--with`
  flag, and token-gated daemons never auto-load even under `--with-all`.

## [0.3.1] - 2026-07-06

### Fixed
Adversarial ship-gate findings from the 0.3.0 productization (all verified
against the tree before fixing):
- **Symlink write-through guard (R1/R2)** — `install.sh`'s copy path now refuses
  to `cp` through a symlinked target: a legacy install left some
  `~/Library/LaunchAgents/*.plist` as symlinks INTO the repo, so a `--apply`
  would have written rendered `/Users/mukuls` content back through the link into
  the committed portable template. Both `ensure_file_copy` and the Section-3
  loop now replace a symlink with a real rendered file (guard, not follow).
- **Doctor scope accuracy (D1/D2)** — `_history_scopes()` makes the
  conversations.history requirement an EITHER/OR set for EVERY target type,
  closing a false-PASS for a `D…` DM-channel target missing `im:history`; and
  `users:read`/`groups:read` are no longer required (no daemon calls
  users.info / conversations.info). Scope docs across
  ONBOARDING.md / assistant-comms-setup.sh / docs now agree with the doctor.
- **Crash-loop announce dedup (D3/D4)** — `comms-listen`'s misconfig announce
  now dedups on a STABLE key (the set of failing check names, not variable
  error text) and fails CLOSED (writes the marker before sending), so a flapping
  network can't spam the operator every 10s.
- **Portability hardcodes (P1/P2/P3/P5/P6)** — `todo-server.py` (renderer path),
  `comms_session.py` (DISPATCH_CWD + --add-dir), `session-context-watcher.py`
  and `world-scanner.py` (cron cwd), `merge-pr-dispatch.py` (sibling scripts),
  and `render-assistant-page.py` (home shortener) now derive from `__file__` /
  `$HOME` instead of `/Users/mukuls` or a fixed `~/dev/assistant`.
- **Docs vs behavior (D2/D3/D5/D6)** — corrected the false "installer loads
  nothing / loading pulse brings up the always-on set" claims (install.sh
  --apply DOES reload the always-on set; the companion agents are independent),
  the README test/fixture counts (48 / 14), and the install.sh --help COPIED
  list.

### Testing
- `test_plist_portability.py` gains a `bin/*.py` author-literal scan (the
  durable gate against re-introducing `/Users/mukuls` / `~/dev/assistant`
  hardcodes). `test_doctor.py` adds D…-target + users:read-not-required cases.
  Three previously author-pinned tests (renderer, world-scanner, session-context
  cron) now assert the portable behavior via `$HOME`. Full suite green
  (1203 passed, 1 skip).

## [0.3.0] - 2026-07-06

### Added
- **Productization pass** — make Assistant installable + onboardable by any
  macOS engineer with repo access (not just the author):
  - `bin/assistant-doctor.py` — a stdlib preflight that classifies checks
    **core** (python/repo/git/cmux — block install) vs **optional** (Slack
    comms / warm-session — warn only), each with an exact remediation. Wired as
    install.sh phase `[0]`, into `assistant-comms-setup.sh` (refuses to advertise
    the launch line on a scope failure), and into `comms-listen.py` startup
    (announces a misconfig once, deduped, instead of a silent 10s crash-loop).
  - `ONBOARDING.md` — the single "start here" with an ordered activation runbook
    (core pulse → cmux-watcher → Slack comms → slack-reactor → memory); the same
    runbook now prints at the end of `install.sh --apply`.

### Fixed
- **Portable LaunchAgent plists (C1)** — the 10 plists hardcoded `/Users/mukuls`
  and install.sh's `/Users/<user>/` sed was a no-op, so every daemon shipped the
  author's home path and died on any other machine. Plists are now templates
  with `__HOME__`/`__REPO__`/`__PYTHON__`/`__PATH__` tokens that install.sh
  substitutes with arch-resolved values (Apple-Silicon vs Intel interpreter).
  Filenames unchanged so `self_update.py` still keys on them.
- **Dead recovery references (C2)** — the comms warm prompt + `comms_lib.Paths`
  pointed at `heartbeat-write.py`/`spawn-assistant.sh` (deleted in 6bfa86c);
  the "restart Assistant" row now runs the live
  `launchctl kickstart -k … assistant-pulse`.
- **Bootstrap repo URL** — overridable via `ASSISTANT_REPO_URL`; no longer
  implies the author-only `git@github-personal:` SSH alias.
- `slack-reactor` plist downgraded to copy-no-load (it crash-loops without tokens).

### Testing
- 30 new tests: `test_plist_portability.py` (per-plist token render + a true
  fresh-checkout end-to-end that runs `install.sh --apply` from a mukuls-free
  path into a sandboxed HOME, asserting zero `/Users/mukuls` survives — **no
  launchctl**), `test_doctor.py` (core/optional + minimal Slack-scope logic),
  `test_reference_integrity.py` (every referenced script path exists — the
  durable gate against the next 6bfa86c-style orphaning).
### Proposal delivery (folded into 0.6.0)

### Fixed
- **Lesson proposals now actually reach the operator.** The lesson-extractor
  has always mined recurring patterns and written pending proposals to
  `~/.assistant/proposals.jsonl`, but nothing delivered them: the comms daemon
  suppressed a `lesson-proposal` ledger kind that was never emitted, and the
  warm session (its supposed delivery path) is reactive-only — it wakes on
  inbound Slack and is `/cleared` at 50% context, so it could never proactively
  surface a proposal. The queue sat dead for a month (436 pending proposals
  accumulated, undelivered).

### Added
- **PROPOSALS delivery loop** in `comms-listen.py` (5th daemon loop). Watches
  `proposals.jsonl` as a durable queue and delivers each new pending lesson
  proposal to the comms channel exactly once, asking the operator to confirm.
  - **Exactly-once via an id high-water-mark cursor** (`proposals.cursor`), not
    a byte offset — the confirm path rewrites `proposals.jsonl` in place, which
    would corrupt a byte cursor. Proposal ids are ISO-µs timestamps, so
    `id > cursor` is a clean, rewrite-safe watermark.
  - **Backlog skipped on first run** (like the ledger cursor) so the daemon
    never blasts a stale queue at the operator's phone; recoverable by deleting
    `proposals.cursor` to replay. Deliveries are capped per drain
    (`PROPOSALS_MAX_PER_DRAIN`) so one extractor batch can't firehose.
  - **Halt-and-retry on send failure** — the cursor advances only past a
    successfully-sent proposal, so a failed send retries next pass and no
    proposal is ever silently lost.
  - Each delivery is mirrored into `conversation.jsonl` and carries the
    proposal **id**, so after a `/clear` the warm session can confirm the
    *specific* proposal the operator's `y` refers to (fixing the prior
    `tail -1` bug that confirmed the wrong proposal when several were pending).
  - `comms_lib`: `read_proposals_cursor` / `write_proposals_cursor` /
    `initialize_proposals_cursor_if_missing` / `read_all_proposals` /
    `read_new_proposals` / `fmt_lesson_proposal`.
- Warm-session prompt updated: the daemon delivers proposals (not the warm
  session), and confirmation reads the id back out of the conversation window.

### Notes
- `pattern` and `lesson_audit` proposals are **not** delivered — they have no
  confirm path yet, so pinging them would be a dead-end. Documented gap.

## [0.2.0] - 2026-07-06

### Added
- **Slack comms** — a bidirectional comms layer, the Slack re-cut of the
  Telegram/Discord comms removed in 0.1.x for Adobe-IT security reasons. Posts
  to a private channel the bot is invited to (bot token, not the operator's
  identity):
  - Outbound pings: verified/failed ledger actions, cmux-watcher
    workspace-signal pages, and heartbeat-stale pages.
  - Inbound: a warm cmux Claude session (Sonnet) reads every message in the
    channel and replies at top level — a **flat 1:1** model (no threading).
  - `CommsSubsystem` wires into the single-process daemon, additive: active only
    when Slack is fully configured (`has_slack`).
  - Transport CLIs (`slack-send.py`, `slack-poll.py`), shared `comms_lib`, warm
    `comms_session`, the `comms-listen.py` daemon, `assistant-comms-setup.sh`,
    the opt-in LaunchAgent, and onboarding docs.
- **Send-gate** — `slack-send.py` and `src/assistant/slack.send()` refuse, with
  no API call, any target not in `config.slack.allowed_targets` (seeded to the
  one comms channel). Defense-in-depth confining the bot to its channel.
- Inbox freshness gate: stale cmux-watcher signals (older than
  `COMMS_INBOX_MAX_AGE_SEC`, default 300s) are dropped without a ping.

### Changed
- Daemon self-cleanup of its own warm session restored under a title guard
  (`close_own_workspace` only ever closes an `assistant-comms (warm)`
  workspace); `test_no_close_workspace.py` narrowed to allowlist exactly that
  one guarded invocation while still banning every other close-workspace call.

## [0.1.0] - 2026-06-06

### Added
- Initial single-process daemon (`python -m assistant`): collapses the pulse
  timer + heartbeat into one binary with a subsystem-per-thread model
  (`pulse`, `tools`, `heartbeat`).

### Removed
- Telegram and Discord comms transports (later in the 0.1.x line), removed
  entirely for Adobe-IT security reasons — the direct predecessor of the 0.2.0
  Slack comms above.

[0.6.0]: https://github.com/elitecoder/assistant/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/elitecoder/assistant/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/elitecoder/assistant/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/elitecoder/assistant/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/elitecoder/assistant/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/elitecoder/assistant/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/elitecoder/assistant/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/elitecoder/assistant/releases/tag/v0.1.0
