# Assistant for Mac

A menu bar app that shows which Claude sessions in cmux need you, and answers
questions about them. The list runs without a model and without a background
service. Claude runs only when you ask a question.

## What it shows

- **Needs you:** the session's last reply asks you something, and it's been
  quiet for an hour or more.
- **Done, ready to clean up:** every pull request the session opened is merged.
- **Quiet:** idle, with nothing asked.

The menu bar shows how many sessions need you. Select a row, or **Take me to
the next one**, to switch cmux to that workspace. When a session newly needs
you, you get one notification for the batch.

## Talk to it

Press Control-Option-V, or select the microphone in the panel, and ask your
question out loud. The listening row shows **Starting…** until the
microphone sends sound, then **Listening…**. Assistant stops listening when
you pause, then reads the answer aloud and shows it in the panel. Press
Control-Option-V again to stop early. To hear the last answer again, select
**Play again** or press Control-Option-R; it starts over if Assistant is still
speaking. Each answer in the chat also has a play button.

Assistant reads a spoken answer as Claude writes it: the first sentence plays
while the rest is still coming. `AssistantBar --ask "<question>" --spoken
--say-to answer.wav` renders the answer to a file instead of playing it, and
prints when the first audio was ready, streamed and waiting for the whole reply.

Assistant never speaks while the microphone is open. If an answer arrives while
you're talking, it reads it when you finish. If you ask a question while an
answer is still running, Assistant shows it and asks it next. If the
microphone disconnects mid-question, what it heard goes into the chat box, for
you to send or clear.

Speech recognition runs on your Mac. On macOS 26 and later, Assistant uses
Apple's SpeechAnalyzer with SpeechTranscriber. If its English model isn't
installed, Assistant downloads it the first time you talk and shows the
progress; until then, and on older macOS, it uses the older
SFSpeechRecognizer, which needs Dictation turned on in
**System Settings > Keyboard > Dictation**. The older recognizer gets your
workspace titles and terms such as PR, Assembler, Kokoro, and cmux as hints.
Assistant passes the same hints to SpeechTranscriber, but in testing they
didn't change its output.

Pick the microphone from the menu at the top of the chat. It doesn't change
your Mac's input for other apps. Assistant gets that microphone and the speech
model ready when it starts and when you open the panel, without turning the
microphone on, so listening starts in 0.2 to 0.3 seconds. A microphone that
sends only silence is skipped for the next one. A Bluetooth headset gets 3
seconds to switch into call mode first: AirPods send silence for about 1.6
seconds after they start, so wait for **Listening…** before you talk.

Assistant answers with Kokoro, a natural voice that runs on your Mac. The
voice (340 MB) downloads once, the first time you talk, into
`~/Library/Application Support/Assistant/Kokoro`. Until then, it uses Apple's
voice. Pick another Kokoro voice, or an Apple voice, from the voice menu. Set the talkback speed, from 1× to 2×, in the same menu.
Kokoro needs Apple Silicon. The first time, macOS asks for
microphone access, and for speech recognition access if Assistant uses the
older recognizer.

To check what voice input hears without a microphone, run
`AssistantBar --transcribe question.wav`. It runs the same SpeechAnalyzer path
on the file. Add `--legacy` to use the older recognizer. Make a test file with
`AssistantBar --render "<text>" question.wav` or `say -o question.aiff "<text>"`.
The timing of each press is in the log:
`log show --last 10m --info --predicate 'subsystem == "com.assistant.sessions" AND category == "voice"'`.

Assistant ends a question 2.5 seconds after you stop talking, and waits longer
when you pause mid-sentence. [Silero VAD](https://github.com/snakers4/silero-vad)
(MIT license) notices when you stop talking, and 0.2 seconds into the pause,
Pipecat's [Smart Turn v3.2](https://github.com/pipecat-ai/smart-turn)
(BSD 2-Clause license) judges whether you've finished. When it says you
haven't, Assistant keeps listening for up to 4 seconds of silence. It never
ends a question sooner than 2.5 seconds: on questions it wasn't tuned on, it
called fragments like "Switch the voice to" finished. Both models run on your
Mac with [ONNX Runtime](https://onnxruntime.ai). They download once, the first
time you talk, into `~/Library/Application Support/Assistant/SmartTurn`
(35 MB), and Assistant checks each file's SHA-256. Until then, only the
2.5-second timer decides. To compare the rules on recorded speech, run
`AssistantBar --turn-eval question.wav`.

### Talk Mode

To talk back and forth without pressing anything, select the waveform button
next to the microphone, or hold Control-Option-V for about half a second.
Assistant listens, answers, speaks, and then listens for your next question.
To interrupt an answer, start talking: about half a second of your voice stops
it, and the rest of that answer isn't read. Your new question is asked next,
as soon as Claude has finished writing the answer you interrupted, which stays
in the chat. Coughs and short sounds like "mm-hmm" don't interrupt.
**Stop speaking**, **Stop**, and Esc also drop the rest of an answer that's
still being written.
Select **Stop**, or press Esc, to end Talk Mode; a question you're in the middle
of is dropped, not asked. A turn where you don't ask anything also ends it, and
so does a microphone that disconnects. Assistant listens for you talking over
an answer only while its voice is actually playing, so talking while Claude is
still writing doesn't count. Start the app in Talk Mode with `AssistantBar --show --talk`.

While it speaks in Talk Mode, Assistant keeps the microphone open with Apple's
voice processing, which removes Assistant's own voice from what the microphone
hears. That microphone only watches for you talking; nothing is transcribed
until the answer stops. Voice processing uses your Mac's default input and
output devices, not the microphone picked in the menu, and it lowers other
apps' audio while it's on. The first half second you say while interrupting
isn't part of your next question, so pause for **Listening…**, or repeat it.

## Work with your sessions

Assistant can message a session for you, wait for its reply, and keep going
back and forth until the job is done. Say or type something like "Tell the
version bump session to use my token, then tell me when it has pushed." Each
message it sends shows in the panel with its workspace and exact text, followed
by what happened: the reply, or why it wasn't sent, confirmed, or answered yet.
Select **Stop** to end it.

- It sends with `cmux send`, which refuses while you're typing in that session
  or a question or permission prompt is open there.
- It skips workspaces on your `/back-off` list, however the workspace is
  named: by ref, ID, or title.
- It sends plain-language messages without asking you first. That's a choice:
  a session that gets a message acts on it with its own permissions, so a
  request like "I'm done here, clean up" can lead it to run a skill that
  deletes things. Back off a workspace to keep Assistant out of it.
- It won't send a message that starts with `/`, `!`, or `#`, which would run a
  slash command or a shell command, or save a memory note, in that session. Hidden
  characters in front don't change that.
- It presses Enter once. If the message doesn't show in the session's
  transcript, it says so and doesn't send it again.
- Everything it reads from other sessions (the workspace digest, their replies,
  and transcripts) reaches its Claude inside marked blocks that it treats as
  information, never as instructions, so a session's text can't pass for you.
- It brings you any decision about merging, deleting, spending money, or
  product behavior instead of answering for you.
- Its only command access is Assistant's own `--sessions`, `--send`,
  `--wait`, `--transcript`, `--find`, `--search`, and `--memory`. Everything
  else is denied, including Read, Grep, and Glob. See
  [What the chat's Claude can do](#what-the-chats-claude-can-do).

From the terminal: `AssistantBar --send workspace:25 "go"` sends a message and
prints the reply. `AssistantBar --wait workspace:25` waits for a session to
finish. `AssistantBar --sessions` lists the sessions it can reach.

## Ask about your sessions

Type a question in the box at the bottom of the panel, for example "What does
the version bump session need from me?" To ask about one session, select the
speech bubble on its row first. Dictation works in the box too.

Each question runs Claude Code with Claude Haiku, about a cent per question.
Pick Sonnet or Opus from the model menu for harder questions. Claude gets Read,
Grep, and Glob on session transcripts, plus Assistant's own commands. The first
question in a chat sends a summary of every workspace. Later questions send
only the workspaces that changed. A question about one session also sends its
recent conversation. The panel shows what each answer cost. Nothing runs until
you ask.

## What the chat's Claude can do

Workspace transcripts can hold anything, including text written to trick a
model, so the chat's Claude runs in a tight sandbox:

- Assistant runs the `claude` binary directly, not your shell alias, so the
  alias's extra folders and permission mode don't apply.
- It passes `--setting-sources ""`, so your `settings.json` allow list, your
  hooks, and your `CLAUDE.md` don't load. MCP servers and slash commands are off.
- Every tool call first goes through `AssistantBar --pretooluse-guard`, a hook
  that allows only these:
  - Bash: exactly `AssistantBar --sessions`, `--send <workspace> "<message>"`,
    `--wait <workspace>`, `--transcript <workspace>`, `--find '<json>'`,
    `--search '<json>'`, or `--memory '<json>'`, with no pipes, redirection,
    command substitution, extra arguments, or line breaks.
  - Read, Grep, and Glob are off. A session's conversation comes from
    `--transcript`, and a search of the transcripts from `--find`.
- Everything those commands print from other sessions (replies, workspace
  titles, transcripts, earlier chats) is marked as untrusted session text, as
  are the workspace digest and compaction summaries. Every kind of line break
  is normalized and look-alike labels and block tags are escaped, so the text
  can't close its block or pass for one of your turns.
- What the guard allows still needs Claude Code's own permission check, in
  `dontAsk` mode.
- Assistant runs each command only as its first argument, with an exact
  argument count, so an allowed command can't carry a second one.

The summarizer that writes compaction summaries also skips your settings, and it
has no tools. Claude Code signs in with your saved login, so no settings are
needed for that. Set `ASSISTANT_CLAUDE` if `claude` isn't in your login shell's
`PATH` or in `~/.local/bin`.

## Chat history, compression, and memory

Assistant keeps the chat itself, the way Hermes Agent does. It saves each
question, each command Claude runs, what the command printed, and each answer
in `~/Library/Application Support/Assistant/conversations.db`. Quit and reopen
the app, and your last chat is still there. Select **History** to continue an
earlier chat, or **New chat** to start a fresh one.

Each question runs a fresh `claude -p` with the chat so far, oldest first, and
the new question last. Each request repeats the previous one word for word
before adding to it, so Claude reads most of it from its prompt cache.

The bar under the menus shows how full the chat's context is, for example
"Context 23,456 of 200,000 tokens (12%)". The number is the prompt size Claude
reported for the last question, plus an estimate for the answer that followed.
When a chat reaches 150,000 tokens with Haiku or Sonnet (75% of a 200,000-token
window), or 500,000 with Opus (half of 1,000,000), Assistant summarizes the
older turns with Haiku before the next question. It then says, for example,
"Compressed 42 messages into a summary." Select **Compress** to summarize now,
or **Compress with focus** to say what the summary should keep in detail.
Compressed turns stay saved, and Claude can still search them.

Claude can search earlier chats with `AssistantBar --search`, and save lasting
facts about you and your setup with `AssistantBar --memory`. Memory lives in
`~/.assistant/mac-memory`, in `MEMORY.md` and `USER.md`. A chat reads memory
once, when it starts, so a fact saved during a chat shows up in the next chat.

To try compression without a long chat, set a small window:
`ASSISTANT_CONTEXT_WINDOW=60000`, or
`defaults write com.assistant.sessions contextWindow 60000`. A window under
about 85,000 tokens compresses at 85%. Claude's own instructions take about
23,000 tokens and the first workspace summary about 8,000, so keep the window
well above that.

The conversation store, compressor, memory, and session search are ported from
[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research,
under the MIT license.

## Use it

- Open the panel: select the icon in the menu bar, or press Control-Option-Space.
- Check from the terminal: `.build/release/AssistantBar --print`.
- Ask from the terminal: `.build/release/AssistantBar --ask "What needs me?"`.
  Add `--spoken` to get the answer in the short style it uses for speech.
  Without `--session`, the question runs in a throwaway chat. Add
  `--session work` to continue the chat named "work" across questions.
- Compress a terminal chat: `AssistantBar --compress --session work --focus "the deploy token"`.
- Point the terminal commands at another history and memory folder with
  `ASSISTANT_DB` and `ASSISTANT_MEMORY_DIR`.

## Build and install

Building needs Xcode and its Metal Toolchain. Install the toolchain once:

```bash
xcodebuild -runFirstLaunch
xcodebuild -downloadComponent MetalToolchain
```

```bash
cd mac
./build-app.sh            # builds mac/build/Assistant.app
./build-app.sh --install  # also copies it to ~/Applications
```

To start it with your Mac, select **Open at login** in the panel.

## How it finds sessions

It reads `cmux workspace list --json`, then finds the transcript under
`~/.claude/projects` that holds each workspace's last submitted message at
the same second. It reads pull request state with `gh`. Workspaces with no
submitted message, such as runs started by automation, count as not tracked.

## Test

```bash
cd mac && swift test
```
