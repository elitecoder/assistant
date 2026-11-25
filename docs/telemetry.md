# Telemetry

Assistant keeps a local record of what it did, so later features can follow one piece of work from the moment it arrives to your decision on it. Nothing in it leaves your Mac, and nothing in it shows on a page yet.

## What is recorded

Each record says that something happened: which kind of event, when, a number that ties together the records about one piece of work, and a few numbers or short states. The kinds are: a source was fetched, a row was sorted, a model was called, a task changed state, a session was started, a check ran, an action took effect, you gave feedback, a guard decided, a job ran, a sign-in happened. A record holds:

- a number that ties together the records about one piece of work;
- a short id for the thing it is about, such as `row:` and 16 letters and digits for a Work queue row, or `td-` and a number for a TODO;
- the part of Assistant that wrote it, such as `pulse`;
- numbers, yes or no values, and short states such as `done` or `timeout`.

## What is recorded for a piece of work

Each step a Work queue row goes through leaves a record with the same trace (the number that ties together the records about one piece of work), so you can follow it from arrival to done:

1. **A refresh runs.** One record for the run (its name, whether the refresh itself finished, how long, how many rows). It is marked ok even when every model call failed: the model calls and the sources carry their own outcomes. There is also one for each source it read (which source, how many rows, how long, and, if it failed, the kind of error and a one-word reason: `not_signed_in`, `tool_missing`, `timeout`, `bad_answer`, `busy` or `other`). They carry a trace of their own, for the run.
2. **The model sorts a row.** One record for each row that gets a new verdict: the row, its trace, the verdict and lane, the model, which version of the instructions it was given, the trace of the run, and which of the run's model calls made it (`batch`). A verdict that is reused has no record. A row the run put to the model that ends it with no verdict gets a record too, with the verdict `unsorted` and a reason: `call_failed` (the model call failed), `bad_answer` (it answered in a shape Assistant can't use), `not_answered` (the answer left that row out), `out_of_time` (the refresh's time ran out), `spend_limit` (a daily spend limit was reached, or the Work queue's own budget couldn't cover another call), or `no_model` (the model command isn't installed). A row is recorded as unsorted at most once a day for each reason, however often you refresh. Rows beyond the 120 a refresh sorts at a time are not put to the model and have no record: they wait for the next refresh and the page counts them as waiting.
3. **Any model call.** One record for each call: the model, its tier, what it was for, how it ended, tokens, time, and cost, which is the same estimate the spend file holds, including for a call that failed. A call that failed also has a one-word reason from the same list a source fetch uses, when the code knows the kind without reading the tool's output (`timeout`, `tool_missing`, or `bad_answer` for an answer it couldn't use), and `other` otherwise: a call that failed because it wasn't signed in or was rate limited isn't told apart. A call about a batch of rows carries the run's trace. Each line in `~/.assistant/cost-ledger.jsonl` also has the trace in a new field, `trace`, which is empty when the call served no piece of work.
4. **You send a row.** A task record: the row's trace, the task's id, that it is open, where it came from, and whether you chose `assist` (first pass) or `handsoff`. A task that arrives some other way (the planner, a policy, the Slack reactor, a skill, or an edit by hand) gets the same record when it is created, or the first time the pulse stamps it.
5. **An agent is started.** A dispatch record (how it ended, the workspace, how long, the trace of the pulse that tried it) and, when it worked, a task record for the task's new state. If it fails, only the dispatch record is written, with `failed` and a one-word reason (`session_tool_down`, `no_workspace`, `no_surface`, `session_not_ready`, `todo_not_marked`, `typing_refused` (Assistant wouldn't type, because it couldn't record the typing or didn't recognise the session. If the session was already started the task is parked; if the record couldn't be written beforehand, nothing is started and the task stays open), `session_refused_text` (the session was started but cmux refused the instruction, so nothing was typed; the task is parked) (both written only in Pro mode), or `other`), and the task is left as it was, except that a task whose session was started but couldn't be typed into is stamped blocked (parked) with the workspace named. A dispatch that went out but whose prompt Assistant couldn't see arrive keeps `ok` true and has `confirmed` false.
6. **The agent reports back.** A task record: `done` or `blocked`, and the state before.
7. **You remove or re-queue a task on the dashboard.** A task record with the state the task has afterwards. A re-queue that changes nothing records nothing.
8. **A finished result is checked.** A check record, written only when a check clears or unblocks a result: which check (for example `pr-merged` or `unblocked`). A blocker that looks gone is recorded the first time only.
9. **Assistant changes something in your mail.** An effect record when the change is finished: the kind of change, how it ended, and the row, task or session it acted on, when it has one. The effects ledger itself works without it.
10. **A pulse runs.** One record for the pulse, with a trace of its own, how long it took and how many tasks it started, written only when the pulse dispatched or stamped something, failed, or ran longer than 60 seconds. A pulse with nothing to show writes nothing; the heartbeat file (`~/.assistant/heartbeat.json`) already shows it is alive.

To see everything recorded about one row, find its pointer (`row:` and 16 letters and digits made from its link; for an email, from the email's own key) and ask for the records that share its trace:

```
row="row:$(printf '%s' 'https://github.com/o/r/pull/7' | shasum -a 256 | cut -c1-16)"
sqlite3 ~/.assistant/telemetry/telemetry.db "select datetime(ts,'unixepoch','localtime'), kind, subject, fields from events where trace in (select trace from events where subject = '$row') order by ts, seq"
```

If the row was never sorted, you see a `triage_verdict` with `"verdict":"unsorted"` and the reason, and its `run_trace`. Ask for that trace (copy the `run_trace` value from the verdict) to see the run: its sources, its model calls (a failed call says how it ended, and its `batch` matches the `batch` on the verdict) and its job record:

```
sqlite3 ~/.assistant/telemetry/telemetry.db "select datetime(ts,'unixepoch','localtime'), kind, fields from events where trace = 'tr-...' order by ts, seq"
```

Recording never changes what Assistant does: if a record can't be made, only that record is lost.

### How much room it takes

A record takes about 400 bytes at most. The main source is sorting: a refresh writes one record for each row it sorts, so a heavy day of sorting is a few hundred records, around 100 KB. The limits drop the oldest records first: anything older than `keep_days` goes, and then the oldest while the database is over `max_mb`.

## What is never recorded

Assistant records ids, counts, timings and states, never message text, titles, prompts or what a model said. A state or an outcome must be one of a short fixed list. An id must start with a known prefix, such as `td-` for a TODO. A few open names (a model's id, a job's name, the kind of error) are checked by their shape: no spaces, no links, no addresses, no long numbers. Anything that looks like a secret is refused.

A refused value is left out, and the record says how many were left out. A Work queue row is recorded by an id made from its link, never by the link.

## Where it lives

`~/.assistant/telemetry/`. Only your Mac user account can read these files. Time Machine and other backups of your home folder include this folder.

A pulse with nothing to do writes no record; check the heartbeat file (`~/.assistant/heartbeat.json`) to see when the pulse last ran.

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
