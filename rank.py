#!/usr/bin/env python3
"""Build the live auction bid board and nomination recommendations.

    uv run rank.py
    uv run rank.py --report
    uv run rank.py --no-draft
    uv run rank.py --selftest

The ranker uses fixed seeds so it is reproducible and small enough to recompute during a
timed auction. It examines at most 240 available players, values each against my current
roster, converts marginal lineup value into a hard maximum bid under my remaining budget,
estimates the field price from opponent boards, rosters, and budgets, and rolls out the
remaining auction to find recurring cost-efficient targets.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ranker.auction import analyze, load_state, selftest
from ranker.pool import load_pool

REPO_ROOT = Path(__file__).resolve().parent
SOURCE_MATCHES = REPO_ROOT / "data_source_matches.json"
SOURCE_RANKINGS = REPO_ROOT / "data_source_investigator/data/rankings.json"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("input", nargs="?", default=Path("pool.json"), type=Path)
    parser.add_argument("-o", "--output", default=Path("rankings.json"), type=Path)
    parser.add_argument("--draft", default=Path("draft.json"), type=Path)
    parser.add_argument(
        "--no-draft",
        action="store_true",
        help="ignore made purchases but retain the auction's roster identities",
    )
    parser.add_argument("--report", action="store_true", help="print auction diagnostics")
    parser.add_argument("--selftest", action="store_true", help="run offline auction checks")
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        players, pool_meta = load_pool(args.input)
        provider_rankings = read_json(SOURCE_RANKINGS)
        if args.selftest:
            failures = selftest(players, provider_rankings)
            for failure in failures:
                print(f"selftest: {failure}", file=sys.stderr)
            print(
                f"auction selftest {'FAILED' if failures else 'passed'} in "
                f"{time.monotonic() - started:.2f}s",
                file=sys.stderr,
            )
            return 1 if failures else 0

        draft = read_json(args.draft)
        if args.no_draft:
            draft = {**draft, "picks": [], "picks_made": 0, "picks_pending": 168}
        matches = read_json(SOURCE_MATCHES) if SOURCE_MATCHES.exists() else None
        state = load_state(draft, players)
        payload = analyze(players, state, provider_rankings, matches)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"cannot build auction board: {exc}", file=sys.stderr)
        return 1

    payload.update(
        {
            "generated_from": str(args.input),
            "value_input": "FantasyPros 2026 projected points under this league's scoring",
            "pool": pool_meta,
            "provider_snapshot": provider_rankings.get("generated_at"),
        }
    )
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elapsed = time.monotonic() - started

    if args.report:
        mine = payload["my_auction"]
        print(
            f"auction: {payload['draft']['picks_made']}/{payload['league']['total_picks']} "
            f"purchases; my budget ${mine['remaining_budget']} for {mine['slots_left']} "
            f"slots (legal max ${mine['max_legal_bid']})",
            file=sys.stderr,
        )
        print(
            f"analysis: {payload['count']} players, field inflation "
            f"{payload['analysis']['field_inflation']:.3f}",
            file=sys.stderr,
        )
        simulation = payload["analysis"]["simulation"]
        print(
            f"simulation: {simulation['completed']}/{simulation['simulations']} legal "
            f"auctions; my mean lineup {simulation['my_projected_lineup_points']['mean']}, "
            f"mean spend ${simulation['my_spend']['mean']}",
            file=sys.stderr,
        )
        print("pursue:", file=sys.stderr)
        for row in payload["purchase_strategy"]["recommendations"][:5]:
            print(
                f"  {row['name']:<24} rostered {row['simulated_roster_rate']:.0%}; "
                f"rollout ${row['simulated_price_low']}-${row['simulated_price_high']} "
                f"(my max ${row['max_bid']})",
                file=sys.stderr,
            )
        print("nominate:", file=sys.stderr)
        for row in payload["nomination_strategy"]["recommendations"][:5]:
            print(
                f"  {row['name']:<24} field ${row['field_price']:>3} vs my "
                f"${row['max_bid']:>3} (drain +${row['nomination_edge']})",
                file=sys.stderr,
            )
        print("highest max bids:", file=sys.stderr)
        for row in payload["rankings"][:8]:
            print(
                f"  ${row['max_bid']:>3} {row['name']:<24} {row['position']:<2} "
                f"gain {row['lineup_gain']:>5.1f}, field ${row['field_price']}",
                file=sys.stderr,
            )

    problems = payload["validation"]["problems"]
    if problems:
        print(f"{len(problems)} validation problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    print(
        f"wrote {args.output} ({payload['count']} available players) in {elapsed:.2f}s",
        file=sys.stderr,
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
