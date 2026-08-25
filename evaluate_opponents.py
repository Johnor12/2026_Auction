#!/usr/bin/env python3
"""Forward-replay every completed opponent pick through the opponent model.

For each pick, source inference sees only picks made before it. The model then draws
100 choices from that exact board state. Error is the absolute distance between the
drawn and actual players on the model's balance-adjusted preference order; exact hits
and the actual player's preference rank are reported alongside it.

Usage:
    uv run evaluate_opponents.py
"""

from __future__ import annotations

import copy
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
INVESTIGATOR_DIR = REPO_ROOT / "data_source_investigator"
sys.path.insert(0, str(INVESTIGATOR_DIR))

import investigate as source_investigator  # noqa: E402

from ranker.board import load_board  # noqa: E402
from ranker.league import NOISE, SEED  # noqa: E402
from ranker.opponents import build_opponent_strategies  # noqa: E402
from ranker.pool import Player, load_pool  # noqa: E402
from ranker.simulation import Draft  # noqa: E402
from ranker.value import seed_wire  # noqa: E402

TRIALS = 100
POOL = REPO_ROOT / "pool.json"
DRAFT = REPO_ROOT / "draft.json"
SOURCE_RANKINGS = INVESTIGATOR_DIR / "data/rankings.json"


def replay_before(draft: dict, pick_no: int) -> dict:
    """Return the board as it stood immediately before ``pick_no``."""
    replay = copy.deepcopy(draft)
    for pick in replay["picks"]:
        pick["status"] = "made" if pick["pick_no"] < pick_no else "pending"
    replay["picks_made"] = pick_no - 1
    replay["picks_pending"] = len(replay["picks"]) - replay["picks_made"]
    return replay


def preference_order(model: Draft, slot: int) -> list[Player]:
    candidates = model.opponent_candidates(slot)
    adjustments = model.opponent_position_adjustments(slot)
    return [
        row[2]
        for row in sorted(
            (
                (rank * adjustments[player.position], rank, player)
                for rank, player in enumerate(candidates, start=1)
            ),
            key=lambda row: (row[0], row[1], row[2].player_id),
        )
    ]


def evaluate(
    players: list[Player], draft: dict, rankings: dict, seed: int = SEED
) -> list[dict]:
    wire = seed_wire(players)
    by_sleeper = {str(player.sleeper_id): player for player in players}
    results = []

    for actual in sorted(
        (
            pick
            for pick in draft["picks"]
            if pick["status"] == "made" and not pick.get("is_mine")
        ),
        key=lambda pick: pick["pick_no"],
    ):
        replay = replay_before(draft, actual["pick_no"])
        source_report = source_investigator.investigate(rankings, replay)
        board, problems = load_board(replay, players, "draft replay")
        if problems:
            raise ValueError("; ".join(problems))
        if not board.order or board.pick_nos[0] != actual["pick_no"]:
            raise ValueError(f"replay did not stop before pick {actual['pick_no']}")
        slot = board.order[0]
        opponents = build_opponent_strategies(
            players, board, replay, source_report, rankings
        )
        strategy = opponents[slot]
        model = Draft(
            players,
            wire,
            board,
            noise=NOISE,
            rng=random.Random(seed + actual["pick_no"]),
            opponents=opponents,
        )
        ordered = preference_order(model, slot)
        ranks = {player.player_id: rank for rank, player in enumerate(ordered, start=1)}
        actual_player = by_sleeper.get(str(actual.get("sleeper_id")))
        if actual_player is None or actual_player.player_id not in ranks:
            # A drafted player outside the pool (or outside the legal candidate set)
            # has zero model probability; score the picks the model can see.
            print(
                f"warning: skipping pick {actual['pick_no']} {actual['name']}: "
                "not in the pool/candidate list",
                file=sys.stderr,
            )
            continue
        actual_rank = ranks[actual_player.player_id]
        predictions = [model.choose_opponent(0, slot) for _ in range(TRIALS)]
        errors = [abs(ranks[predicted.player_id] - actual_rank) for predicted in predictions]
        counts = Counter(predicted.player_id for predicted in predictions)
        mode_id, mode_count = min(
            counts.items(),
            key=lambda row: (-row[1], ranks[row[0]], row[0]),
        )
        mode = next(player for player in ordered if player.player_id == mode_id)
        results.append(
            {
                "pick_no": actual["pick_no"],
                "username": actual.get("username") or f"roster {actual['roster_id']}",
                "actual": actual["name"],
                "source": strategy.source_id,
                "confidence": strategy.confidence,
                "prior_picks": next(
                    (
                        owner["pick_count"]
                        for owner in source_report["owners"]
                        if owner["roster_id"] == actual["roster_id"]
                    ),
                    0,
                ),
                "actual_rank": actual_rank,
                "mean_error": statistics.fmean(errors),
                "error_variance": statistics.variance(errors),
                "hit_rate": counts[actual_player.player_id] / TRIALS,
                "hit_variance": statistics.variance(
                    predicted.player_id == actual_player.player_id
                    for predicted in predictions
                ),
                "mode": mode.name,
                "mode_rate": mode_count / TRIALS,
            }
        )
    return results


def standard_error(values: list[float]) -> float:
    return statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else 0.0


def monte_carlo_standard_error(results: list[dict], variance_key: str) -> float:
    return math.sqrt(
        sum(row[variance_key] / TRIALS for row in results) / len(results) ** 2
    )


def report(results: list[dict]) -> None:
    errors = [row["mean_error"] for row in results]
    hits = [row["hit_rate"] for row in results]
    ranks = [row["actual_rank"] for row in results]
    print(
        f"{len(results)} opponent picks x {TRIALS} trials: "
        f"mean error {statistics.fmean(errors):.2f} "
        f"(Monte Carlo SE {monte_carlo_standard_error(results, 'error_variance'):.2f}; "
        f"pick-level SE {standard_error(errors):.2f}), "
        f"exact hit rate {statistics.fmean(hits):.1%} "
        f"(Monte Carlo SE {monte_carlo_standard_error(results, 'hit_variance'):.1%}), "
        f"mean actual rank {statistics.fmean(ranks):.2f}"
    )

    by_owner: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        by_owner[row["username"]].append(row)
    print("\nby opponent:")
    for username, rows in sorted(by_owner.items()):
        print(
            f"  {username:<20} picks {len(rows):>2}, "
            f"error {statistics.fmean(row['mean_error'] for row in rows):>5.2f}, "
            f"hits {statistics.fmean(row['hit_rate'] for row in rows):>5.1%}, "
            f"actual rank {statistics.fmean(row['actual_rank'] for row in rows):>5.2f}"
        )

    print("\nworst replay errors:")
    for row in sorted(results, key=lambda item: (-item["mean_error"], item["pick_no"]))[:10]:
        print(
            f"  {row['pick_no']:>3} {row['username']:<20} actual {row['actual']:<24} "
            f"rank {row['actual_rank']:>3}, predicted {row['mode']} "
            f"({row['mode_rate']:.0%}), error {row['mean_error']:.2f}, "
            f"source {row['source']} after {row['prior_picks']} picks"
        )


def main() -> int:
    try:
        players, _ = load_pool(POOL)
        draft = json.loads(DRAFT.read_text())
        rankings = json.loads(SOURCE_RANKINGS.read_text())
        results = evaluate(players, draft, rankings)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot evaluate opponent predictions: {exc}", file=sys.stderr)
        return 1
    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
