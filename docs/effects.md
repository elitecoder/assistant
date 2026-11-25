# The effects record

An effect is anything Assistant changes outside itself: archiving an email,
moving a conversation to Trash, sending an unsubscribe request. Assistant
writes every effect it makes to one record, `~/.assistant/effects/effects.jsonl`,
before it acts and again when it finishes.

Today the Work queue's Gmail buttons (**Archive**, **Mark read**, and moving a
conversation back to your inbox), **Apply approvals** for Gmail Trash,
unsubscribes, the Slack Later saves the Work queue marks completed or reopens,
and, in Pro mode, everything Assistant's own code types into an agent session
are recorded. In the default mode typing into sessions is not recorded and
works as it always did. Three ways of typing that run outside that code aren't
recorded in either mode; they are listed under "Not recorded yet".

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
  more than ten minutes old stops counting as in progress, so a crash holds
  an action you can undo for ten minutes, not for good. An unsubscribe is
  different, because it can't be undone: see `unknown` below.
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
finished, for example because it stopped partway or your Mac went to sleep.
For an action that can't be undone, Assistant treats that the same as
`unknown` once ten minutes have passed: it may have gone through, so it
isn't sent again unless you approve it again. To find the ones whose
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
| Mark a Slack save done | Reopen | A button press or an approval |
| Reopen a Slack save | Mark done | An approval |
| Type into an agent session | Can't be undone | A button press, a flag on the task, or a rule you turned on |

### Slack saves

A save is marked through your Slack browser session, one run for a whole batch,
and each save in the batch has its own lines. A save is recorded by its
channel id, its message time and the short code of its Work queue row, never
its text or link. When the record can't write a save's first line, or turns it
down, that save is left out of the run and the others still go through.

| What happened | Outcome | `verified` |
|---|---|---|
| Slack accepted the change and the Later list shows it | `done` | `true` |
| Slack accepted it but the list still shows the old state | `done` | `false` |
| Slack no longer has the save | `failed` | empty |
| Slack answered with another error | `failed` | empty |
| Slack isn't linked, or its sign-in is needed | `failed` | empty |
| The browser run stopped, timed out, couldn't read your Later list back, or answered oddly after it started, or didn't mention the save | `unknown` | empty |
| An unexpected error before the browser started | `failed` | empty |
| An unexpected error after it started | `unknown` | empty |

`verified` is `true` when Assistant read your Slack Later list again after the
change and the save was in the state asked for: off the open list after marking
it done, back on it after reopening.

An odd-looking channel id or message time never stops a save being marked. The
record writes a short code made from it instead.

The worker that marks saves after a send and **Apply approvals** take turns
with the browser session, so one never marks a save while the other does. A
line with no result left by a run that died turns both away for ten minutes.
When a mark is held back after a send (the record turned it down, Slack answered
with an error, the browser run failed, or Slack needed you to sign in),
Assistant tries it again by itself three times over about twenty minutes, after
1, 5 and 15 minutes. A save you approved under Ready to close is also tried by
the next **Apply approvals**. If it still can't be marked, the Work queue page
says so in one line at the top, in plain words, with a link to each save, and you
mark it done in Slack yourself. When Slack needs you to sign in, the line shows
at once, because trying again can't fix that, and the tries go on. The line goes
away when Slack no longer has the save (a refresh sees that), or when a retry
marks it.
A save Slack no longer has isn't tried again.

When the record has the action an undo reverses, the undo names it and is
refused unless that action succeeded, is on the same conversation and hasn't
been undone already. An undo of something from before the record existed just
runs.

## Typing into agent sessions

**This applies in Pro mode only.** In the default mode Assistant types into
sessions exactly as it did before this record existed: nothing is recorded or
refused, there is no loop limit or hold, no task is parked, any member of a
configured Slack channel can still have a message typed into the Slack session,
and the dashboard's "run now" still types what a proposal file says, into
whatever session it names. Everything below describes Pro mode. The mode is read
once, when a typing begins: a typing begun in Pro mode still gets its result
line if the mode changes before it ends, and one begun in the default mode
writes nothing.

Agents that Assistant starts run with their permission prompts off and act on
whatever is typed into them, and typing can't be taken back. So nothing in
Assistant's own code types into a session until the line saying so is on
disk. One module, `src/assistant/session_effects.py`, is the only place that
sends keystrokes, and a test fails the build if any other Python file under
`bin/`, `src/`, `work_queue/` or `hooks/` does. Three things that type
without going through it aren't recorded; they're listed at the end of this
section.

Each typing is one action with the session (`workspace:N`), the task when
there is one, and a short fixed name for where the text came from. It never
holds the text, a prompt's file name, a title or a Slack message.

| What is typed | Where it comes from | What allowed it |
|---|---|---|
| The instruction that starts a task, and the folder-trust answer before it | `task`, `trust` | The task's auto-start flag, as your own (a Work queue send, your Slack reaction, `/todo`) or as a standing setting (the goals planner, a policy rule, or anything not listed) |
| The "stalled" nudge, and `/cleanup` | `nudge:stranded`, `cleanup` | Pro mode, the Observer and the watcher being on, and actions not held |
| A merge request (`/merge-when-ready`, `/monitor-ffp-ci`) | `merge` | The same standing setting when the pulse sends it; a button press when you run the script |
| A message in the Slack session | `slack-message`, and `submit` for pressing Enter again | The Slack message (your own, when Assistant knows which member is you), as of its time |
| The Slack session's start-up instruction, trust answer and `/clear` | `boot`, `trust`, `clear` | The Slack channel being set up and its service running |
| "Run now" on a proposal | `run-now` | A button press on that route |
| `cmux-restore-sessions` | `restore` | You running it |
| `bin/cmux-send.py` run by hand | `cli` | You running it |

What the record does for you:

- **No record, no typing.** When the line can't be written, nothing is typed.
  Before Assistant starts a session for a task, it checks that it can write
  to the record (it writes and flushes a small file), and if it can't it
  starts nothing: the task stays open, and the Tasks tab shows "Dispatch
  paused — can't record what it types" with what to check (the disk, and the
  permissions of `~/.assistant/effects`). If the record fails after a session
  was started, or cmux refuses the instruction, the task is parked (blocked,
  with the workspace named on the card), so the next pulse doesn't start
  another; the session is left for you to close, and "Queue to start" on the
  task tries it again.
  A nudge or `/cleanup` is skipped that round, a Slack message stays queued,
  and `cmux-send.py` exits 4.
- **A loop is stopped.** The same thing typed into the same session more than
  three times in thirty minutes is refused, and the log says it tries again
  once the oldest of them is 30 minutes old. Typings that failed don't count;
  ones nobody could tell about do. Each task and each Slack message counts as
  its own thing. A task whose id the record can't hold (one that Assistant
  made itself, such as `td-auto-1a2b3c4d`) is recorded without the task, and is
  still typed into. For `bin/cmux-send.py`, "the same thing" is everything sent
  with the same `--source`, because the record never holds the text. The three
  sources whose text a person or an outside script chooses each time (a send
  with no `--source`, "run now" and the merge request) are the exception: the
  record also holds a keyed digest of the text (16 characters, made with a
  secret kept on this Mac; the text never reaches the record), so for them
  "the same thing" is the same text into the same session, and four different
  texts inside thirty minutes are all typed. Typings recorded before this
  change have no digest and never match one that has.
- **Typing that may have landed isn't repeated by Assistant on its own.** The
  result is `done` when the text and Enter both went out and nothing says they
  didn't land, `failed` when nothing went out, and `unknown` when the text went
  out and Enter didn't, the transcript was checked and didn't show it, or
  Assistant can't tell (a time-out). A message you send in Slack is typed once,
  whatever was typed before it. Everything else Assistant types, including a
  script that you or an agent runs (`cmux-send.py`, the merge script, "run now",
  restore), is limited to three of the same thing into one session in thirty
  minutes, and is not typed again for thirty minutes after a typing whose
  outcome isn't known. For a script, "run now" and the merge request, that
  means the same text: a different text into the same session is typed. No option or flag overrides this: a script can't tell a
  person from an agent, and you can always type into the session yourself. The
  page tells you what happened (for a time-out: "Assistant sent this but didn't
  hear back, so it may have been typed. Check the session before you send it
  again."). Two more cases:
  - a task's instruction is not held by an earlier `unknown`: each dispatch
    starts a new session, so there is nothing earlier in it to repeat. (The
    ledger's own rule would hold it, but its authorization is "now".)
  - the second and later Enter presses for one Slack message count like
    automatic typings, so they stop at the loop limit, and the message then gets
    the "couldn't confirm" reply.
- **A Slack message is never typed twice, and it is dropped only with a word
  to you.** If it was typed but never seen in the session's transcript, and its
  text is still sitting in the prompt box, only Enter is pressed again, as its own
  recorded typing (`submit`), and only while a fresh look at the box shows that
  message's own marker (a collapsed paste doesn't count), so text you are typing
  yourself is never submitted. If it landed since, that is all. Otherwise
  nothing is pressed and it leaves the queue, and Assistant replies in its thread: "I couldn't confirm
  your message (sent HH:MM) reached the session, and I won't type it twice.
  Check the session, or send it again." Messages that waited three hours get a
  note too. Each message in a batch typed together has its own record.
- **Only your Slack messages are typed, when Assistant knows which member is
  you.** A message from someone else in the channel is then kept as received
  and never typed, and Assistant says so in its thread once a day for each
  author. When it can't tell which account is yours and the target is a
  channel, every message there is typed, as before, and the Slack check in
  `assistant-doctor.py` warns. See
  [assistant-comms-onboarding.md](assistant-comms-onboarding.md).
- **"Run now" is stricter.** The session must be a `workspace:N`, and the text
  one line of at most 500 characters with no control characters. Nothing in
  this repository's dashboard page calls the route today; only a tool that
  writes proposal files and posts to it would.
- **A typing refused before anything was sent leaves no row in
  `~/.assistant/sends.jsonl`**, which the pulse reads to tell whether its last
  send landed.

### What the fence does and doesn't stop

A test fails the build if an ordinary call that types into a session is added
anywhere but the one module: the cmux method names, a `cmux send` command, an
f-string that runs a variable's `send`, and calls whose method isn't a literal.
It stops a call added by mistake. It doesn't stop a call someone hides on
purpose (a verb built at run time, a raw socket to cmux), and it doesn't see
anything that isn't Python.

### What the record can't prove

The record says what Assistant did and what it said allowed it; it can't check
either. `bin/cmux-send.py` trusts the `--source` and `--auth-kind` its caller
gives it (anything that can run the script can claim a standing setting). An `owner_flag` on a task made with the `/todo` skill is whatever the skill's caller said: its source defaults to `manual:` and it accepts any other, and agents run it, so an agent can produce one, the
text typed is whatever the caller passed, and cmux, not Assistant, says which
terminal belongs to which session number. An entry's `verified` is `true` only
when the session's transcript showed the text arrive.

Between reading the prompt box and pressing Enter there is one cmux call's
worth of time in which your own keystrokes can land, so text you are typing
yourself could still be submitted. That can't be closed from outside cmux.

### Not recorded yet

Three ways of typing into a session don't pass through this record, because
they run outside Assistant's own Python code:

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
