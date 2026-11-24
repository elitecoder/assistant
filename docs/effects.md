# The effects record

An effect is anything Assistant changes outside itself: archiving an email,
moving a conversation to Trash, sending an unsubscribe request. Assistant
writes every effect it makes to one record, `~/.assistant/effects/effects.jsonl`,
before it acts and again when it finishes.

Today the Work queue's Gmail buttons (**Archive**, **Mark read**, and moving a
conversation back to your inbox), **Apply approvals** for Gmail Trash,
unsubscribes, and everything Assistant types into an agent session are
recorded. Slack saves follow.

## What it promises

- **Recorded first.** Assistant writes a line saying what it is about to do
  and waits until it's on disk. When it can't, it doesn't do the action. The
  page says "Assistant couldn't record this action, so it didn't run it.
  Check that your disk isn't full, then try again"
- **Approved first, when it can't be undone.** Each kind of action says
  whether it can be undone and what approval it needs. An unsubscribe can't be
  undone, so it needs an approval you gave on the page. A button press, or a
  rule, doesn't count. An unsubscribe that went through is never sent again,
  and neither is one that was sent and never answered, unless you approve it
  again. Unsubscribing can't be undone, because the sender has already
  received the request.
- **No double runs.** A second press while the first is still working is
  turned away ("This is already in progress, or Assistant stopped partway
  through it. Try again in ten minutes"). A line with no result that is
  more than ten minutes old stops counting, so a crash holds that action for
  ten minutes, not for good.
- **Only ever added to.** Lines are appended. Nothing rewrites or deletes one,
  and the file is never trimmed automatically.
- **Pointers, not content.** A line holds ids: the Gmail address you linked
  (it's needed to undo), a conversation or message id, a short code for the
  Work queue row (for a conversation of more than 100 messages, a count and a
  short digest of their ids), and the host name of an unsubscribe link. It never holds a
  subject, a sender's name, message text or the unsubscribe link itself, which
  works like a password. Every line is also run through the same token
  scrubber as the logs. A failure is recorded as a kind (`timeout`,
  `network`, `tool failed`, `http 500`), never as the server's own words.
- **Private.** The folder is readable only by you, and so is the file.
- **Not there before.** Actions from before this record existed aren't in it,
  and undoing them still works.

## What the file looks like

One JSON object per line. An action writes two lines with the same id:

```
{"v":1,"kind":"intent","id":"ef-0123456789abcdef","at":"2026-11-02T10:00:10Z","t":1793613610.0,"class":"gmail.archive","target":{"account":"me@example.com","thread":"18c3f0a1b2"},"auth":{"kind":"tap","ref":"/api/gmail","at":"2026-11-02T10:00:10Z"},"trace":"","component":"work-queue","undoes":""}
{"v":1,"kind":"result","id":"ef-0123456789abcdef","at":"2026-11-02T10:00:11Z","t":1793613611.0,"outcome":"done","verified":true,"error":""}
```

`verified` is `true` when Gmail (or, for an unsubscribe, the sender) confirmed
the change, `false` when Gmail showed something else, and `null` when it
couldn't be checked.

The result's `outcome` is one of three words:

- `done`: it happened.
- `failed`: it didn't happen. Gmail or the sender answered with an error, or
  the request never left your Mac.
- `unknown`: it may or may not have happened. Assistant sent it and never
  heard back (a time-out, a dropped connection, or an error nobody
  expected). An unsubscribe in this state isn't sent again unless you approve
  it again, and the page says "Assistant sent this but didn't hear back, so it
  may have gone through. Approve it again to send it once more". An undo never
  points at an action whose outcome is unknown.

An intent with no result at all is an action Assistant started and never
finished, for example because it stopped partway. To find the ones whose
outcome is unknown, run `grep '"outcome":"unknown"' ~/.assistant/effects/effects.jsonl`.

## What can be undone

| Action | Undone by | Approval it accepts |
|---|---|---|
| Archive | Move back to the inbox | A button press |
| Move back to the inbox | Archive | A button press |
| Mark read | Nothing yet | A button press |
| Gmail Trash | Put back | An approval on the page |
| Put back from Trash | Trash | An approval on the page |
| Unsubscribe | Can't be undone | An approval on the page |
| Mark a Slack save done (not recorded yet) | Reopen | A button press or an approval |
| Reopen a Slack save (not recorded yet) | Mark done | An approval |
| Type into an agent session | Can't be undone | A button press, a flag on the task, or a rule you turned on |

When the record has the action an undo reverses, the undo names it and is
refused unless that action succeeded, is on the same conversation and hasn't
been undone already. An undo of something from before the record existed just
runs.

## Typing into agent sessions

Agents that Assistant starts run with their permission prompts off and act on
whatever is typed into them, and typing can't be taken back. So nothing in
Assistant's own code types into a session until the line saying so is on
disk. One module, `src/assistant/session_effects.py`, is the only place that
sends keystrokes, and a test fails the build if any other file under `bin/`,
`src/`, `work_queue/` or `hooks/` does.

Each typing is one action with the session (`workspace:N`), the task when
there is one, and a short fixed name for where the text came from. It never
holds the text, a prompt's file name, a title or a Slack message.

| What is typed | Where it comes from | What allowed it |
|---|---|---|
| The instruction that starts a task, and the folder-trust answer before it | `task`, `trust` | The task's auto-start flag (the task's id) |
| The "stalled" nudge, and `/cleanup` | `nudge:stranded`, `cleanup` | Pro mode, the Observer and the watcher being on, and actions not held |
| A merge request (`/merge-when-ready`, `/monitor-ffp-ci`) | `merge` | The same standing setting when the pulse sends it; a button press when you run the script |
| A message in the Slack session | `slack-message` | Your own Slack message, as of its time |
| The Slack session's start-up instruction, trust answer and `/clear` | `boot`, `trust`, `clear` | The Slack channel being set up and its service running |
| "Run now" on a proposal | `run-now` | A button press on that route |
| `cmux-restore-sessions` | `restore` | You running it |
| `bin/cmux-send.py` run by hand | `cli` | You running it |

What the record does for you:

- **No record, no typing.** When the line can't be written, nothing is typed.
  A task stays open and is tried again by the next pulse, and the dashboard
  shows "Dispatch paused" with what to check (the disk, and the permissions
  of `~/.assistant/effects`). A nudge or `/cleanup` is skipped that round, a
  Slack message stays queued, and `cmux-send.py` exits 4.
- **A loop is stopped.** The same thing typed into the same session more than
  three times in thirty minutes is refused, and Assistant logs one line naming
  the session and where it came from. Each task and each Slack message counts
  as its own thing.
- **Typing is not repeated when it may have landed.** The result is `done`
  when the text and Enter both went out, `failed` when nothing did, and
  `unknown` when the text went out and Enter didn't, or Assistant can't tell.
  A typing that is `done` or `unknown` is not repeated by Assistant on its
  own. For a nudge or `/cleanup` that means that session isn't sent that
  one again until you look at it. A Slack message that was typed isn't typed
  again, and it leaves the queue with a line in the log. `verified` is `true`
  when the session's transcript shows the text arrived and `false` when it
  doesn't, and is empty when nothing checked.
- **Only your Slack messages are typed.** A message from someone else in the
  channel is kept as received and never typed. See
  [assistant-comms-onboarding.md](assistant-comms-onboarding.md).
- **"Run now" is stricter.** The session must be a `workspace:N`, and the text
  one line of at most 500 characters. Nothing in this repository's dashboard
  page calls the route today; only a tool that writes proposal files and posts
  to it would.

### Not recorded yet

Three ways of typing into a session don't pass through this record, because
they run outside Assistant's own code:

- The `spawn-claude-workspace` skill's shell snippet types the first
  instruction itself, run by you or by an agent.
- The Slack session's own model can run `cmux`, as its start-up instructions
  allow.
- A session started with a command rather than typed into (crash resume in
  `bin/workspace-watcher.py`).

## Reading it and keeping it

To see the last actions, run `tail -n 20 ~/.assistant/effects/effects.jsonl`.
The file `effects.lock` sits beside it; it's empty and only makes two Assistant
processes take turns. Each action takes about 450 bytes, so a year of normal
use is typically 5 to 30 megabytes. Assistant keeps what it has read in
memory: about 1 MB for each thousand actions, and the first action after it
starts is slower with a very large file.

The file is never trimmed. Deleting it also deletes the record that stops a
second unsubscribe, so leave it alone unless you mean to start over.
