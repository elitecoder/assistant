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
question out loud. Assistant stops listening when you pause, then reads the
answer aloud and shows it in the panel. Press Control-Option-V again to stop
early. Speech recognition runs on your Mac. It needs Dictation turned on in
**System Settings > Keyboard > Dictation**. Pick the microphone from the menu at
the top of the chat. It doesn't change your Mac's input for other apps.

Assistant answers with the most natural English voice you have installed.
The most human-sounding free option is Kokoro: install
[KokoroVoice](https://github.com/vicnaum/kokoro-tts-macos/releases/latest),
open it once, and Assistant switches to Kokoro Heart. It runs on your Mac. The
first answer after a restart takes a few extra seconds while the voice loads.
For a human-sounding voice, download a Premium voice, such as Zoe or Ava, in
**System Settings > Accessibility > Spoken Content > System voice > Manage
Voices**. Then pick it from the voice menu. Siri voices don't work in apps. The first time, macOS asks for
microphone and speech recognition access.

## Ask about your sessions

Type a question in the box at the bottom of the panel, for example "What does
the version bump session need from me?" To ask about one session, select the
speech bubble on its row first. Dictation works in the box too.

Each question runs your `claude` command with read-only tools: Read, Grep,
and Glob. The first question sends a summary of every workspace. A question
about one session also sends its recent conversation. Follow-ups continue the
same chat. The panel shows what each answer cost. Nothing runs until you ask.

## Use it

- Open the panel: select the icon in the menu bar, or press Control-Option-Space.
- Check from the terminal: `.build/release/AssistantBar --print`.
- Ask from the terminal: `.build/release/AssistantBar --ask "What needs me?"`.
  Add `--spoken` to get the answer in the short style it uses for speech.

## Build and install

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
