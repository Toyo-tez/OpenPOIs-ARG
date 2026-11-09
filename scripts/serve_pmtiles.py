#!/usr/bin/env python3
"""Serve PMTiles archives (and their JSON sidecars) for local development.

The frontend fetches PMTiles with HTTP Range requests, so a plain
``python -m http.server`` does not work: it ignores ``Range`` and returns the
whole archive. This server implements single-range requests, CORS, and the
headers ``ol-pmtiles`` needs.

Usage (from the repo root):
    python scripts/serve_pmtiles.py --root ~/data/openpois/AR/site-data --port 8765

Layout expected under ``--root`` (the same layout Source Cooperative uses):
    osm-pmtiles/osm.pmtiles
    overture-pmtiles/overture.pmtiles
    overture-pmtiles/overture_categories.json
    conflated-pmtiles/conflated.pmtiles
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CONTENT_TYPES = {
    ".pmtiles": "application/octet-stream",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}


class RangeRequestHandler(BaseHTTPRequestHandler):
    root: Path

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Range, Content-Type, Accept, Origin"
        )
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Length, Content-Range, Accept-Ranges",
        )

    def _resolve(self) -> Path | None:
        raw = unquote(urlparse(self.path).path)
        candidate = (self.root / raw.lstrip("/")).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def _content_type(self, path: Path) -> str:
        return CONTENT_TYPES.get(
            path.suffix.lower(),
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head = True)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head = False)

    def _serve(self, head: bool) -> None:
        path = self._resolve()
        if path is None or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")

        if range_header:
            match = RANGE_RE.match(range_header.strip())
            if not match:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._cors()
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            first, last = match.groups()
            if first == "":
                if last == "":
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                start = max(0, size - int(last))
                end = size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._cors()
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1 if size else 0
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", self._content_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if head or not length:
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt: str, *args) -> None:
        # Range requests are chatty; keep only errors.
        if args and isinstance(args[1], str) and args[1].startswith("4"):
            super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument("--root", type = Path, required = True)
    parser.add_argument("--host", default = "127.0.0.1")
    parser.add_argument("--port", type = int, default = 8765)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a directory: {root}")
    RangeRequestHandler.root = root

    server = ThreadingHTTPServer((args.host, args.port), RangeRequestHandler)
    print(f"Serving {root} at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
