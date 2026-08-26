"""Assemble the documented rankings.json payload."""

from __future__ import annotations

from .board import Board
from .league import (
    BENCH_SLOTS,
    CANDIDATE_SURVIVAL_FLOOR,
    FIRST_PICK_PER_POS,
    LOOKAHEAD_PICKS,
    OPPONENT_DEPTH_PENALTY,
    OPPONENT_DEPTH_TARGETS,
    POINTS_FIELD,
    POSITIONS,
    ROUNDS,
    SCHEME,
    STARTING_SLOTS,
    SURVIVAL_SIGMA,
    TAXI_SLOTS,
    TEAMS,
    TOTAL_PICKS,
    UNAVAILABLE_RATE,
    draft_order,
    pick_label,
    picks_for_slot,
)
from .opponents import OpponentStrategy
from .pool import Player
from .rankings import _sum_levels, my_next_picks
from .simulation import Draft
from .value import HORIZONS


def team_names(board: Board) -> dict[int, str | None]:
    return {
        s["draft_slot"]: (s.get("team_name") or s.get("username"))
        for s in ((board.live or {}).get("slots") or [])
    }


def draft_block(board: Board) -> dict | None:
    """How the output describes the board it started from. None when there was no live one."""
    if not board.live:
        return None
    names = team_names(board)
    block = {k: v for k, v in board.live.items() if k != "slots"}
    block["my_remaining_picks"] = [pick_label(n) for n in board.my_picks]
    block["note"] = (
        "The simulation starts here: made picks are already on their teams' rosters and out "
        "of the pool, and only the pending picks are played out, in this order — so a traded "
        "pick is exercised by the roster that acquired it. `rankings` covers the undrafted "
        "players only."
    )
    block["off_pool_note"] = (
        "Made picks with no match in pool.json by sleeper_id: kickers, IDP and anyone past "
        "the pool's rank cut. They fill a roster spot and satisfy a mandatory position, so "
        "the team owes one fewer pick, but they are never started and never valued — there "
        "is no projection to value them with."
    )
    block["rosters"] = []
    for slot in range(1, TEAMS + 1):
        made, off = board.rosters[slot - 1], board.off_pool[slot - 1]
        counts = {pos: 0 for pos in POSITIONS}
        for p in made:
            counts[p.position] += 1
        for o in off:
            if o.get("position") in counts:
                counts[o["position"]] += 1
        block["rosters"].append(
            {
                "draft_slot": slot,
                "team": names.get(slot),
                "is_mine": slot == board.my_slot,
                "picks_made": len(made) + len(off),
                "picks_left": board.picks_left[slot - 1],
                "positions": {pos: n for pos, n in counts.items() if n},
                "players": [f"{p.name} ({p.position})" for p in made],
                "off_pool": [f"{o['name']} ({o['position']})" for o in off],
            }
        )
    return block


def example_rosters(draft: Draft, board: Board) -> list[dict]:
    """Every team's final roster from the deterministic draft, for eyeballing smells.

    Rosters read in pick order: the live board's made picks first (the board does not keep
    their pick numbers), then the simulated picks with the pick they were taken at. Each
    entry carries the projection fields the dashboard needs; rankings only contains
    undrafted players, so looking rostered players up there leaves every live pick blank.
    """
    names = team_names(board)
    out: list[dict] = []
    for slot in range(1, TEAMS + 1):
        roster, off = draft.rosters[slot - 1], draft.off_pool[slot - 1]
        counts = {pos: 0 for pos in POSITIONS}
        for p in roster:
            counts[p.position] += 1
        for o in off:
            if o.get("position") in counts:
                counts[o["position"]] += 1
        picks: list[dict] = []
        for p in roster:
            pick_no = draft.pick_of.get(p.player_id)
            future_per_year = (p.points - p.points_1yr) / 2
            picks.append(
                {
                    "pick": pick_label(pick_no) if pick_no else None,
                    "is_made": pick_no is None,
                    "player_id": p.player_id,
                    "name": p.name,
                    "position": p.position,
                    "nfl_team": p.team,
                    "age": p.age,
                    "is_rookie": p.is_rookie,
                    "points_1yr": p.points_1yr,
                    "points_3yr": p.points,
                    "future_points_per_year": round(future_per_year, 1),
                    "growth_per_year": round(future_per_year - p.points_1yr, 1),
                    "off_pool": False,
                }
            )
        picks += [
            {
                "pick": o["pick"],
                "is_made": True,
                "player_id": None,
                "name": o["name"],
                "position": o["position"],
                "nfl_team": None,
                "age": None,
                "is_rookie": None,
                "points_1yr": None,
                "points_3yr": None,
                "future_points_per_year": None,
                "growth_per_year": None,
                "off_pool": True,
            }
            for o in off
        ]
        out.append(
            {
                "draft_slot": slot,
                "team": names.get(slot),
                "is_mine": slot == board.my_slot,
                "players": len(roster) + len(off),
                "rookies": sum(p.is_rookie for p in roster),
                "positions": counts,
                "picks": picks,
            }
        )
    return out


def build_payload(
    players: list[Player],
    pool_meta: dict,
    board: Board,
    stream: dict[str, dict[str, float]],
    draft: Draft,
    history: dict,
    rows: list[dict],
    problems: list[str],
    sims: int,
    noise: float,
    seed: int,
    opponents: dict[int, OpponentStrategy],
    option_redraw: dict | None = None,
    rollout: dict | None = None,
    survival: dict[int, dict[int, float]] | None = None,
) -> dict:
    return {
        "generated_from": pool_meta["source_file"],
        "scoring_scheme": SCHEME,
        "value_input": f"pool.json {POINTS_FIELD} + points_1yr ({SCHEME})",
        "value_note": (
            "Projected points in this league's scheme, split into two horizons — year 1 "
            "(points_1yr) and years 2-3 (points_3yr - points_1yr) — because lineups are "
            "fielded per season. Roster value is the best expected legal lineup in each "
            "horizon, including the probability that deeper players are called on when "
            "higher teammates are unavailable and one unique waiver body per position. "
            "Draftsharks' 3D value is deliberately unused: it is a "
            "provider-scaled ordinal, not points, so it cannot enter a "
            "points-denominated lineup objective."
        ),
        "league": {
            "teams": TEAMS,
            "starting_slots": STARTING_SLOTS,
            "bench_slots": BENCH_SLOTS,
            "taxi_slots": TAXI_SLOTS,
            "rounds": ROUNDS,
            "total_picks": TOTAL_PICKS,
            "draft_type": "snake",
            "my_slot": board.my_slot,
            "my_picks": [pick_label(p) for p in picks_for_slot(board.my_slot, draft_order())],
        },
        "pool": pool_meta,
        "draft": draft_block(board),
        "example_draft": {
            "note": (
                "Full final rosters from the single deterministic draft at the converged "
                "levels — the same draft sim_pick reports. When the conditional first-pick "
                "recommendation is taken earlier on this noiseless path, this block follows "
                "the reported deterministic fallback instead. Made picks are "
                "facts from the live board; every pending pick is the model drafting. Read "
                "it for smells: position hoarding, a position left to the last rounds."
            ),
            "rosters": example_rosters(draft, board),
        },
        "my_next_picks": {
            "note": (
                "The model's own choice at each of my next picks, from the deterministic "
                "draft at the converged levels. The first shortlist starts from the live "
                "board before intervening opponents pick, then drops candidates below a "
                "5% chance of reaching my turn. value_now is the candidate's marginal "
                "expected-lineup value with one unique waiver fallback per position; "
                "next_pick_ev is E[value of the best player still there at my following "
                "pick] if I take him now. No positional roster-size heuristic adjusts "
                "either value. The pick "
                "maximizes their sum, so it can disagree with the board's static "
                "lineup_gain order — the lookahead prices what my following pick keeps, "
                "which a single-pick gain does not. At my first pending pick, each "
                "next_pick_ev redraws the pre-pick opponents until that candidate survives, "
                "takes him, and measures the best marginal option actually left at my "
                "following turn; "
                "later displayed picks retain the fast global-rank approximation used by "
                "the bulk draft policy. My first pending pick searches target plans across "
                "my next four held picks; every shorter prefix is eligible, so the ordinary "
                "policy can resume at any turn. Later targets below 5% conditional survival "
                "are dropped. The best screened plan of each length is then scored over the "
                "whole remaining draft, and the best noisy EV "
                "represents that first candidate "
                "(rollout_ev/rollout_edge/rollout_se), with a planned target used only if he "
                "survives and the ordinary policy used otherwise. rollout_edge is that "
                "plan's paired gain over the ordinary policy on the same draws, measured "
                "for every candidate including the two-pick leader, whose plan is a plan "
                "too — so rollout_ev ranks candidates directly. `take` is the EV choice "
                "conditional on being available; `deterministic_fallback` is emitted when "
                "the noiseless example removes that choice first. The two-pick leader's "
                "take stands unless another candidate beats his plan by twice the standard "
                "error of their paired difference. When the recommended player survives the noiseless "
                "prefix, that draft is re-played with the selected plan; otherwise the "
                "example draft and later displayed picks describe the fallback path. Its "
                "candidates also carry "
                "p_available_if_i_pass — P(no opponent has taken him before each of my "
                "picks) across redraws where my slot never takes him: the honest 'how "
                "long can I wait', with the pass-on-him counterfactual actually played "
                "out rather than estimated (compare p_available_at_my_picks, which is "
                "Kaplan-Meier from redraws where I do take him). It covers every remaining "
                "pick, including the certainties, because 'nobody else ever wants him' and "
                "'gone before my next pick' are the two answers that decide the pick."
            ),
            "option_sims": option_redraw["sims"] if option_redraw else None,
            "rollout_sims": rollout["sims"] if rollout else None,
            "lookahead_picks": LOOKAHEAD_PICKS,
            "first_pick_candidates_per_position_horizon": FIRST_PICK_PER_POS,
            "candidate_survival_floor": CANDIDATE_SURVIVAL_FLOOR,
            "picks": my_next_picks(draft, board, rollout, survival),
        },
        "rankings_note": (
            "Undrafted players only, ranked by `lineup_gain` — the decision metric the "
            "pick engine maximizes (before lookahead): the player's marginal "
            "expected-lineup value on my current roster at the converged wire levels. "
            "Roster-aware, so it shrinks where my roster is already deep. The rank "
            "columns are renumbered over the rows emitted here."
            if board.live
            else "The whole pool, from an empty board: no live draft was read."
        ),
        "opponent_model": {
            "who": "the other eleven teams; my slot alone uses projections and roster value",
            "how": (
                "Each opponent orders legal available players by the provider board most "
                "associated with its completed picks in data_source_matches.json. Opponent "
                "valuation never reads personal projections, wire levels, or team "
                "value. A position's source rank receives a soft boost in "
                "proportion to its unfilled dedicated starters and a compounding penalty "
                "beyond its comfortable depth; the deterministic draft takes the best "
                "adjusted rank, and Monte Carlo draws around that preference."
            ),
            "depth_preference": {
                "targets": OPPONENT_DEPTH_TARGETS,
                "penalty_per_extra_player": OPPONENT_DEPTH_PENALTY,
                "note": (
                    "Targets sum to 12, leaving two roster spots to spill into the best "
                    "remaining positions. This adjusts opponent source rank only and is "
                    "not a draft limit or an input to my roster valuation."
                ),
            },
            "adherence": (
                "The investigator's mean_log2_loss is converted to a power distribution "
                "over source rank among legal available players, before the roster-balance "
                "adjustment. --noise 1 uses that fitted randomness; 0 removes randomness "
                "but retains the balance adjustment. fit_score and confidence identify "
                "association strength, while mean_log2_loss determines adherence."
            ),
            "coverage": (
                "A provider's normalized players come first. Any pool player it does not "
                "rank is appended in DraftSharks ADP order so all 168 picks remain possible; "
                "the fallback is still an external opponent board, never my personal board."
            ),
            "delta": (
                "opponent_consensus_rank averages the eleven managers' complete source "
                "orders, counting a source once per associated manager, then re-ranks the "
                "available pool. opponent_rank_delta is opponent_consensus_rank - rank; "
                "positive identifies players my board values earlier than the modeled "
                "field. Monte Carlo availability determines whether that gap is exploitable."
            ),
            "divergence": {
                "distinct_sources": len({s.source_id for s in opponents.values()}),
                "mean_absolute_rank_delta": round(
                    sum(abs(row["opponent_rank_delta"]) for row in rows) / len(rows), 1
                ),
                "max_absolute_rank_delta": max(
                    abs(row["opponent_rank_delta"]) for row in rows
                ),
            },
            "strategies": [opponents[slot].public() for slot in sorted(opponents)],
        },
        "wire": {
            "definition": (
                "The best player at each position left undrafted in the converged "
                "simulated draft — the post-draft free agent baseline, per horizon "
                "(yr1 = year 1, yr23 = years 2-3; the best year-1 add and the best "
                "years-2-3 stash can differ). Inside roster valuation, each position "
                "contributes this body once as an always-available fallback; it cannot "
                "fill two simultaneous lineup jobs. `levels` is the horizon sum; the "
                "split is in levels_by_horizon."
            ),
            "levels": {k: round(v, 1) for k, v in _sum_levels(stream).items()},
            "levels_by_horizon": {
                h: {k: round(v, 1) for k, v in stream[h].items()} for h in HORIZONS
            },
            "convergence": history,
        },
        "strategy": {
            "objective": (
                "sum over horizons (year 1, years 2-3) of expected optimal legal lineup "
                "points under position-wide player availability"
            ),
            "unavailable_rate": UNAVAILABLE_RATE,
            "depth_note": (
                "The lineup is re-optimized per availability draw: dedicated slots take "
                "each position's best available bodies (a deeper body contributes with "
                "the exact probability it is called on, from unavailable_rate), and the "
                "FLEX/superflex seats take the best leftovers pooled across positions, "
                "at most one of them a QB. Computed in closed form as "
                "expectation-of-weekly-max, not one locked seasonal composition. "
                "Years 2-3 receive no extra growth bonus because their projection and "
                "separately solved lineup already contain growth."
            ),
            "lookahead": (
                "first pending decision: four held picks with survival-aware target plans; "
                "personal bulk policy: value now + E[best value at the following pick]"
            ),
            "survival_sigma": SURVIVAL_SIGMA,
            "note": (
                "The bulk policy is a two-pick greedy with an independence approximation. "
                "The first pending pick uses banned-me availability redraws to build plans "
                "across four held picks, then noisy full-draft rollouts choose among them — "
                "see my_next_picks."
            ),
        },
        "monte_carlo": {
            "sims": sims,
            "noise": noise,
            "seed": seed,
            "note": (
                "Balance-adjusted source-rank Gumbel draws on the other 9 teams only, to "
                "turn 0/1 availability under the deterministic preference into a usable "
                "probability band. At noise=1 the source-rank component is calibrated to "
                "each manager's observed mean log-rank loss before the balance adjustment; "
                "noise=0 removes random variation but retains that adjustment. sim_pick is "
                "from the noiseless draft; sim_adp, p_drafted and "
                "p_available_at_my_picks are from these redraws and measure the other eleven "
                "teams' demand only — my own "
                "simulated picks are the policy under evaluation, not opponent demand. "
                "p_available_at_my_picks is a Kaplan-Meier estimate (an opponent take is "
                "the event, my own take censors the redraw); "
                "my_next_picks.p_available_if_i_pass is the assumption-free counterfactual "
                "for the candidates that matter. sim_adp and p_drafted are over observed "
                "opponent takes, so both read shallow for a player this policy usually "
                "grabs first."
            ),
        },
        "validation": {"problems": problems, "ok": not problems},
        "count": len(rows),
        "rankings": rows,
    }
