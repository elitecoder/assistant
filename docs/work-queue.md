# The Work queue

The Work queue is a local page, at `http://127.0.0.1:8851/` by default, that
merges your Slack Later saves, the pull requests that ask for your review, your
Outlook and Gmail inboxes, and today's calendar into one list you clear. It's
part of Pro mode.

Rules sort every row. Then a model triages the open rows: it says why each one
needs you, who should own it, and how big it is, drafts the replies, picks
where to start, and summarizes what changed since last night. You decide what
each row needs: mark it Done, snooze it, drop it, or send it to Assistant. See
[Model triage](#model-triage).

## Turn it on

```bash
./install.sh --apply --pro
```

In Pro mode, the installer copies and loads the
`com.assistant.work-queue` LaunchAgent. If the server is loaded but not
running, for example because its port was taken when you logged in, the
installer starts it. When you run the installer from a terminal, it also offers
to link each source you haven't linked yet. Automatic updates and other runs
without a terminal never ask.

The hourly automatic update never starts the server. It reloads a running
server whose LaunchAgent changed, and restarts a running server whose code
changed, but it doesn't load a server that isn't loaded or restart one you
stopped.

### Turn it off

- `./install.sh --apply --no-pro` switches to default mode and unloads the
  server.
- The dashboard's Pro mode switch does the same: switching to default mode
  unloads the server, and switching to Pro mode loads it again. It changes
  the LaunchAgent only when the installer copied it into
  `~/Library/LaunchAgents/`, and it doesn't start it when the plist's
  `python3` is older than 3.11, doesn't run, or can't be read; the switch says
  why instead.
- However the mode changes, the server checks it on every request and every
  5 seconds. Once Pro mode is off, it answers `403` and exits cleanly, so
  launchd doesn't restart it.

In default mode, the dashboard has no **Work queue** tab, and
`python3 -m work_queue.server` exits without listening.

The Work queue needs Python 3.11 or newer, the version `pyproject.toml`
requires. `install.sh` runs the LaunchAgents with the first `python3` of 3.11
or newer from Homebrew, `/usr/local/bin`, then `/usr/bin`, or with the
interpreters `ASSISTANT_PYTHON` lists, separated by colons. When it finds none,
it doesn't install the Work queue's LaunchAgent and removes one an earlier
install left, so launchd never starts it at login. On an older Python the
server itself exits cleanly, with a message, before it loads anything else.

### What the server accepts

The server listens on `127.0.0.1` only. It refuses any request whose `Host`
isn't `127.0.0.1` or `localhost` with its port, which stops a web page from
reaching it through a look-alike domain name. It also refuses a `POST` from
any other origin. `GET /files/<absolute path>` serves a file inside
`~/dev/generated-docs`, read-only, so links in an agent's result open. It
refuses anything outside that folder, including through `..` or a symbolic
link, serves Markdown as plain text, and sandboxes every file it serves, so a
page in that folder can't act on the Work queue. When a request fails
unexpectedly, for example on a saved file that isn't valid JSON, the server
answers with a JSON error instead of dropping the request.

It exits cleanly, and stays stopped, when Pro mode is off, when its settings
file can't be read, or when another process holds its port. With no source
linked, it runs and shows how to link one.

## Link your sources

Each source reads with your own sign-in. Nothing in the Work queue prints or
stores a token: tokens stay in your environment or in each tool's own store.

| Source | Tool | What it reads | Sign-in |
|---|---|---|---|
| `slack` | `uv` and Playwright 1.63.0's Chromium | Your open Later saves and their conversations | The [Slack focus list](connectors.md#slack-focus-list) user token, plus a Slack browser session |
| `gmail` | `gog` | Every inbox conversation, plus unread mail outside the inbox, for each account | `gog auth credentials <client-secret.json>` once, then `gog auth add you@example.com` |
| `calendar` | `gog` | Today's events on each account's primary calendar | `gog auth credentials <client-secret.json>` once, then `gog auth add you@example.com` |
| `outlook` | `npx -y @microsoft/workiq` | Your Outlook inbox and sent items, and today's Outlook calendar, read-only | Work IQ's Microsoft 365 sign-in |
| `github` | `gh` | Open pull requests that ask for your review, in every repository or only in the ones you list | `gh auth login` |

`bin/work-queue-setup.py` links and checks them:

```bash
bin/work-queue-setup.py status                 # one line per source, no sign-in
bin/work-queue-setup.py link gmail             # every gog account with Gmail access
bin/work-queue-setup.py link gmail --account you@example.com --account other@example.com
bin/work-queue-setup.py link github --host github.example.com
bin/work-queue-setup.py link github --repo octo-org/hello-world --repo octo-org/docs
bin/work-queue-setup.py check                  # a read-only probe for each linked source
bin/work-queue-setup.py unlink calendar          # unlink slack also deletes its browser profile and cookies
bin/work-queue-setup.py refresh                # re-read the linked sources now
```

`status` and `check` print one line per source, and `link` prints one line
for the source it links: `linked`, `not linked`, `needs sign-in`, or
`tool missing`, with the exact fix. Linking detects the
tool, runs a read-only probe, and saves the source. For Slack, `link` reads
your workspace from your user token, then opens a browser window. Sign in to
Slack there once; later refreshes reuse that session without a window.

For GitHub, `--repo` takes a repository in `OWNER/NAME` form, and you can
repeat it. With it, the page lists only the pull requests in those
repositories. Without it, the page lists every pull request that asks for
your review.

The connectors in [Connect your sources](connectors.md) are separate daemons.
The Work queue doesn't use or start them.

### The settings file

Linking writes `~/.assistant/work-queue/sources.json`. It holds no secrets, and
you can edit it by hand:

```json
{
  "port": 8851,
  "slack": {"team_id": "T0123456789", "workspace_url": "https://example.slack.com", "close_after_days": 14},
  "gmail": {"accounts": ["you@example.com"]},
  "calendar": {"accounts": ["you@example.com"]},
  "outlook": {"address": "you@example.com"},
  "github": {"skip_authors": ["dependabot[bot]"], "host": "github.example.com", "repos": ["octo-org/hello-world"]}
}
```

| Setting | What it does |
|---|---|
| `port` | The server's port: a whole number from 1 to 65535. Default 8851. |
| `slack.close_after_days` | A Later save whose thread moved more recently than this stays on your Open list. Default 14. |
| `outlook.calendar` | Set it to `false` to keep your Outlook calendar off the Today tab. Default `true`. |
| `github.skip_authors` | Logins whose pull requests you don't want listed. |
| `github.repos` | The `OWNER/NAME` repositories to list pull requests from. Leave it out to list every repository. |
| `github.host` | A GitHub Enterprise host, passed to `gh` as `GH_HOST`. |
| `triage.model` | The model that triages the rows when the provider is Claude. Default: the `balanced` tier, Sonnet 5.5. Droid, Copilot, and local models use their own model settings. |
| `triage.dailyUsd` | The Work queue's own daily model budget in US dollars. Default 3.00. |

A source that's missing from the file isn't linked, and every refresh skips
it.

## What's on the page

Select **Refresh** to re-read every linked source. A source that fails shows
its error at the top of the page, and the other sources still render.

- **Start here**: the 5 to 8 open rows to do first, with the **Ready for
  you** and **Since last night** boxes above them. It's the first tab when it
  has a list. When it only has a change summary or a note, it comes after the
  Open list. The page opens on the tab you used last. See
  [Ready for you](#ready-for-you).
- **Open list**: the rows that need you. Slack saves whose thread moved
  recently, pull requests that ask for your review, Outlook mail a person sent
  you that you haven't replied to, and Gmail from a person that's unread or
  starred. Each row has **Done**, **Snooze** (3 days), **Drop**, and **Send to
  Agent**, whatever the model said about it. Marks are saved on the server, so
  they follow you to another browser. In Pro mode, the rows the model sorted
  come first, in its lanes, in this order: **Do first** (someone is waiting on
  your reply or decision), **Promise** (you said you would), **This week**
  (needs you, but not first thing), and **Optional** (skim if you have time).
  Each of those rows names its source. The rest follow under **Not sorted
  yet**, grouped by source. Without any sorted row, the Open list is grouped
  by source only.
- **Send to agents**: every open row, from every source, with the ones the
  model said an agent can finish first. Pick rows, or select **Select all**,
  then **Send N to agents**. See [Send work to an agent](#send-work-to-an-agent).
- **Today**: today's events from each linked Google calendar and, when
  Outlook is linked, from your Outlook calendar. Today is your computer's
  local day. Cancelled Outlook events are left out, and events you declined
  say **you declined**. When the Outlook calendar can't be read, it shows as
  its own **Outlook calendar** error, and your Google events and Outlook mail
  still show.
- **Email**: one tab with a part for Outlook, a part for each Gmail account,
  and a part for Unsubscribe. Each part opens and closes, and a part with email
  that needs you starts open. The tab's count is the email that needs you and
  that you haven't hidden. A link or a saved tab from before, such as `#email`,
  `#gmail-1`, or `#unsubscribe`, opens the Email tab with that part open. See
  [The Email tab](#the-email-tab).
  - **Outlook**: mail that needs you, mail worth reading, and groups you can
    approve for deletion. The Work queue only reads Outlook, so approving
    builds a search you paste into Outlook.
  - **Gmail**: for each account, mail that needs you, mail worth a look, and
    groups you can approve for Trash.
  - **Unsubscribe**: senders that support one-click unsubscribe and send you
    nothing you keep.
- **Slack saves**: Later saves that look finished, with the last message as
  evidence.

### Ready for you

**Ready for you** lists each TODO you sent from the page that's done or
blocked, newest first: its title, its TODO id, and the agent's result, or why
it's blocked. Links in a result open in a new tab. A `file://` link to a file in
`~/dev/generated-docs` opens through the server, since browsers don't open
`file://` links from a web page; see
[What the server accepts](#what-the-server-accepts). **Done** hides an item for
good. **Open workspace** brings the agent's cmux workspace to the front and
selects it. It refuses when the workspace is closed, or when the workspace's
title no longer starts with the TODO's id: cmux reuses workspace refs.
A workspace you renamed so its title doesn't start with the id can't be opened
from here.

#### What clears on its own

Each refresh checks every done item you haven't marked **Done**, and clears
the ones whose follow-up already happened:

- **The work landed.** The agent's result names one or more GitHub pull
  requests, and every one of them is merged or closed. When the result names
  none, the pull requests in the TODO's detail and link count instead, which
  name the row it came from.
- **You followed up.** After the TODO finished, you commented on or reviewed
  one of those pull requests, or you posted in the Slack thread the row came
  from. Your GitHub login comes from `gh api user`, and your Slack id from
  your Slack token.

Clearing writes the same mark **Done** writes, plus the reason. **Show
cleared** lists the items that cleared on their own in the last 7 days, with
why, and **Undo** brings one back. An item you bring back never clears on its
own again. While a pull request it names is still open, the item says what
would clear it, for example **Clears when o/r#7 and #8 merge or close, or
once you comment on or review them.**

A blocked item never clears and never starts again on its own. When its
reason says the agent couldn't reach a GitHub repository, and
`gh api repos/OWNER/NAME` now works, the item says the blocker looks gone and
that you can resume it.

The checks use your own sign-in: the `gh` CLI, with `GH_HOST` set for pull
requests on the GitHub host you linked, and `$SLACK_USER_TOKEN`. A pull
request on any other host isn't checked. Each refresh reads each pull request
once, and each `gh` call has 30 seconds. A check that fails, for example
because the network is down, `gh` isn't signed in, or GitHub answers `403`,
leaves the item showing and doesn't stop the refresh. The refresh and the
server change your marks under one lock, `marks.lock` in the data folder, so a
**Done** you select during a refresh is never lost.

A blocked Start here item, whose row was already sent, shows **Sent td-NNN**,
turned off. While any TODO you sent is still open or still on **Ready for
you**, the page reads its status again every 20 seconds, so a result shows up
without a reload. It never redraws a note you're typing, and when a refresh
finishes while you type one, it asks you to reload once you're done instead of
reloading.

### The Email tab

Every Outlook and Gmail row on the Email tab, whether it needs you, is worth
reading, or is ready to delete, has these actions:

- **Send to Agent**, with the note box, as on the Open list.
- **Snooze** hides the email for 3 days.
- **Archive**, on Gmail mail in your inbox, archives the whole conversation,
  including mail that arrived since the last refresh.
- **Mark read**, on unread Gmail mail, marks the whole conversation read.

The Work queue only reads Outlook, so Outlook has no **Archive** or **Mark
read**. After **Archive** or **Mark read**, the server reads the labels of
every message in the conversation back from Gmail. The page says the change
worked only when Gmail confirms it; when `gog` fails, times out, or answers in
a form it can't read, the page says the change isn't confirmed and changes
nothing.

A hidden email, one you snoozed, archived, sent to an agent, or marked done or
dropped on the Open list, leaves the Email tab, the tab's count, the Outlook
search, **Approve all**, the approved count, and **Apply approvals**. Select
**Show hidden email** to list each one with why it's hidden, and select
**Undo** to bring it back. **Undo** on an archived email moves the
conversation back to your Gmail inbox. An email you sent to an agent stays
hidden while its TODO exists. Hiding uses the email's own id, so it lasts
across refreshes even when Gmail's link for it changes. An email that needs
you is also on the Open list, and hiding or sending it in one place hides it
in the other.

### How rows are sorted

| Source | Groups |
|---|---|
| Slack | You had the last word, your own messages, read-only posts, the thread moved on without you, old asks nobody chased, your old promises, and threads that ended on a question. A save that can't be read isn't judged. |
| Outlook | Needs you, worth reading, GitHub notifications, calendar notices, broadcasts and automated notices, and read mail older than 30 days. |
| Gmail | Needs you, worth a look, notes to yourself, newsletters, shopping, notifications and promotions, and mail you never opened. |

The rules live in `work_queue/inputs/`.

## Model triage

In Pro mode, each refresh first writes the page with the rule-based rows and
the verdicts it already has. Then it asks a model about the rest and writes the
page again, so a slow or failed model never holds the rows back. Default mode
makes no model call.

- **Each open row** gets a short action title, a one-line reason, a
  disposition, a lane, an effort (**Quick**, **Small**, **Medium**, or
  **Large**), and, when it needs a written reply, a draft with **Copy draft**.
  The action title, for example "Review Ana's pull request #42 on the
  parser", is the row's headline and its Start here item's, with the row's own
  text under it. **Send to Agent** still sends the row's own text. A row
  without a usable title shows its own text as before. The lane places the
  row on the Open list: `do_first`, `promise`, `week`, or `optional`. A row
  without a usable lane goes under **Not sorted yet**. The pill reads **Agent can
  finish** when the model says an agent can finish it unattended, and
  otherwise **Model suggests:** and then **You** (only you can answer or
  decide), **Hand off** (someone else should own it), or **Drop** (nothing is
  needed). The pill is a hint: **Send to Agent** shows on every open row.
- **Start here** ranks the 5 to 8 open rows to do first by who's waiting,
  deadlines, effort, and age, with a one-line reason each. A row you marked
  Done, dropped, or snoozed doesn't appear there, and its **Done** button saves
  the same mark as the row's. A row whose Assistant TODO is blocked always
  leads the list, ahead of the model's picks, even when it's marked. The list
  is ranked again when the rows or your marks change, and each day. Each item
  that isn't blocked also has **Send to Agent**, which sends its row for a
  first pass exactly as the row's own button does, and the item and its row
  leave the page.
- **Since last night** compares every row of each source, not only open rows,
  with the last snapshot from an earlier day, or with the previous refresh on
  your first day. A row counts as changed when what it says changes, not when
  a rule's wording such as "Quiet for 3 days" or a read flag changes. The new,
  gone, and changed counts per source come from the rows; the model writes one
  sentence per source from the titles. A source that wasn't read in both
  snapshots isn't compared, and a Slack save that can't be read in either
  snapshot doesn't count as new or gone.

A refresh triages up to 120 new or changed rows, 30 to a call. The rest wait
for the next refresh, and the Open list says how many. Verdicts are cached in
`$WQ_DATA/triage-cache.json` by a hash of each row's content:

- A row whose content didn't change isn't sent again while the other rows from
  its call stay unchanged. When one of them changes or leaves the Open list,
  the rest of that call is asked again. A row from a source that failed this
  refresh doesn't count as gone.
- A verdict stays cached for 14 days after its row was last seen.
- A row the model didn't answer about is shown without a verdict and isn't
  sent again until the next day. The Open list says how many there are. Rows
  asked again go to the model in an order that changes each day, so splitting
  finds different rows each time.
- A row you marked **Done** or **Drop** isn't triaged.
- When a call about rows fails (it times out, the model command fails, or the
  answer isn't in the expected shape, for example a refusal or an answer about
  2 or more rows with usable verdicts for fewer than half of them), its rows are
  skipped for the day and the rest of triage goes on. After **Start here** and
  the summary, the failed call is split in halves to find the rows that break
  it: at most 6 more calls a refresh, and only while the Work queue's own
  budget still covers the next one.
- When the **Start here** or summary call fails, the page says so, the other
  parts still show, and that part isn't asked again until the next day or
  until what it would be asked about changes.
- A model that refuses everything makes, in each refresh, at most 4 row calls
  (one per 30 rows it hasn't asked about yet), 6 split calls, and one **Start
  here** call and one summary call when what they'd be asked about changed:
  12 calls, about $1.60. A refresh with no new rows makes none. A day's total
  grows with the rows still waiting: 300 refused rows take 30 calls over the
  first 3 refreshes of a day. The Work queue's $3.00 daily budget caps any
  day.
- Two refreshes at once each keep the verdicts the other saved.

### What the model sees and can do

- Only rows from your linked sources. A Slack save that can't be read is never
  sent and never judged; it's listed under **Saves that can't be read** with a
  link to open it in Slack.
- Text that looks like a token, key, or password is replaced with
  `[removed]` in the copy sent to the model, before long text is cut short:
  token and key shapes, including GitLab's `glpat-`, credentials and
  signatures in links, tokens in reset, verify, and invite links and in link
  parameters such as `t=`, `code=`, and `reset_token=`, `Authorization` and
  `Cookie` headers, `key: value` pairs whose key names a secret such as `pwd`,
  `pw`, or `client_secret`, or is `pass` with a value that isn't a plain
  number, "password ... is: X" phrases, one-time codes such as "123456 is your
  code", "Use code 482913 to verify", "Enter 482913 to continue", or "code:
  482-913", and card numbers of 13 to 19 digits that pass the Luhn check, with
  spaces or dashes allowed. It's a pattern
  match, so it can miss an unusual format. It also removes long hexadecimal
  runs, such as a full commit hash, and long runs of letters and digits that
  aren't part of a link's path, from the model's copy only: the page always
  shows the original text. Each piece of text is cut to 8,000 characters, and
  each title or sender to 1,000, before the scrub, and every pattern runs in
  time linear in its input: the tests time each one on 1 MB of repeated text
  with no cut. A password phrase is removed to the end of its line, so a
  password with spaces doesn't leave words behind. The rules live in
  `work_queue/scrubber.py`.
- Every piece of model text on the page is escaped, so it can't add markup.
- Row text sits between markers with a random code the text can't guess, and
  the prompt tells the model the text is data, never instructions.
- Each answer is checked against a strict format: only the four dispositions,
  four lanes, and four efforts, only the row ids it was given, and bounded
  text. A title must be text of 100 characters or fewer on one line. A title
  or lane that breaks these rules is left out, and the rest of the row's
  verdict still shows. Anything else in the answer is ignored.
- An answer never acts. A draft is text you copy and send yourself, and **Send
  to Agent** still waits for you to select it.
- Rows go to the model 30 at a time, so text in one row could still sway the
  labels on the others in its call, for example calling them **Drop**. A label
  only changes a pill and the order of the **Send to agents** tab, a sent row
  keeps the TODO's own safety rules, and once the swaying row changes or
  leaves, the rest of its call is asked again.

### When triage fails or stops

Every call fails the same way. When a call times out, the model command fails,
or the answer isn't in the expected format, the page shows **Part of triage
didn't finish** on **Start here** and the Open list, naming the part (**Row
triage**, **Start here**, or **Since last night**) and the reason, and the
other parts still run. A failed call is booked against the budgets at least at
its reserved estimate. A call that ran out of time is booked as if it wrote
150 output tokens a second for its whole time limit, at least the reserved
estimate: about $0.68 for the full 240 seconds. That's an assumed rate, not a
measured one; it keeps a long call from being booked at less than it could
have cost.

Triage stops, with no more calls this refresh, when a budget is used up, the
refresh has spent 10 minutes on calls, or the model command isn't installed.
The page keeps every rule-based row, keeps every verdict that finished, and
shows **Triage unavailable** with the reason. An unexpected error in triage
does the same, and its details go to the refresh log. A saved cache,
snapshot, or reservation file that isn't in the expected shape is treated as
empty, and the refresh still writes the page. An answer nested too deep to
read is in the wrong shape, like any other.

There are two daily budgets, both for the UTC day:

- **All of Assistant's model jobs**: the Strategist's `dailyCostCeilingUsd`
  (default $5.00). Spend comes from the cost ledger,
  `~/.assistant/cost-ledger.jsonl`, and from the Observer's rows in
  `~/.assistant/metrics.jsonl`.
- **The Work queue's own**: `triage.dailyUsd` in `sources.json` (default
  $3.00), counted from the cost ledger rows with `caller` set to `work_queue`.
  Each call adds one. A value that isn't a finite number of zero or more means
  the default.

Before each call, triage reserves the call's estimated cost under the
Strategist's lock, so two refreshes at once can't both spend the last of a
budget. The estimate counts the prompt, about 14,000 tokens of the model
command's own prompt, and an expected answer of 220 tokens a row, 6,600 for 30
rows (the measured call wrote about 210 a row), priced by `bin/metering.py`
and then scaled by the ratio of a real call's cost to that price for its
tokens (about 1.74). A call about 30 short rows reserves about $0.17, and rows
with more text reserve more. Triage stops once
spend, counting calls still running, has reached a budget. A single call can
still go over it, by at most that call's cost. A reservation that isn't well
formed, is older than 10 minutes, or is dated in the future doesn't count.

### What it costs

These figures come from one real Sonnet 5.5 call about 5 rows: 14,800 input
tokens, most of them the Claude command's own prompt, 1,054 output tokens, and
$0.0697. Each further row adds about $0.0045: $0.0026 at the repo's price
estimate in `bin/metering.py`, scaled by the same 1.74. A call about 30 rows is
about $0.18, **Start here** about $0.10, and the summary about $0.07. They're
estimates, not a bill.

| Refresh | Calls | About |
|---|---|---|
| The first one, 120 new rows | 4 row calls and Start here | $0.83 |
| Nothing changed | none | $0.00 |
| One row changed | its call of 30 rows, Start here, and the summary | $0.35 |
| A queue of 300 new rows | 3 refreshes on the first day | $2.13 in all |

At the $3.00 default, that's about 8 refreshes a day in which one row changes.
Splitting a call to find a poisoned row adds at most 6 calls: about $0.12 for
a half of 15 rows, down to about $0.07 for the smallest.

### Choose the model

With the Claude provider, the model is Sonnet 5.5 by default: the `balanced`
tier in `src/assistant/model_tiers.py`, which gives each Claude backend its own
id. To use another Claude model for the Work queue only, set it in
`sources.json`:

```json
{"triage": {"model": "claude-opus-4-8"}}
```

The provider follows the rest of Assistant. To pick one for the Work queue
only, run `bin/assistant-llm.py set <provider> --feature work_queue`. Droid,
Copilot, and local models ignore `triage.model` and use their own model
settings in `~/.assistant/comms/config.json`.

## Send work to an agent

**Send to Agent** on a row, or **Send N to agents** on the **Send to agents**
tab, creates a P2 TODO in `~/.claude/assistant-todo.json` that starts on its
own, and creates that file if it doesn't exist yet.

**Send to Agent** on an Open list row, a Start here item, or an email first
opens a note box under the row. Type an instruction for the agent, such as
"draft a reply" or "check the logs first", or leave it empty, then select
**Send** or press Cmd+Enter (Ctrl+Enter). Esc or **Cancel** closes it without
sending. The box grows with your text to about 240 pixels, then scrolls. The
note goes in the TODO above the row's untrusted block, outside its markers,
labeled as your own instruction, so text in the row can never pose as it. The
safety rules still apply to it. If the send fails, the box keeps your note. If
the row was already sent, the page says so; the existing TODO stays as it was,
so your note isn't added to it. The **Send to agents** tab sends a batch and
has no note box. It also marks the row
Done, and the row leaves the page at once: the Open list, the **Send to
agents** tab, and the counts. The row stays off the page for as long as its
TODO exists; if the TODO is removed, the row comes back, marked Done.

Each row gets a short id the first time a refresh sees it, kept in
`traces.json` in the data folder. A TODO you send takes its row's id, and any
other TODO gets one when it is created or, if an agent wrote it by hand, on
the next pulse. An email has one id whether you send it from the Email tab or
the Open list, and keeps it if it moves to another Gmail folder. The first
refresh after this update matches rows you had already sent with their TODOs.
The first refresh or pulse after the update adds the id to each TODO it
touches. For a file written by hand or by a skill, it may also change the
file's spacing and how accented characters are written, never what it says.
Nothing shows any id yet, and nothing changes for you. If the id file is ever
damaged, the refresh keeps a copy next to it as `traces.json.damaged` and
starts a new one.

Follow the work on the Assistant dashboard. The TODO's status and result show
on the **Tasks** tab, and its session shows on the **Sessions** tab once it
starts. Assistant starts up to 2 TODOs every 5 minutes and keeps at most 5
sessions busy, so a large batch takes a while to start. Sessions you are using
count toward the 5, so a row you send can wait; the **Tasks** tab says when
starts are on hold and why. A row whose TODO is
blocked stays on **Start here**, even though it's marked Done; that blocked
state shows after the next refresh.

On the **Send to agents** tab, the rows the model said an agent can finish
come first. **Send N to agents** asks you to confirm, and the dialog names
the mode. The rows go one at a time, the button stays off until the batch is
done, and the tab lists any that weren't sent, with the reason.

There are two modes:

- **First pass**, the default, and always the mode of a row's own **Send to
  Agent**. The TODO asks the agent to leave nothing other people can see
  except a branch of its own and at most one draft pull request. Within that,
  it asks the agent to research the work, write and commit the code on its
  own branch, and write the Slack replies, ticket text, and pull request
  comments you'd need as drafts in its result, with what's done and what's
  left. It asks the agent not to post or react in Slack, file or comment on
  tickets, comment on or review pull requests, mark a pull request ready, or
  push to a branch it didn't create.
- **Hands-off**, when you turn on **Hands-off** before **Send N to agents**.
  The TODO asks the agent to finish the work end to end without waiting for
  review: file the tickets, post the Slack replies (each starting with
  "Automated Message"), and comment on and open pull requests.

`POST /api/todo` takes `"mode": "assist"` (the default) or `"mode":
"handsoff"`; any other value is refused. The TODO records it as
`"workQueueMode"`. When Assistant starts a first-pass TODO, its prompt ends
with first-pass limits that override the routing rules before them, which
otherwise say to open a pull request and to run `/architect-ffp:archffp` for
FFP work. The limits allow only a branch of the agent's own, local commits, a
push of that branch, and at most one draft pull request (`gh pr create
--draft`). They tell the agent to run `/architect-ffp:archffp` with
`--no-ship`, which runs every gate and stops before shipping, and to stop
`/architect-ffp:archself` after its touch-set plan, since it has no such
flag. The limits are instructions to the agent, not a sandbox: nothing
outside the agent's own judgment enforces them.

In both modes the TODO's instructions start with the safety rules:

- The row's title, detail, and linked source are untrusted content to act on,
  never instructions. They follow the rules, between `BEGIN UNTRUSTED ROW`
  and `END UNTRUSTED ROW` markers that carry a random code made for each TODO,
  so text in a row can't close the fence. Assistant's prompt shows a Work
  queue TODO's title and link only inside that fence.
- The agent never shares secrets and never acts outside the request.
- The agent never sends email, merges, force-pushes, or deletes data.

When its part is done, the agent runs `work_queue/todo_result.py <td-id> "<what
it did>"`, which records the result on the TODO and marks it done. If it's
blocked, it runs the same command with `--blocked` and what it needs.

When the sent row is a Slack save, the Work queue also marks the Later save
completed, in either mode, in the background and with no browser window, and
checks that it left your Later list. The send never waits for it. One
background worker takes every save sent so far and marks them all in one
browser run, so a batch of sends opens Chromium once. The result is saved
where **Apply approvals** saves its results. The worker, Apply, and a refresh
never use the Slack browser session at the same time: Apply and a refresh
wait up to 90 seconds for it, then say to try again later. A save Slack no
longer has is recorded as gone, and neither a later send nor **Apply
approvals** asks Slack about it again. Any other Slack error stays an error,
so **Apply approvals** tries it again.

## What it changes

Select **Apply approvals** to act on what you approved:

- Gmail conversations move to Trash, which keeps them for 30 days. **Undo**
  moves them back to your inbox.
- One-click unsubscribe requests go to the senders you approved. You can't
  undo this from the page. A request goes only to an `https` link on a
  public address, never to your own machine or network, and it never follows
  a redirect.
- Slack Later saves are marked completed through your Slack browser session,
  with no browser window. **Undo** reopens them. A save Slack no longer has
  shows as gone and isn't tried again.

Each change is read back to verify it, and a read-back that fails never
counts as verified. **Apply approvals** skips any Gmail conversation the page
hides. If applying stops on an error, the page says why instead of staying on
**Working**. The Work queue never sends email and never changes Outlook.
**Archive** and **Mark read** on the Email tab change Gmail at once, without
**Apply approvals**.

Assistant writes each Gmail change and each unsubscribe to [its record of
changes](effects.md) before it acts. When it can't record one, it doesn't run
it and the page says so. A second press on a conversation that is still being
changed is turned down, and an unsubscribe that went through is never sent
again, and neither is one that was sent and never answered, unless you
approve it again. When an **Undo** doesn't happen for a conversation in
Trash, because the record refused it or Gmail did, the row stays in Trash and
says why ("In Gmail Trash, verified · Undo not done: …"), the last-run line
gives each reason once, and the next **Apply approvals** tries again.

## Where your data lives

| Path | What's there |
|---|---|
| `~/.assistant/work-queue/sources.json` | Your linked sources |
| `~/.assistant/work-queue/browser-profile/` | The Slack browser session |
| `~/.claude/work-queue/` (or `$WQ_DATA`) | Decisions, applied results, marks, the TODOs rows were sent to and in which mode, the triage cache and budget reservations, what the last refresh found about each Ready for you item in `ready-checks.json`, the built page in `site/`, the Gmail conversation behind each Gmail row in `emails.json`, the emails you archived or marked read in `mail-actions.json`, and the refresh snapshots in `work/`, including the row snapshots for **Since last night** |
| `~/.assistant/effects/effects.jsonl` | The record of each Gmail change and unsubscribe, written before it runs; see [the effects record](effects.md) |

| `~/.claude/work-queue/` (or `$WQ_DATA`) | Decisions, applied results, marks, the TODOs rows were sent to and in which mode, the triage cache and budget reservations, what the last refresh found about each Ready for you item in `ready-checks.json`, the built page in `site/`, each row's id in `traces.json`, the Gmail conversation behind each Gmail row in `emails.json`, the emails you archived or marked read in `mail-actions.json`, and the refresh snapshots in `work/`, including the row snapshots for **Since last night** |
| `~/.assistant/logs/work-queue.launchd.*` | The server's output |

Tests point `HOME` and `WQ_DATA` at temporary folders, so they never touch
your real data.

## Move from the standalone work queue

If you ran the standalone work queue from `~/dev/work-queue`, the data folder
is the same, so your decisions, applied results, and marks carry over for rows
whose link didn't change. Both servers use port 8851: stop the standalone one,
or set `port` in `sources.json`, and then start the server again:

```bash
launchctl kickstart -k gui/$UID/com.assistant.work-queue
```

Then link your sources and select **Refresh**.
