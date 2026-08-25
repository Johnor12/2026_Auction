"""Ranking rows and serialized next-pick recommendations."""

from __future__ import annotations

import math

from .board import Board
from .league import LOOKAHEAD_PICKS, POSITIONS, pick_label
from .pool import Player
from .simulation import Draft
from .value import HORIZONS, team_values_with_candidates, upside_points


def _sum_levels(levels: dict[str, dict[str, float]]) -> dict[str, float]:
    """Collapse per-horizon levels to their 3-year sum, for the headline columns."""
    return {k: sum(levels[h][k] for h in HORIZONS) for k in POSITIONS}


def build_rankings(
    players: list[Player],
    wire: dict[str, dict[str, float]],
    draft: Draft,
    picks: dict[int, list[int]],
    sims: int,
    board: Board,
) -> list[dict]:
    """One row per *undrafted* player: the board is a list of decisions still to make.

    Drafted players are dropped rather than flagged — they cannot be picked, and leaving
    them in would put a name at rank 1 that is not available. Their points still shape the
    converged wire levels every row's lineup gain is measured with, which happens in
    `converge`. All rank columns are renumbered over what is emitted, so they read as
    positions on the remaining board rather than as gapped survivors of the preseason one.

    Two simulations are reported per player, and they answer different questions:
      * `sim_pick` is from the single deterministic draft, no noise.
      * `sim_adp` / `p_drafted` / `p_available_at_my_picks` come from the noisy redraws
        (adherence-calibrated draws over their provider ranks) and measure the other nine
        teams' demand only. My own simulated picks are this policy's behaviour, not market
        pressure — counting them as takes reported the model's own favourite stashes as
        scarce — but a redraw where I took him early also observes no opponent demand
        afterwards, so availability is a Kaplan-Meier estimate: an opponent take is the
        event, my own take censors the redraw. Under deterministic play availability is
        0 or 1, which tells you nothing about risk, so the noise band is what makes the
        columns usable at the table. It is also where uncertainty in the inferred source
        policies belongs.
    """
    my_picks = board.my_picks
    available = board.available(players)
    # The board's one metric and sort key — what the pick engine actually maximizes
    # (before lookahead): the player's marginal expected-lineup value on my current
    # roster at the converged wire levels. Matches value_now for the first pending
    # pick's candidates; roster-aware, so it shrinks where my roster is already deep.
    base, with_candidate = team_values_with_candidates(
        board.rosters[board.my_slot - 1], wire, available
    )
    gain = {pid: value - base for pid, value in with_candidate.items()}
    ranked = sorted(available, key=lambda p: (-gain[p.player_id], p.player_id))
    opponent_rank = {
        p.player_id: i
        for i, p in enumerate(
            sorted(
                available,
                key=lambda p: (draft.opponent_consensus_rank[p.player_id], p.player_id),
            ),
            start=1,
        )
    }
    pos_rank: dict[str, int] = {pos: 0 for pos in POSITIONS}
    rows: list[dict] = []
    for i, p in enumerate(ranked, start=1):
        pos_rank[p.position] += 1
        seen = picks[p.player_id]  # (pick, taken_by_me) per redraw he was drafted in
        # P(no opponent has taken him before each of my picks), Kaplan-Meier: walking the
        # takes in pick order, an opponent take drops survival by 1/at_risk; my own take
        # censors the redraw (removes it from at_risk without an event) — after it that
        # redraw can no longer show opponent demand, and counting it as either scarcity
        # or availability was wrong in turn. Ties sort events before censors ((pick,
        # False) < (pick, True)), the conservative convention. KM assumes my take times
        # are independent of opponent demand — my slot drafts noiselessly, so roughly
        # true; `p_available_if_i_pass` in my_next_picks is the assumption-free
        # counterfactual where the decision needs one. Only uncertain entries are
        # emitted; a 0.0 or 1.0 carries no information for a draft board. `latest` is
        # the last pick still at even odds, not the first (which is trivially 1.02 for
        # everybody except whoever goes 1.01).
        avail = {}
        latest = None
        surv, at_risk, j = 1.0, sims, 0
        removals = sorted(seen)
        for pick in my_picks:
            while j < len(removals) and removals[j][0] < pick:
                if not removals[j][1]:
                    surv *= 1.0 - 1.0 / at_risk
                at_risk -= 1
                j += 1
            if 0.01 < surv < 0.99:
                avail[pick_label(pick)] = round(surv, 3)
            if surv >= 0.5:
                latest = pick_label(pick)
        sim_pick = draft.pick_of.get(p.player_id)
        opp = [pk for pk, mine in seen if not mine]
        mean = sum(opp) / len(opp) if opp else None
        sd = (
            math.sqrt(sum((x - mean) ** 2 for x in opp) / len(opp))
            if len(opp) > 1
            else None
        )
        rows.append(
            {
                "rank": i,
                "player_id": p.player_id,
                "name": p.name,
                "position": p.position,
                "positional_rank": pos_rank[p.position],
                "team": p.team,
                "age": p.age,
                "bye_week": p.bye_week,
                "is_rookie": p.is_rookie,
                "points_3yr": p.points,
                "points_1yr": p.points_1yr,
                # Diagnostic of growth shape: his 3-year total at his years-2-3 pace.
                # Equal to points_3yr for a flat scorer; the gap is the provider's
                # implied growth. See value.upside_points.
                "upside_points": round(upside_points(p), 1),
                "lineup_gain": round(gain[p.player_id], 1),
                "sim_pick": sim_pick,
                "sim_pick_label": pick_label(sim_pick) if sim_pick else None,
                "sim_adp": round(mean, 1) if mean is not None else None,
                "sim_adp_sd": round(sd, 1) if sd is not None else None,
                "p_drafted": round(sum(not mine for _, mine in seen) / sims, 3),
                "latest_my_pick_likely_available": latest,
                "p_available_at_my_picks": avail,
                "provider_adp": p.provider_adp,
                "opponent_consensus_rank": opponent_rank[p.player_id],
                # Positive means my board values him earlier than the average of the
                # nine slot-specific provider boards actually used in the simulations.
                "opponent_rank_delta": opponent_rank[p.player_id] - i,
            }
        )
    return rows


def my_next_picks(
    draft: Draft,
    board: Board,
    rollout: dict | None = None,
    survival: dict[int, dict[int, float]] | None = None,
    limit: int = LOOKAHEAD_PICKS,
) -> list[dict]:
    """The model's recommendation plus the deterministic draft's fallback path.

    This is the question the whole script exists to answer, so it is surfaced rather than
    left implicit in `sim_pick`. The candidates carry the two parts of the decision score:
    what the player adds to my expected lineup now, and the expected value of the best
    player still there at my following pick if I take him. Positional depth enters only
    through projected expected-lineup value, never through a roster-size heuristic.

    At my first pending pick, `next_pick_ev` comes from short branch-specific opponent
    redraws conditioned on the candidate reaching that turn rather than the global-rank
    survival shortcut used by the bulk draft policy.
    That pick additionally carries a four-pick target plan and full-horizon rollout:
    `rollout_ev` is the mean final value of my whole roster if I take the candidate and
    the rest of the draft plays out, `rollout_edge` is his paired advantage over the
    ordinary policy on the same conditioned path, and `rollout_se` is its standard error.
    Every candidate carries a measured edge, the two-pick leader included, so `rollout_ev`
    ranks them directly. The first `take` is the rollout's conditional recommendation. If
    the deterministic example removes it before my turn, `deterministic_fallback` records
    what that path takes instead. A rollout can overrule the two-pick leader only by
    beating his plan by more than the playout noise between them.

    Its candidates also carry `p_available_if_i_pass` (planning.candidate_survival): across
    redraws where my slot is banned from ever taking him, the share where no opponent
    has taken him before each of my picks — the honest "how long can I wait on him".
    Unlike the Kaplan-Meier `p_available_at_my_picks`, it needs no independence
    assumption: the redraws actually play the pass-on-him counterfactual out.
    """
    out: list[dict] = []
    for pick_no in board.my_picks[:limit]:
        detail = draft.my_decisions.get(pick_no)
        if not detail:
            continue
        rolled = rollout if rollout and rollout["pick_no"] == pick_no else None
        actual_id = next((pid for pid, pk in draft.pick_of.items() if pk == pick_no), None)
        fallback = draft.by_id[actual_id] if actual_id is not None else detail[0][2]
        take = draft.by_id[rolled["take_id"]] if rolled else fallback
        candidates = []
        for now, later, c in detail:
            row = {
                "player_id": c.player_id,
                "name": c.name,
                "position": c.position,
                "value_now": round(now, 1),
                "next_pick_ev": round(later, 1),
                "score": round(now + later, 1),
            }
            if rolled:
                s = rolled["stats"][c.player_id]
                row["rollout_ev"] = round(s["ev"], 1)
                row["rollout_edge"] = round(s["edge"], 1)
                row["rollout_se"] = round(s["se"], 1)
                plan = rolled.get("plans", {}).get(c.player_id)
                if plan:
                    row["four_pick_plan"] = [
                        {
                            "pick": pick_label(pk),
                            "player_id": player_id,
                            "name": draft.by_id[player_id].name,
                            "position": draft.by_id[player_id].position,
                        }
                        for pk, player_id in zip(rolled["pick_nos"], plan["target_ids"])
                    ]
                    row["plan_screen_ev"] = round(plan["screen_ev"], 1)
            if survival and pick_no == board.my_picks[0] and c.player_id in survival:
                # Emitted for every pick, unlike p_available_at_my_picks: dropping the
                # certainties here deletes the answer. A player nobody else will ever take
                # and one who is gone by my next pick both serialize to nothing, and the
                # two cases carry opposite advice. This is one pick's shortlist, so the
                # full series is cheap.
                row["p_available_if_i_pass"] = {
                    pick_label(pk): round(p, 3) for pk, p in survival[c.player_id].items()
                }
            candidates.append(row)
        pick = {
            "pick": pick_label(pick_no),
            "overall": pick_no,
            "take_id": take.player_id,
            "take": f"{take.name} ({take.position})",
            "candidates": candidates,
        }
        if fallback.player_id != take.player_id:
            pick["deterministic_fallback_id"] = fallback.player_id
            pick["deterministic_fallback"] = f"{fallback.name} ({fallback.position})"
        out.append(pick)
    return out
