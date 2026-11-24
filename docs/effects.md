# The effects record

An effect is anything Assistant changes outside itself: archiving an email,
moving a conversation to Trash, sending an unsubscribe request. Assistant
writes every effect it makes to one record, `~/.assistant/effects/effects.jsonl`,
before it acts and again when it finishes.

Today the Work queue's Gmail buttons (**Archive**, **Mark read**, and moving a
conversation back to your inbox), **Apply approvals** for Gmail Trash, and
unsubscribes are recorded. Slack saves and typing into agent sessions follow.

## What it promises

- **Recorded first.** Assistant writes a line saying what it is about to do
  and waits until it's on disk. When it can't, it doesn't do the action. The
  page says "Assistant couldn't record this action, so it didn't run it."
- **Approved first, when it can't be undone.** Each kind of action says
  whether it can be undone and what approval it needs. An unsubscribe can't be
  undone, so it needs an approval you gave on the page. A button press, or a
  rule, doesn't count. Once an unsubscribe has gone through, it isn't sent
  again unless you approve it again afterwards.
- **No double runs.** A second press while the first is still working is
  turned away ("This is already in progress"). A line with no result that's
  more than ten minutes old stops counting, so a crash never locks a button.
- **Only ever added to.** Lines are appended. Nothing rewrites or deletes one,
  and the file is never trimmed automatically.
- **Pointers, not content.** A line holds ids: the Gmail address you linked
  (it's needed to undo), a conversation or message id, a short code for the
  Work queue row, and the host name of an unsubscribe link. It never holds a
  subject, a sender's name, message text or the unsubscribe link itself, which
  works like a password. Every line is also run through the same token
  scrubber as the logs. A failure is recorded as a kind (`timeout`,
  `network`, `tool failed`, `http 500`), never as the server's own words.
- **Private.** The folder is readable only by you, and so is the file.

## What the file looks like

One JSON object per line. An action writes two lines with the same id:

```
{"v":1,"kind":"intent","id":"ef-0123456789abcdef","at":"2026-11-02T10:00:10Z","t":1793613610.0,"class":"gmail.archive","target":{"account":"me@example.com","thread":"18c3f0a1b2"},"auth":{"kind":"tap","ref":"/api/gmail","at":"2026-11-02T10:00:10Z"},"trace":"","component":"work-queue","undoes":""}
{"v":1,"kind":"result","id":"ef-0123456789abcdef","at":"2026-11-02T10:00:11Z","t":1793613611.0,"outcome":"done","verified":true,"error":""}
```

`verified` is `true` when Gmail confirmed the change, `false` when it showed
something else, and `null` when it couldn't be checked. An intent with no
result is an action whose outcome is unknown, for example because Assistant
stopped partway.

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

An undo names the action it reverses, and is refused unless that action
succeeded, is on the same conversation, and hasn't been undone already.
