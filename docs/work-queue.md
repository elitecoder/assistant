# The Work queue

The Work queue is a local page, at `http://127.0.0.1:8851/` by default, that
merges your Slack Later saves, the pull requests that ask for your review, your
Outlook and Gmail inboxes, and today's calendar into one list you clear. It's
part of Pro mode.

Rules sort every row. The page has no model step yet, so it doesn't rank rows,
write summaries, or draft replies. You decide what each row needs: mark it
Done, snooze it, drop it, or send it to Assistant to finish on its own.

## Turn it on

```bash
./install.sh --apply --pro
```

In Pro mode, the installer copies and loads the
`com.assistant.work-queue` LaunchAgent. When you run it from a terminal, it
also offers to link each source you haven't linked yet. Automatic updates and
other runs without a terminal never ask.

`./install.sh --apply --no-pro` unloads the server. In default mode, the
server isn't loaded, the dashboard has no **Work queue** tab, and
`python3 -m work_queue.server` exits without listening.

The server listens on `127.0.0.1` only. It refuses a `POST` from any other
origin. It exits cleanly, and stays stopped, when Pro mode is off, when its
settings file can't be read, or when another process holds its port. With no
source linked, it runs and shows how to link one.

## Link your sources

Each source reads with your own sign-in. Nothing in the Work queue prints or
stores a token: tokens stay in your environment or in each tool's own store.

| Source | Tool | What it reads | Sign-in |
|---|---|---|---|
| `slack` | `uv` and Playwright's Chromium | Your open Later saves and their conversations | The [Slack focus list](connectors.md#slack-focus-list) user token, plus a Slack browser session |
| `gmail` | `gog` | Every inbox conversation, plus unread mail outside the inbox, for each account | `gog auth add you@example.com` |
| `calendar` | `gog` | Today's events on each account's primary calendar | `gog auth add you@example.com` |
| `outlook` | `npx -y @microsoft/workiq` | Your Outlook inbox and sent items, read-only | Work IQ's Microsoft 365 sign-in |
| `github` | `gh` | Open pull requests that ask for your review | `gh auth login` |

`bin/work-queue-setup.py` links and checks them:

```bash
bin/work-queue-setup.py status                 # one line per source, no sign-in
bin/work-queue-setup.py link gmail             # every gog account with Gmail access
bin/work-queue-setup.py link gmail --account you@example.com --account other@example.com
bin/work-queue-setup.py link github --host github.example.com
bin/work-queue-setup.py check                  # a read-only probe for each linked source
bin/work-queue-setup.py unlink calendar
bin/work-queue-setup.py refresh                # re-read the linked sources now
```

Each command prints one line per source: `linked`, `not linked`,
`needs sign-in`, or `tool missing`, with the exact fix. Linking detects the
tool, runs a read-only probe, and saves the source. For Slack, `link` reads
your workspace from your user token, then opens a browser window. Sign in to
Slack there once; later refreshes reuse that session without a window.

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
  "github": {"skip_authors": ["dependabot[bot]"], "host": "github.example.com"}
}
```

| Setting | What it does |
|---|---|
| `port` | The server's port. `$REVIEW_PORT` overrides it. |
| `slack.close_after_days` | A Later save whose thread moved more recently than this stays on your Open list. Default 14. |
| `github.skip_authors` | Logins whose pull requests you don't want listed. |
| `github.host` | A GitHub Enterprise host, passed to `gh` as `GH_HOST`. |

A source that's missing from the file isn't linked, and every refresh skips
it.

## What's on the page

Select **Refresh** to re-read every linked source. A source that fails shows
its error at the top of the page, and the other sources still render.

- **Open list**: the rows that need you. Slack saves whose thread moved
  recently, pull requests that ask for your review, Outlook mail a person sent
  you that you haven't replied to, and Gmail from a person that's unread or
  starred. Each row has **Done**, **Snooze** (3 days), **Drop**, and **Send to
  Assistant**. Marks are saved on the server, so they follow you to another
  browser.
- **Today**: today's events from each linked calendar.
- **Outlook**: mail worth reading, plus groups you can approve for deletion.
  The Work queue only reads Outlook, so approving builds a search you paste
  into Outlook.
- **Gmail**: one tab per account, with groups you can approve for Trash.
- **Unsubscribe**: senders that support one-click unsubscribe and send you
  nothing you keep.
- **Slack saves**: Later saves that look finished, with the last message as
  evidence.

### How rows are sorted

| Source | Groups |
|---|---|
| Slack | You had the last word, your own messages, read-only posts, the thread moved on without you, old asks nobody chased, your old promises, and threads that ended on a question. A save that can't be read isn't judged. |
| Outlook | Needs you, worth reading, GitHub notifications, calendar notices, broadcasts and automated notices, and read mail older than 30 days. |
| Gmail | Needs you, worth a look, notes to yourself, newsletters, shopping, notifications and promotions, and mail you never opened. |

The rules live in `work_queue/inputs/`.

## Send work to Assistant

**Send to Assistant** creates a P2 TODO in `~/.claude/assistant-todo.json`
that starts on its own. Its instructions tell the agent to finish the work
without waiting for review: file the tickets, post the Slack replies (each
starting with "Automated Message"), comment on and open pull requests. The
agent never sends email, merges, force-pushes, or deletes data. When it's
done, it runs `work_queue/todo_result.py <td-id> "<what it did>"`, and the row
shows the result. If it's blocked, it runs the same command with `--blocked`,
and the row shows what it needs.

## What it changes

Select **Apply approvals** to act on what you approved:

- Gmail conversations move to Trash, which keeps them for 30 days. **Undo**
  moves them back to your inbox.
- One-click unsubscribe requests go to the senders you approved. You can't
  undo this from the page.
- Slack Later saves are marked completed through your Slack browser session.
  **Undo** reopens them.

Each change is read back to verify it. The Work queue never sends email and
never changes Outlook.

## Where your data lives

| Path | What's there |
|---|---|
| `~/.assistant/work-queue/sources.json` | Your linked sources |
| `~/.assistant/work-queue/browser-profile/` | The Slack browser session |
| `~/.claude/work-queue/` (or `$WQ_DATA`) | Decisions, applied results, marks, queued TODOs, the built page in `site/`, and the refresh snapshots in `work/` |
| `~/.assistant/logs/work-queue.launchd.*` | The server's output |

Tests point `WQ_DATA`, `TODO_PATH`, `QUEUED_PATH`, and `MARKS_PATH` at
temporary files, so they never touch your real data.

## Move from the standalone work queue

If you ran the standalone work queue from `~/dev/work-queue`, the data folder
is the same, so your decisions, applied results, and marks carry over for rows
whose link didn't change. Both servers use port 8851: stop the standalone one,
or set `port` in `sources.json`, and then load the LaunchAgent again:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.work-queue.plist
```

Then link your sources and select **Refresh**.
