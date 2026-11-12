import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals/observer"))
spec = importlib.util.spec_from_file_location("replay", REPO / "evals/observer/replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_the_cut_point_reproduces_the_excerpt_sonnet_saw(tmp_path):
    latch = json.dumps({"type": "atis-latch", "sessionId": "s"})
    lines = []
    for i in range(400):
        lines += [json.dumps({"type": "user", "message": {"content": f"turn {i} " + "x" * 400}}), latch]
    data = ("\n".join(lines) + "\n").encode()
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    cut = data.index(f"turn 300 ".encode())
    cut = data.index(latch.encode(), cut) + len(latch) + 1
    ctx = {"transcript_path": str(path), "transcript_excerpt": replay.raw_excerpt(data[:cut])}
    assert replay.transcript_end(ctx) == cut


def test_no_cut_point_when_the_transcript_no_longer_matches(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n')
    ctx = {"transcript_path": str(path), "transcript_excerpt": '{"z": 9}\n{"b": 2}\n'}
    assert replay.transcript_end(ctx) is None
