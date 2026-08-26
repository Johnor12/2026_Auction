#!/usr/bin/env python3
"""Download the public ranking pages used by the investigator.

The raw responses are snapshots: parsing happens in ``build_rankings.py`` so a
provider parser can be fixed without downloading a newer board and losing the
evidence that existed during the draft.

Usage:
    uv run data_source_investigator/fetch_rankings.py
    uv run data_source_investigator/fetch_rankings.py --report
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import paths

TIMEOUT_SECONDS = 30
USER_AGENT = "dynasty-data-source-investigator/0.1"

# The league is 12-team, superflex, half PPR, no TE premium. Providers expose
# different subsets of those settings; the exact format requested from each one is
# carried into rankings.json.
SOURCES = {
    "fantasycalc": {
        "name": "FantasyCalc",
        "url": (
            "https://api.fantasycalc.com/values/current?"
            "isDynasty=true&numQbs=2&numTeams=12&ppr=0.5&includeAdp=true"
        ),
        "file": "fantasycalc.json",
        "format": "12-team dynasty superflex, 0.5 PPR; no TEP option in the public API",
    },
    "keeptradecut": {
        "name": "KeepTradeCut",
        "url": (
            "https://keeptradecut.com/dynasty-rankings?"
            "filters=QB%7CWR%7CRB%7CTE&format=2&page=0"
        ),
        "file": "keeptradecut.html",
        "format": "dynasty superflex, 0.5 PPR, TE+ (this league has no TEP)",
    },
    "dynastynerds": {
        "name": "Dynasty Nerds",
        "url": "https://www.dynastynerds.com/dynasty-rankings/sf-tep/",
        "file": "dynastynerds.html",
        "format": "dynasty superflex TE premium consensus (this league has no TEP)",
    },
    "fantasypros": {
        "name": "FantasyPros ECR",
        "url": "https://www.fantasypros.com/nfl/rankings/dynasty-superflex.php",
        "file": "fantasypros.html",
        "format": "dynasty superflex PPR expert consensus; no TEP",
    },
}


def fetch(url: str, timeout: int = TIMEOUT_SECONDS) -> tuple[bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json,text/html", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type")


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json(path: Path, payload: dict, indent: int) -> None:
    encoded = (json.dumps(payload, indent=indent, ensure_ascii=False) + "\n").encode()
    write_bytes(path, encoded)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)

    fetched_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    metadata: dict[str, object] = {"fetched_at": fetched_at, "sources": {}}
    failures: list[str] = []
    downloads: dict[str, tuple[Path, bytes]] = {}

    for source_id, source in SOURCES.items():
        destination = paths.RAW / str(source["file"])
        try:
            body, content_type = fetch(str(source["url"]), args.timeout)
            if not body:
                raise ValueError("empty response")
            downloads[source_id] = (destination, body)
            metadata["sources"][source_id] = {
                "name": source["name"],
                "url": source["url"],
                "format": source["format"],
                "file": destination.name,
                "fetched_at": fetched_at,
                "content_type": content_type,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            failures.append(f"{source_id}: {exc}")

    if failures:
        print("ranking fetch failed; existing snapshots were left intact:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    for source_id, (destination, body) in downloads.items():
        write_bytes(destination, body)
        if args.report:
            print(
                f"  {source_id}: {len(body):,} bytes -> {paths.display(destination)}",
                file=sys.stderr,
            )
    write_json(paths.FETCH_META, metadata, args.indent)
    print(f"fetched {len(SOURCES)} ranking sources -> {paths.display(paths.RAW)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
