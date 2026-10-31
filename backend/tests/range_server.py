"""
Minimal HTTP server with `Range` support, used to exercise the REAL raster read path.

Why this exists: ESA WorldCover tiles are ~100 MB Cloud-Optimized GeoTIFFs, and the
production code reads only a byte window over HTTP (`/vsicurl`). That code path cannot
be verified against the real bucket inside a sandbox without outbound internet, so the
tests point `WORLDCOVER_URL_PREFIX` at this local server and serve a locally generated,
clearly-labelled SYNTHETIC COG. The HTTP behaviour (HEAD, 206 Partial Content,
Content-Range) is real; the pixels are not ESA data.
"""

from __future__ import annotations

import contextlib
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class RangeRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        return

    def _target(self):
        return self.translate_path(self.path)

    def do_HEAD(self):
        path = self._target()
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/tiff")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        path = self._target()
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        raw = self.headers.get("Range")
        if raw and raw.startswith("bytes="):
            spec = raw[6:].split(",")[0].strip()
            try:
                if spec.startswith("-"):
                    length = int(spec[1:])
                    start = max(0, size - length)
                else:
                    parts = spec.split("-", 1)
                    start = int(parts[0])
                    end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            except ValueError:
                self.send_error(416, "Bad range")
                return
            end = min(end, size - 1)
            if start > end:
                self.send_error(416, "Unsatisfiable range")
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "image/tiff")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(262_144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


class LocalRangeServer:
    def __init__(self, directory: str):
        handler = functools.partial(RangeRequestHandler, directory=directory)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "LocalRangeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        with contextlib.suppress(RuntimeError):
            self._thread.join(timeout=5)
        return False
