# Assistant for Mac

A menu bar app that shows which Claude sessions in cmux need you. It runs
without a model and without a background service.

## What it shows

- **Needs you:** the session's last reply asks you something, and it's been
  quiet for an hour or more.
- **Done, ready to clean up:** every pull request the session opened is merged.
- **Quiet:** idle, with nothing asked.

The menu bar shows how many sessions need you. Select a row, or **Take me to
the next one**, to switch cmux to that workspace. When a session newly needs
you, you get one notification for the batch.

## Use it

- Open the panel: select the icon in the menu bar, or press Control-Option-Space.
- Check from the terminal: `.build/release/AssistantBar --print`.

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
