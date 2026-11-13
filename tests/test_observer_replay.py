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
    seen = data[:cut].decode().splitlines(keepends=True)
    assert len(data[:cut]) > 131072
    head, used = [], 0
    while used + len(seen[len(head)]) <= 24576:
        used += len(seen[len(head)])
        head.append(seen[len(head)])
    tail, budget = [], 131072 - used
    while sum(map(len, tail)) + len(seen[-len(tail) - 1]) < budget:
        tail.insert(0, seen[-len(tail) - 1])
    excerpt = "".join(head + tail)
    assert replay.raw_excerpt(data[:cut]) == excerpt
    ctx = {"transcript_path": str(path), "transcript_excerpt": excerpt}
    assert replay.transcript_end(ctx) == cut


def test_no_cut_point_when_the_transcript_no_longer_matches(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n')
    ctx = {"transcript_path": str(path), "transcript_excerpt": '{"z": 9}\n{"b": 2}\n'}
    assert replay.transcript_end(ctx) is None
