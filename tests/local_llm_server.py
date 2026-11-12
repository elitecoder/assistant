"""A fake OpenAI-compatible chat server for local-model tests. No network."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLocalModel:
    """Serves /v1/chat/completions with a canned reply and records requests."""

    def __init__(self, reply="ok", status=200, raw: bytes | None = None):
        self.reply, self.status, self.raw = reply, status, raw
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "body": body})
                data = owner.raw if owner.raw is not None else json.dumps({
                    "choices": [{"message": {"content": owner.reply}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 7}}).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
