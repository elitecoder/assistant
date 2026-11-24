# Feedback

When you tell Assistant something about the Work queue, it writes one line
down. This page says what it writes, what it never writes, where the file is,
how to read it, and how to delete it. Nothing in it is sent anywhere.

Assistant only records this today. It does not yet change what it does because
of it; that comes in the next update, and the records are written so that it
can.

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
| Tap the thumbs-down on an item, then a reason, or tap it again | wrong, its reason, or taking it back |
| Send words with **Tell Assistant**, on an item or on the page | the words |

A line is written after the action has worked, and it is on disk before the
page says it is noted. If it can't be written, the page says so, and the action
you took still happens exactly as it did before. Feedback never holds up your
work.

## What a line holds

A time, what you did, what kind of thing it was about (a row, a Start here
item, an email, a draft, a result, or the page), and the small pointers that
say which one:

- the item's pointer, such as `row:3f2a9c41d8e07b65`, or its task id, such as
  `td-051`. A pointer is a short hash. It is not the item's title, link or text,
  and it can't be turned back into them.
- the item's trace id, which ties it to the same piece of work elsewhere in
  Assistant's records.
- where it came from (Slack, GitHub, Gmail, Outlook) and a hash of the sender,
  so Assistant can later tell "mail from the same sender" without knowing who
  it is.
- a reason you tapped, and the id of the line it belongs to, when it is a reason
  or a take-back.
- the words you typed, only when you used **Tell Assistant**. They are cut to
  2,000 characters and scrubbed of anything shaped like a password or token.

It never holds a title, a sender's name, an email address, message text, a link,
or anything a model wrote.

Each line also adds one record to the local record described in
[Telemetry](telemetry.md), with the same fields and never your words.

## Where it lives

`~/.assistant/feedback/feedback.jsonl`, one JSON object per line, in a folder
only you can open. Lines are only ever added. Taking something back adds a new
line that names the old one; the old one stays.

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
changes nothing else.
