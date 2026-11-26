"""bin/tmux_backend.py answers the commands Assistant sends to cmux, from a real tmux server.

Each test has a tmux server of its own (tests/tmux_fakes.py). The shapes checked here are the ones the callers
read: the refs and UUIDs in `tree`, the text the regexes in pulse, the comms daemon and the context builder
match, and cmux's own error lines.
"""
from __future__ import annotations

import io
import json
import os
import pty
import re
import subprocess
import sys
import time
import uuid

import pytest

import agent_session
import tmux_backend as tb
from tmux_fakes import BACKEND, TMUX, FakeClaude, needs_tmux, server, tmux, until  # noqa: F401 - `server` is a fixture

pytestmark = needs_tmux


@pytest.fixture
def backend(server):
    """Run one backend command in this process; its output as text. A refused command raises tb.Failure."""
    def call(argv: list) -> str:
        out = io.StringIO()
        assert tb.run(argv, out=out) == 0
        return out.getvalue()
    return call


@pytest.fixture
def work(server):
    folder = server / "work"
    folder.mkdir()
    return folder


@pytest.fixture
def claude(server, monkeypatch):
    return FakeClaude(server, monkeypatch)


def new(backend, work, name="td-1: Fix it", command=None) -> str:
    argv = ["new-workspace", "--cwd", str(work), "--name", name, "--focus", "false"]
    out = backend(argv + (["--command", command] if command else []))
    return re.search(r"workspace:\d+", out).group(0)


def screen(backend, ws_ref: str) -> str:
    return backend(["read-screen", "--workspace", ws_ref, "--lines", "50"])


def workspaces(backend) -> list:
    return [ws for window in json.loads(backend(["tree", "--all", "--json"]))["windows"]
            for ws in window["workspaces"]]


# ── with no server ────────────────────────────────────────────────────────────

def test_with_no_tmux_server_there_are_no_workspaces_and_ping_still_answers(backend):
    assert json.loads(backend(["--id-format", "both", "tree", "--all", "--json"])) == {"windows": []}
    assert backend(["ping"]) == "PONG\n"
    assert json.loads(backend(["rpc", "workspace.list", "{}"])) == {
        "window_id": None, "window_ref": None, "workspaces": []}
    assert json.loads(backend(["sessions", "list", "--json", "--all"]))["sessions"] == []


def test_a_tmux_that_fails_for_another_reason_is_an_error_not_an_empty_fleet(backend, monkeypatch):
    monkeypatch.setattr(tb, "_tmux", lambda *args: (1, "", "lost server\n"))
    with pytest.raises(tb.Failure, match="unavailable: lost server"):
        backend(["tree", "--json"])
    # A socket nothing listens on is no server. One that can't be opened is not knowing.
    monkeypatch.setattr(tb, "_tmux", lambda *args: (1, "", "error connecting to /tmp/x (Connection refused)\n"))
    assert tb.tree() == {"windows": []}
    monkeypatch.setattr(tb, "_tmux", lambda *args: (1, "", "error connecting to /tmp/x (Permission denied)\n"))
    with pytest.raises(tb.Failure, match="unavailable: error connecting to /tmp/x .Permission denied"):
        backend(["tree", "--json"])
    monkeypatch.setattr(tb, "_tmux", lambda *args: (1, "", ""))
    with pytest.raises(tb.Failure, match="unavailable: tmux list-panes exit 1"):
        backend(["tree", "--json"])
    with pytest.raises(tb.Failure, match="tmux_error: tmux kill-window exit 1"):
        tb._must("kill-window")


def test_a_line_tmux_prints_in_another_shape_is_left_out(backend, monkeypatch):
    monkeypatch.setattr(tb, "_tmux", lambda *args: (0, "not a pane row\n", ""))
    assert tb.tree() == {"windows": []}


# ── finding tmux ──────────────────────────────────────────────────────────────

def test_tmux_is_found_by_setting_then_path_then_the_usual_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TMUX_BIN", " /somewhere/tmux ")
    assert tb.tmux_bin() == "/somewhere/tmux"
    monkeypatch.delenv("ASSISTANT_TMUX_BIN")
    monkeypatch.setattr(tb.shutil, "which", lambda name: "/on/path/tmux")
    assert tb.tmux_bin() == "/on/path/tmux"
    monkeypatch.setattr(tb.shutil, "which", lambda name: None)
    installed = tmp_path / "brew" / "tmux"
    installed.parent.mkdir()
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.setattr(tb, "TMUX_DIRS", (str(tmp_path / "empty"), str(installed.parent)))
    assert tb.tmux_bin() == str(installed)
    monkeypatch.setattr(tb, "TMUX_DIRS", (str(tmp_path / "empty"),))
    assert tb.tmux_bin() == "tmux"


def test_a_missing_or_silent_tmux_is_reported_as_cmux_reports_errors(monkeypatch, capsys):
    monkeypatch.setenv("ASSISTANT_TMUX_BIN", "/nonexistent/tmux")
    assert tb.main(["tree", "--json"]) == 1
    assert capsys.readouterr().err.startswith("Error: unavailable: tmux can't be run")

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("tmux", tb.TIMEOUT_SEC)
    monkeypatch.setattr(tb.subprocess, "run", hang)
    assert tb.main(["ping"]) == 1
    assert capsys.readouterr().err == f"Error: timeout: tmux didn't answer in {tb.TIMEOUT_SEC}s\n"


def test_commands_go_to_the_default_server_even_when_run_inside_another_tmux(server, monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setenv("TMUX", "/tmp/other-server,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(tb.subprocess, "run", run)
    tb._tmux("list-sessions")
    assert "TMUX" not in seen and "TMUX_PANE" not in seen and seen["TMUX_TMPDIR"] == str(server)


# ── workspaces ────────────────────────────────────────────────────────────────

def test_a_new_workspace_is_a_detached_session_whose_shell_runs_the_command(backend, work):
    ref = new(backend, work, "td-7: Fix the flaky.test", "echo dispatched-$((40+2))")
    assert ref == "workspace:1"  # refs count from 1; tmux's own id for the window is @0
    until(lambda: "dispatched-42" in screen(backend, ref))
    (ws,) = workspaces(backend)
    assert ws["title"] == "td-7: Fix the flaky.test" and ws["ref"] == ref
    assert ws["current_directory"] == os.path.realpath(work)
    assert tmux("list-sessions", "-F", "#{session_name} #{session_attached} #{window_width}x#{window_height}"
                ).strip() == "td-7-Fix-the-flaky-test 0 200x50"


def test_two_workspaces_with_one_title_get_sessions_of_their_own(backend, work):
    assert [new(backend, work, "same"), new(backend, work, "same"), new(backend, work, "same")] == [
        "workspace:1", "workspace:2", "workspace:3"]
    assert sorted(tmux("list-sessions", "-F", "#{session_name}").split()) == ["same", "same-2", "same-3"]


def test_a_workspace_with_no_name_or_folder_gets_plain_ones(backend, monkeypatch, server):
    monkeypatch.setenv("HOME", str(server))
    assert backend(["new-workspace"]) == "OK workspace:1\n"
    (ws,) = workspaces(backend)
    assert ws["title"] == "Terminal" and ws["current_directory"] == os.path.realpath(server)
    assert tb._session_name("::..") == "assistant"


def test_a_workspace_is_not_started_in_a_folder_that_does_not_exist(backend, server):
    with pytest.raises(tb.Failure, match="invalid_params: cwd is not a folder"):
        backend(["new-workspace", "--cwd", str(server / "nowhere"), "--name", "x"])
    assert workspaces(backend) == []


def test_the_tree_names_every_session_window_and_pane_the_way_cmux_does(backend, work):
    first = new(backend, work, "one")
    new(backend, work, "two")
    tmux("split-window", "-d", "-t", "one")
    tmux("new-window", "-d", "-t", "one:", "-n", "one-b")
    tree = json.loads(backend(["--id-format", "both", "tree", "--all", "--json"]))
    assert [(w["ref"], w["title"], w["workspace_count"]) for w in tree["windows"]] == [
        ("window:1", "one", 2), ("window:2", "two", 1)]
    one, one_b = tree["windows"][0]["workspaces"]
    assert (one["ref"], one["title"], one["selected"], one["index"]) == (first, "one", True, 0)
    assert (one_b["title"], one_b["selected"], one_b["index"]) == ("one-b", False, 1)
    assert tree["windows"][0]["selected_workspace_ref"] == first
    assert [p["ref"] for p in one["panes"]] == ["pane:1", "pane:3"]
    assert [p["focused"] for p in one["panes"]] == [True, False]
    surface = one["panes"][0]["surfaces"][0]
    assert surface["ref"] == "surface:1" and surface["type"] == "terminal"
    assert surface["tty"] == tmux("display-message", "-p", "-t", "%0", "#{pane_tty}").strip()[len("/dev/"):]
    ids = [item["id"] for w in tree["windows"] for item in (w, *w["workspaces"])]
    ids += [s["id"] for w in tree["windows"] for ws in w["workspaces"] for p in ws["panes"]
            for s in (p, *p["surfaces"])]
    assert len(set(ids)) == len(ids) and all(str(uuid.UUID(i)).upper() == i for i in ids)
    # The same objects get the same UUIDs every time they are asked for.
    assert json.loads(backend(["tree", "--json"])) == tree


def test_a_restarted_server_gives_its_workspaces_new_uuids(backend, work):
    new(backend, work, "one")
    before = workspaces(backend)[0]["id"]
    tmux("kill-server")
    new(backend, work, "one")  # a new server process, so new UUIDs even within the same second
    after = workspaces(backend)[0]
    assert after["ref"] == "workspace:1" and after["id"] != before


def test_tree_for_one_workspace_and_cmuxs_error_for_one_that_is_gone(backend, work, capsys):
    new(backend, work, "one")
    two = new(backend, work, "two")
    only = json.loads(backend(["tree", "--workspace", two, "--json"]))
    assert [(w["ref"], [ws["ref"] for ws in w["workspaces"]]) for w in only["windows"]] == [("window:2", [two])]
    by_id = json.loads(backend(["tree", "--workspace", workspaces(backend)[1]["id"].lower(), "--json"]))
    assert by_id == only
    assert tb.main(["tree", "--workspace", "workspace:99", "--json"]) == 1
    assert capsys.readouterr().err == "Error: invalid_params: Missing or invalid workspace_id\n"
    text = backend(["tree"])
    assert 'workspace workspace:1 "one"' in text and 'surface surface:2 [terminal]' in text


def test_a_title_is_the_given_name_else_what_the_program_in_the_pane_set(backend, work):
    named = new(backend, work, "td-3: named")
    tmux("new-session", "-d", "-c", str(work))  # a session someone started by hand: tmux names the window itself
    by_hand = "workspace:2"
    host = tb.socket.gethostname()
    assert {ws["ref"]: ws["title"] for ws in workspaces(backend)}[by_hand] not in (host, host.split(".")[0], "")
    backend(["send", "--workspace", by_hand, r"printf '\033]2;Refactor the parser\007'"])
    backend(["send-key", "--workspace", by_hand, "enter"])
    until(lambda: {ws["ref"]: ws["title"] for ws in workspaces(backend)}[by_hand] == "Refactor the parser")
    backend(["send", "--workspace", named, r"printf '\033]2;ignored\007'"])
    backend(["send-key", "--workspace", named, "enter"])
    until(lambda: workspaces(backend)[0]["panes"][0]["surfaces"][0]["title"] == "ignored")
    assert workspaces(backend)[0]["title"] == "td-3: named"


def test_listings_print_what_the_callers_regexes_read(backend, work):
    one = new(backend, work, "one")
    tmux("split-window", "-d", "-t", "one")
    tmux("new-window", "-d", "-t", "one:", "-n", "other")
    assert backend(["list-workspaces"]) == "* workspace:1  one  [selected]\n  workspace:2  other\n"
    assert backend(["workspace", "list"]) == backend(["list-workspaces"])
    listed = json.loads(backend(["list-workspaces", "--json"]))
    assert listed["window_ref"] == "window:1"
    assert [(w["ref"], w["title"], w["index"]) for w in listed["workspaces"]] == [
        ("workspace:1", "one", 0), ("workspace:2", "other", 1)]
    assert json.loads(backend(["workspace", "list", "--json"])) == listed
    (window,) = json.loads(backend(["list-windows", "--json"]))
    assert (window["ref"], window["workspace_count"], window["key"]) == ("window:1", 2, True)
    assert backend(["list-workspaces", "--window", window["id"]]) == backend(["list-workspaces"])
    with pytest.raises(tb.Failure, match="invalid_params: Missing or invalid window_id"):
        backend(["list-workspaces", "--window", "window:9"])
    assert backend(["list-panes", "--workspace", one]) == "* pane:1  [1 surface]  [focused]\n  pane:2  [1 surface]\n"
    focused = backend(["--id-format", "both", "list-pane-surfaces", "--workspace", one])
    assert re.fullmatch(r"\* surface:1 [0-9A-F-]{36}  .*  \[selected\]\n", focused)
    other = backend(["list-pane-surfaces", "--workspace", one, "--pane", "pane:2"])
    assert re.search(r"(surface:\d+)\s+([0-9A-Fa-f-]{36})", other).group(1) == "surface:2"
    assert backend(["focus-window", "--window", "window:1"]) == "OK\n"
    with pytest.raises(tb.Failure, match="Missing or invalid window_id"):
        backend(["focus-window"])


def test_closing_a_workspace_ends_its_window(backend, work):
    one, two = new(backend, work, "one"), new(backend, work, "two")
    assert backend(["close-workspace", "--workspace", one, "--force"]) == "OK\n"
    assert [ws["ref"] for ws in workspaces(backend)] == [two]
    with pytest.raises(tb.Failure, match="Missing or invalid workspace_id"):
        backend(["close-workspace", "--workspace", one])


def test_selecting_a_workspace_shows_it_in_the_attached_terminal_or_says_how_to_attach(backend, work):
    one, two = new(backend, work, "one"), new(backend, work, "two words")
    tmux("new-window", "-d", "-t", "two-words:", "-n", "second")
    second = "workspace:3"
    with pytest.raises(tb.Failure) as refused:
        backend(["select-workspace", "--workspace", second])
    assert str(refused.value) == "no_client: no terminal is attached to tmux; run: tmux attach -t two-words"
    assert {ws["ref"]: ws["selected"] for ws in workspaces(backend)}[second] is False  # and nothing was changed
    controller, terminal = pty.openpty()
    client = subprocess.Popen([TMUX, "attach", "-t", "one"], stdin=terminal, stdout=terminal, stderr=terminal,
                              env={**os.environ, "TERM": "xterm"})
    try:
        until(lambda: tmux("list-clients", "-F", "#{client_session}").strip() == "one")
        assert backend(["select-workspace", "--workspace", two]) == "OK\n"
        assert tmux("list-clients", "-F", "#{client_session}").strip() == "two-words"
    finally:
        client.terminate()
        client.wait(timeout=10)
        os.close(controller)
        os.close(terminal)


# ── reading and typing ────────────────────────────────────────────────────────

def test_the_screen_is_read_without_the_blank_rows_below_the_last_line(backend, work):
    ref = new(backend, work, "one", "for i in 1 2 3 4 5 6 7 8; do echo line-$i; done")
    until(lambda: "line-8" in screen(backend, ref))
    whole = backend(["read-screen", "--workspace", ref])
    assert "line-1" in whole and not whole.endswith("\n\n")
    last = backend(["read-screen", "--workspace", ref, "--surface", "surface:1", "--lines", "3"]).splitlines()
    assert len(last) == 3 and "line-8" in "\n".join(last) and "line-1" not in "\n".join(last)
    assert "line-1" in backend(["read-screen", "--workspace", ref, "--scrollback"])
    with pytest.raises(tb.Failure, match="invalid_params: lines must be a number"):
        backend(["read-screen", "--workspace", ref, "--lines", "many"])
    with pytest.raises(tb.Failure, match="invalid_params: lines must be a number"):
        backend(["rpc", "surface.read_text", '{"surface_id": "surface:1", "lines": [3]}'])
    assert backend(["read-screen", "--workspace", ref, "--lines", "0"]) == whole  # no count: the screen as it is
    assert json.loads(backend(["rpc", "surface.read_text", '{"surface_id": "surface:1", "lines": -4}']))["text"] \
        == whole.rstrip("\n")
    with pytest.raises(tb.Failure, match="not_found: Surface not found"):
        backend(["read-screen", "--workspace", ref, "--surface", "surface:9"])


def test_rpc_reads_and_types_by_ref_or_uuid_and_answers_with_the_ref_it_was_given(backend, work):
    ref = new(backend, work, "one")
    surface = workspaces(backend)[0]["panes"][0]["surfaces"][0]

    def rpc(method, **params):
        return json.loads(backend(["rpc", method, json.dumps(params)]))

    assert rpc("surface.send_text", surface_id="surface:1", text="echo by-$((1+1))-ref")["surface_ref"] == "surface:1"
    assert rpc("surface.send_key", surface_id="surface:1", key="enter")["workspace_ref"] == ref
    until(lambda: "by-2-ref" in screen(backend, ref))
    assert rpc("surface.send_text", surface_id=surface["id"].lower(), text="echo by-$((2+2))-uuid\r") == {
        "surface_id": surface["id"], "surface_ref": "surface:1",
        "workspace_id": workspaces(backend)[0]["id"], "workspace_ref": ref}
    until(lambda: "by-4-uuid" in screen(backend, ref))  # "\r" is Enter typed as a character
    read = rpc("surface.read_text", surface_id="surface:1", lines=200)
    assert read["surface_ref"] == "surface:1" and "by-4-uuid" in read["text"]
    assert tb.base64.b64decode(read["base64"]).decode() == read["text"]
    assert "by-4-uuid" in rpc("surface.read_text", surface_id="surface:1", scrollback=True)["text"]
    assert [w["ref"] for w in rpc("workspace.list")["workspaces"]] == [ref]


def test_typing_or_reading_with_no_pane_named_is_refused_not_guessed(backend, work):
    ref = new(backend, work, "one")
    for argv in (["rpc", "surface.send_text", '{"text": "echo guessed"}'], ["rpc", "surface.send_key", '{"key": "enter"}'],
                 ["send", "echo guessed"], ["send-key", "enter"], ["read-screen"]):
        with pytest.raises(tb.Failure, match="not_found: Surface not found"):
            backend(argv)
    assert "guessed" not in screen(backend, ref)


def test_rpc_refuses_what_cmux_refuses(backend, work, capsys):
    new(backend, work, "one")
    assert tb.main(["rpc", "surface.read_text", '{"surface_id": "surface:99"}']) == 1
    assert capsys.readouterr().err == "Error: not_found: Surface not found for the given surface_id\n"
    assert tb.main(["rpc", "nope.nope", "{}"]) == 1
    assert capsys.readouterr().err == "Error: method_not_found: Unknown method\n"
    for params in ("[1]", "{not json"):
        assert tb.main(["rpc", "surface.read_text", params]) == 1
        assert capsys.readouterr().err == "Error: invalid_params: params must be a JSON object\n"


def test_a_key_tmux_would_type_as_text_is_refused(backend, work, monkeypatch):
    ref = new(backend, work, "one")
    with pytest.raises(tb.Failure, match="invalid_params: Unknown key 'hyper'"):
        backend(["send-key", "--workspace", ref, "hyper"])
    assert "hyper" not in screen(backend, ref)
    sent = []
    monkeypatch.setattr(tb, "_must", lambda *args: sent.append(args[-1]))
    for key in ("Enter", "escape", "shift+tab", "ctrl+c", "Control-D"):
        tb.send_key({"tmux_pane": "%0"}, key)
    assert sent == ["Enter", "Escape", "BTab", "C-c", "C-d"]


def test_send_types_into_the_focused_pane_of_the_workspace(backend, work):
    ref = new(backend, work, "one")
    assert backend(["send", "--workspace", ref, "echo sent-$((3+3))"]) == "OK\n"
    assert backend(["send-key", "--workspace", ref, "Enter"]) == "OK\n"
    until(lambda: "sent-6" in screen(backend, ref))


def test_text_that_starts_with_dashes_is_typed_not_read_as_a_flag(backend, work):
    ref = new(backend, work, "one")
    backend(["send", "--workspace", ref, "echo --resume $((4+4)) --json"])
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "--resume 8 --json" in screen(backend, ref))
    backend(["send", "--workspace", ref, "--", "echo", "--workspace", "after-$((5+5))"])  # after `--`, all is text
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "--workspace after-10" in screen(backend, ref))
    assert tb._parse(["tree", "--json", "--workspace"]) == (["tree", "--workspace"], {"--json": True})


def test_text_longer_than_one_tmux_command_arrives_whole(backend, work, monkeypatch):
    ref = new(backend, work, "one")
    monkeypatch.setattr(tb, "_CHUNK", 7)
    backend(["rpc", "surface.send_text", json.dumps({"surface_id": "surface:1", "text": "echo long-$((6+6))-text-é-end"})])
    backend(["rpc", "surface.send_text", json.dumps({"surface_id": "surface:1", "text": ""})])  # nothing to type
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "long-12-text-é-end" in screen(backend, ref))


def test_a_title_or_folder_with_a_hash_is_taken_as_written_not_expanded_by_tmux(backend, server):
    folder = server / "c#-project #{pid}"
    folder.mkdir()
    marker = server / "ran-by-tmux"
    title = f"td-9: Fix PR #12 #{{pid}} #(touch {marker}) ## done"
    ref = new(backend, folder, title)
    (ws,) = workspaces(backend)
    assert ws["title"] == title and ws["current_directory"] == os.path.realpath(folder)
    assert json.loads(backend(["list-workspaces", "--json"]))["workspaces"][0]["title"] == title
    backend(["send", "--workspace", ref, "pwd"])
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "c#-project #{pid}" in screen(backend, ref))
    time.sleep(0.5)
    assert not marker.exists()


def test_a_tmux_that_expands_nothing_still_gets_the_plain_title(monkeypatch):
    """Some tmux versions take a name as written. They keep the doubled #s, and are then given the plain title,
    one command at a time, only after that command has shown it expands nothing."""
    def tmux_that(expands_rename: bool):
        state, renames = {"name": "PR ##7"}, []

        def must(*args):
            if args[0] == "rename-window":
                renames.append(args[-1])
                state["name"] = args[-1].replace("##", "#") if expands_rename else args[-1]
            return state["name"] + "\n"
        monkeypatch.setattr(tb, "_must", must)
        tb._settle_name("@0", "PR #7")
        return state["name"], renames
    assert tmux_that(expands_rename=False) == ("PR #7", ["PR ##7", "PR #7"])
    assert tmux_that(expands_rename=True) == ("PR #7", ["PR ##7"])  # the plain title was never handed over


def test_a_session_that_did_not_start_in_its_folder_is_ended(backend, server, monkeypatch):
    folder = server / "has#hash"
    folder.mkdir()
    monkeypatch.setattr(tb, "_plain", lambda text: text.replace("#", "-missing-"))  # tmux falls back to the home
    monkeypatch.setattr(tb.time, "sleep", lambda seconds: None)
    with pytest.raises(tb.Failure, match="tmux_error: the new session started in .*, not in .*has#hash"):
        backend(["new-workspace", "--cwd", str(folder), "--name", "one"])
    assert workspaces(backend) == []


def test_a_session_whose_command_cant_be_typed_is_ended_not_left_behind(backend, work, monkeypatch):
    def refuse(surface, text):
        raise tb.Failure("tmux_error", "command too long")
    monkeypatch.setattr(tb, "send_text", refuse)
    with pytest.raises(tb.Failure, match="command too long"):
        backend(["new-workspace", "--cwd", str(work), "--name", "td-1: Fix it", "--command", "claude"])
    assert workspaces(backend) == []


# ── agent sessions ────────────────────────────────────────────────────────────

def test_a_claude_session_is_placed_in_the_pane_its_process_runs_in(backend, work, claude, monkeypatch, server):
    monkeypatch.setenv("HOME", str(server))
    one, two = new(backend, work, "one"), new(backend, work, "two")
    pid = claude.agent(backend, two)
    started = int(time.time() * 1000) - 5000
    claude.rows([claude.row(pid, "sess-in-two", startedAt=started, status="busy"),
                 claude.row(os.getpid(), "sess-not-in-tmux"),
                 claude.row(pid, "sess-headless", kind="print"),
                 {"pid": "7", "sessionId": "sess-bad-pid", "kind": "interactive"}, "junk"])
    listed = json.loads(backend(["sessions", "list", "--json", "--all"]))
    (row,) = listed["sessions"]
    assert listed["total_matches"] == 1
    ws = workspaces(backend)[1]
    assert (row["session_id"], row["agent"], row["pid"], row["status"]) == ("sess-in-two", "claude", pid, "busy")
    assert (row["workspace_id"], row["surface_id"]) == (ws["id"], ws["panes"][0]["surfaces"][0]["id"])
    assert (row["workspace_ref"], row["active_for_surface"], row["transcript_path"]) == (two, True, None)
    assert row["updated_at_unix"] == started / 1000 and row["started_at"].endswith("Z")
    assert json.loads(backend(["sessions", "list", "--session", "other", "--json"]))["sessions"] == []
    assert json.loads(backend(["sessions", "list", "--session", "sess-in-two", "--json"]))["sessions"] == [row]

    shown = json.loads(backend(["surface", "resume", "show", "--json", "--surface", "surface:2", "--workspace", two]))
    assert shown["resume_binding"] == {"kind": "claude", "name": "Claude Code", "checkpoint_id": "sess-in-two",
                                       "cwd": str(server), "source": "tmux", "updated_at": started / 1000}
    assert shown["workspace_ref"] == two
    empty = json.loads(backend(["surface", "resume", "show", "--json", "--surface", "surface:1", "--workspace", one]))
    assert empty == {"surface_ref": "surface:1", "workspace_ref": one, "pane_ref": "pane:1"}


def test_a_sessions_latest_moment_is_its_transcripts_unless_the_session_started_later(backend, work, claude,
                                                                                    monkeypatch, server):
    monkeypatch.setenv("HOME", str(server))
    ref = new(backend, work, "one")
    pid = claude.agent(backend, ref)
    transcript = agent_session.transcript_root(agent_session.CLAUDE, home=server) / "-proj" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    os.utime(transcript, (1_700_000_500, 1_700_000_500))

    def row(started_ms):
        claude.rows([claude.row(pid, "sess-1", startedAt=started_ms)])
        return json.loads(backend(["sessions", "list", "--json"]))["sessions"][0]
    assert row(1_700_000_000_000)["updated_at_unix"] == 1_700_000_500
    assert row(1_700_000_000_000)["transcript_path"] == str(transcript)
    # A resumed session: its transcript is older than its process until the first new line.
    assert row(1_700_000_900_000)["updated_at_unix"] == 1_700_000_900
    no_start = row(None)
    assert (no_start["started_at"], no_start["updated_at_unix"]) == (None, 1_700_000_500)
    transcript.unlink()
    monkeypatch.setattr(tb.agent_session, "transcript_for_session", lambda *a, **k: str(transcript))
    gone = row(None)
    assert (gone["updated_at"], gone["updated_at_unix"]) == (None, None)


def test_a_pane_with_two_candidate_sessions_gets_none(backend, work, claude):
    ref = new(backend, work, "one")
    pid = claude.agent(backend, ref)
    claude.rows([claude.row(pid, "sess-a"), claude.row(pid, "sess-b")])
    assert json.loads(backend(["sessions", "list", "--json"]))["sessions"] == []


def test_a_claude_process_that_is_not_its_panes_foreground_job_is_not_a_session(backend, work, claude, server):
    ref = new(backend, work, "one")
    pid_file = server / "bg-pid"
    backend(["send", "--workspace", ref, f"sleep 600 & echo $! > {pid_file}"])
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: pid_file.exists() and pid_file.read_text().strip())
    claude.rows([claude.row(int(pid_file.read_text()), "sess-bg")])
    assert json.loads(backend(["sessions", "list", "--json"]))["sessions"] == []


@pytest.mark.parametrize("printed", ["not json", '{"a": 1}', "exit 3", "hang"])
def test_a_claude_that_does_not_answer_is_an_error_not_an_empty_fleet(backend, work, claude, monkeypatch, capsys,
                                                                     printed):
    ref = new(backend, work, "one")
    pid = claude.agent(backend, ref)
    claude.rows([claude.row(pid, "sess-1")])
    assert len(tb.sessions(tb.tree())) == 1
    if printed == "exit 3":
        claude.program.write_text("#!/bin/sh\nexit 3\n")
    elif printed == "hang":
        real = subprocess.run

        def slow(cmd, **kwargs):
            if cmd[1:] == ["agents", "--json"]:
                raise subprocess.TimeoutExpired(cmd, tb.TIMEOUT_SEC)
            return real(cmd, **kwargs)
        monkeypatch.setattr(tb.subprocess, "run", slow)
    else:
        claude.rows(printed)
    for argv in (["sessions", "list", "--json", "--all"],
                 ["surface", "resume", "show", "--json", "--surface", "surface:1", "--workspace", ref]):
        assert tb.main(argv) == 1
        assert capsys.readouterr().err.startswith("Error: unavailable: `claude agents --json` didn't answer")


def test_no_claude_code_on_the_machine_is_no_claude_sessions(backend, work, claude, monkeypatch):
    ref = new(backend, work, "one")
    claude.rows([claude.row(claude.agent(backend, ref), "sess-1")])
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    assert json.loads(backend(["sessions", "list", "--json"]))["sessions"] == []
    claude.program.chmod(0o644)  # there, but not a program
    monkeypatch.setenv("CLAUDE_BIN", str(claude.program))
    assert tb.sessions(tb.tree()) == []


def test_a_ps_that_cant_be_run_is_an_error_not_an_empty_fleet(backend, work, claude, monkeypatch):
    ref = new(backend, work, "one")
    claude.rows([claude.row(claude.agent(backend, ref), "sess-1")])
    real = subprocess.run

    def no_ps(cmd, **kwargs):
        if cmd[0] == "ps":
            raise OSError("no ps")
        return real(cmd, **kwargs)
    monkeypatch.setattr(tb.subprocess, "run", no_ps)
    with pytest.raises(tb.Failure, match="unavailable: ps can't be run"):
        tb.sessions(tb.tree())


def test_identify_names_the_pane_it_runs_in_or_the_workspace_asked_about(backend, work, server, monkeypatch):
    one, two = new(backend, work, "one"), new(backend, work, "two")
    asked = json.loads(backend(["identify", "--workspace", two]))["caller"]
    assert (asked["workspace_ref"], asked["window_ref"], asked["surface_ref"]) == (two, "window:2", "surface:2")
    assert json.loads(backend(["identify"])) == {"caller": None}
    answer = server / "identify.json"
    backend(["send", "--workspace", one, f"{sys.executable} {BACKEND} identify --json > {answer}"])
    backend(["send-key", "--workspace", one, "enter"])
    until(lambda: answer.exists() and answer.read_text().strip().endswith("}"))
    inside = json.loads(answer.read_text())["caller"]
    assert (inside["workspace_ref"], inside["pane_ref"], inside["is_browser_surface"]) == (one, "pane:1", False)
    # The same pane id on some other tmux server is not this one's pane.
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setenv("TMUX", "/tmp/another-server/default,1,0")
    assert json.loads(backend(["identify"])) == {"caller": None}


# ── events ────────────────────────────────────────────────────────────────────

def test_events_report_a_finished_turn_and_a_session_that_starts_waiting(backend, work, claude, monkeypatch, server):
    monkeypatch.setenv("HOME", str(server))
    ref = new(backend, work, "one")
    pid = claude.agent(backend, ref)
    transcript = agent_session.transcript_root(agent_session.CLAUDE, home=server) / "-proj" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    steps = [
        dict(status="idle"),                       # first seen: nothing
        dict(status="busy"),                       # started a turn: nothing
        dict(status="idle"),                       # finished it: Stop
        dict(status="idle"),                       # no change: nothing
        dict(status="idle", grow=True),            # a whole turn between two looks: Stop
        dict(status="waiting", waitingFor="input needed"),   # Notification
        dict(status="waiting", waitingFor="input needed"),   # still waiting: nothing more
        dict(status="idle", waitingFor="dialog open"),       # waiting by its dialog alone: nothing more
        dict(status="blocked"),                    # still waiting
        dict(status="shell"),                      # the user ran a shell command: not a turn's end
        "tmux down",                               # tmux didn't answer: what was seen is kept
        dict(status="busy"),
        dict(status="idle"),                       # Stop
        "gone",                                    # the session ended
        dict(status="idle"),                       # seen again as new: nothing
    ]
    written, real_tree = [], tb.tree

    def next_step(_seconds=None):
        step = steps.pop(0) if steps else None
        monkeypatch.setattr(tb, "tree", real_tree)
        if step == "tmux down":
            def down():
                raise tb.Failure("timeout", "tmux didn't answer")
            monkeypatch.setattr(tb, "tree", down)
        elif step == "gone":
            claude.rows([])
        elif step:
            if step.pop("grow", False):
                transcript.write_text(transcript.read_text() + "{}\n")
            claude.rows([claude.row(pid, "sess-1", **step)])

    rounds = len(steps)
    next_step()
    tb.events(written.append, sleep=next_step, rounds=rounds)
    events = [json.loads(line) for line in written]
    assert [e["name"] for e in events] == ["agent.hook.Stop", "agent.hook.Stop", "agent.hook.Notification",
                                           "agent.hook.Stop"]
    ws = workspaces(backend)[0]
    for event in events:
        assert event["type"] == "event" and event["workspace_id"] == ws["id"]
        assert event["payload"]["session_id"] == "sess-1" and event["payload"]["cwd"] == str(server)
    assert len({e["payload"]["_opencode_request_id"] for e in events}) == 4
    assert all(line.endswith("\n") for line in written)


def test_the_events_command_streams_until_its_reader_goes_away(backend, monkeypatch):
    def one_event(write, **kwargs):
        write('{"type": "event"}\n')
    monkeypatch.setattr(tb, "events", one_event)
    assert backend(["events", "--category", "agent", "--reconnect", "--no-heartbeat"]) == '{"type": "event"}\n'

    def reader_gone(write, **kwargs):
        raise BrokenPipeError
    monkeypatch.setattr(tb, "events", reader_gone)
    assert tb.main(["events"]) == 0


def test_events_wait_between_looks(server, monkeypatch):
    waits, looks = [], []
    monkeypatch.setattr(tb, "sessions", lambda doc: looks.append(doc) or [])
    tb.events(lambda line: None, sleep=waits.append, rounds=3)
    assert waits == [tb.EVENTS_POLL_SEC, tb.EVENTS_POLL_SEC] and len(looks) == 3


def test_events_of_a_kind_tmux_doesnt_have_are_silence(server, monkeypatch, backend):
    looks, written = [], []
    monkeypatch.setattr(tb, "sessions", lambda doc: looks.append(doc) or [])
    tb.events(written.append, sleep=lambda seconds: None, rounds=3, category="workspace")
    assert (looks, written) == ([], [])
    asked = {}
    monkeypatch.setattr(tb, "events", lambda write, **kwargs: asked.update(kwargs))
    backend(["events", "--category", "workspace", "--reconnect"])
    assert asked == {"category": "workspace"}


# ── the command line ──────────────────────────────────────────────────────────

def test_a_command_the_backend_does_not_have_is_refused(server, capsys):
    assert tb.main(["hooks", "claude", "session-start"]) == 2
    assert capsys.readouterr().err == "Error: Unknown command: hooks (sessions are kept in tmux)\n"
    assert tb.main([]) == 2
    assert "Unknown command: (none)" in capsys.readouterr().err
    for incomplete in (["send", "--workspace", "workspace:1"], ["rpc"], ["sessions"], ["surface", "resume"]):
        assert tb.main(incomplete) == 2
        capsys.readouterr()


def test_it_runs_as_a_program_the_way_the_scripts_run_cmux(server, work):
    made = subprocess.run([sys.executable, str(BACKEND), "new-workspace", "--cwd", str(work), "--name", "as program",
                           "--focus", "false", "--command", "echo ran-$((5+5))"], capture_output=True, text=True)
    assert (made.returncode, made.stdout, made.stderr) == (0, "OK workspace:1\n", "")
    listed = subprocess.run([str(BACKEND), "list-workspaces"], capture_output=True, text=True)
    assert listed.stdout == "* workspace:1  as program  [selected]\n"
    gone = subprocess.run([sys.executable, str(BACKEND), "tree", "--workspace", "workspace:5", "--json"],
                          capture_output=True, text=True)
    assert (gone.returncode, gone.stderr) == (1, "Error: invalid_params: Missing or invalid workspace_id\n")


def test_it_keeps_to_the_syntax_of_the_python_macos_ships():
    """Scripts run this file through its `#!` line, which can be the system's Python 3.9."""
    import ast
    for path in (BACKEND, BACKEND.parent / "agent_session.py"):
        ast.parse(path.read_text(), feature_version=(3, 9))
    assert BACKEND.read_text().startswith("#!/usr/bin/env python3\n") and os.access(BACKEND, os.X_OK)
    assert "from __future__ import annotations" in BACKEND.read_text()
