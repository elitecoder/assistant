# Telemetry

Assistant keeps a local record of what it did, so later features can follow one piece of work from the moment it arrives to your decision on it. Nothing in it leaves your Mac, and nothing in it shows on a page yet.

## What is recorded

Each record says that something happened: which kind of event, when, a number that ties together the records about one piece of work, and a few numbers or short states. The kinds are: a source was fetched, a row was sorted, a model was called, a task changed state, a session was started, a check ran, an action took effect, you gave feedback, a guard decided, a job ran, a sign-in happened. A record holds:

- a number that ties together the records about one piece of work;
- a short id for the thing it is about, such as `row:` and 16 letters and digits for a Work queue row, or `td-` and a number for a TODO;
- the part of Assistant that wrote it, such as `pulse`;
- numbers, yes or no values, and short states such as `done` or `timeout`.

## What is recorded for a piece of work

Each step a Work queue row goes through leaves a record with the same trace (the number that ties the records about one piece of work together), so you can follow it from arrival to done:

1. **A refresh runs.** One record for the run (its name, whether it went well, how long, how many rows) and one for each source it read (which source, how many rows, how long, and the kind of error if it failed). They carry a trace of their own, for the run.
2. **The model sorts a row.** One record for each row that gets a new verdict: the row, its trace, the verdict and lane, the model, which version of the instructions it was given, and the trace of the run, so the verdict can be tied to the call that made it. A verdict that is reused has no record.
3. **Any model call.** One record for each call: the model, its tier, what it was for, how it ended, tokens, cost and time. A call about a batch of rows carries the run's trace. Each line in `~/.assistant/cost-ledger.jsonl` also has the same trace in a new field, `trace`, which is empty when the call served no piece of work.
4. **You send a row.** A task record: the row's trace, the task's id, that it is open, where it came from and whether you chose first pass or hands-off. A task that arrives some other way (the planner, a policy, the Slack reactor, a skill, or an edit by hand) gets the same record when it is created, or the first time the pulse stamps it.
5. **An agent is started.** A dispatch record (how it ended, the workspace, how long) and a task record for the task's new state.
6. **The agent reports back.** A task record: `done` or `blocked`, and the state before.
7. **You remove or re-queue a task on the dashboard.** A task record with the new state.
8. **A finished result is checked.** A check record: which check (for example `pr-merged`) and how it ended.
9. **Assistant changes something in your mail.** An effect record when the change is finished: the kind of change, how it ended, and the row it acted on. The effects ledger itself works without it.
10. **A pulse runs.** One record for the pulse, with a trace of its own, how long it took and how many tasks it started.

To see everything recorded about one row, find its pointer (`row:` and 16 letters and digits made from its link) and ask for the records that share its trace:

```
row="row:$(printf '%s' 'https://github.com/o/r/pull/7' | shasum -a 256 | cut -c1-16)"
sqlite3 ~/.assistant/telemetry/telemetry.db "select datetime(ts,'unixepoch','localtime'), kind, subject, fields from events where trace in (select trace from events where subject = '$row') order by ts, seq"
```

The sorting record's `run_trace` field is the trace of the refresh that read the row; ask for `where trace = 'tr-...'` with that value to see the run, its sources and its model calls. Recording never changes what Assistant does: if a record can't be made, only that record is lost.

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
