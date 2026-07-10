"""Tests for src/assistant/todostore.py — the ONE shared lock every writer of
assistant-todo.json holds (Keel M3 lost-update fix). The regression: goals
staged a TODO under the *goals* lock while the todo-server / pulse dispatch /
triage create wrote the same file under NO shared lock, so a concurrent write
silently clobbered an update. This drives many concurrent locked read-modify-
writes and asserts NOT ONE append is lost.

Named test_todostore (sorts AFTER test_daemon); stdlib-only so it loads under
both python3.9 and python3.12.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import todostore  # noqa: E402


class TodoLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        p = todostore.todo_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"_schema": 1, "items": []}))

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _locked_append(self, item_id):
        p = todostore.todo_path()
        with todostore.todo_lock():
            data = json.loads(p.read_text())
            items = data.setdefault("items", [])
            # A tiny window between read and write is where the lost update used
            # to happen; the lock must serialize it away.
            items.append({"id": item_id})
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, p)

    def test_concurrent_writers_lose_no_update(self):
        n = 25
        barrier = threading.Barrier(n)

        def worker(i):
            barrier.wait()  # maximize contention: all fire at once
            self._locked_append(f"td-{i:03d}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        items = json.loads(todostore.todo_path().read_text())["items"]
        ids = sorted(it["id"] for it in items)
        self.assertEqual(len(ids), n, f"lost an update: {len(ids)} of {n}")
        self.assertEqual(ids, sorted(f"td-{i:03d}" for i in range(n)))

    def test_lock_path_is_dedicated_not_the_json(self):
        # Locking the json itself would be meaningless — it is replaced via
        # tmp+os.replace, so the lock must be a separate file.
        self.assertNotEqual(todostore.lock_path(), todostore.todo_path())
        self.assertTrue(str(todostore.lock_path()).endswith(".lock"))


if __name__ == "__main__":
    unittest.main()
