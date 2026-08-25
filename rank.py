#!/usr/bin/env python3
"""Roster-aware draft board for this league, with an optimal-drafter draft simulation.

    uv run rank.py                         # pool + draft + source artifacts -> rankings.json
    uv run rank.py --report                # + board, convergence and recommendation on stderr
    uv run rank.py --no-draft              # ignore the live board, rank the whole pool
    uv run rank.py --selftest              # verify solver, opponents, and board loader

Scope is this league and nothing else; the league constants and strategy knobs live in
ranker/league.py. The value inputs are `points_3yr` and `points_1yr` from `pool.json` —
points in this league's scoring, split into two horizons: year 1 and years 2-3 (see
ranker/pool.py for why the provider's 3D value is deliberately unused). Lineups are
valued per horizon, so a 69-point injury year cannot hide inside a healthy 3-year
sum. The method, in one breath: what the waiver wire holds after the draft is an
*outcome* of how the league drafts, so per-horizon wire levels are measured from the
converged draft and feed valuation, while my roster is valued as expected optimal
lineup points under position-wide availability with one unique waiver fallback per
position (`ranker/value.py`). `draft.json` — the live board — is the
simulation's starting state, not a filter (ranker/board.py). Only my slot uses the
projection-based roster objective. Every opponent uses the external provider board most
associated with its prior picks, loaded from the data-source investigator; unfilled
dedicated starters softly adjust that order, and its observed adherence controls Monte
Carlo choice noise. A compounding soft-depth preference keeps opponents' late roster
shapes plausible without imposing position limits. My slot's choices contain no
positional roster-size heuristic.

The board's headline `lineup_gain` is the decision metric itself: the player's marginal
expected-lineup value on my current roster at the converged wire levels, so it shrinks
at positions my roster already covers. The `my_next_picks` block is the direct answer to
"who should I draft next" — on top of that same gain it prices opponent demand and what
my following picks keep. Its first decision searches target plans across my next four
held picks, then plays each first-candidate plan out to the end of the draft
(`ranker/planning.py`). Rank columns are renumbered over the undrafted
players actually emitted.

A caveat the data imposes, not the model: the provider projects backup and rookie QBs at
starter-grade volume (the QB20-21 range lands around ~820 points over three
years), which compresses elite QB value hard. If those depth projections are not
credible, deep QBs are overrated and every elite QB is underrated here. The lever is the
projections, not the ranking method.

Python stdlib only. Deterministic: every tie breaks on player_id and the RNG is seeded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ranker.board import fresh_board, load_board
from ranker.convergence import converge
from ranker.league import NOISE, ROLLOUT_SIMS, SEED, SIMS
from ranker.opponents import load_opponent_strategies
from ranker.output import build_payload
from ranker.planning import (
    apply_option_redraw,
    apply_rollout,
    apply_survival_floor,
    broaden_first_pick,
    candidate_survival,
    four_pick_lookahead,
    monte_carlo,
    option_redraw,
    rollout,
)
from ranker.pool import load_pool
from ranker.rankings import build_rankings
from ranker.report import report_board, report_summary
from ranker.selftest import selftest
from ranker.validate import validate

REPO_ROOT = Path(__file__).resolve().parent
SOURCE_MATCHES = REPO_ROOT / "data_source_matches.json"
SOURCE_RANKINGS = REPO_ROOT / "data_source_investigator/data/rankings.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", nargs="?", default=Path("pool.json"), type=Path)
    ap.add_argument("-o", "--output", default=Path("rankings.json"), type=Path)
    ap.add_argument(
        "--draft",
        type=Path,
        default=None,
        help="live board to start from (default: draft.json if it is there)",
    )
    ap.add_argument(
        "--no-draft",
        action="store_true",
        help="ignore the live board: rank the whole pool from an empty draft",
    )
    ap.add_argument("--report", action="store_true", help="validation summary on stderr")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="check the solver, opponent separation, and board loader, then exit",
    )
    ap.add_argument("--sims", type=int, default=SIMS, help="noisy redraws for availability")
    ap.add_argument(
        "--noise",
        type=float,
        default=NOISE,
        help="opponent pick randomness (0 = deterministic balance-adjusted source order)",
    )
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"missing {args.input} - run pipeline.py first", file=sys.stderr)
        return 1

    players, pool_meta = load_pool(args.input)
    if args.selftest:
        return selftest(players)

    # Even --no-draft needs draft.json's roster-to-slot map: the investigator associates
    # providers with roster ids, while the simulation assigns strategies by draft slot.
    board_problems: list[str] = []
    draft_path = args.draft or Path("draft.json")
    draft_raw = None
    if draft_path.exists():
        try:
            draft_raw = json.loads(draft_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read {draft_path}: {exc}", file=sys.stderr)
            return 1
    if args.no_draft:
        board = fresh_board()
    elif draft_raw is not None:
        board, board_problems = load_board(draft_raw, players, str(draft_path))
    elif args.draft is not None:
        print(f"missing {draft_path} - run draft_pipeline/fetch_draft.py", file=sys.stderr)
        return 1
    else:
        print(f"missing {draft_path} - opponent source association requires it", file=sys.stderr)
        return 1

    if draft_raw is None:
        print(f"missing {draft_path} - opponent source association requires it", file=sys.stderr)
        return 1
    try:
        opponents = load_opponent_strategies(
            players, board, draft_raw, SOURCE_MATCHES, SOURCE_RANKINGS
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"cannot build opponent strategies: {exc} - run "
            "data_source_investigator/investigate.py",
            file=sys.stderr,
        )
        return 1

    if args.report:
        print(
            f"pool: {len(players)} players "
            + ", ".join(f"{k} {v}" for k, v in pool_meta["by_position"].items())
            + f"; dropped {pool_meta['dropped_non_offense']} non-offense and "
            f"{len(pool_meta['dropped_zero_projection'])} zero-projection",
            file=sys.stderr,
        )
        report_board(board)
        print("opponent source strategies:", file=sys.stderr)
        for slot in sorted(opponents):
            strategy = opponents[slot]
            print(
                f"  slot {slot:>2} {strategy.username or 'unknown':<20} "
                f"{strategy.source_name:<25} fit {strategy.fit_score:>4.1f}, "
                f"loss {strategy.mean_log2_loss:.3f}",
                file=sys.stderr,
            )
        print("converging wire levels:", file=sys.stderr)

    stream, draft, history = converge(players, board, args.report, opponents)
    draft = broaden_first_pick(draft, players, board, stream, opponents)

    candidates = (
        [c for _, _, c in draft.my_decisions.get(board.my_picks[0], [])]
        if board.my_picks
        else []
    )
    if args.report and candidates:
        print(
            f"candidate survival: {args.sims} banned-me redraws x {len(candidates)} "
            f"live-board candidates",
            file=sys.stderr,
        )
    survival = candidate_survival(
        players, board, stream, candidates,
        args.sims, args.noise, args.seed, opponents,
    )
    draft = apply_survival_floor(draft, board, survival)
    candidates = (
        [c for _, _, c in draft.my_decisions.get(board.my_picks[0], [])]
        if board.my_picks
        else []
    )
    if args.report and candidates:
        print(
            f"next-pick options: {args.sims} conditional opponent redraws x "
            f"{len(candidates)} candidates",
            file=sys.stderr,
        )
    options = option_redraw(
        players, board, stream, candidates,
        args.sims, args.noise, args.seed, opponents,
    )
    draft = apply_option_redraw(
        draft, options, players, board, stream, opponents
    )
    candidates = (
        [c for _, _, c in draft.my_decisions.get(board.my_picks[0], [])]
        if board.my_picks
        else []
    )
    if args.report and candidates:
        print(
            f"four-pick lookahead: broadened {len(candidates)}-candidate first-pick pool",
            file=sys.stderr,
        )
    lookahead = four_pick_lookahead(
        players, board, stream, candidates, survival, opponents,
        args.noise, args.seed,
    )
    if args.report and candidates:
        print(
            f"rollout: {ROLLOUT_SIMS} full-draft playouts x "
            f"{len(candidates)} four-pick plan sets",
            file=sys.stderr,
        )
    rolled = rollout(
        players, board, stream, candidates,
        ROLLOUT_SIMS, args.noise, args.seed, opponents, lookahead,
    )
    draft = apply_rollout(draft, rolled, players, board, stream, opponents)

    if args.report:
        print(
            f"monte carlo: {args.sims} source-strategy drafts "
            f"(adherence noise multiplier={args.noise})",
            file=sys.stderr,
        )
    picks = monte_carlo(
        players, board, stream, args.sims, args.noise, args.seed, opponents
    )
    rows = build_rankings(players, stream, draft, picks, args.sims, board)

    problems = board_problems + validate(rows, players, stream, draft, board, history)
    payload = build_payload(
        players, pool_meta, board, stream, draft, history, rows, problems,
        args.sims, args.noise, args.seed, opponents, options, rolled, survival,
    )
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    if args.report:
        report_summary(rows, draft, board, rolled, survival)
    if problems:
        print(f"\n{len(problems)} VALIDATION PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
    scope = f"{len(rows)} undrafted of {len(players)}" if board.live else f"{len(rows)} players"
    print(f"wrote {args.output} ({scope})", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
