# Telemetry

Assistant keeps a local record of what it did, so later features can follow one piece of work from the moment it arrives to your decision on it. Nothing in it leaves your Mac, and nothing in it shows on a page yet.

## What is recorded

Each record says that something happened: which kind of event, when, a number that ties together the records about one piece of work, and a few numbers or short states. The kinds are: a source was fetched, a row was sorted, a model was called, a task changed state, a session was started, a check ran, an action took effect, you gave feedback, a guard decided, a job ran, a sign-in happened. A record holds:

- a number that ties together the records about one piece of work;
- a short id for the thing it is about, such as `row:` and 16 letters and digits for a Work queue row, or `td-` and a number for a TODO;
- the part of Assistant that wrote it, such as `pulse`;
- numbers, yes or no values, and short states such as `done` or `timeout`.

## What is never recorded

Assistant records ids, counts, timings and states, never message text, titles, prompts or what a model said. A state or an outcome must be one of a short fixed list. An id must start with a known prefix, such as `td-` for a TODO. A few open names (a model's id, a job's name, the kind of error) are checked by their shape: no spaces, no links, no addresses, no long numbers. Anything that looks like a secret is refused.

A refused value is left out, and the record says how many were left out. A Work queue row is recorded by an id made from its link, never by the link.

## Where it lives

`~/.assistant/telemetry/`. Only your Mac user account can read these files. Time Machine and other backups of your home folder include this folder.

- `telemetry.db`: the records. While it is open, SQLite keeps two working files beside it, `telemetry.db-wal` and `telemetry.db-shm`.
- `spool.jsonl`: a waiting list of records that couldn't be saved at that moment, for example because the database was busy. The hourly upkeep moves them in. It stops growing at 5 MB; later records are dropped and counted.
- `dropped.json`: how many records were lost, and why.
- `telemetry.db.corrupt-...`: a damaged database Assistant set aside. Its records are not read again. The two newest are kept.
- `upkeep-at`, `spool.lock`, `dropped.lock`, `recover.lock`: bookkeeping.
- `~/.assistant/telemetry-config-note-at`: the time Assistant last mentioned in the pulse log that `config.json` couldn't be read. It sits outside the telemetry folder and can be deleted.

The pulse writes its notes about telemetry to `~/.assistant/assistant-pulse.log`.

## Settings

In `~/.assistant/config.json`:

```json
{"telemetry": {"enabled": true, "keep_days": 90, "max_mb": 200}}
```

All three are optional. It keeps 90 days and up to about 200 MB, deleting the oldest records first. `keep_days` can be 1 to 3650 and `max_mb` 1 to 100000. A value outside that, or one that isn't a number, deletes nothing: Assistant keeps recording, skips the deletion that setting controls, and the pulse log says so.

If the pulse log says records were lost, the database was busy or full for a while; `dropped.json` shows the counts. If it says the database was set aside, a damaged file was replaced with a new one.

## Turn it off, or delete it

Set `{"telemetry": {"enabled": false}}`. A value other than `true` turns it off. If `config.json` can't be read at all (a missing comma, say), recording stops until the file is fixed, and the pulse log says so. When recording is off, Assistant writes nothing and creates nothing.

Turning recording off does not delete what is already there. To delete it, remove the folder: `rm -r ~/.assistant/telemetry`. Assistant starts a new one if recording is on.
