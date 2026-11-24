# Feedback

When you tell Assistant something about the Work queue, it writes one line
down. This page says what it writes, what it never writes, where the file is,
how to read it, and how to delete it. Nothing in it is sent anywhere.

Assistant only records this today. It doesn't use it yet; that's planned for the
next update, and the records are written so that it can.

It is a Pro mode feature, because the Work queue is. In default mode nothing is
recorded.

## What is recorded, and when

One line each time you:

| You do this | It records |
|---|---|
| Select **Done**, **Snooze** or **Drop** on a row or a Start here item | done, snooze, or drop |
| Select the same button again to clear it | undo |
| Select **Send to Agent**, on a row, an email or the **Send to agents** tab | send (or send hands-off, when you pick Hands-off) |
| Select **Done** on a Ready for you result | you cleared a result |
| Select **Approve**, **Keep** or **Undo** on a row waiting for approval, or **Approve all** (one line per row) | approve, keep, or undo |
| Select **Archive**, **Mark read** or **Undo** on a Gmail email | archive, mark read, or undo |
| Select **Copy draft** | you used the draft |
| Tap the thumbs-down on an item; then, if you like, **Add a reason** (or **Change** it later) | wrong, and its reason |
| Tap the thumbs-down again, or select **Undo**, on what you said | taking it back |
| Send words with **Tell Assistant**, on an item or on the page | the words |

Nothing is ever edited. A new reason is a new line that replaces the old one on
the page, and taking something back is a new line that names the one it takes
back.

### Which items have the controls

The thumbs-down and **Tell Assistant** are on:

- every row of the Open list, and every Start here item;
- every email in the Email tab;
- every model draft (marked **Draft**, with **Tell Assistant about this draft**);
- every Ready for you result;
- every row waiting for your approval (**Ready to delete**, **Ready to close**,
  and the unsubscribe rows). These take the thumbs-down and **Tell Assistant**
  but offer no reasons.

**Tell Assistant about this page**, at the top of the page, is about the page as
a whole.

The reasons on offer are: **Not important**, **Wrong order**, **Not mine**,
**Already done** (rows, Start here items and emails); **Wrong tone**, **Wrong
facts**, **Not how I'd say it** (drafts); **Not what I asked**, **Didn't
finish**, **Should not have been started** (results).

A line is written after the action has worked, and it is on disk before the
page says it is noted. If it can't be written, the page says so, and the action
you took still happens exactly as it did before. Feedback never holds up your
work.

## What a line holds

A time, what you did, what kind of thing it was about (a row, a Start here
item, an email, a draft, a result, or the page), and the small pointers that
say which one:

- the item's pointer, such as `row:3f2a9c41d8e07b65`, or its task id, such as
  `td-051`. A pointer is a short keyed hash. It lets Assistant recognise the
  same item or sender again; it does not hold their text or name.
- the item's trace id, which ties it to the same piece of work elsewhere in
  Assistant's records.
- where it came from (Slack, GitHub, Gmail, Outlook) and a keyed hash of the
  sender (`who:` and 16 characters), so Assistant can later tell "mail from the
  same sender" without knowing who it is. The key is a random secret made once
  for your install, in `~/.assistant/feedback/sender.key`, that only you can
  read, so nobody can match the hashes against a list of names. If you delete
  that key, a new one is made, and old and new sender pointers stop matching
  each other. A key that is empty or
  shorter than 32 bytes is treated as no key: no sender pointer is written until
  it is deleted and made again.
- a reason you tapped, and the id of the line it belongs to, when it is a reason
  or a take-back.
- the words you typed, only when you used **Tell Assistant**. Your own words are
  kept as you typed them, apart from anything that looks like a password or
  token, and cut to 2,000 characters.

Apart from your own words, it never holds a title, a sender's name, an email
address, message text, a link, or anything a model wrote.

Each line also sends one record to the local record described in
[Telemetry](telemetry.md), with the same fields and never your words. That
record is sent after the page has its answer, and it can be delayed or dropped
when the Mac is busy; the line in the feedback file is not.

If a save fails after the line was written, the typed words can remain in the
file, followed by a line that cancels them, and the page says it couldn't save.
Trying again writes a new line; the cancelled one does not count.

## Where it lives

`~/.assistant/feedback/feedback.jsonl`, one JSON object per line, in a folder
only you can open. Lines are only ever added. Taking something back adds a new
line that names the old one; the old one stays. The file only grows. Delete it
whenever you like.

Assistant reads each line once, so a long file doesn't slow it down. Beside it are
`sender.key` (described above) and `feedback.lock`, which has no content.
Each row's source and sender hash, which the refresh saves so a line can carry
them, are in `rowctx.json` in the Work queue's data folder, also only for you.

## Read it

```
tail -n 20 ~/.assistant/feedback/feedback.jsonl
```

For example, a thumbs-down and the reason you tapped after it:

```
{"id":"fb-1a2b3c4d5e6f7a8b","ts":"2026-11-24T10:00:00Z","signal":"thumbs_down","kind":"row","surface":"work-queue","subject":"row:3f2a9c41d8e07b65","trace":"tr-0123456789abcdef","context":{"source":"slack","who":"who:9c1e5d7a2b4f6038"}}
{"id":"fb-8b7a6f5e4d3c2b1a","ts":"2026-11-24T10:00:02Z","signal":"thumbs_down","kind":"row","surface":"work-queue","subject":"row:3f2a9c41d8e07b65","trace":"tr-0123456789abcdef","reason":"not_mine","amends":"fb-1a2b3c4d5e6f7a8b","context":{"source":"slack","who":"who:9c1e5d7a2b4f6038"}}
```

## Delete it

```
rm -r ~/.assistant/feedback
```

Assistant starts a new file the next time you tell it something. Deleting it
changes nothing else. Deleting the folder also deletes `sender.key`, so sender
pointers written afterwards don't match the older ones. To keep them matching,
delete only `feedback.jsonl`.
