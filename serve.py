#!/usr/bin/env python3
"""Serve both dashboards locally at http://127.0.0.1:8123.

Usage:
    uv run serve.py
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADDRESS = ("127.0.0.1", 8123)


def main() -> None:
    handler = partial(SimpleHTTPRequestHandler, directory=ROOT)
    with ThreadingHTTPServer(ADDRESS, handler) as server:
        print(
            f"Serving the draft board at http://{ADDRESS[0]}:{ADDRESS[1]}/ "
            f"and source investigator at /data_source_investigator/"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
