#!/usr/bin/env python3
"""Fetch Sleeper's live draft and publish its live board or auction purchases.

This is a network-only, on-demand pipeline. Board geometry and the output contract live
in `draft_board.py`; reporting and offline checks are isolated so this entry point only
coordinates I/O.

Usage:
    uv run draft_pipeline/fetch_draft.py
    uv run draft_pipeline/fetch_draft.py --report
    uv run draft_pipeline/fetch_draft.py --selftest
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import paths
import report as draft_report
from draft_board import (
    Board,
    build_document,
    index_users,
    pick_number_problems,
    pick_rows,
    resolve_me,
)
from selftest import selftest as run_selftest

# Gnosis Auction 2026: https://sleeper.com/leagues/1396606685107200000
DRAFT_ID = "1396606686923341824"
MY_USERNAME = "johnor"
API = "https://api.sleeper.app/v1"
TIMEOUT_SECONDS = 30


def get_json(url: str, timeout: int = TIMEOUT_SECONDS):
    # Sleeper's CDN serves cached snapshots up to 5 minutes stale
    # (s-maxage=15, stale-while-revalidate=300), and different cache entries
    # can even roll the pick count backwards between refreshes. A unique
    # query param forces a cache MISS so every fetch comes from origin.
    bust = f"{'&' if '?' in url else '?'}nocache={time.time_ns()}"
    request = urllib.request.Request(url + bust, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def fetch(draft_id: str, api: str = API, timeout: int = TIMEOUT_SECONDS) -> dict:
    """The draft, picks, trades, league users, and league rosters.

    The first three are load-bearing and a failure is fatal — half a board is worse
    than none, and a missing traded_picks would silently misattribute pending picks.
    The user list only supplies display names, so it degrades to a warning.
    """
    draft = get_json(f"{api}/draft/{draft_id}", timeout)
    if not isinstance(draft, dict) or not draft.get("draft_id"):
        raise ValueError(f"no draft {draft_id} at {api} — check the id in the draft URL")

    # League mocks carry the source league only in metadata.
    league_id = draft.get("league_id") or (draft.get("metadata") or {}).get("league_id")
    urls = {
        "picks": f"{api}/draft/{draft_id}/picks",
        "traded": f"{api}/draft/{draft_id}/traded_picks",
    }
    if league_id:
        urls |= {
            "rosters": f"{api}/league/{league_id}/rosters",
            "users": f"{api}/league/{league_id}/users",
        }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls)) as executor:
        pending = {
            name: executor.submit(get_json, url, timeout) for name, url in urls.items()
        }
        picks = pending["picks"].result() or []
        traded = pending["traded"].result() or []
        rosters = (pending["rosters"].result() or []) if league_id else []
        users, warning = [], None
        if league_id:
            try:
                users = pending["users"].result() or []
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                warning = f"could not read league users ({exc}) — names will be null"

    if not isinstance(picks, list) or not isinstance(traded, list):
        raise ValueError("picks/traded_picks did not come back as lists")
    if not isinstance(rosters, list):
        raise ValueError("league rosters did not come back as a list")
    if league_id:
        try:
            if not isinstance(users, list):
                raise ValueError("league users did not come back as a list")
        except ValueError as exc:
            warning = f"{exc} — names will be null"
            users = []
    else:
        warning = "draft has no league_id — names will be null"

    return {
        "draft": draft,
        "picks": picks,
        "traded": traded,
        "users": users,
        "rosters": rosters,
        "warning": warning,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-o", "--output", default=paths.DRAFT, type=Path)
    ap.add_argument("--draft-id", default=DRAFT_ID, help=f"default: {DRAFT_ID}")
    ap.add_argument(
        "--me",
        default=MY_USERNAME,
        help=f"Sleeper username whose picks get is_mine (default {MY_USERNAME})",
    )
    ap.add_argument("--api", default=API, help=f"default: {API}")
    ap.add_argument("--pool", default=paths.POOL, type=Path, help="--report checks the join here")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="check the board geometry offline (no network), then exit",
    )
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    print(
        f"GET {args.api}/draft/{args.draft_id} "
        "(+picks, traded_picks, league rosters/users)",
        file=sys.stderr,
    )
    try:
        fetched = fetch(args.draft_id, args.api, args.timeout)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if fetched["warning"]:
        print(f"warning: {fetched['warning']}", file=sys.stderr)

    try:
        board = Board(fetched["draft"], fetched["traded"], fetched["rosters"])
        fatal, notable = pick_number_problems(fetched["picks"], board)
        fatal = board.problems() + fatal
    except (TypeError, ValueError) as exc:
        print(f"error: draft {args.draft_id} is not shaped as expected: {exc}", file=sys.stderr)
        return 1
    if fatal:
        for problem in fatal:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    for note in notable:
        print(f"warning: {note}", file=sys.stderr)
    if board.type != "auction" and not board.slot_to_user:
        print("warning: draft_order is empty — pick owners will be null", file=sys.stderr)

    by_user = index_users(fetched["users"])
    me = resolve_me(args.me, board, by_user)
    if me["user_id"] is None:
        print(
            f"warning: no league user named {args.me!r} — no pick will be marked is_mine",
            file=sys.stderr,
        )
    elif me["draft_slot"] is None:
        print(f"warning: {me['username']} has no slot in this draft's order", file=sys.stderr)

    rows, checks = pick_rows(board, fetched["picks"], by_user, me["user_id"])
    if checks["mismatches"]:
        print(
            f"warning: the derived pick order disagrees with Sleeper on "
            f"{len(checks['mismatches'])} of {checks['made_picks_checked']} made picks — "
            "pending picks may be attributed to the wrong team; run --report",
            file=sys.stderr,
        )

    document = build_document(fetched, board, rows, checks, me, args.api)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    clock = document["on_the_clock"]
    mine_next = document["my_next_pick"]
    print(
        f"draft: {document['picks_made']}/{document['pick_count']} picks made, "
        f"status {document['status']}"
        + (
            "; auction has no pending pick order"
            if board.type == "auction"
            else
            f"; on the clock #{clock['pick_no']} ({clock['slot']}) "
            f"{clock['username'] or clock['user_id']}"
            if clock
            else "; complete"
        )
        + (
            f"; mine #{mine_next['pick_no']} ({mine_next['slot']}) in "
            f"{mine_next.get('picks_away')}"
            if mine_next
            else ""
        )
        + f" -> {paths.display(args.output)}",
        file=sys.stderr,
    )
    if args.report:
        draft_report.report(document, rows, board, args.pool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
