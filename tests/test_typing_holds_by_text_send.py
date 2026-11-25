"""Pro mode, through `bin/cmux-send.py`, the merge script and "run now": a typing is held and counted by what is typed,
and "run now" types what a proposal says."""
from __future__ import annotations

import json
import re
import subprocess as sp
import sys
from pathlib import Path
from unittest import mock

import pytest

from assistant import feedback, session_effects as se

from test_send_dispatch_in_process import load_module
from test_session_effects_send import Rpc, flags_of, home, ledger as send_ledger, run, send  # noqa: F401 - fixtures
from test_todo_server import _seed_proposal, env, mod as server  # noqa: F401 - fixtures

REPO = Path(__file__).resolve().parent.parent
HEX16 = re.compile(r"[0-9a-f]{16}")
TEXTS = ["/merge-when-ready 7", "/merge-when-ready 8", "/merge-when-ready 9", "/merge-when-ready 10"]
SEND_ARGV = {  # the flags each caller gives bin/cmux-send.py
    "cli": [],
    "run-now": ["--source", "run-now", "--auth-kind", "tap", "--auth-ref", "/proposal/run-now"],
    "merge": ["--source", "merge", "--auth-kind", "standing", "--auth-ref", "merge"],
}


def test_a_task_with_an_auto_id_can_be_typed_for_by_the_script_too(send, home):
    code, _ = run(send, Rpc(home), "--enter", "--task", "td-auto-1a2b3c4d")
    assert code == 0 and "task" not in send_ledger(home)[0]["target"]


# --- through bin/cmux-send.py ----------------------------------------------------------------------------------------

def unconfirmed(send):
    """The transcript is there and doesn't grow: the typing went out and couldn't be confirmed."""
    return mock.patch.object(send, "find_transcript_for_ws", return_value="/t.jsonl"), \
        mock.patch.object(send, "file_size", return_value=5)


def typed(send, rpc, text, source, confirmed=True):
    argv = ["--enter", *SEND_ARGV[source]]
    if confirmed:
        return run(send, rpc, *argv, text=text)
    patches = unconfirmed(send)
    with patches[0], patches[1]:
        return run(send, rpc, *argv, text=text)


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_four_different_texts_into_one_session_are_all_typed_by_the_script(send, home, source):
    for text in TEXTS:
        rpc = Rpc(home)
        assert typed(send, rpc, text, source)[0] == 0 and rpc.calls[0][1]["text"] == text
    intents = [r for r in send_ledger(home) if r["kind"] == "intent"]
    assert len({r["target"]["msg_digest"] for r in intents}) == 4
    assert all(HEX16.fullmatch(r["target"]["msg_digest"]) and r["target"]["source"] == source for r in intents)
    raw = (home / ".assistant/effects/effects.jsonl").read_text()
    assert not any(t in raw for t in TEXTS)


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_a_different_text_right_after_an_unconfirmed_one_is_typed_and_the_same_text_is_still_held(send, home, source):
    assert typed(send, Rpc(home), TEXTS[0], source, confirmed=False)[0] == 0
    assert send_ledger(home)[1]["outcome"] == "unknown"
    rpc = Rpc(home)
    assert typed(send, rpc, TEXTS[1], source)[0] == 0 and rpc.calls
    rpc = Rpc(home)
    code, record = typed(send, rpc, TEXTS[0], source)
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "unknown"
    assert "couldn't tell if it arrived" in record["refused"]["reason"]


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_the_same_text_is_still_stopped_the_fourth_time_by_the_script(send, home, source):
    for _ in range(3):
        assert typed(send, Rpc(home), TEXTS[0], source)[0] == 0
    rpc = Rpc(home)
    code, record = typed(send, rpc, TEXTS[0], source)
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "loop"


def test_a_nudge_is_still_limited_whatever_its_text(send, home):
    flags = ["--enter", "--source", "nudge:stranded", "--auth-kind", "standing", "--auth-ref", "nudge:stranded"]
    for text in TEXTS[:3]:
        assert run(send, Rpc(home), *flags, text=text)[0] == 0
    rpc = Rpc(home)
    code, record = run(send, rpc, *flags, text=TEXTS[3])
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "loop"
    assert all("msg_digest" not in r["target"] for r in send_ledger(home) if r["kind"] == "intent")


def test_a_nudge_after_an_unconfirmed_one_is_still_held_whatever_its_text(send, home):
    flags = ["--enter", "--source", "cleanup", "--auth-kind", "standing", "--auth-ref", "cleanup"]
    patches = unconfirmed(send)
    with patches[0], patches[1]:
        assert run(send, Rpc(home), *flags, text=TEXTS[0])[0] == 0
    code, record = run(send, Rpc(home), *flags, text=TEXTS[1])
    assert code == 4 and record["refused"]["code"] == "unknown"


def test_the_script_makes_no_digest_for_a_source_that_doesnt_carry_one(send, home):
    """No secret is read or made for a nudge: the digest is only asked for when the key will use it."""
    with mock.patch.object(se, "typed_digest", side_effect=AssertionError("not for a nudge")):
        assert run(send, Rpc(home), "--enter", "--source", "nudge:stranded", "--auth-kind", "standing",
                   "--auth-ref", "nudge:stranded")[0] == 0
    assert not (home / ".assistant/feedback").exists()


def test_a_digest_that_cant_be_made_still_types_under_the_old_key(send, home):
    with mock.patch.object(se, "typed_digest", return_value=""):
        code, _ = typed(send, Rpc(home), TEXTS[0], "cli")
    assert code == 0 and send_ledger(home)[0]["target"] == {"source": "cli", "session": "workspace:1000010005"}


def test_the_merge_script_gives_the_send_script_a_source_whose_text_is_its_own(send, home):
    """The argv `bin/merge-pr-dispatch.py` really builds is run through the script: two pull requests, two texts."""
    merge = load_module(home, "merge-pr-dispatch.py")
    for number in (7, 8, 9, 10):
        out = json.dumps({"outcome": "refused", "refused": {"reason": "x"}})
        with mock.patch.object(merge.subprocess, "run", return_value=mock.Mock(returncode=4, stdout=out, stderr="")) as ran:
            merge.step2_send_and_verify("workspace:1000010005", f"/merge-when-ready {number}", "standing", pro=True)
        flags = flags_of(ran.call_args.args[0])
        assert flags["--source"] == "merge" and flags["--text"] == f"/merge-when-ready {number}"
        assert run(send, Rpc(home), "--enter", "--source", flags["--source"], "--auth-kind", flags["--auth-kind"],
                   "--auth-ref", flags["--auth-ref"], text=flags["--text"])[0] == 0


# --- "run now" through the dashboard's own function, with the real script ------------------------------------------

def real_script(tmp_path, home, modes):
    """Make `server.subprocess.run` run the real `bin/cmux-send.py` in Pro mode with cmux faked: `modes` says, per
    run, `ok` or `timeout` (cmux didn't answer, so the typing may have landed)."""
    (home / ".assistant").mkdir(parents=True, exist_ok=True)
    (home / ".assistant/config.json").write_text(json.dumps({"mode": "pro"}))
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(
        "import sys, importlib.util as u\n"
        "mode = sys.argv[-1]\n"
        "sys.argv = ['cmux-send.py'] + sys.argv[1:-1]\n"
        f"spec = u.spec_from_file_location('s', {str(REPO / 'bin/cmux-send.py')!r}); s = u.module_from_spec(spec)\n"
        "spec.loader.exec_module(s)\n"
        "s.resolve_terminal_surface = lambda ws: ('S-1', 'surface:1', 'tty', 't')\n"
        "s.rpc = (lambda m, p: (124, {})) if mode == 'timeout' else (lambda m, p: (0, {}))\n"
        "s.time.sleep = lambda x: None\n"
        f"s.SENDS_LOG = {str(tmp_path / 'sends.jsonl')!r}\n"
        "s.main()\n")
    real, queue = sp.run, iter(modes)
    return lambda argv, **kw: real([sys.executable, str(wrapper), *argv[2:], next(queue)],
                                   **{**kw, "env": {"HOME": str(home), "PATH": "/usr/bin"}})


def press(env, text):
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": text,
                         "touches": [{"type": "session", "ref": "workspace:1000010007"}]})
    return server.proposal_action("prop-1", "run-now", {})


def test_four_run_nows_with_four_different_texts_are_all_typed(env, tmp_path, monkeypatch):
    home = tmp_path / "rn-home"
    monkeypatch.setattr(server.subprocess, "run", real_script(tmp_path, home, ["ok"] * 4))
    for text in TEXTS:
        assert press(env, text) == (True, "prop-1.run-now ok")
    rows_ = [json.loads(line) for line in (home / ".assistant/effects/effects.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in rows_ if r["kind"] == "result"] == ["done"] * 4
    assert len({r["target"]["msg_digest"] for r in rows_ if r["kind"] == "intent"}) == 4


def test_the_same_run_now_is_still_stopped_the_fourth_time_and_says_so(env, tmp_path, monkeypatch):
    monkeypatch.setattr(server.subprocess, "run", real_script(tmp_path, tmp_path / "rn-home", ["ok"] * 4))
    for _ in range(3):
        assert press(env, TEXTS[0])[0] is True
    ok, why = press(env, TEXTS[0])
    assert ok is False and "three times" in why


def test_a_different_run_now_goes_through_after_one_nobody_could_tell_about(env, tmp_path, monkeypatch):
    monkeypatch.setattr(server.subprocess, "run", real_script(tmp_path, tmp_path / "rn-home", ["timeout", "ok", "ok"]))
    ok, why = press(env, TEXTS[0])
    assert ok is False and why.startswith("Assistant sent this but didn't hear back")
    assert press(env, TEXTS[1]) == (True, "prop-1.run-now ok")
    ok, why = press(env, TEXTS[0])
    assert ok is False and "couldn't tell if it arrived" in why


# --- 3. "run now" types what the proposal says (the default mode types it whatever it is) -------------------------------

@pytest.mark.parametrize("text", ["x" * 501, "x" * 5000, "one\ntwo", "one\n\ntwo\n", "col1\tcol2", "line one\nline two\n" * 40,
                                  "naïve — “quoted” 日本語"])
def test_run_now_types_a_long_or_several_line_proposal_as_it_says_it(env, monkeypatch, text):
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": text,
                         "touches": [{"type": "session", "ref": "workspace:1000010007"}]})
    seen = []
    monkeypatch.setattr(server.subprocess, "run", lambda argv, **k: seen.append(argv) or mock.Mock(returncode=0))
    assert server.proposal_action("prop-1", "run-now", {}) == (True, "prop-1.run-now ok")
    assert len(seen) == 1 and seen[0][seen[0].index("--text") + 1] == text and "--enter" in seen[0]


@pytest.mark.parametrize("text, named", [
    ("a\x03b", "U+0003"), ("a\x1b[Zb", "U+001B"), ("a\x15b", "U+0015"), ("a\x1b[201~b", "U+001B"), ("a b", "U+2028"),
    ("a b", "U+2029"), ("a‮b", "U+202E"), ("one\rtwo", "U+000D"), ("a​b", "U+200B"), ("a\x00b", "U+0000"),
])
def test_run_now_still_refuses_characters_nobody_could_see_and_says_which(env, monkeypatch, text, named):
    path = _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": text,
                                "touches": [{"type": "session", "ref": "workspace:1000010007"}]})
    before = path.read_text()
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: pytest.fail("must not type"))
    ok, why = server.proposal_action("prop-1", "run-now", {})
    assert ok is False and named in why and "nothing was typed" in why and path.read_text() == before


def test_run_now_names_at_most_five_hidden_characters(env, monkeypatch):
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": "".join(map(chr, range(1, 9))),
                         "touches": [{"type": "session", "ref": "workspace:1000010007"}]})
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: pytest.fail("must not type"))
    why = server.proposal_action("prop-1", "run-now", {})[1]
    assert why.count("U+") == 5 and "U+0001" in why and "U+0006" not in why


@pytest.mark.parametrize("action", ["", None, ["a"], 7])
def test_run_now_with_no_text_types_nothing(env, monkeypatch, action):
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": action,
                         "touches": [{"type": "session", "ref": "workspace:1000010007"}]})
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: pytest.fail("must not type"))
    ok, why = server.proposal_action("prop-1", "run-now", {})
    assert ok is False and "no text to type" in why


def test_the_script_types_several_lines_in_one_text_call_and_then_enter_like_the_default_mode_does(send, home):
    """In Pro mode and in the default mode `bin/cmux-send.py` sends the text in one `surface.send_text` and then Enter."""
    text = "first line\nsecond line\n\tthird"
    rpc = Rpc(home)
    assert run(send, rpc, "--enter", text=text)[0] == 0
    assert [(m, p.get("text") or p.get("key")) for m, p in rpc.calls] == [("surface.send_text", text), ("surface.send_key", "enter")]
