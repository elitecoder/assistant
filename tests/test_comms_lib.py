"""Unit tests for comms_lib — every helper, 100% line coverage."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import comms_lib as cl


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def tmp_paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cmux_bin = tmp_path / "fake-cmux"
    cmux_bin.write_text("#!/bin/sh\necho 'fake'\n")
    cmux_bin.chmod(0o755)
    env = {
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(bin_dir),
        "CMUX_BIN": str(cmux_bin),
    }
    paths = cl.Paths.from_env(env)
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    return paths


# --------------------------------------------------------------------------- Paths

class TestPathsFromEnv:
    def test_default(self, monkeypatch):
        monkeypatch.setenv("HOME", "/tmp/x")
        for k in ("COMMS_HOME", "COMMS_ASSISTANT_DIR", "COMMS_BIN_DIR", "CMUX_BIN"):
            monkeypatch.delenv(k, raising=False)
        p = cl.Paths.from_env()
        assert p.home == Path("/tmp/x")
        assert p.ledger == Path("/tmp/x/.assistant/actions-ledger.jsonl")
        assert p.curator == Path("/tmp/x/dev/assistant/bin/assistant-curator.py")
        assert p.spawn_comms == Path("/tmp/x/dev/assistant/bin/spawn-comms.sh")

    def test_overrides(self):
        env = {"HOME": "/h", "COMMS_HOME": "/h2",
               "COMMS_ASSISTANT_DIR": "/a", "COMMS_BIN_DIR": "/b", "CMUX_BIN": "/c"}
        p = cl.Paths.from_env(env)
        assert p.home == Path("/h2")
        assert p.assistant_dir == Path("/a")
        assert p.ledger == Path("/a/actions-ledger.jsonl")
        assert p.curator == Path("/b/assistant-curator.py")
        assert p.cmux_bin == Path("/c")
        assert p.threads == Path("/a/comms/threads.jsonl")
        assert p.tg_cursor == Path("/a/comms/tg.cursor")
        assert p.terminal_tab == Path("/a/comms/terminal-tab.txt")


# --------------------------------------------------------------------------- Config

class TestConfig:
    def test_load_save_roundtrip(self, tmp_paths):
        raw = {"telegram": {"bot_token": "t", "chat_ids": [1, 2]},
               "stale_heartbeat_sec": 999, "mute_until_epoch": 5}
        tmp_paths.config.write_text(json.dumps(raw))
        cfg = cl.Config.load(tmp_paths.config)
        assert cfg.bot_token == "t"
        assert cfg.chat_ids == {1, 2}
        assert cfg.stale_heartbeat_sec == 999
        cfg.mute_until_epoch = 100
        cfg.save()
        rt = json.loads(tmp_paths.config.read_text())
        assert rt["mute_until_epoch"] == 100
        assert sorted(rt["telegram"]["chat_ids"]) == [1, 2]
        assert oct(tmp_paths.config.stat().st_mode)[-3:] == "600"

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(SystemExit):
            cl.Config.load(tmp_path / "nope.json")

    def test_save_without_path_raises(self):
        cfg = cl.Config(bot_token="t", chat_ids={1})
        with pytest.raises(RuntimeError):
            cfg.save()

    def test_load_uses_defaults_when_missing(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"telegram": {"bot_token": "t"}}))
        cfg = cl.Config.load(path)
        assert cfg.chat_ids == set()
        assert cfg.stale_heartbeat_sec == 600
        assert cfg.mute_until_epoch == 0


# --------------------------------------------------------------------------- time helpers

class TestNowIso:
    def test_default(self):
        s = cl.now_iso()
        assert s.endswith("Z") and len(s) == 20

    def test_with_clock(self):
        assert cl.now_iso(lambda: 0) == "1970-01-01T00:00:00Z"


class TestFmtAge:
    def test_seconds(self):
        assert cl.fmt_age(0) == "0s"
        assert cl.fmt_age(45) == "45s"

    def test_minutes(self):
        assert cl.fmt_age(60) == "1m"
        assert cl.fmt_age(3599) == "59m"

    def test_hours(self):
        assert cl.fmt_age(3600) == "1h0m"
        assert cl.fmt_age(7320) == "2h2m"

    def test_days(self):
        assert cl.fmt_age(86400) == "1d"
        assert cl.fmt_age(86400 * 3) == "3d"

    def test_negative_clamped(self):
        assert cl.fmt_age(-5) == "0s"


class TestParseDuration:
    def test_units(self):
        assert cl.parse_duration("30s") == 30
        assert cl.parse_duration("5m") == 300
        assert cl.parse_duration("2h") == 7200
        assert cl.parse_duration("2H") == 7200

    def test_bad(self):
        assert cl.parse_duration("5d") is None
        assert cl.parse_duration("abh") is None
        assert cl.parse_duration("") is None
        assert cl.parse_duration("h") is None
        assert cl.parse_duration("-5m") is None


# --------------------------------------------------------------------------- formatting

class TestEscapeHtml:
    def test_basic(self):
        assert cl.escape_html("<b>x</b>&y") == "&lt;b&gt;x&lt;/b&gt;&amp;y"


class TestFmtActionLine:
    def test_full(self):
        e = {"kind": "cleanup", "key": "x", "ws_ref": "ws:1", "td": "td-1",
             "outcome": "verified", "verified_via": "jsonl_transcript",
             "pulse_idx": 12, "evidence": "ok"}
        s = cl.fmt_action_line(e)
        assert "[cleanup]" in s and "ok" in s and "ws:1" in s and "td-1" in s and "pulse=12" in s

    def test_screen_read_flagged(self):
        s = cl.fmt_action_line({"kind": "k", "key": "x", "verified_via": "screen_read"})
        assert "(!)screen_read" in s

    def test_missing_fields_default_dashes(self):
        s = cl.fmt_action_line({})
        assert "[?]" in s and "ws=-" in s and "td=-" in s

    def test_evidence_truncated_to_200(self):
        s = cl.fmt_action_line({"evidence": "x" * 500})
        assert "x" * 200 in s and "x" * 201 not in s

    def test_html_escaped(self):
        # Hostile content in user-controlled fields must be escaped — only the
        # template's own <b>/<code>/<i> wrappers should be raw HTML.
        s = cl.fmt_action_line({"kind": "<scr>", "key": "<x>", "evidence": "<i>x</i>"})
        assert "<scr>" not in s
        assert "&lt;scr&gt;" in s
        assert "&lt;x&gt;" in s
        assert "&lt;i&gt;x&lt;/i&gt;" in s

    def test_outcome_markers(self):
        for o, m in [("failed", "fail"), ("skipped", "skip"), ("rejected", "rej")]:
            assert m in cl.fmt_action_line({"outcome": o})

    def test_unknown_outcome_passes_through(self):
        assert "weird" in cl.fmt_action_line({"outcome": "weird"})


class TestFmtHeartbeatAlert:
    def test_basic(self):
        s = cl.fmt_heartbeat_alert({"ws_ref": "ws:1", "status": "frozen",
                                    "last_pulse_iso": "2026-05-27T22:00:00Z"}, 700)
        assert "ws:1" in s and "frozen" in s and "11m" in s

    def test_missing_fields(self):
        s = cl.fmt_heartbeat_alert({}, 0)
        assert "ws=?" in s and "status=?" in s


# --------------------------------------------------------------------------- ledger cursor

class TestLedgerCursor:
    def test_read_missing(self, tmp_paths):
        assert cl.read_ledger_cursor(tmp_paths) == 0

    def test_read_garbage(self, tmp_paths):
        tmp_paths.cursor.write_text("not-int")
        assert cl.read_ledger_cursor(tmp_paths) == 0

    def test_read_blank(self, tmp_paths):
        tmp_paths.cursor.write_text("")
        assert cl.read_ledger_cursor(tmp_paths) == 0

    def test_read_value(self, tmp_paths):
        tmp_paths.cursor.write_text("42\n")
        assert cl.read_ledger_cursor(tmp_paths) == 42

    def test_write(self, tmp_paths):
        cl.write_ledger_cursor(tmp_paths, 99)
        assert tmp_paths.cursor.read_text() == "99"

    def test_initialize_already_set(self, tmp_paths):
        tmp_paths.cursor.write_text("999")
        cl.initialize_cursor_if_missing(tmp_paths)
        assert tmp_paths.cursor.read_text() == "999"

    def test_initialize_with_ledger(self, tmp_paths):
        tmp_paths.ledger.write_text("hello\nworld\n")
        cl.initialize_cursor_if_missing(tmp_paths)
        assert int(tmp_paths.cursor.read_text()) == tmp_paths.ledger.stat().st_size

    def test_initialize_no_ledger(self, tmp_paths):
        cl.initialize_cursor_if_missing(tmp_paths)
        assert tmp_paths.cursor.read_text() == "0"


class TestReadNewLedgerLines:
    def test_no_file(self, tmp_paths):
        assert cl.read_new_ledger_lines(tmp_paths) == []

    def test_no_new(self, tmp_paths):
        tmp_paths.ledger.write_text("")
        cl.write_ledger_cursor(tmp_paths, 0)
        assert cl.read_new_ledger_lines(tmp_paths) == []

    def test_appended_returned(self, tmp_paths):
        e1 = {"kind": "k", "key": "1"}
        tmp_paths.ledger.write_text(json.dumps(e1) + "\n")
        cl.write_ledger_cursor(tmp_paths, 0)
        out = cl.read_new_ledger_lines(tmp_paths)
        assert out == [e1]
        assert cl.read_ledger_cursor(tmp_paths) == tmp_paths.ledger.stat().st_size

    def test_partial_read_advances(self, tmp_paths):
        e1, e2 = {"kind": "k1"}, {"kind": "k2"}
        line1, line2 = json.dumps(e1) + "\n", json.dumps(e2) + "\n"
        tmp_paths.ledger.write_text(line1)
        cl.write_ledger_cursor(tmp_paths, len(line1.encode("utf-8")))
        with open(tmp_paths.ledger, "a") as f:
            f.write(line2)
        out = cl.read_new_ledger_lines(tmp_paths)
        assert out == [e2]

    def test_rotation_resets_cursor(self, tmp_paths):
        tmp_paths.ledger.write_text("")
        cl.write_ledger_cursor(tmp_paths, 9999)
        assert cl.read_new_ledger_lines(tmp_paths) == []
        assert cl.read_ledger_cursor(tmp_paths) == 0

    def test_skips_malformed_and_blank(self, tmp_paths):
        body = "not-json\n\n" + json.dumps({"kind": "ok"}) + "\n"
        tmp_paths.ledger.write_text(body)
        cl.write_ledger_cursor(tmp_paths, 0)
        out = cl.read_new_ledger_lines(tmp_paths)
        assert out == [{"kind": "ok"}]


# --------------------------------------------------------------------------- TG cursor

class TestTgCursor:
    def test_read_missing(self, tmp_paths):
        assert cl.read_tg_cursor(tmp_paths) == 0

    def test_read_garbage(self, tmp_paths):
        tmp_paths.tg_cursor.write_text("xx")
        assert cl.read_tg_cursor(tmp_paths) == 0

    def test_read_blank(self, tmp_paths):
        tmp_paths.tg_cursor.write_text("")
        assert cl.read_tg_cursor(tmp_paths) == 0

    def test_round_trip(self, tmp_paths):
        cl.write_tg_cursor(tmp_paths, 12345)
        assert cl.read_tg_cursor(tmp_paths) == 12345


# --------------------------------------------------------------------------- threads

class TestThreads:
    def test_append_and_lookup_by_msg(self, tmp_paths):
        cl.append_thread(tmp_paths, "ledger-1", 100, 42, "action", clock=lambda: 0)
        cl.append_thread(tmp_paths, "ledger-2", 101, 42, "urgent", clock=lambda: 1)
        rec = cl.lookup_thread_by_msg_id(tmp_paths, 100)
        assert rec is not None and rec["ledger_key"] == "ledger-1" and rec["kind"] == "action"
        rec2 = cl.lookup_thread_by_msg_id(tmp_paths, 101)
        assert rec2["kind"] == "urgent"

    def test_lookup_msg_missing_file(self, tmp_paths):
        assert cl.lookup_thread_by_msg_id(tmp_paths, 999) is None

    def test_lookup_msg_not_found(self, tmp_paths):
        cl.append_thread(tmp_paths, "k", 1, 1, "action")
        assert cl.lookup_thread_by_msg_id(tmp_paths, 999) is None

    def test_lookup_msg_skips_malformed_lines(self, tmp_paths):
        # Hand-write a corrupt + good line.
        tmp_paths.threads.write_text("not-json\n" + json.dumps({"tg_msg_id": 7, "ledger_key": "x"}) + "\n")
        rec = cl.lookup_thread_by_msg_id(tmp_paths, 7)
        assert rec is not None and rec["ledger_key"] == "x"

    def test_lookup_msg_blank_lines_skipped(self, tmp_paths):
        tmp_paths.threads.write_text("\n" + json.dumps({"tg_msg_id": 7}) + "\n\n")
        assert cl.lookup_thread_by_msg_id(tmp_paths, 7) == {"tg_msg_id": 7}

    def test_lookup_msg_last_wins_on_dup(self, tmp_paths):
        cl.append_thread(tmp_paths, "k1", 5, 1, "action")
        cl.append_thread(tmp_paths, "k2", 5, 1, "urgent")
        rec = cl.lookup_thread_by_msg_id(tmp_paths, 5)
        assert rec["ledger_key"] == "k2"

    def test_lookup_by_ledger_key_missing_file(self, tmp_paths):
        assert cl.lookup_thread_by_ledger_key(tmp_paths, "x") == []

    def test_lookup_by_ledger_key(self, tmp_paths):
        cl.append_thread(tmp_paths, "k", 1, 10, "action")
        cl.append_thread(tmp_paths, "k", 2, 11, "action")
        cl.append_thread(tmp_paths, "other", 3, 12, "action")
        rows = cl.lookup_thread_by_ledger_key(tmp_paths, "k")
        assert len(rows) == 2 and {r["chat_id"] for r in rows} == {10, 11}

    def test_lookup_by_ledger_key_skips_malformed_and_blank(self, tmp_paths):
        tmp_paths.threads.write_text(
            "not-json\n\n" +
            json.dumps({"ledger_key": "k", "tg_msg_id": 1}) + "\n"
        )
        rows = cl.lookup_thread_by_ledger_key(tmp_paths, "k")
        assert len(rows) == 1


# --------------------------------------------------------------------------- run_cmd

class TestRunCmd:
    def test_success(self):
        rc, out, _ = cl.run_cmd(["/bin/sh", "-c", "echo hi"])
        assert rc == 0 and "hi" in out

    def test_failure(self):
        rc, _, _ = cl.run_cmd(["/bin/sh", "-c", "exit 7"])
        assert rc == 7

    def test_timeout(self):
        rc, _, err = cl.run_cmd(["/bin/sh", "-c", "sleep 5"], timeout=1)
        assert rc == -1 and "timeout" in err

    def test_not_found(self):
        rc, _, err = cl.run_cmd(["/no/such/x" + "y" * 30])
        assert rc == -1 and err


class TestCmuxReadScreen:
    def test_success(self, tmp_paths):
        assert cl.cmux_read_screen(tmp_paths, "ws:1") == "fake"

    def test_empty(self, tmp_paths):
        tmp_paths.cmux_bin.write_text("#!/bin/sh\nexit 0\n")
        assert cl.cmux_read_screen(tmp_paths, "ws:1") == "(empty)"

    def test_failure(self, tmp_paths):
        tmp_paths.cmux_bin.write_text("#!/bin/sh\necho 'oops' >&2; exit 5\n")
        out = cl.cmux_read_screen(tmp_paths, "ws:1")
        assert "rc=5" in out and "oops" in out

    def test_truncates_to_lines(self, tmp_paths):
        body = "\n".join(f"line{i}" for i in range(100))
        tmp_paths.cmux_bin.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\n")
        out = cl.cmux_read_screen(tmp_paths, "ws:1", lines=50)
        assert "line99" in out and "line49" not in out


# --------------------------------------------------------------------------- comms heartbeat

class TestCommsHeartbeat:
    def test_write_then_read(self, tmp_paths):
        cl.write_comms_heartbeat(tmp_paths, status="active", pulse_idx=3, clock=lambda: 1779999999)
        rec = cl.read_comms_heartbeat(tmp_paths)
        assert rec["status"] == "active"
        assert rec["pulse_idx"] == 3
        assert rec["epoch"] == 1779999999
        assert rec["pid"] == os.getpid()

    def test_write_with_note(self, tmp_paths):
        cl.write_comms_heartbeat(tmp_paths, status="frozen", note="boot complete")
        assert cl.read_comms_heartbeat(tmp_paths)["note"] == "boot complete"

    def test_read_missing(self, tmp_paths):
        assert cl.read_comms_heartbeat(tmp_paths) is None

    def test_read_unparseable(self, tmp_paths):
        tmp_paths.daemon_hb.write_text("not json")
        assert cl.read_comms_heartbeat(tmp_paths) is None

    def test_default_clock_used_when_none(self, tmp_paths):
        # Just ensure no clock raises and we get reasonable values.
        cl.write_comms_heartbeat(tmp_paths)
        rec = cl.read_comms_heartbeat(tmp_paths)
        assert rec["epoch"] > 0
