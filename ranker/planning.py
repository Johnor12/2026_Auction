"""Stochastic availability, lookahead, and rollout planning.

The deterministic draft engine lives in `simulation.py`, and wire convergence
lives in `convergence.py`. This module fans independent redraws and plan playouts across
worker processes, then applies the selected recommendation back to a deterministic draft.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import random
from collections.abc import Sequence

from .board import Board
from .league import CANDIDATE_SURVIVAL_FLOOR, FIRST_PICK_PER_POS, LOOKAHEAD_PICKS
from .opponents import OpponentStrategy
from .pool import Player, by_position
from .simulation import Draft
from .value import sorted_by_horizon, team_value, wire_replacement


def broaden_first_pick(
    draft: Draft,
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    opponents: dict[int, OpponentStrategy],
) -> Draft:
    """Build the live shortlist before intervening opponents can remove candidates."""
    if not board.my_picks:
        return draft
    pick_no = board.my_picks[0]
    pick_index = board.pick_nos.index(pick_no)
    state = Draft(players, stream, board, opponents=opponents)
    draft.my_decisions[pick_no] = state.score_my_candidates(
        pick_index, per_pos=FIRST_PICK_PER_POS
    )
    return draft


def apply_survival_floor(
    draft: Draft,
    board: Board,
    survival: dict[int, dict[int, float]],
) -> Draft:
    """Drop live candidates that almost never reach the pending decision."""
    if not board.my_picks:
        return draft
    pick_no = board.my_picks[0]
    detail = [
        row
        for row in draft.my_decisions[pick_no]
        if survival[row[2].player_id][pick_no] >= CANDIDATE_SURVIVAL_FLOOR
    ]
    assert detail, "no first-pick candidate clears the survival floor"
    draft.my_decisions[pick_no] = detail
    return draft

# The noisy redraws and the rollout playouts are hundreds of independent, seeded draft
# simulations, so they fan out over a process pool (stdlib multiprocessing). Workers get
# the shared inputs once via the initializer; each task is identified by its seed index,
# so results are deterministic regardless of scheduling. The playout worker looks its
# forced candidate up by id in its *own* copy of the pool — Draft stamps `availability_index`
# onto the pool's Player objects, so a separately-pickled Player would carry a stale one.
_WORKER: dict = {}
_FOUR_PICK_BEAM = 16


def _init_worker(
    players, board, stream, noise, seed, opponents, i_my, plans=None
) -> None:
    _WORKER.update(
        players=players, board=board, stream=stream, noise=noise, seed=seed,
        opponents=opponents, i_my=i_my, plans=plans or {},
        by_id={p.player_id: p for p in players},
    )


def _worker_pool_size() -> int:
    # Four busy workers leave enough host headroom for sustained reranks.
    return max(1, min(8, len(os.sched_getaffinity(0))))


def _target_map(plan: Sequence[int]) -> dict[int, Player]:
    """A player-id plan mapped onto the worker board's next held picks."""
    w = _WORKER
    indices = [i for i, slot in enumerate(w["board"].order) if slot == w["board"].my_slot]
    return {i: w["by_id"][player_id] for i, player_id in zip(indices, plan)}


def _conditioned_seed(kind: str, cand_id: int, sample: int) -> str:
    """A seeded opponent path where the candidate reaches my pending pick."""
    w = _WORKER
    for attempt in range(10_000):
        draw_seed = f"{kind}-{w['seed']}-{sample}-{attempt}"
        probe = Draft(
            w["players"], w["stream"], w["board"],
            noise=w["noise"], rng=random.Random(draw_seed), opponents=w["opponents"],
        )
        probe.run(stop_before=w["i_my"])
        if cand_id not in probe.taken:
            return draw_seed
    raise RuntimeError(f"candidate {cand_id} never survived to my pick")


def _final_roster_value(draft: Draft) -> float:
    """The common end-of-draft objective used by plan screening and noisy rollouts."""
    wire = wire_replacement(draft.taken, by_position(draft.players))
    return team_value(sorted_by_horizon(draft.rosters[draft.board.my_slot - 1]), wire)


def _plan_playout(plan: tuple[int, ...]) -> float:
    """One conditional full-draft screen for a four-pick target plan."""
    w = _WORKER
    draw_seed = _conditioned_seed("screen", plan[0], 0)
    d = Draft(
        w["players"], w["stream"], w["board"],
        noise=w["noise"], rng=random.Random(draw_seed), opponents=w["opponents"],
        targets=_target_map(plan),
    )
    d.run()
    return _final_roster_value(d)


def _mc_draft(s: int) -> dict[int, tuple[int, bool]]:
    """One noisy redraw -> per player (pick taken at, taken by my slot?).

    The flag matters because my own simulated picks are this policy's behaviour, not
    market pressure: counting them as takes reported the model's own favourite stashes
    as scarce (a player the policy grabs early looked "gone by 5.03" when in most
    redraws *I* was the one taking him). They cannot just be dropped either — a redraw
    where I took him early observes no opponent demand after that pick — so downstream
    they are censoring times, not events (see build_rankings).
    """
    w = _WORKER
    d = Draft(
        w["players"], w["stream"], w["board"], noise=w["noise"],
        rng=random.Random(w["seed"] + s),
        opponents=w["opponents"],
    )
    d.run()
    slot_of = dict(zip(d.pick_nos, d.order))
    return {pid: (pk, slot_of[pk] == d.my_slot) for pid, pk in d.pick_of.items()}


def _rollout_playout(task: tuple[int, int]) -> tuple[list[float], float]:
    cand_id, s = task
    w = _WORKER
    draw_seed = _conditioned_seed("rollout", cand_id, s)
    baseline = Draft(
        w["players"], w["stream"], w["board"], noise=w["noise"],
        rng=random.Random(draw_seed), opponents=w["opponents"],
    )
    baseline.run()
    baseline_value = _final_roster_value(baseline)
    values = []
    for plan in w["plans"].get(cand_id, ((cand_id,),)):
        d = Draft(
            w["players"], w["stream"], w["board"],
            noise=w["noise"], rng=random.Random(draw_seed), opponents=w["opponents"],
            targets=_target_map(plan),
        )
        d.run()
        values.append(_final_roster_value(d))
    return values, baseline_value


def _best_option_value(draft: Draft) -> float:
    """Best marginal value available to my roster at the current draft state."""
    slot = draft.my_slot
    roster = draft.rosters[slot - 1]
    roster_sorted = sorted_by_horizon(roster)
    base = team_value(roster_sorted, draft.wire)
    off = draft.off_pool[slot - 1]
    values = [
        team_value(roster_sorted, draft.wire, cand) - base
        for cand in draft.candidates(
            roster,
            per_pos=1,
            picks_left=draft.picks_left[slot - 1],
            off=off,
        )
    ]
    assert values, "pool exhausted at my following pick"
    return max(values)


def _option_playout(task: tuple[int, int]) -> float:
    """Condition on one candidate, take him, and price my following option."""
    cand_id, s = task
    w = _WORKER
    i_my = w["i_my"]
    assert i_my is not None
    draw_seed = _conditioned_seed("option", cand_id, s)
    d = Draft(
        w["players"], w["stream"], w["board"], noise=w["noise"],
        rng=random.Random(draw_seed),
        opponents=w["opponents"],
        forced={i_my: w["by_id"][cand_id]},
    )
    i_next = d.next_pick[i_my]
    assert i_next is not None
    d.run(stop_before=i_next)
    return _best_option_value(d)


def monte_carlo(
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    sims: int,
    noise: float,
    seed: int,
    opponents: dict[int, OpponentStrategy],
) -> dict[int, list[tuple[int, bool]]]:
    """Noisy redraws -> per-player (pick, taken-by-me) observations (see `_mc_draft`).

    Each observation records whether my slot made the pick; consumers can count
    opponent takes without maintaining the same information in a second structure.
    `candidate_survival` is the assumption-free counterfactual, priced only for the
    players where the decision actually needs it.
    """
    picks: dict[int, list[tuple[int, bool]]] = {p.player_id: [] for p in players}
    with multiprocessing.Pool(
        _worker_pool_size(),
        initializer=_init_worker,
        initargs=(players, board, stream, noise, seed, opponents, None),
    ) as pool:
        for pick_of in pool.map(_mc_draft, range(sims)):
            for pid, (pick, mine) in pick_of.items():
                picks[pid].append((pick, mine))
    return picks


def _survival_draft(task: tuple[int, int]) -> int | None:
    cand_id, s = task
    w = _WORKER
    d = Draft(
        w["players"], w["stream"], w["board"], noise=w["noise"],
        rng=random.Random(w["seed"] + s),
        opponents=w["opponents"], my_ban=cand_id,
    )
    return d.run(until_taken=cand_id)


def candidate_survival(
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    candidates: list[Player],
    sims: int,
    noise: float,
    seed: int,
    opponents: dict[int, OpponentStrategy],
) -> dict[int, dict[int, float]]:
    """P(a next-pick candidate is still there at each of my picks) if I keep passing on him.

    "How long can I wait on him" cannot be read off `monte_carlo`: my slot drafts in
    those redraws, and for a player this policy likes it takes him early in most of them,
    which censors the opponents' demand exactly where the question matters. So each
    candidate gets his own redraws with my slot banned from ever taking him (`my_ban`) —
    the other eleven teams play exactly as in `monte_carlo` — and availability at my pick
    is simply "no opponent had taken him yet". Priced for my next pick's candidates only:
    one banned-me redraw set per player is too expensive for the whole board.
    """
    if not board.my_picks or not candidates:
        return {}
    tasks = [(cand.player_id, s) for cand in candidates for s in range(sims)]
    with multiprocessing.Pool(
        _worker_pool_size(),
        initializer=_init_worker,
        initargs=(players, board, stream, noise, seed, opponents, None),
    ) as pool:
        flat = pool.map(_survival_draft, tasks)
    out: dict[int, dict[int, float]] = {}
    for i, cand in enumerate(candidates):
        taken = flat[i * sims : (i + 1) * sims]
        out[cand.player_id] = {
            pick: sum(1 for pk in taken if pk is None or pk >= pick) / sims
            for pick in board.my_picks
        }
    return out


def conditional_survival(
    survival: dict[int, dict[int, float]],
    player_id: int,
    first_pick: int,
    pick_no: int,
) -> float:
    """Survival to a later pick, given that the player reached the first one."""
    observations = survival[player_id]
    at_first = observations[first_pick]
    return min(1.0, observations[pick_no] / at_first) if at_first else 0.0


def four_pick_lookahead(
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    candidates: list[Player],
    survival_by_candidate: dict[int, dict[int, float]],
    opponents: dict[int, OpponentStrategy],
    noise: float,
    seed: int,
    lookahead_picks: int = LOOKAHEAD_PICKS,
) -> dict | None:
    """Choose one target plan per first-pick candidate across my next four held picks.

    Candidate survival supplies the market timing signal the old two-pick policy discarded.
    A small beam proposes sequences by survival-weighted marginal lineup gain; targets
    below the survival floor are removed at each turn. Every prefix is also retained, so
    resuming the ordinary policy is an explicit option at turns two, three, and four. One
    conditional playout screens each plan before `rollout` applies the full simulation.
    """
    if not board.my_picks or not candidates:
        return None
    pick_nos = board.my_picks[:lookahead_picks]
    first_index = board.pick_nos.index(pick_nos[0])

    # Generate from the live board. Opponents between now and my pick are uncertain, and
    # candidate_survival decides which of these current options remain worth planning for.
    state = Draft(players, stream, board, opponents=opponents)
    roster = state.rosters[board.my_slot - 1]
    off = state.off_pool[board.my_slot - 1]
    picks_left = state.picks_left[board.my_slot - 1]
    by_id = {p.player_id: p for p in candidates}
    candidate_ids = tuple(by_id)
    base = team_value(sorted_by_horizon(roster), state.wire)
    value_cache: dict[tuple[int, ...], float] = {(): base}

    def roster_value(plan: tuple[int, ...]) -> float:
        key = tuple(sorted(plan))
        value = value_cache.get(key)
        if value is None:
            value = team_value(
                sorted_by_horizon(roster + [by_id[player_id] for player_id in plan]),
                state.wire,
            )
            value_cache[key] = value
        return value

    def available_probability(player_id: int, pick_no: int) -> float:
        return conditional_survival(
            survival_by_candidate, player_id, pick_nos[0], pick_no
        )

    def legal_after(plan: tuple[int, ...], depth: int) -> list[int]:
        future_roster = roster + [by_id[player_id] for player_id in plan]
        remaining = [player_id for player_id in candidate_ids if player_id not in plan]
        for positions, rookies_only in state._eligibility_scenarios(
            future_roster, picks_left - depth, off
        ):
            legal = [
                player_id
                for player_id in remaining
                if by_id[player_id].position in positions
                and (not rookies_only or by_id[player_id].is_rookie)
                and available_probability(player_id, pick_nos[depth])
                >= CANDIDATE_SURVIVAL_FLOOR
            ]
            if legal:
                return legal
        return []

    proposed: dict[int, dict[tuple[int, ...], float]] = {}
    for first in candidates:
        first_plan = (first.player_id,)
        states = [(roster_value(first_plan) - base, first_plan)]
        plans = {first_plan: states[0][0]}
        for depth, pick_no in enumerate(pick_nos[1:], start=1):
            expanded: list[tuple[float, tuple[int, ...]]] = []
            for score, plan in states:
                before = roster_value(plan)
                for player_id in legal_after(plan, depth):
                    next_plan = plan + (player_id,)
                    marginal = roster_value(next_plan) - before
                    next_score = score + available_probability(player_id, pick_no) * marginal
                    expanded.append((next_score, next_plan))
            if not expanded:
                break
            expanded.sort(key=lambda row: (-row[0], row[1]))
            states = expanded[:_FOUR_PICK_BEAM]
            plans.update({plan: score for score, plan in states})
        proposed[first.player_id] = plans

    all_plans = [plan for plans in proposed.values() for plan in plans]
    with multiprocessing.Pool(
        _worker_pool_size(),
        initializer=_init_worker,
        initargs=(players, board, stream, noise, seed, opponents, first_index),
    ) as pool:
        final_values = pool.map(_plan_playout, all_plans)

    screened: dict[int, dict] = {}
    cursor = 0
    for first in candidates:
        plans = proposed[first.player_id]
        rows = []
        for plan, heuristic_gain in plans.items():
            rows.append((final_values[cursor], heuristic_gain, plan))
            cursor += 1
        finalists = []
        for length in range(1, len(pick_nos) + 1):
            at_length = [row for row in rows if len(row[2]) == length]
            if not at_length:
                continue
            at_length.sort(key=lambda row: (-row[0], -row[1], row[2]))
            screen_value, heuristic_gain, plan = at_length[0]
            finalists.append(
                {
                    "target_ids": list(plan),
                    "heuristic_gain": heuristic_gain,
                    "screen_ev": screen_value,
                }
            )
        screened[first.player_id] = {
            "finalists": finalists,
        }
    return {
        "pick_no": pick_nos[0],
        "pick_nos": pick_nos,
        "depth": len(pick_nos),
        "candidate_count": len(candidates),
        "beam_width": _FOUR_PICK_BEAM,
        "plans": screened,
    }


def option_redraw(
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    candidates: list[Player],
    sims: int,
    noise: float,
    seed: int,
    opponents: dict[int, OpponentStrategy],
) -> dict | None:
    """Empirical next-pick option value for each candidate at my first pending pick.

    Each branch redraws opponents before my pick until its candidate survives, forces the
    candidate, then continues the same noisy path to my following pick. The result is the
    conditional marginal value of the best player actually left there. This deliberately
    replaces the global-rank survival shortcut for the decision in front of me.
    """
    if not board.my_picks or not candidates:
        return None
    pick_no = board.my_picks[0]
    i_my = board.pick_nos.index(pick_no)
    i_next = next(
        (i for i in range(i_my + 1, len(board.order)) if board.order[i] == board.my_slot),
        None,
    )
    if i_next is None:
        return {
            "pick_no": pick_no,
            "sims": 0,
            "stats": {cand.player_id: {"ev": 0.0} for cand in candidates},
        }

    tasks = [(cand.player_id, s) for cand in candidates for s in range(sims)]
    with multiprocessing.Pool(
        _worker_pool_size(),
        initializer=_init_worker,
        initargs=(players, board, stream, noise, seed, opponents, i_my),
    ) as pool:
        flat = pool.map(_option_playout, tasks)
    return {
        "pick_no": pick_no,
        "sims": sims,
        "stats": {
            cand.player_id: {
                "ev": sum(flat[i * sims : (i + 1) * sims]) / sims,
            }
            for i, cand in enumerate(candidates)
        },
    }


def _replay_pick(
    draft: Draft,
    pick_no: int,
    take: Player,
    detail: list[tuple[float, float, Player]],
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    opponents: dict[int, OpponentStrategy],
    targets: dict[int, Player] | None = None,
) -> Draft:
    """Re-play the deterministic draft when a re-scored recommendation changes."""
    actual_id = next((pid for pid, pk in draft.pick_of.items() if pk == pick_no), None)
    if actual_id == take.player_id and not targets:
        draft.my_decisions[pick_no] = detail
        return draft
    forced = Draft(
        players, stream, board,
        opponents=opponents,
        forced=None if targets else {board.pick_nos.index(pick_no): take},
        targets=targets,
    )
    forced.run()
    forced.my_decisions[pick_no] = detail
    return forced


def _available_in_deterministic_draft(draft: Draft, pick_no: int, player: Player) -> bool:
    """Whether the noiseless path still has the player on the board at my pick."""
    taken_at = draft.pick_of.get(player.player_id)
    return taken_at is None or taken_at >= pick_no


def apply_option_redraw(
    draft: Draft,
    redrawn: dict | None,
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    opponents: dict[int, OpponentStrategy],
) -> Draft:
    """Replace the first pick's sigmoid lookahead with its branch-redraw estimates."""
    if redrawn is None:
        return draft
    pick_no = redrawn["pick_no"]
    detail = [
        (
            now,
            redrawn["stats"][cand.player_id]["ev"],
            cand,
        )
        for now, _, cand in draft.my_decisions[pick_no]
    ]
    detail.sort(key=lambda t: (-(t[0] + t[1]), t[2].player_id))
    take = next(
        cand
        for _, _, cand in detail
        if _available_in_deterministic_draft(draft, pick_no, cand)
    )
    return _replay_pick(
        draft, pick_no, take, detail,
        players, board, stream, opponents,
    )


def _paired_mean(diffs: list[float]) -> float:
    return sum(diffs) / len(diffs)


def _paired_se(diffs: list[float]) -> float:
    """Standard error of a paired difference of means over common random draws."""
    if len(diffs) <= 1:
        return 0.0
    mean = _paired_mean(diffs)
    var = sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1)
    return math.sqrt(var / len(diffs))


def rollout_decision(
    ids: list[int],
    values: dict[int, list[float]],
    baselines: dict[int, list[float]],
) -> tuple[dict[int, dict[str, float]], int]:
    """Paired rollout stats and the conditional take, with `ids[0]` as the base.

    The base's own multi-pick plan can beat the ordinary policy too, so its edge is
    measured like everyone else's rather than assumed zero — otherwise a candidate worth
    less than the base looks like the only improvement on the board. That also makes the
    base a noisy reference, so an override is tested against its plan directly on the
    shared draws rather than against its edge.
    """
    stats: dict[int, dict[str, float]] = {}
    for pid in ids:
        diffs = [value - base for value, base in zip(values[pid], baselines[pid])]
        stats[pid] = {
            "ev": _paired_mean(values[pid]),
            "edge": _paired_mean(diffs),
            "se": _paired_se(diffs),
        }
    base_id = ids[0]
    take_id, best_margin = base_id, 0.0
    for pid in ids[1:]:
        over_base = [value - base for value, base in zip(values[pid], values[base_id])]
        margin = _paired_mean(over_base)
        if margin > best_margin and margin > 2 * _paired_se(over_base):
            take_id, best_margin = pid, margin
    return stats, take_id


def rollout(
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    candidates: list[Player],
    sims: int,
    noise: float,
    seed: int,
    opponents: dict[int, OpponentStrategy],
    lookahead: dict | None = None,
) -> dict | None:
    """Full-horizon EV for each four-pick plan at my next pick.

    `four_pick_lookahead` supplies one screened target plan of each length for every
    first-pick candidate. Each playout redraws the opponents before my pick until that
    candidate survives, then compares the target plan with the ordinary policy on the
    same path. Later targets still fall back if an opponent gets there first. Every
    candidate's `edge` is that paired comparison, the base included: its plan is a plan
    too, not the ordinary policy. `take_id` is conditional on availability and overrides
    the base only when it beats the base's own plan by 2 standard errors of their paired
    difference.
    """
    if not board.my_picks or not candidates:
        return None
    pick_no = board.my_picks[0]
    i_my = board.pick_nos.index(pick_no)
    plans = {
        cand.player_id: tuple(
            tuple(finalist["target_ids"])
            for finalist in (lookahead or {}).get("plans", {}).get(
                cand.player_id, {"finalists": [{"target_ids": [cand.player_id]}]}
            )["finalists"]
        )
        for cand in candidates
    }
    tasks = [(cand.player_id, s) for cand in candidates for s in range(sims)]
    with multiprocessing.Pool(
        _worker_pool_size(),
        initializer=_init_worker,
        initargs=(players, board, stream, noise, seed, opponents, i_my, plans),
    ) as pool:
        flat = pool.map(_rollout_playout, tasks)
    values: dict[int, list[float]] = {}
    baselines: dict[int, list[float]] = {}
    selected_plans: dict[int, dict] = {}
    for i, cand in enumerate(candidates):
        runs = flat[i * sims : (i + 1) * sims]
        finalists = (lookahead or {}).get("plans", {}).get(cand.player_id, {}).get(
            "finalists", [{"target_ids": [cand.player_id]}]
        )
        choices = []
        for plan_index, finalist in enumerate(finalists):
            samples = [plan_values[plan_index] for plan_values, _ in runs]
            choices.append((sum(samples) / sims, tuple(finalist["target_ids"]), samples, finalist))
        choices.sort(key=lambda row: (-row[0], len(row[1]), row[1]))
        _, _, values[cand.player_id], selected_plans[cand.player_id] = choices[0]
        baselines[cand.player_id] = [baseline for _, baseline in runs]

    # candidates remain sorted by the ordinary two-pick policy, so the first one is the base
    stats, take_id = rollout_decision(
        [cand.player_id for cand in candidates], values, baselines
    )
    return {
        "pick_no": pick_no,
        "pick_nos": (lookahead or {}).get("pick_nos", [pick_no]),
        "sims": sims,
        "take_id": take_id,
        "stats": stats,
        "plans": selected_plans,
    }


def apply_rollout(
    draft: Draft,
    rolled: dict | None,
    players: list[Player],
    board: Board,
    stream: dict[str, dict[str, float]],
    opponents: dict[int, OpponentStrategy],
) -> Draft:
    """Apply the recommended plan when its first target survives the noiseless path.

    The recommendation itself is conditional on availability. If the noiseless example
    removes that player first, it remains a fallback example rather than vetoing the
    higher-EV recommendation or forcing an impossible duplicate pick.
    """
    if rolled is None:
        return draft
    pick_no = rolled["pick_no"]
    detail = draft.my_decisions[pick_no]
    take = next(c for _, _, c in detail if c.player_id == rolled["take_id"])
    if not _available_in_deterministic_draft(draft, pick_no, take):
        return draft
    plan_ids = rolled.get("plans", {}).get(take.player_id, {}).get(
        "target_ids", [take.player_id]
    )
    target_indices = [
        i for i, slot in enumerate(board.order) if slot == board.my_slot
    ][: len(plan_ids)]
    by_id = {p.player_id: p for p in players}
    targets = {i: by_id[player_id] for i, player_id in zip(target_indices, plan_ids)}
    return _replay_pick(
        draft, pick_no, take, detail,
        players, board, stream, opponents, targets,
    )
