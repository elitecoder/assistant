# Changelog

All notable changes to the Assistant daemon are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is carried in `pyproject.toml` and `src/assistant/__init__.py`
(`__version__`); keep the two in sync when bumping.

## [Unreleased]

### Added
- A pause mid-sentence no longer cuts your question off. Assistant still ends
  a question 2.5 seconds after you stop talking, but 0.2 seconds into each
  pause it asks Pipecat's Smart Turn v3.2 (BSD 2-Clause) whether you've
  finished, and when it says you haven't (below 0.5), Assistant waits up to 4
  seconds of silence. Silero VAD (MIT) notices when you stop talking; both run
  on your Mac with ONNX Runtime. Smart Turn never ends a question early. On 40
  questions it wasn't tuned on (clean, and with brown, pink, fan, and babble
  noise at 10 to 25 dB), ending early cut off 1 to 3 paused questions in every
  condition but babble, even with the recognizer's punctuation and last word
  as a second check. What shipped cut off none of them and none of 32 more,
  where the 2.5-second timer cut off 2 or 3 in six of nine conditions; it held 0 to 4
  of 20 finished questions longer than the timer. When noise sounds like
  speech to the voice detector, the timer after your last recognized word
  still ends the question. The models (35 MB) download once into
  `~/Library/Application Support/Assistant/SmartTurn` and are checked by
  SHA-256; until then, and if they can't load, only the timer decides.
  `AssistantBar --turn-eval <file.wav>...` prints the timer's decision, the
  shipped one, and early ending's for each file.
- Talk Mode: select the waveform button, hold Control-Option-V, or start the
  app with `--talk`, and Assistant listens, answers, speaks, and listens again
  until you select **Stop** or press Esc. While it speaks, the microphone stays
  open with Apple's voice processing, which cancels Assistant's own voice, only
  to notice you talking over the answer: half a second of voice above the
  room's noise stops the answer, drops the rest of it, and asks your next
  question as soon as the interrupted answer is saved, while a cough or an
  "mm-hmm" doesn't. **Stop speaking**, **Stop**, and Esc also drop the rest of
  an answer that's still streaming. Nothing is transcribed while Assistant speaks. A turn
  with no question ends Talk Mode.
- A spoken answer starts on its first sentence instead of waiting for the
  whole reply. Assistant runs Claude with `--include-partial-messages` and
  speaks each sentence as it's written; the stored chat and the reply are
  unchanged. On five real questions, the first audio came 0.8 to 1.3 seconds
  sooner (for example 3.9 seconds instead of 5.2). Try it with
  `AssistantBar --ask "<question>" --spoken --say-to answer.wav`, which renders
  the answer to a file and prints both timings.
- Assistant for Mac transcribes your voice with Apple's SpeechAnalyzer and
  SpeechTranscriber on macOS 26 and later. On a test set of 32 spoken
  questions, half with room noise added, it got 25 of 444 words wrong, down
  from 69 with the older SFSpeechRecognizer. It no longer repeats words after
  a pause. Live text shows the model's guess, and words it has finalized stay
  put. If the speech model isn't installed, Assistant downloads it on first
  use and shows its progress, and uses the older recognizer until it's ready.
  On older macOS, or without the model, it keeps the older recognizer, now
  with your workspace titles and terms such as PR, Kokoro, and cmux as hints.
- A question you ask while an answer is running waits its turn: Assistant
  shows it, says it will ask it next, and asks it when the answer is done.
- If the microphone disconnects mid-question, Assistant stops listening, says
  so, and puts what it heard in the chat box for you to send or clear.
- Select **Play again**, or press Control-Option-R, to hear the last answer
  again with the current voice and speed. It starts over if Assistant is
  already speaking. Each answer in the chat also has a play button.
- `AssistantBar --transcribe <file.wav>` prints what voice input hears in a
  sound file, through the same SpeechAnalyzer path. Add `--legacy` to use the
  older recognizer.
- Pick a talkback speed from 1× to 2× in the voice menu. It applies to Kokoro
  and Apple's voices, and Assistant plays a short sample at the new speed.
- Assistant for Mac now keeps its own chat history, the way Hermes Agent (MIT,
  Nous Research) does, instead of resuming a Claude Code session. It saves each
  question, each command Claude runs, what the command printed, and each answer
  in `~/Library/Application Support/Assistant/conversations.db`, and reopens
  your last chat when the app starts. Select **History** to continue an earlier
  chat. **New chat** starts one linked to the last.
- Each question runs a fresh `claude -p` with the chat so far, oldest first,
  and the new question last, so each request extends the previous one word for
  word and reuses Claude's prompt cache. The first question sends every
  workspace, and later questions send only the workspaces that changed.
- Show how full the chat's context is, from the prompt size Claude reports. At
  150,000 tokens with Haiku or Sonnet (75% of a 200,000-token window), or
  500,000 with Opus (half of 1,000,000), Assistant summarizes older turns with
  Haiku before the next question and
  says how many messages it compressed. Select **Compress**, or **Compress with
  focus** to name what to keep in detail. Set `ASSISTANT_CONTEXT_WINDOW` or the
  `contextWindow` setting to try a smaller window.
- Let the chat's Claude search earlier chats, including compressed turns, with
  `AssistantBar --search`, and save lasting facts to `~/.assistant/mac-memory`
  with `AssistantBar --memory`. A chat reads memory once, when it starts.
- `AssistantBar --ask` takes `--session NAME` to continue a named chat, and
  `AssistantBar --compress --session NAME` compresses it. `ASSISTANT_DB` and
  `ASSISTANT_MEMORY_DIR` point the terminal commands at another history and
  memory folder.
- Sandbox the chat's Claude. Assistant runs the `claude` binary directly
  instead of your alias, and passes `--setting-sources ""`, so your allow list,
  hooks, and `CLAUDE.md` no longer reach the chat. A PreToolUse guard,
  `AssistantBar --pretooluse-guard`, allows only Assistant's own commands in
  their exact shapes, and Read, Grep, and Glob only under `~/.claude/projects`.
  Assistant runs a command only when it's the first argument, with an exact
  argument count, so `--search '{}' --render x /tmp/f` is refused. The
  summarizer also skips your settings and has no tools. Effort is now always
  explicit (high), since your settings' effort level no longer applies. Set
  `ASSISTANT_CLAUDE` if Assistant can't find `claude`.
- Save each question, and its workspace context, before Claude runs, so a crash
  or quitting mid-answer keeps it and any message Claude already sent. A failed
  answer is saved as a no-reply note. History hides failed questions.
- Compress before a turn when this turn's question and workspace context would
  push the chat past the limit, not only the stored turns. When Claude refuses
  a prompt as too long, compress and try once more.
- A compaction now reloads memory into the chat's system prompt, as Hermes
  does, so a fact saved mid-chat applies after the next compaction.
- When a compaction can't be saved, the chat keeps going uncompressed and
  automatic compression pauses for five minutes. The pause, and the pause after
  a failed summary, now hold across the app and terminal commands.
- The spoken reply style is sent only with the spoken question, not saved, so it
  no longer shapes later typed answers.

- Add Assistant for Mac, a menu bar app in `mac/` that lists the cmux sessions
  that need you, the ones that are done, and the quiet ones. It reads
  transcripts and `gh` directly, with no model and no background service.
  Select a row, or **Take me to the next one**, to switch cmux to that
  workspace. Open it with Control-Option-Space. It sends one notification per
  batch of newly waiting sessions and can open at login. Build it with
  `mac/build-app.sh`.
- Ask Assistant for Mac about your sessions. Type a question in the panel, or
  select a row's speech bubble to ask about that session. It answers through
  your `claude` command with read-only tools, keeps follow-ups in one chat,
  and shows each answer's cost. `AssistantBar --ask` asks from the terminal.
- Talk to Assistant for Mac. Press Control-Option-V and ask out loud. It
  stops listening when you pause, answers in a few spoken sentences, and
  reads the answer aloud. Speech recognition runs on your Mac.
- Show a live waveform and the microphone's name while Assistant for Mac
  listens. When it hears nothing, it names the microphone to check. When
  speech recognition fails, it names the setting to turn on.
- Pick the microphone Assistant for Mac listens with from the panel, without
  changing the system input. With no pick, it uses the system input, but
  skips the built-in microphone when the lid is closed and skips virtual
  devices.
- When the microphone sends pure silence, Assistant for Mac switches to the
  next one within a second and says so. The microphone menu also appears
  while it listens, so you can switch mid-question. It remembers your pick.
- Answer with the most natural voice installed: Premium, then Enhanced, then
  the system default. Pick another from the voice menu, which plays a short
  sample. Siri voices aren't offered, because macOS refuses them to apps.
- Speak with Kokoro, a natural neural voice built into Assistant for Mac. It
  runs on the Mac and costs nothing per answer. The 340 MB voice downloads once,
  the first time you talk, and its checksum is verified. Until it's ready,
  Assistant uses Apple's voice. Kokoro loads when it speaks and unloads after
  five idle minutes. Pick Heart, Bella, Michael, Emma, or George from the voice
  menu. Requires Apple Silicon and macOS 15. Building now needs Xcode's Metal
  Toolchain: `xcodebuild -downloadComponent MetalToolchain`.
- `AssistantBar --render "<text>" <file.wav>` saves Kokoro speech to a file.
- Let Assistant for Mac work with your sessions. It messages a workspace's
  Claude session through `cmux send`, confirms delivery, waits for the turn to
  finish, and reads the reply, so it can go back and forth until a request is
  done. The panel shows every message and reply, and **Stop** ends the run. It
  finds each session's pane exactly from the Claude process on that terminal,
  skips `/back-off` workspaces, and its only command access is its own
  `--sessions`, `--send`, and `--wait`.
- Answer with Claude Haiku at high effort by default instead of Opus. On 12 of
  the operator's real Slack questions, it scored 24 to 25 of 36 against 19 for
  Haiku at default effort and 21 for Sonnet, for about 2 cents a question
  against 15 for Sonnet. Status answers now list every session that waits on
  you, with the exact decision. Switch models from the panel's model menu.
- Add `evals/mac-chat/`, which asks the real questions on several setups for
  grading. The questions stay in `~/.assistant/evals/mac-chat/`.
- Fix garbled spots in Kokoro speech. Numbers, percentages, dollar amounts,
  version numbers, and PR numbers are now written as words before speaking,
  and long sentences are split at commas. Transcribing Kokoro output with
  Apple's recognizer confirmed digits were misread ("workspace 25" came out as
  "workspace 5") and "%", "$0.24", and "1.0.11" were dropped.
- Add Pro mode. Default mode keeps the Sessions, Session details, Slack, and
  Tasks tabs, Slack replies, Slack emoji to task, auto-started tasks, and the
  `/todo`, `/cleanup`, and `/spawn-claude-workspace` skills. Pro mode adds
  notifications and triage, goals and the planner, the Notifications,
  Decisions, Background work, and Connected apps tabs, and the `/goal`,
  `/attend`, `/back-off`, and `/lesson` skills. The watcher, strategist,
  lessons extractor, and brief narrator also need Pro mode, and connectors
  pause before their next check while it's off. Alerts that arrive while Pro
  mode is off are skipped, not saved for later. Switch modes in the dashboard's
  **Settings and background services**, or with `./install.sh --apply --pro`
  and `--no-pro`. The mode is saved as `"mode"` in `~/.assistant/config.json`.
  If that file can't be read, Assistant uses default mode and the dashboard
  shows the error next to the switch.
- **Upgrading:** every existing install stays in Pro mode, because earlier
  versions installed the Pro skills for everyone. Default mode is for new
  installs and for anyone who switches. An automatic update never removes
  skills. When the code of the dashboard server or a connector changes, an
  update restarts the ones that are running, so the switch works right away.
  If a restart fails, the dashboard says so and the hourly update retries.
- Install workspace numbers by default. The installer no longer asks and keeps
  a remembered "no". An automatic update never starts them if they aren't
  running. Install with `--no-ws-numberer` to turn them off and stop a running
  copy.

### Fixed
- Pressing the microphone or Control-Option-V during a spoken answer ends the
  run, as talking over it does, so your next question doesn't wait for it.
- After a barge-in or **Stop**, the chat keeps what the answer had already
  said, including text still streaming when it ended, above the note that says
  why, and Control-Option-R can replay it.
- Code fences in spoken answers follow CommonMark: a fence opens only on a line
  of its own and closes only with the same mark, at least as long, so a stray
  ```` ``` ```` in a sentence no longer drops the rest of the answer.
- The speed test uses its own settings store, and the rolling room-level test
  now fails if the level just follows the last buffer.
- Kokoro and Smart Turn files are hashed once per install. A checked file's
  size, modification time, and checksum are stamped next to it, so the first
  spoken answer after launch no longer hashes 340 MB; a changed file is hashed
  again, and one that fails its check downloads again right away.
- Talking over an answer ends only the chat's `claude` run. A compaction
  running beside it keeps going; **Stop** still ends both.
- Ending a run mid-`--send` (Stop, Quit, or talking over the answer) no
  longer leaves the message pasted without its Enter: `--send` holds off
  SIGTERM, SIGINT, and SIGHUP, in itself and in its two `cmux` calls, until
  both finish, then exits. A send still open when the run ends is stored as
  "Delivery unknown", and the chat's Claude checks with `--wait` before
  sending it again.
- Untrusted session text reaches the chat's Claude as one JSON string per
  block instead of quoted lines. Line breaks, `<`, `>`, `[`, `]`, anything
  whose compatibility form is one of those, and control and invisible
  characters are escaped, so no Unicode trick can start a line, form a tag, or
  forge a label inside a block. The old line-quoting code is gone.
- `--send` checks how a message starts with an allow list instead of a list of
  bad prefixes. After leading spaces, the first character must be a letter or
  digit, or one of `"`, `'`, `(`, or `@`, and none of the first three may be a
  combining mark or an invisible character. It reads Unicode scalars, so an
  accent or joiner after `!`, `#`, or `/` can no longer hide it.
- Kokoro's files are checked against their SHA-256 before the first load, and
  a damaged one downloads again. **Stop** reads the process table off the main
  thread, and it freezes the processes it ends first, so none can start another
  in between, then looks once more after ending them.
- Untrusted session text can no longer escape its block: every line break
  (CRLF, CR, U+2028, U+2029, next line, vertical tab, form feed) is normalized,
  labels after invisible or no-break characters are quoted, and block tags
  anywhere in a line are escaped. Within the turn, `--send`, `--wait`,
  `--sessions`, and `--search` print session text already marked, compaction
  summaries are marked too, and Read, Grep, and Glob are replaced by
  `--transcript <workspace>` and `--find '<json>'`, which print it marked.
- Every `--send` shows in the chat with its workspace and exact text, then its
  result: the reply, or that it wasn't sent, wasn't confirmed, or has no reply
  yet. Plain-language sends stay unconfirmed by design.
- `--send` refuses messages that start with `/` (a slash command), `!` (a
  shell command), or `#` (a memory note), even behind invisible or control
  characters, and says why, passes the message after `--`, checks the
  back-off list against the workspace a name resolves to, presses Enter only
  once (a message it can't confirm is reported, not resent), recognizes
  delivery of messages that start with `<`, and follows a session to its new
  transcript after `/clear` only when the new file's own entries carry the
  session ID its process started with, which Claude Code keeps stamping after
  `/clear`. A cleared file from another session, live or exited, is never
  followed. When it can't tell (no prompt since the `/clear` yet, or the
  session changed directory first), it keeps the named transcript and says so
  in the reply.
- Text from other sessions reaches the chat's Claude in marked blocks that a
  session can't close or use to fake one of your turns.
- Stopping an answer, or `claude` exiting before it read the chat, no longer
  kills the app with SIGPIPE; the turn's record, including any message
  already sent to another session, is kept.
- **Stop** and quitting now end every process Assistant started and what those
  started in turn: the chat's `claude`, the summarizer, and `--send` and
  `--wait`. A `claude` crash is recorded as a crash, not as a Stop, and the
  `cmux`, `ps`, `gh`, and `lsof` calls time out instead of hanging a scan.
- **Stop speaking**, **Stop**, Esc, and talking over an answer now stop a
  streamed answer completely: the sentences Claude hasn't written yet aren't
  read either. Talking over an answer also ends Claude's run and everything it
  started, as **Stop** does, even mid-command, and saves what it wrote with
  "Interrupted by voice.", so your new question is asked as soon as you finish
  it. Talk Mode no longer listens again before asking that question.
- **Stop** and Esc also drop the questions waiting for the answer they end, and
  pressing the microphone or Control-Option-V while an answer streams, even
  between its sentences, silences the rest of it.
- Talk Mode listens for interruptions only while Assistant's voice is playing,
  and starts over for each sentence, so talking while Claude is still writing
  no longer uses up the interruption. Stopping Talk Mode mid-question drops
  the half question, a disconnected microphone ends Talk Mode with a note, and
  interrupting while you're already asking keeps your question. The room's
  noise level comes from the second before the answer plays, not from its
  first moment, which can hold the answer's own echo.
- The panel says when Talk Mode watches a different microphone than the one
  picked (voice processing uses the Mac's default input), or when that
  microphone sends no sound. Only a microphone sending nothing at all counts;
  a quiet room doesn't.
- Code blocks in a spoken answer aren't read aloud, whether fenced with
  backticks or tildes. A block left open ends with its part of the answer, so
  what Claude writes after a command is still read.
- Control-Option-R while an answer streams replays the previous answer, and
  the streaming one isn't read after it.
- The microphone menu notices the lid opening or closing, so it stops picking
  the built-in microphone while the lid is closed.
- Smart Turn and Kokoro downloads are refused unless their SHA-256 matches, a
  damaged Smart Turn file is downloaded again before it's loaded, and Kokoro
  now downloads from a pinned commit. Loading Smart Turn no longer holds a
  lock the main thread waits on.
- The microphone menu reads the device list only when Core Audio reports a
  change, not on every redraw while listening.
- Apple's voices now play at the speed you pick. Their rate isn't linear:
  1.5× used to play at about 2.5×, and 2× at about 4×. Assistant now uses a
  map measured from rendered speech, within 15% of each speed.
- Assistant never speaks into an open microphone. A spoken answer that arrives
  while you're talking waits until you finish, and voice and speed samples and
  **Play again** wait for listening to end.
- Long questions and files are transcribed in full. `--transcribe` waits for
  the recognizer to finish the whole file, reads it a little at a time, and
  exits with an error when it hears no words. A spoken question waits for
  final text as long as the recognizer still has audio, from 2 to 30 seconds.
- The older recognizer no longer drops the first sentence after a pause when
  the next one starts with the same word, or repeats a phrase it revised. It
  now detects a restart from the recognizer's word timings.
- When Apple's speech model isn't ready within 1.5 seconds of a press, the
  question goes to the older recognizer, with the audio heard so far. A failed
  download is retried a minute later, and repeated failures to prepare the
  model switch to the older recognizer until then.
- A microphone that records only on its second channel is heard: Assistant
  mixes every channel for recognition and meters the loudest one.
- AirPods are no longer skipped because a silent buffer from before they
  started was counted against them, and a late result or a stale retry from
  one question no longer leaks into the next.
- **Stop speaking** stays up until speech ends, even after **Play again**
  cuts off an Apple voice.
- The context window uses the window reported for the model that answered,
  and ignores reports under 8,000 or over 10,000,000 tokens.
- Workspace titles longer than 60 characters aren't sent to the recognizer as
  one phrase, a failed turn's "No reply" note isn't replayed, and the chat
  scrolls when the live text is revised or the speech model's download
  progress changes.
- Start listening in 0.2 to 0.3 seconds, down from 1 to 1.7 seconds. Assistant gets the microphone and the speech model ready when it
  starts and when you open the panel, without turning the microphone on, and
  skips the permission check when access is already granted. The listening
  row appears the moment you press, and says **Starting…** until the
  microphone sends sound.
- Listen through AirPods and other Bluetooth headsets. Assistant used to tap
  them at the rate the previous device used, which failed and fell back to
  another microphone. It now uses the headset's own rate, and gives a
  Bluetooth headset 3 seconds, not 0.7, to switch into call mode before it
  tries another microphone. AirPods still send silence for about 1.6 seconds
  while they switch, so wait for **Listening…** before you talk.
- Keep the Mac chat scrolled to the newest message: it now follows new
  replies, the Working and Listening rows, and reopened chats.
- Keep everything you said when you pause mid-sentence. Apple's on-device
  recognizer can start a fresh transcript after a pause; Assistant now joins
  it to what it already heard. It also waits 2.5 seconds of silence, up from
  1.8, before it treats you as done.
- Use the context window the provider reports for each model: 1,000,000
  tokens for Haiku, Sonnet, and Opus today, up from a fixed 200,000 for Haiku
  and Sonnet. Compression starts at half the window.
- Stop Assistant for Mac from crashing when it starts listening right after
  microphone access is granted. It now waits for a microphone whose format
  isn't ready, skips one that delivers no audio, and catches audio errors
  macOS raises instead of quitting.
- A reply or tool output containing U+2028, U+2029, or U+0085 is no longer cut
  at that character and lost. Stream-json lines now end only at a line feed.
- A summary folded into a kept message no longer strips that message's
  `[Operator]` or `[You]` label from the prompt.
- Workspace context rows are no longer taken for the operator's request in a
  summary, and session search no longer offers them as matches.
- Two Assistant processes on one chat no longer both send the full workspace
  digest, or both save a memory snapshot: they take turns on a per-chat lock.
- History no longer shows a compressed question twice.
- Long tool-call commands are capped at 20,000 characters, like tool output.
- **New chat** and **History** wait while an answer is running, so the late
  reply can't land in another chat.

### Removed
- Remove the dollars-per-day boxes from the Brief tab and the background-work
  stats. The model-heavy background jobs that drove that cost are off.

### Changed
- The Brief tab now rebuilds from your current notifications, completed
  actions, and service status every time the dashboard updates, instead of
  showing the copy saved at 7 AM. A **Refresh now** button rebuilds it on the
  spot. If the live build fails, the tab falls back to the saved copy.
- Turn off the daily Sonnet call that writes the morning brief's wording. Its
  summary never showed on the dashboard, and its advice showed only when the
  live queue couldn't be read. The Brief tab still builds without a model. Set
  `ASSISTANT_BRIEF_NARRATOR=1` to turn it back on.
- Turn off the watcher (the Observer) by default, along with its Opus
  double-check. It sent 40–75K tokens per check, about 870 checks a day. The
  dashboard's **Waiting on you** tab covers its view without a model. Set
  `ASSISTANT_WATCHER=1` to turn it back on.
- Turn off the lessons extractor and the strategist by default. Their output
  went unread: 689 lesson proposals sat pending, and the strategist's notes
  covered 9 items 119 times. Set `ASSISTANT_LESSON_EXTRACTOR=1` or
  `ASSISTANT_STRATEGIST=1` to turn either back on. With the strategist off, the
  goals planner uses its built-in step templates.

### Added
- Add a **Slack** tab to the dashboard, ported from the standalone focus-board
  tool. It lists the Slack threads where the next move is yours, in Do first,
  Promise, This week, and Optional, plus Done by you, Just read, Set aside, and
  Auto-resolved.
  - **Refresh from Slack** POSTs `/slack-focus/refresh` to `todo-server.py`,
    which starts `bin/slack-focus.py` in the background under zsh with
    `~/.zprofile` sourced, so `$SLACK_USER_TOKEN` and `gh` resolve under
    launchd. The page polls `/slack-focus/status` and shows each step. A second
    click while a refresh runs starts nothing.
  - **Done** and **Undo** POST `/slack-focus/done` and `/slack-focus/undone`.
    A checked-off row comes back when its thread gets a new reply or edit.
  - The pipeline lives in `src/assistant/slack_focus/`. Collect and triage are
    rules only; the judge is one model call over the surfaced candidates,
    tools off, metered as `caller="slack_focus"`, with verdicts cached by
    content. If the model fails, the raw rows show. It uses the balanced
    model tier with one attempt; the focus-board ran your `claude` alias and
    retried once. `bin/assistant-llm.py` routes it as the `slack_judge`
    feature.
  - The Slack client refuses any method outside a read allowlist before a
    request leaves the machine, and a test checks that no module names a
    common Slack write method.
  - Everything about the user is in the new `slack_focus` block of
    `~/.assistant/comms/config.json`: watched channels, bot names, skipped
    channel names, GitHub repos and hosts, and Jira projects. The Slack ID,
    name, and workspace come from the token. Jira project keys and GitHub
    hosts used to be hard-coded.
  - Files live in `~/.assistant/slack-focus/`.
  - Each row stores its thread's latest message, so a check-off keeps its
    key while a new pull renumbers threads. The key formula matches the
    focus-board's, so its `done.json` carries over.
  - A refresh counts as running only while it holds `refresh.lock`, so a
    crash or a reused process ID can't leave the button stuck.
  - Any Slack error other than a deleted or private thread or channel stops
    the refresh with that error; `ratelimited` answers are retried. The
    focus-board carried on with a partial pull. A refresh stops after 20
    minutes.
  - The Slack routes refuse `Origin: null`, which the other routes allow
    for file:// pages.
  - Times on the tab show in local time.
  - Promises made in DMs read "DM" or "group DM" instead of a channel ID.
  - Jira uses a bearer token unless `email_env` names the env var with your
    account email; the focus-board always looked for `$JIRA_EMAIL`.
  - A new reply in a DM thread that started before the window now shows up.
    The focus-board dropped it.
  - A message counts as answered only when you posted after it. The
    focus-board also counted a message of yours from up to 60 seconds
    before, which hid quick DM asks.
  - A direct @-mention of you outranks an earlier group mention in the same
    thread.
  - DMs and threads are read to the end. The focus-board stopped at 200
    messages.
  - A model answer of `[]` sets every new row aside as noise. The
    focus-board treated it as a failure and showed raw rows.
  - A promise the model moves to Do first ranks below direct asks instead
    of above every row.
  - A refresh error never includes the Slack token.
  - If a refresh launch hangs before it starts, the next **Refresh from
    Slack** stops it and starts a new one.
  - The unused low-signal hints are gone.
  - A `config.json` that isn't valid JSON shows that on the tab, with the
    line, instead of the setup steps next to the last list.
  - "Last pulled" shows when Slack was last pulled, also after a
    `--triage` run.
- A row's text now quotes the other person's latest message, not yours. The
  focus-board compared author names with the label "you", which never
  matched a real name.

### Changed
- Read agent sessions from cmux first, with mukul-memory's registry as a
  fallback. cmux 0.64.25 records agent sessions itself, and
  `cmux sessions list --json --all` prints those records without the cmux
  socket. The new `bin/cmux_sessions.py` is the one place that runs it. It
  waits up to 15 seconds, and when cmux can't answer it returns no records and
  writes one stderr line. `world-scanner.py`, `session-context-watcher.py`,
  `build-ws-context.py`, `transcript-tail.py --closed-cwd`, and
  `cmux-restore-sessions` read it instead of `~/.cmux-session-ledger.jsonl`.
  - cmux misses a session whose SessionStart never reached it. On 2026-10-19
    that was ee4ee641 in workspace:27. For such sessions every reader also
    takes the row from `~/.claude/cmux-registry.json`, written by mukul-memory's
    `cmux-registry.py` hook.
  - `world.json` `_meta.session_record_fallback` counts the sessions that came
    from that fallback, and the scanner log shows `fallback_hits=N`. Retire the
    fallback after a soak period with zero hits.
  - A registry row with a live pid must still be a `claude` or `droid` process.
    This drops sessions like aea1111b, whose recorded pid now belongs to another
    program.
  - On live state the scanner verifies the same 10 sessions as before. Only
    the aea1111b ghost drops.
  - Only the session cmux marks active on a surface counts as running there, so
    a session left behind by `/clear` in the same process no longer looks live.
  - Output fields keep their names, and `registry_live_pid` and
    `session_start_registry` keep their values. Some values now come from
    different sources:
    - **First recorded** comes from cmux's `started_at`, and from a registry
      row's SessionStart time for fallback sessions. A session restored into
      another workspace shows its original start, not when it arrived there.
    - A session's `cwd` is its launch directory, and `ts` is the record's
      latest update.
    - `transcript-tail.py --closed-cwd` picks the most recently active session.
    - Sessions of agents the fleet doesn't run, such as Copilot, are left out.
- Make `hooks/cmux-auto-resume.py` forward reliably. It's what makes cmux
  record agents started without cmux's Claude wrapper, such as an absolute
  `~/.local/bin/claude` like the warm comms session, and the scheduled fleet
  spawns.
  - The hook now starts a detached worker and returns at once.
  - The worker checks each `cmux hooks <provider> session-start` exit code.
    It then confirms the record with `cmux sessions list --session <id>`.
  - It retries after 0, 2, 5, 15, and 30 seconds.
  - A session cmux still lacks gets one `cmux-session-forward-fail` entry in
    `~/.assistant/actions-ledger.jsonl`.
  - Before, one try was made, with a 5-second timeout and its errors ignored.
- `install/patch-settings.py` registers `cmux-auto-resume.py` on SessionStart in
  Claude settings and Factory hooks when it's missing. A fresh machine used to
  link the hook without registering it. The file is written without escaping
  non-ASCII text.
- `cmux-restore-sessions` finds a session for a panel with no resume binding in
  cmux's records, then in the registry. The ledger is no longer used. Native
  `cmux restore --surface` needs a binding, so this CLI stays.
- Measure Python under `install/` and `hooks/` in the new-code coverage gate,
  so changes there can pass it.

### Removed
- Retire `hooks/cmux-session-ledger.py`. For this release it's a no-op, so a
  registration that outlives `git pull` still exits 0.
  `install/patch-settings.py` now removes its SessionStart and SessionEnd
  registrations from `~/.claude/settings.json` and `~/.factory/hooks.json`
  instead of adding them. Delete the stub and its install.sh link next release.
  `~/.cmux-session-ledger.jsonl` stays on disk, but nothing writes to it any
  more. mukul-memory's `cmux-registry.py` registration is left alone.

### Rollback
- Revert the merge commit, then run `install.sh --apply`. That restores the
  readers on registry and ledger, `cmux-restore-sessions` on the ledger, and
  the single fire-and-forget forward. The base `patch-settings.py` also
  re-registers the ledger hooks on SessionStart and SessionEnd, and the ledger
  file resumes from where it stopped.
- The `cmux-auto-resume.py` registration this release added on a fresh machine
  stays, because the base installer never removes hooks. It's harmless: the
  base hook makes the same single forward.
- Ledger events between deploy and rollback are lost, so **First recorded**
  for sessions started in that window is unknown until they restart.

### Added
- Add the `cmux-ws-numberer` LaunchAgent as an opt-in feature. It adds a `[N]`
  suffix to cmux workspace titles and gives each workspace an unused sidebar
  color. Enable it with `install.sh --apply --with-ws-numberer`, answer the
  prompt, or use `--with-all`. Its label is now `com.assistant.cmux-ws-numberer`;
  the installer removes the old `com.mukuls.cmux-ws-numberer` plist. Its log
  moves to `~/.assistant/logs/cmux-ws-numberer.log`, which it creates if
  missing, and rotates at 5 MB with two old copies.
- Let changes to `install.sh` pass the new-code coverage gate when a targeted
  mutation proves them. No line-coverage tool can measure it safely here.
  Each changed hunk that holds code needs a check in `tests/mutation_smoke.py`
  that breaks one of its lines. The gate fails on any unproven hunk, and the
  mutation step fails unless a test rejects each break and the broken script
  still parses. A check proves a hunk only when every line it breaks is changed
  code in that hunk. One check is enough for a whole hunk, so add more for
  large hunks. Review new checks like any test, because a check written to
  dodge the gate can still pass it. Hunks that only
  delete lines need no check. Other shell scripts still fail the gate.
- Check every self-update before it lands: read each runtime file the fetched
  commits add or change straight from git, scan it for leftover merge conflict
  markers, and parse it if it's Python. If a file fails, refuse the update
  before anything is stashed or merged, and record the failure on the
  dashboard. Skip that commit quietly until the remote moves, with a reminder
  once a day. Updates now fast-forward to exactly the commit that was checked.
- Run the pulse through a pre-flight (`bin/run-pulse.py`) that parses `pulse.py`
  and the modules it loads at startup before each start. If one won't parse,
  the pre-flight skips the run and exits cleanly instead of crashing. The
  dashboard shows the error at the top of the page until a pulse runs again, and
  an actions-ledger entry reaches Slack for a new error, then once a day. New installs
  get this right away. Existing machines pick it up after a reboot, a logout, or
  a manual reload of the pulse LaunchAgent, since self-update defers that reload.
- Enforce 100% changed-code coverage with separate Python and real-browser reports.
  Add missing failure-path tests and repeatable mutation checks for key protections.
  Bind reports to measured sources and correctly map multiline Python and JavaScript changes.
- Show **Go to session** on collapsed cards so you can focus a workspace
  without opening its details. Existing identity and freshness checks still apply.
- Allow manual close-out review for plan-only work when its exact saved document
  still matches the reviewed hash; keep implementation explicitly separate.
- Preserve actionable transcript context and outstanding question choices.
  Display reviewed, conversation-bound return notes with specific next steps.
- Group live GitHub alerts into review topics, show three first, and keep
  individual alerts available in history. Surface stale saved requests separately.
- Add approval-bound backlog cleanup with record fingerprints, backups, and a
  private audit receipt that cannot trigger outgoing notification broadcasts.
- Add an attention overview with compact task cards, expandable return notes,
  and reminders to finish older pending work before starting another task.

### Changed
- Move the `balanced` model tier from Sonnet 4.6 to Sonnet 5.5 on Bedrock, the
  Anthropic API, and Vertex. The warm Slack session, the Observer, the brief
  narrator, and the strategist use this tier. Restart
  `com.assistant.assistant-comms` to pick it up; the restarted daemon replaces
  a warm session on the old model before its next Slack message. The Observer
  and fleet evals now default to this tier's model, and their qualification
  hashes include `model_tiers.py`. Cost estimates price Sonnet 5 models at $2
  and $10 per million input and output tokens.

### Fixed
- Bring the warm Slack session up again on Claude Code 2.1.303 and cmux 0.65.
  Claude Code now writes several hundred KB of records right after each prompt,
  so the check that read only the last 256 KB of the transcript never saw the
  boot prompt. Every spawn was abandoned, and cmux 0.65 refused to close the
  abandoned workspace, so each retry left another one open. The check now
  searches the whole transcript, and the daemon retries the close with
  `--force` when cmux asks to confirm, still only for its own warm workspace.
- Stop the dashboard from crashing on sessions with no working folder. cmux
  0.65 lists a `restore_record` before the `resume_binding`, so
  `world-scanner.py` read the folder from a record that names it
  `working_directory` and saved no folder for those sessions. The renderer then
  failed on every run. The scanner now reads either name, and the open-sessions
  list shows a session even when its folder is unknown.
- Stop the Slack daemon from following a headless run's copy of a Slack
  message. The proofgate check's `claude -p` run records the warm session's
  prompts as queued prompts with no entrypoint, so the daemon could bind to
  that transcript and watch it for the reply. A queued prompt now counts only
  in a transcript whose first entrypoint is interactive.
- Match each terminal to its session through the `resume_binding`, which agent
  hooks keep current, before the `restore_record` that cmux 0.65 lists first.
  The restore record is a snapshot from when cmux restored the surface, so
  `world-scanner.py` and `build-ws-context.py` could name an old session.
- Stop two time-of-day test failures. The Observer audit tests failed in the
  last minute of a two-hour window, and five Strategist tests that count calls
  per UTC day failed in the last minutes of the day. Each test now starts its clock at the
  beginning of its window, so calls a few minutes later stay in the same one.
  The audit tests also set their own window length.
- Keep a Strategist test from writing a row to the real
  `~/.assistant/actions-ledger.jsonl`. It now runs with a temporary home.
- Name workspaces in Slack by the same number the sidebar shows. Workspace
  pings and action lines named the workspace by its `workspace:N` ref, which no
  longer matches the `[N]` in the title. `comms-listen` now looks up sidebar
  positions once per pass that sends something, and the footer shows `[4]`
  instead of `workspace:1000010013`. Only titles that already end in that
  `[N]` count, so a machine without `cmux-ws-numberer` keeps showing refs.
  Ledger keys stay raw so they still match `actions-ledger.jsonl`, and
  `conversation.jsonl` keeps the refs. The warm comms prompt now uses the
  sidebar number and resolves it to a ref right before acting.
- Number workspaces by sidebar position. cmux 0.65 numbers workspace refs from
  1000010000, so `cmux-ws-numberer` was adding suffixes like `[1000010009]`.
  The suffix is now the workspace's 0-based position in its window, the number
  `--workspace <index>` accepts. It follows reorders within one poll. Renames
  and colors still target the stable ref, and a cmux that reports no index
  gets colors without a suffix. The `spawn-claude-workspace` skill now says the
  bracket number isn't the ref.
- Stop the pulse tests from self-updating the checkout they run in. The tests
  that run the pulse's `main()` called the real self-update, so a checkout
  behind its remote got pulled. When the pull touched installer files, it also
  ran `install.sh --apply` with the test's temporary home. That reloaded this
  machine's daemons from a scratch copy. It also failed a push-to-`main` CI run
  whenever `main` moved before the run finished. The test loaders in
  `test_pulse.py` and `test_observer_skip.py` now stub it out, and a
  `conftest.py` guard fails any test that still reaches it.
- Keep a killed git from blocking self-updates. The pulse's timeout used to
  SIGKILL a context check mid-`git status`, leaving `.git/index.lock` behind;
  on 2026-09-28 that blocked every self-update, and seven repos on this machine
  held such locks. Timeouts now send SIGTERM first (git removes its locks) and
  still SIGKILL the group after 3 seconds, the background `git status` and
  `git log` calls take no index lock at all, and self-update removes an
  index.lock older than 10 minutes when no process has it open and no git
  process is working in the repo. Each removal is recorded in the ledger.
- Ignore Claude Code's `.claude/worktrees/` and the stray `.claude/.assistant/`
  marker, which made the tree look dirty and set off daily auto-stashes.
- Ping about an idle workspace only when it needs you: the agent's last lines
  ask you something, a permission or plan prompt is up, or the session stopped
  on an API error. Claude's idle alert fires about a minute after every turn,
  and about 75% of recent turns ended with a status update instead.
- Match the watcher's patterns ("CI red", "PR opened", "tests passed") against
  what the agent said this turn, not 50 lines of scrollback, so old tool output
  no longer triggers them. Name workspaces cmux left as "Terminal" by Claude's own
  session title, or the folder and branch.
- Mute the "stranded", "awaiting-review", and "emit-card" word lists by default
  (168 pings in two weeks, mostly on status prose), and stop a muted pattern
  from hiding a real one in the same message.
- Stop closing a healthy warm comms session whenever cmux is slow to answer.
  A refused or timed-out check now leaves the session alone; only cmux saying
  the workspace doesn't exist, or a successful workspace list without it,
  counts as gone. Short blips get two more looks a few seconds apart.
- Submit prompts to the warm session reliably. The daemon now presses Enter by
  writing a carriage return, because `surface.send_key enter` can leave the
  prompt typed but unsent on a workspace that was never shown. Every boot
  prompt and Slack message is confirmed in the transcript, with up to two more
  Enter presses when the text is still in the prompt box. A session whose boot
  prompt never lands is closed instead of being declared ready, and the session
  is bound to the transcript that recorded its prompt, never the newest file.
- Keep inbound Slack messages until the warm session confirms it received them.
  They're recorded on arrival, retried every 30 seconds for up to 3 hours, sent
  together once the session is back, and you get one "I'll answer as soon as
  it's back" note per outage. Before, a message that arrived while no session
  was up was dropped for good.
- Cut the automatic Slack feed. The heartbeat pages once per outage and posts
  once when it recovers, instead of every 30 minutes. Housekeeping ledger
  entries stay in the brief, and a ledger pass posts at most 5 updates plus one
  summary line. A workspace gets at most one ping per 15 minutes unless it asks
  a real question.
- Write Slack posts in plain language: what happened first, with the workspace
  title and the agent's own question or last message, and refs in a trailing
  line. Screen snippets drop Claude's spinner, prompt, and status lines, and the
  on-disk pattern bank inherits the built-in "CI green" mute it was missing.
- Point tests at a cmux binary that doesn't exist, so a test that misses a stub
  can't drive the real cmux.
- Pin the clock in the review-topic focus test so it stops failing once its fixture alert is more than 4 days old.
- Download browser-check dependencies publicly so CI doesn't require Adobe's internal network.
- Invalidate return notes after completed tool traffic; require review before reusing older notes.
- Block close-out for unverified terminals, and safely show incomplete question choices.
- Keep installer tests away from real terminal hooks, and count isolated Python
  subprocesses in coverage. Update regression checks for the new observation
  metadata and plain-language dashboard labels.
- Rewrite dashboard labels, status messages, and instructions in plain language
  using Google's documentation style. Preserve original questions and technical
  records in expanded details, and add a wording check for every session state.
- Resolve full session filenames directly instead of scanning every transcript
  filename for prefix matches on each collector pass.
- Keep ordinary updates and unverified sessions out of the human decision queue.
  Preserve response endings instead of dropping next steps after 800 characters.
- Separate cmux workspace counts from GitHub notifications. Sessions stay the
  default view, notification counts stay inside their inbox, and expired
  workspace counts show as unverified.
- Show snapshot age independently from page age. Suppress completion suggestions
  when data is outdated, and preserve expanded context while you read.
- Bind context and workspace-opening requests to stable identities. Distinguish
  failed observations from evidence that work is progressing.
- Track outstanding tool calls across long transcripts without nudging active
  sessions to close. Preserve separate live bindings when sessions are resumed twice.
- Isolate installer tests from host service registrations and terminal hooks.

## [0.7.4] - 2026-09-16

### Fixed
- **Warm comms session respawns on a backend toggle instead of freezing its
  spawn-time model id**: the warm session is kept alive by ref across daemon
  restarts, so its `--model` id was fixed at spawn. After a
  `claude-backend bedrock|sub` switch, a session spawned on Bedrock kept running
  `us.anthropic.claude-sonnet-4-6[1m]` even though the box moved to direct
  Anthropic (observed 2026-09-16 — a stale Bedrock sonnet id on a non-Bedrock
  box). `spawn_session` now records the resolved model id in `session.json`, and
  `ensure_warm_session` respawns the session when that id no longer matches the
  current backend's resolution. A pre-upgrade session with no recorded id
  respawns once onto the tracked id; a droid session (no such id) is unaffected.

## [0.7.3] - 2026-09-16

### Fixed
- **Warm comms session no longer leaks cmux workspaces until cmux dies**: the
  warm-session watchdog (`bin/comms-listen.py`) spawned a fresh cmux workspace
  every ~minute when `claude` never reached its ready marker, and
  `spawn_session` (`bin/comms_session.py`) returned without closing the failed
  workspace. On 2026-09-14 that leaked ~200 orphaned workspaces (refs climbing
  to 340) and killed cmux. `spawn_session` now closes the specific workspace it
  created on every post-creation failure path (no ref, no surface, never ready,
  and the new-workspace-timeout partial ref), and `watchdog_loop` backs off
  exponentially (60s doubling to 30m, resetting on recovery) so a persistent
  boot failure can't hammer cmux.
- **Warm-workspace orphan sweep is now instance-scoped**: `reconcile_warm_workspaces`'
  title-scan pass matched every `assistant-comms (warm)` workspace machine-wide,
  so a failing comms instance would close a SECOND instance's healthy warm
  session (distinct `COMMS_HOME` — a second box or a live-validation spawn).
  Each warm workspace's cmux name now carries a stable per-instance tag
  (`warm_workspace_title`), and the sweep filters on it — it still closes this
  instance's own orphans but can never reach another instance's session.

## [0.7.2] - 2026-09-05

### Fixed
- **Warm comms session no longer assumes Bedrock**: `bin/comms_session.py`
  hardcoded a Bedrock-shaped model id (`us.anthropic.claude-sonnet-4-6[1m]`)
  for the warm session it spawns, bypassing `model_tiers` (the module every
  other spawn site in this repo already resolves its model id through). If
  the operator's backend wasn't Bedrock, the spawned session got handed an id
  its own backend would reject. The model id and backend now resolve fresh on
  every spawn (not frozen at daemon import) from the same `model_tiers`
  resolution, and the launch command explicitly prefixes
  `CLAUDE_CODE_USE_BEDROCK=<0|1>` — unless the model is an explicit
  `COMMS_MODEL` pin, in which case the flag is left alone rather than risk
  contradicting an operator's deliberate override. AWS auth vars are
  deliberately NOT baked into the command (that would put secrets in the
  process's argv); they still reach the child via the pane's own login-shell
  `~/.zprofile` sourcing.
- **`model_tiers.model_for(long_context=True)` no longer drops 1M context on
  direct Anthropic**: it previously appended the `[1m]` suffix only on
  Bedrock, on the mistaken assumption that `[1m]` was Bedrock-specific.
  Verified false against the operator's own non-Bedrock `~/.zprofile` alias
  (`claude --model "claude-opus-4-8[1m]"` with Bedrock off). This was latent
  in every `long_context=True` caller (`pulse.py`'s Observer + Strategist
  models); it became an active regression the moment `comms_session.py`
  started routing through `model_tiers` above, silently breaking the warm
  session's 50%-of-1M-token `/clear` threshold on a non-Bedrock box.

## [0.7.1] - 2026-08-04

### Fixed
- **Working-override gate in pulse.py**: when `agent_status == "working"`
  (a `tool_use` is in flight with no matching `tool_result`), the Observer's
  verdict is now deterministically forced to `active` before it reaches
  `execute_verdict`. Previously, the transcript JSONL's mtime went stale during
  long tool executions (builds, test suites) because the file isn't appended to
  until the tool returns. The Observer prompt *told* the LLM to treat
  `agent_status=working` as `active`, but an LLM hint is not a gate — when
  `last_turn_age_sec > 1800` the LLM could still emit `stranded`, nudging a
  workspace that was mid-execution. The new `apply_working_override()` function
  makes the override mechanical, with a safety valve at
  `WORKING_OVERRIDE_MAX_AGE_SEC` (7200s / 2h): past that age a tool "in flight"
  is probably hung, so the Observer's verdict stands and the workspace can
  still be rescued.

## [0.7.0] - 2026-07-25

Completes Factory Droid provider parity across the assistant. Droid was
previously wired only into the headless (`llm_runner`) and dispatch
(`agent_session`) paths; the comms warm-session subsystem, the doctor, and setup
were still Claude-only, so setting `llm.provider: droid` left the warm session
spawning Claude and reading only Claude-schema transcripts.

### One-knob provider selection (G4)
- `agent_session.dispatch_agent` now falls back to the global `llm.provider` knob
  (precedence: `ASSISTANT_DISPATCH_AGENT` env → `dispatch.agent` → `llm.provider`
  → `claude`), so a single `llm.provider: droid` drives BOTH headless calls and
  spawns.
- New `agent_session.warm_agent()` resolves the comms warm-session provider:
  `ASSISTANT_COMMS_AGENT` env → `llm.features.comms.provider` (comms-only pin) →
  `llm.provider` → `claude`.

### Comms warm-session Droid parity (G1+G2+G3)
- `comms_session.spawn_session` picks the provider via `warm_agent()` and launches
  per-agent: Claude keeps its exact byte-identical launch (Sonnet, scoped
  `--add-dir`s); Droid uses `agent_session.launch_command(DROID)` with `--cwd`.
  Readiness and the first-launch trust prompt route through
  `agent_session.ready_re`/`trust_marker`.
- Transcript reads are schema-agnostic: `project_dir_for_cwd`/`newest_transcript`
  resolve the per-agent root (`~/.factory/sessions` for Droid) via
  `agent_session.confirm_dir`, and `last_assistant_text` uses
  `agent_session.record_role` (Droid `type=="message"`/`message.role`). The
  chosen provider is persisted in `session.json` (`agent`) so post-restart reads
  use the right root + schema.
- Context management is provider-aware: Droid transcripts carry no usage/token
  block, so `should_clear` uses an on-disk transcript-size proxy
  (`COMMS_DROID_CLEAR_BYTES`, ~2 MB default), and `clear_session` performs a
  lossless RESPAWN for Droid (close + spawn) instead of the Claude `/clear`. The
  `comms-listen` daemon threads the persisted session agent through every
  transcript-root / context call.

### Doctor + setup (G7, G5)
- `assistant-doctor.py` adds droid binary + `droid-glm-settings.json` checks
  (mirroring `agent_session.agent_available`), run when any seam resolves to
  droid and skipped for a Claude-only box.
- `assistant-comms-setup.sh` persists `llm.provider` into config.json
  (`ASSISTANT_LLM_PROVIDER` env, else preserved, else `claude`), merged without
  disturbing the Slack send-gate invariant.

### Cleanup
- Removed the dead `PROJECTS_DIR` constant from `session-context-watcher.py`
  (transcript discovery already flows through the Droid-aware world-scanner).

## [0.6.0] - 2026-07-14

Unifies the productization line (0.3.1–0.5.1) with the lesson-proposal delivery
loop (developed in parallel), and integrates Slack comms + productization onto
the Keel `main` line (connectors M5, gated-outbound M7, model-tiers M8). The
proposal-delivery details are under "Proposal delivery" below.

### Integrated onto Keel main
- **Slack comms sits alongside Keel as a confined pull feed** — it is NOT routed
  through the decision spine (`outbound-dispatch.py`) or the interrupt-gate. A
  reply is *solicited* (the inbound message authorizes it) and every post goes
  only to the operator's own 1:1 channel, which the send-gate (`allowed_targets`)
  refuses to leave — so comms touches no governed push surface and needs no
  chokepoint. This is a DIFFERENT action from the code-forbidden
  `slack.reply.send` (posting into a shared human channel), which stays banned.
- **CI send-fence allowlist** (`test_no_rogue_notifications.py`): `slack-send.py`
  and `src/assistant/slack.py` are consciously added to the push ALLOWLIST — the
  fence bans the `chat.postMessage` *string*, not the confined behavior, and the
  confinement is proven by `test_slack_send.py` (HTTP is never called for a
  non-allowlisted target). `assistant-doctor.py`'s scope table was reworded so it
  no longer carries the literal API string (it names no real send site).

### Fixed
- **Connector plists were author-locked** (P1 bug-class, pre-existing on main):
  the six Keel connector plists (github/gcal/gmail/slack/outlook) and
  `machine-config-sync` still carried a literal `/Users/mukuls` (or the legacy
  `/Users/<user>/` placeholder + a hardcoded interpreter), which no `sed` scheme
  substituted — they would have pointed at the author's home / wrong arch on any
  other machine. Tokenized all of them to `__HOME__`/`__REPO__`/`__PYTHON__`/
  `__PATH__`, matching the rest of the fleet; surfaced by
  `test_plist_portability.py` at merge time.

## [0.5.1] - 2026-07-07

### Fixed
- **Fresh-machine install crash** in `install/patch-settings.py`: it
  unconditionally `shutil.copy2`'d `~/.claude/settings.json` to a backup, but on
  a machine that has never run Claude Code that file is absent →
  `FileNotFoundError`, aborting the install (and the `bash <(curl…)` bootstrap)
  right after the hooks step. Now it backs up only if the file exists, else
  creates the parent dir and writes fresh. Caught by a real interactive
  fresh-install run in a sandboxed cmux workspace (the earlier
  ASSISTANT_SELF_UPDATE portability test had dodged this path).

### Testing
- `test_patch_settings.py` (4): fresh-machine create (no crash, no spurious
  backup), idempotent re-run (no duplicate hooks), and preservation of existing
  user settings (with backup).
- Validated the v0.5.0 interactive opt-in prompt **live** in a fresh sandboxed
  cmux workspace: prompt fired on a real TTY, answered memory=y/crash-resume=n,
  state persisted, re-run honored it with no re-prompt; the piped/headless path
  correctly skipped the prompt and persisted nothing.

## [0.5.0] - 2026-07-07

### Added
- **Interactive feature discovery.** On an interactive `install.sh --apply` (a
  real terminal, incl. the `bash <(curl …)` bootstrap), each undecided opt-in
  feature is now OFFERED with a one-line explanation + `[y/N]` prompt — so users
  discover memory-sync / crash-resume without reading docs to learn the `--with`
  flags exist. Answers persist in `~/.assistant/feature-opt-in`, so re-runs
  never re-ask. Precedence per feature (first match wins): explicit `--with`
  flag → remembered answer → already-running daemon (adopted) → prompt →
  headless default-OFF.
  - **Never hangs the headless paths** (the cardinal rule): the prompt fires
    only when `--apply` AND stdin is a TTY AND not `ASSISTANT_SELF_UPDATE` AND
    no explicit flag AND undecided. The pulse self-update, `curl | bash`, and CI
    each fail ≥2 of those terms and fall through to default-OFF. `read -t 60`
    bounds a walk-away; a bare `read` is never used (set -e would abort on EOF).
  - A **timeout** leaves the feature undecided (re-asked next run) — only an
    explicit decline (`n`/Enter/EOF) is remembered as "no", so a walk-away can't
    silently suppress future discovery.
  - Changing your mind: re-run with the `--with` flag (beats a remembered "no"),
    or delete the line from `~/.assistant/feature-opt-in` to be re-asked. An
    already-running daemon is adopted, never torn out.

### Testing
- `test_install_optin_prompt.py` (8 tests): proves the headless paths
  (self-update, non-tty --apply, dry-run) never prompt or hang, pins the
  answer→persist mapping (y/yes→yes; n/Enter/EOF→no), and confirms an explicit
  flag suppresses the prompt.

## [0.4.0] - 2026-07-07

### Added
- **Opt-in daemon tiers.** A bare `install.sh --apply` now installs **CORE only**
  — the fleet loop + dashboard (pulse, world-scanner, session-context-watcher,
  assistant-page, todo-server). Feature daemons are off by default and enabled
  per-flag: `--with-memory` (cross-machine memory sync — only useful on >1
  machine), `--with-crash-resume` (auto-resume crashed cmux workspaces),
  `--with-all`. Slack comms + slack-reactor remain token-gated (copied,
  hand-loaded after setup — never auto-loaded even with `--with-all`). Flags
  only affect a fresh install's loaded set; an already-running feature daemon is
  never torn out, and every feature plist is always copied so it can be enabled
  later. Rationale: a usage analysis showed memory-sync had pulled once ever and
  workspace-watcher fired 3× ever — real features, but not everyone's problem,
  so they shouldn't be forced on. Documented in ONBOARDING.md + install.sh --help.

### Changed
- **assistant-page render cadence 15s → 30s** to match world-scanner's output
  rate — the renderer's only input (world.json) changes every 30s, so rendering
  at 15s made ~half the renders byte-identical no-ops. The dashboard's own 15s
  `<meta>` auto-refresh keeps the browser view as fresh as before. (Not made
  WatchPaths-event-driven: world.json is written non-atomically, so a vnode
  event could fire mid-write — a cadence match is the safe fix.)

### Testing
- `test_install_tiers.py` (5 tests): drives `install.sh` dry-run and asserts
  CORE loads by default, feature daemons are copy-no-load until their `--with`
  flag, and token-gated daemons never auto-load even under `--with-all`.

## [0.3.1] - 2026-07-06

### Fixed
Adversarial ship-gate findings from the 0.3.0 productization (all verified
against the tree before fixing):
- **Symlink write-through guard (R1/R2)** — `install.sh`'s copy path now refuses
  to `cp` through a symlinked target: a legacy install left some
  `~/Library/LaunchAgents/*.plist` as symlinks INTO the repo, so a `--apply`
  would have written rendered `/Users/mukuls` content back through the link into
  the committed portable template. Both `ensure_file_copy` and the Section-3
  loop now replace a symlink with a real rendered file (guard, not follow).
- **Doctor scope accuracy (D1/D2)** — `_history_scopes()` makes the
  conversations.history requirement an EITHER/OR set for EVERY target type,
  closing a false-PASS for a `D…` DM-channel target missing `im:history`; and
  `users:read`/`groups:read` are no longer required (no daemon calls
  users.info / conversations.info). Scope docs across
  ONBOARDING.md / assistant-comms-setup.sh / docs now agree with the doctor.
- **Crash-loop announce dedup (D3/D4)** — `comms-listen`'s misconfig announce
  now dedups on a STABLE key (the set of failing check names, not variable
  error text) and fails CLOSED (writes the marker before sending), so a flapping
  network can't spam the operator every 10s.
- **Portability hardcodes (P1/P2/P3/P5/P6)** — `todo-server.py` (renderer path),
  `comms_session.py` (DISPATCH_CWD + --add-dir), `session-context-watcher.py`
  and `world-scanner.py` (cron cwd), `merge-pr-dispatch.py` (sibling scripts),
  and `render-assistant-page.py` (home shortener) now derive from `__file__` /
  `$HOME` instead of `/Users/mukuls` or a fixed `~/dev/assistant`.
- **Docs vs behavior (D2/D3/D5/D6)** — corrected the false "installer loads
  nothing / loading pulse brings up the always-on set" claims (install.sh
  --apply DOES reload the always-on set; the companion agents are independent),
  the README test/fixture counts (48 / 14), and the install.sh --help COPIED
  list.

### Testing
- `test_plist_portability.py` gains a `bin/*.py` author-literal scan (the
  durable gate against re-introducing `/Users/mukuls` / `~/dev/assistant`
  hardcodes). `test_doctor.py` adds D…-target + users:read-not-required cases.
  Three previously author-pinned tests (renderer, world-scanner, session-context
  cron) now assert the portable behavior via `$HOME`. Full suite green
  (1203 passed, 1 skip).

## [0.3.0] - 2026-07-06

### Added
- **Productization pass** — make Assistant installable + onboardable by any
  macOS engineer with repo access (not just the author):
  - `bin/assistant-doctor.py` — a stdlib preflight that classifies checks
    **core** (python/repo/git/cmux — block install) vs **optional** (Slack
    comms / warm-session — warn only), each with an exact remediation. Wired as
    install.sh phase `[0]`, into `assistant-comms-setup.sh` (refuses to advertise
    the launch line on a scope failure), and into `comms-listen.py` startup
    (announces a misconfig once, deduped, instead of a silent 10s crash-loop).
  - `ONBOARDING.md` — the single "start here" with an ordered activation runbook
    (core pulse → cmux-watcher → Slack comms → slack-reactor → memory); the same
    runbook now prints at the end of `install.sh --apply`.

### Fixed
- **Portable LaunchAgent plists (C1)** — the 10 plists hardcoded `/Users/mukuls`
  and install.sh's `/Users/<user>/` sed was a no-op, so every daemon shipped the
  author's home path and died on any other machine. Plists are now templates
  with `__HOME__`/`__REPO__`/`__PYTHON__`/`__PATH__` tokens that install.sh
  substitutes with arch-resolved values (Apple-Silicon vs Intel interpreter).
  Filenames unchanged so `self_update.py` still keys on them.
- **Dead recovery references (C2)** — the comms warm prompt + `comms_lib.Paths`
  pointed at `heartbeat-write.py`/`spawn-assistant.sh` (deleted in 6bfa86c);
  the "restart Assistant" row now runs the live
  `launchctl kickstart -k … assistant-pulse`.
- **Bootstrap repo URL** — overridable via `ASSISTANT_REPO_URL`; no longer
  implies the author-only `git@github-personal:` SSH alias.
- `slack-reactor` plist downgraded to copy-no-load (it crash-loops without tokens).

### Testing
- 30 new tests: `test_plist_portability.py` (per-plist token render + a true
  fresh-checkout end-to-end that runs `install.sh --apply` from a mukuls-free
  path into a sandboxed HOME, asserting zero `/Users/mukuls` survives — **no
  launchctl**), `test_doctor.py` (core/optional + minimal Slack-scope logic),
  `test_reference_integrity.py` (every referenced script path exists — the
  durable gate against the next 6bfa86c-style orphaning).
### Proposal delivery (folded into 0.6.0)

### Fixed
- **Lesson proposals now actually reach the operator.** The lesson-extractor
  has always mined recurring patterns and written pending proposals to
  `~/.assistant/proposals.jsonl`, but nothing delivered them: the comms daemon
  suppressed a `lesson-proposal` ledger kind that was never emitted, and the
  warm session (its supposed delivery path) is reactive-only — it wakes on
  inbound Slack and is `/cleared` at 50% context, so it could never proactively
  surface a proposal. The queue sat dead for a month (436 pending proposals
  accumulated, undelivered).

### Added
- **PROPOSALS delivery loop** in `comms-listen.py` (5th daemon loop). Watches
  `proposals.jsonl` as a durable queue and delivers each new pending lesson
  proposal to the comms channel exactly once, asking the operator to confirm.
  - **Exactly-once via an id high-water-mark cursor** (`proposals.cursor`), not
    a byte offset — the confirm path rewrites `proposals.jsonl` in place, which
    would corrupt a byte cursor. Proposal ids are ISO-µs timestamps, so
    `id > cursor` is a clean, rewrite-safe watermark.
  - **Backlog skipped on first run** (like the ledger cursor) so the daemon
    never blasts a stale queue at the operator's phone; recoverable by deleting
    `proposals.cursor` to replay. Deliveries are capped per drain
    (`PROPOSALS_MAX_PER_DRAIN`) so one extractor batch can't firehose.
  - **Halt-and-retry on send failure** — the cursor advances only past a
    successfully-sent proposal, so a failed send retries next pass and no
    proposal is ever silently lost.
  - Each delivery is mirrored into `conversation.jsonl` and carries the
    proposal **id**, so after a `/clear` the warm session can confirm the
    *specific* proposal the operator's `y` refers to (fixing the prior
    `tail -1` bug that confirmed the wrong proposal when several were pending).
  - `comms_lib`: `read_proposals_cursor` / `write_proposals_cursor` /
    `initialize_proposals_cursor_if_missing` / `read_all_proposals` /
    `read_new_proposals` / `fmt_lesson_proposal`.
- Warm-session prompt updated: the daemon delivers proposals (not the warm
  session), and confirmation reads the id back out of the conversation window.

### Notes
- `pattern` and `lesson_audit` proposals are **not** delivered — they have no
  confirm path yet, so pinging them would be a dead-end. Documented gap.

## [0.2.0] - 2026-07-06

### Added
- **Slack comms** — a bidirectional comms layer, the Slack re-cut of the
  Telegram/Discord comms removed in 0.1.x for Adobe-IT security reasons. Posts
  to a private channel the bot is invited to (bot token, not the operator's
  identity):
  - Outbound pings: verified/failed ledger actions, cmux-watcher
    workspace-signal pages, and heartbeat-stale pages.
  - Inbound: a warm cmux Claude session (Sonnet) reads every message in the
    channel and replies at top level — a **flat 1:1** model (no threading).
  - `CommsSubsystem` wires into the single-process daemon, additive: active only
    when Slack is fully configured (`has_slack`).
  - Transport CLIs (`slack-send.py`, `slack-poll.py`), shared `comms_lib`, warm
    `comms_session`, the `comms-listen.py` daemon, `assistant-comms-setup.sh`,
    the opt-in LaunchAgent, and onboarding docs.
- **Send-gate** — `slack-send.py` and `src/assistant/slack.send()` refuse, with
  no API call, any target not in `config.slack.allowed_targets` (seeded to the
  one comms channel). Defense-in-depth confining the bot to its channel.
- Inbox freshness gate: stale cmux-watcher signals (older than
  `COMMS_INBOX_MAX_AGE_SEC`, default 300s) are dropped without a ping.

### Changed
- Daemon self-cleanup of its own warm session restored under a title guard
  (`close_own_workspace` only ever closes an `assistant-comms (warm)`
  workspace); `test_no_close_workspace.py` narrowed to allowlist exactly that
  one guarded invocation while still banning every other close-workspace call.

## [0.1.0] - 2026-06-06

### Added
- Initial single-process daemon (`python -m assistant`): collapses the pulse
  timer + heartbeat into one binary with a subsystem-per-thread model
  (`pulse`, `tools`, `heartbeat`).

### Removed
- Telegram and Discord comms transports (later in the 0.1.x line), removed
  entirely for Adobe-IT security reasons — the direct predecessor of the 0.2.0
  Slack comms above.

[0.6.0]: https://github.com/elitecoder/assistant/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/elitecoder/assistant/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/elitecoder/assistant/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/elitecoder/assistant/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/elitecoder/assistant/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/elitecoder/assistant/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/elitecoder/assistant/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/elitecoder/assistant/releases/tag/v0.1.0
