#!/usr/bin/env python3
"""Download Sleeper's full NFL player dump. **Manual step — not a pipeline stage.**

    uv run pool_pipeline/fetch_sleeper.py

Sleeper publishes every player it knows about at ``/v1/players/nfl`` as one ~14 MB
object keyed by their player id (12k entries, ~4k of them QB/RB/WR/TE). Their docs
are explicit that this is not an endpoint to call casually — "you should save this
information on your own servers... do not call this endpoint more than once per
day" — and the data it returns is a roster of humans that changes on the scale of
weeks, not a projection that changes with every rebuild. So it is fetched by hand
and cached in ``pool_pipeline/data/``, and ``pipeline.py`` never calls it.

Two files are written:

    data/sleeper_players.json       the response body, byte-for-byte, gitignored
    data/sleeper_players.meta.json  when/where/how big, small enough to commit

The dump is stored unmodified for the same reason ``projections.json`` is kept
whole: it is the record of what the provider said, and every narrowing decision
belongs downstream in ``match_sleeper.py`` where it can be re-run without another
download. The sidecar exists so the age of the dump survives a file copy — the
match stage stamps ``fetched_at`` into pool.json and warns when it goes stale.

Usage:
    uv run fetch_sleeper.py                  # -> data/sleeper_players.json
    uv run fetch_sleeper.py --force          # re-download even if fresh
    uv run fetch_sleeper.py --max-age 0      # same thing
    uv run fetch_sleeper.py -o other.json
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

URL = "https://api.sleeper.app/v1/players/nfl"

#: Sleeper asks for at most one call per day; a re-fetch inside this window is
#: refused unless --force. Rosters move on a weekly cadence anyway.
MAX_AGE_HOURS = 24

TIMEOUT_SECONDS = 300

#: A dump this much smaller than expected means a truncated or error response,
#: not a quiet off-season. Sleeper returns ~12k players / ~14 MB.
MIN_PLAYERS = 5000


def load_meta(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None


def age_hours(meta: dict | None) -> float | None:
    """Hours since the recorded fetch, or None if unknown."""
    if not meta or not meta.get("fetched_at"):
        return None
    try:
        fetched = dt.datetime.fromisoformat(meta["fetched_at"])
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds() / 3600


def download(url: str, timeout: int = TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def summarize(players: dict) -> dict:
    """The few counts worth recording without keeping the whole dump in review."""
    offensive = [
        p
        for p in players.values()
        if isinstance(p, dict) and p.get("position") in ("QB", "RB", "WR", "TE")
    ]
    return {
        "player_count": len(players),
        "offensive_count": len(offensive),
        "active_offensive_count": sum(1 for p in offensive if p.get("active")),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-o", "--output", default=paths.SLEEPER_PLAYERS, type=Path)
    ap.add_argument("--url", default=URL, help=f"default: {URL}")
    ap.add_argument(
        "--max-age",
        type=float,
        default=MAX_AGE_HOURS,
        metavar="HOURS",
        help=f"refuse to re-download a dump younger than this (default {MAX_AGE_HOURS})",
    )
    ap.add_argument("--force", action="store_true", help="download regardless of age")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    args = ap.parse_args(argv)

    meta_path = args.output.with_suffix(".meta.json")
    existing = load_meta(meta_path)
    age = age_hours(existing)
    if args.output.is_file() and not args.force and age is not None and age < args.max_age:
        print(
            f"{paths.display(args.output)} is {age:.1f}h old (< {args.max_age}h) — "
            "not re-downloading; pass --force to override",
            file=sys.stderr,
        )
        return 0

    print(f"GET {args.url}", file=sys.stderr)
    try:
        body = download(args.url, args.timeout)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: download failed: {exc}", file=sys.stderr)
        return 1

    try:
        players = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"error: response is not JSON ({exc})", file=sys.stderr)
        return 1
    if not isinstance(players, dict) or len(players) < MIN_PLAYERS:
        got = len(players) if isinstance(players, dict) else type(players).__name__
        print(
            f"error: expected an object of >={MIN_PLAYERS} players, got {got} — "
            "leaving any existing dump in place",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)

    counts = summarize(players)
    meta = {
        "url": args.url,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "file": args.output.name,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        **counts,
    }
    with meta_path.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")

    print(
        f"wrote {counts['player_count']} players ({len(body) / 1e6:.1f} MB) to "
        f"{paths.display(args.output)}; {counts['offensive_count']} QB/RB/WR/TE, "
        f"{counts['active_offensive_count']} of them active",
        file=sys.stderr,
    )
    print(f"meta -> {paths.display(meta_path)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
