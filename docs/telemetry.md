# Telemetry

Assistant keeps a local record of what it did, so later features can follow one piece of work from the moment it arrives to your decision on it. Nothing in it leaves your Mac, and nothing in it shows on a page yet.

## What is recorded

Each record says that something happened: which kind of event, when, which piece of work it belongs to, and a few numbers or short states. A record holds ids, counts, timings, states and money. It is made of:

- the kind: a source was fetched, a row was sorted, a model was called, a task changed state, a session was started, a check ran, an action took effect, you gave feedback, a guard decided, a job ran, a sign-in happened;
- a trace id, which names one piece of work across all of Assistant's stores;
- a pointer to the thing it is about, such as `row:` and 16 letters and digits for a Work queue row;
- the part of Assistant that wrote it, such as `pulse`;
- numbers, yes or no values, and short states such as `open` or `timeout`.

## What is never recorded

Never message text, a title, a prompt, a model's reason or summary, an email address, or a link. The store enforces this itself: each kind lists the fields it accepts, and a value must be a number, a yes or no, or a short id-like word with no spaces and no `//`. Anything else is left out, and the record counts how many were left out (`_dropped`). A Work queue row is recorded by a pointer made from its link, never by the link. Every word also passes through the same secret cleaner the logs use, and a word that looks like a token is left out.

## Where it lives

`~/.assistant/telemetry/`, readable only by you (the folder is `0700`, the files `0600`):

- `telemetry.db`: the records.
- `spool.jsonl`: records that couldn't be saved at that moment, for example because the database was busy. The hourly upkeep moves them in. It stops growing at 5 MB; later records are dropped and counted in `dropped.json`.
- `telemetry.db.corrupt-...`: a damaged database, set aside when Assistant starts a new one. The two newest are kept.

## Settings

In `~/.assistant/config.json`:

```json
{"telemetry": {"enabled": true, "keep_days": 90, "max_mb": 200}}
```

All three are optional. `keep_days` is how long a record is kept; `max_mb` is the size the database is kept under, oldest records first. A setting Assistant can't read never deletes anything: it keeps recording with the defaults, and the hourly upkeep skips the deletion that setting controls.

## Turn it off, or delete it

Set `{"telemetry": {"enabled": false}}`. Assistant then writes nothing and creates nothing. To delete what is already there, remove the folder: `rm -r ~/.assistant/telemetry`. Assistant starts a new one if recording is on.
