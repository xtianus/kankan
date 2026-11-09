"""A plain HTTP server on ``localhost``: a different origin from the fake Trello server (``127.0.0.1``).

It serves ``content`` on any GET path (a 302 for the paths in ``redirects``) and records every request,
so tests can check that a redirected download reaches it without the Trello credential.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ExternalHost:
    def __init__(self, content: bytes = b"external content\n"):
        self.content = content
        self.requests = []  # (path, headers dict)
        self.redirects = {}  # path -> Location answered with a 302 instead of the content
        self.server = None
        self.thread = None

    def __enter__(self):
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host.requests.append((self.path, dict(self.headers.items())))
                if self.path in host.redirects:
                    self.send_response(302)
                    self.send_header("Location", host.redirects[self.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(host.content)))
                self.end_headers()
                try:
                    self.wfile.write(host.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://localhost:{self.server.server_address[1]}"
