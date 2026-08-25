"""Offline checks for the states the live files cannot currently reach.

Three suites: the greedy lineup solver against brute force, source-based opponent
behavior, and the board loader against synthetic draft.json boards — a traded pick, a
selection outside the pool, a resumed partial board, and six malformed boards that must
be rejected.
"""

from __future__ import annotations

import dataclasses
import sys

from .board import fresh_board, load_board
from .league import (
    DEDICATED_SLOTS,
    FIRST_PICK_PER_POS,
    MY_SLOT,
    OPPONENT_DEPTH_TARGETS,
    OPPONENT_POSITION_TILT,
    POSITIONS,
    ROUNDS,
    STARTING_SLOTS,
    TEAMS,
    UNAVAILABLE_RATE,
    TOTAL_PICKS,
    draft_order,
    pick_label,
)
from .opponents import OpponentStrategy, expected_log2_rank, rank_power
from .planning import (
    apply_survival_floor,
    broaden_first_pick,
    conditional_survival,
    rollout_decision,
)
from .pool import Player
from .rankings import my_next_picks
from .simulation import Draft
from .value import (
    expected_lineup_value,
    position_expected_value,
    seed_wire,
    sorted_by_horizon,
    team_value,
)


def synthetic_opponents(
    players: list[Player], board, first_order: list[Player] | None = None
) -> dict[int, OpponentStrategy]:
    """Complete external boards for simulation tests; no personal value is stored."""
    default = first_order or players
    order = tuple(p.player_id for p in default)
    ranks = {player_id: rank for rank, player_id in enumerate(order, start=1)}
    return {
        slot: OpponentStrategy(
            slot=slot,
            roster_id=slot,
            username=f"team{slot}",
            source_id=f"test_source_{slot}",
            source_name=f"Test source {slot}",
            source_format="selftest",
            fit_score=100.0,
            confidence="strong",
            mean_log2_loss=0.5,
            rank_power=rank_power(0.5, len(players)),
            primary_players=len(players),
            ranks=ranks,
            order=order,
        )
        for slot in range(1, TEAMS + 1)
        if slot != board.my_slot
    }


def opponent_selftest(players: list[Player]) -> list[str]:
    """Opponent source order stays personal-value-independent and bends toward balance."""
    fails: list[str] = []
    board = fresh_board()
    wire = seed_wire(players)
    # Put the lowest-projected player first on every external board. An opponent must take
    # him; my optimizer must not, demonstrating that candidate generation is separated too.
    external = sorted(players, key=lambda p: (p.points, p.player_id))
    opponents = synthetic_opponents(players, board, external)
    draft = Draft(players, wire, board, opponents=opponents)
    opponent_take = draft.choose_opponent(0, 1)
    my_take = draft.choose(1, board.my_slot)
    if opponent_take != external[0]:
        fails.append("opponent ignored the top player on its inferred source board")
    if my_take == external[0]:
        fails.append("my optimizer followed the opponent source board")

    # A filled WR group versus an empty TE group gives the best TE a soft 3x rank boost:
    # close source values bend toward TE, while a large enough source gap still wins.
    # TE probes these boundaries because OPPONENT_POSITION_TILT would shift them for RB.
    wrs = [p for p in players if p.position == "WR"]
    te = next(p for p in players if p.position == "TE")
    board = fresh_board()
    board.rosters[0] = wrs[:3]
    board.picks_left[0] -= 3

    def complete(prefix: list[Player]) -> list[Player]:
        ids = {p.player_id for p in prefix}
        return prefix + [p for p in players if p.player_id not in ids]

    close_order = complete([wrs[3], te])
    close = Draft(
        players,
        wire,
        board,
        opponents=synthetic_opponents(players, board, close_order),
    )
    if close.choose_opponent(0, 1) != te:
        fails.append("opponent did not prefer a close TE with the TE starter unfilled")

    value_order = complete([wrs[3], wrs[4], wrs[5], te])
    value = Draft(
        players,
        wire,
        board,
        opponents=synthetic_opponents(players, board, value_order),
    )
    if value.choose_opponent(0, 1) != wrs[3]:
        fails.append("opponent balance preference overrode too large a source-rank gap")

    adjustments = value.opponent_position_adjustments(1)
    if abs(adjustments["RB"] - OPPONENT_POSITION_TILT["RB"] * adjustments["TE"]) > 1e-12:
        fails.append("RB tilt missing from opponent position adjustments")

    # Once a roster reaches comfortable WR depth, another WR's adjusted source rank is
    # doubled against other positions. This is a preference, not a positional limit.
    board = fresh_board()
    board.rosters[0] = wrs[: OPPONENT_DEPTH_TARGETS["WR"]]
    board.picks_left[0] -= len(board.rosters[0])
    depth_order = complete([wrs[OPPONENT_DEPTH_TARGETS["WR"]], te])
    depth = Draft(
        players,
        wire,
        board,
        opponents=synthetic_opponents(players, board, depth_order),
    )
    if depth.choose_opponent(0, 1) != te:
        fails.append("opponent did not prefer close TE over excessive WR depth")

    start = OPPONENT_DEPTH_TARGETS["WR"]
    depth_value_order = complete(wrs[start : start + 6] + [te])
    depth_value = Draft(
        players,
        wire,
        board,
        opponents=synthetic_opponents(players, board, depth_value_order),
    )
    if depth_value.choose_opponent(0, 1) != wrs[start]:
        fails.append("opponent depth preference acted like a hard positional limit")
    if depth.opponent_depth_penalty(wrs[: start - 1], (), "WR") != 1.0:
        fails.append("opponent depth preference started before its target")
    if depth.opponent_depth_penalty(wrs[:start], (), "WR") != 2.0:
        fails.append("opponent depth preference did not start at its target")
    if depth.opponent_depth_penalty(wrs[: start + 1], (), "WR") != 4.0:
        fails.append("opponent depth preference did not compound with excess depth")

    for loss in (0.3, 1.5, 2.7):
        power = rank_power(loss, len(players))
        if abs(expected_log2_rank(power, len(players)) - loss) > 1e-6:
            fails.append(f"source-adherence calibration missed mean log2 loss {loss}")

    print(
        "  opponent strategies: provider order stays personal-value-independent, starter "
        "needs and excessive depth softly adjust it, and fitted rank noise reproduces "
        "source adherence",
        file=sys.stderr,
    )
    return fails


def planning_selftest(players: list[Player]) -> list[str]:
    """My policy uses lineup value, survival-gates live choices, and falls back safely."""
    fails: list[str] = []
    board = fresh_board()
    wire = seed_wire(players)
    opponents = synthetic_opponents(players, board)
    first_index = board.pick_nos.index(board.my_picks[0])
    state = Draft(players, wire, board, opponents=opponents)
    narrow = state.score_my_candidates(first_index)
    broad = state.score_my_candidates(first_index, per_pos=FIRST_PICK_PER_POS)
    if len(broad) <= len(narrow):
        fails.append("planning: the first-pick candidate pool did not broaden")

    # The live pool is built before an intervening deterministic opponent can erase a
    # plausible option. The survival floor, rather than that one path, removes long shots.
    contested = broad[0][2]
    external = [contested] + [p for p in players if p.player_id != contested.player_id]
    opponents = synthetic_opponents(players, board, external)
    deterministic = Draft(players, wire, board, opponents=opponents)
    deterministic.run()
    if deterministic.pick_of.get(contested.player_id) != 1:
        fails.append("planning: synthetic opponent did not take the contested candidate")
    broaden_first_pick(deterministic, players, board, wire, opponents)
    detail = deterministic.my_decisions[board.my_picks[0]]
    if contested.player_id not in {candidate.player_id for _, _, candidate in detail}:
        fails.append("planning: deterministic pre-pick path erased a live candidate")
    low = detail[-1][2]
    survival = {
        candidate.player_id: {
            board.my_picks[0]: 0.04 if candidate.player_id == low.player_id else 0.50
        }
        for _, _, candidate in detail
    }
    apply_survival_floor(deterministic, board, survival)
    kept = {
        candidate.player_id
        for _, _, candidate in deterministic.my_decisions[board.my_picks[0]]
    }
    if low.player_id in kept or contested.player_id not in kept:
        fails.append("planning: the 5% first-pick survival floor kept the wrong candidates")
    rolled = {
        "pick_no": board.my_picks[0],
        "take_id": contested.player_id,
        "stats": {
            candidate.player_id: {"ev": 1.0, "edge": 0.0, "se": 0.0}
            for _, _, candidate in deterministic.my_decisions[board.my_picks[0]]
        },
        "plans": {},
    }
    recommendation = my_next_picks(
        deterministic, board, rolled, survival, limit=1
    )[0]
    if recommendation["take_id"] != contested.player_id:
        fails.append("planning: deterministic availability vetoed the conditional take")
    if recommendation.get("deterministic_fallback_id") in (None, contested.player_id):
        fails.append("planning: conditional take did not retain a distinct legal fallback")
    later = board.my_picks[1]
    conditional = {contested.player_id: {board.my_picks[0]: 0.50, later: 0.02}}
    if conditional_survival(
        conditional, contested.player_id, board.my_picks[0], later
    ) >= 0.05:
        fails.append("planning: later survival was not conditioned on reaching the first pick")

    # Base 1's own plan is worth +60 over the ordinary policy, so it keeps the take. A base
    # pinned at zero instead makes candidate 2's +20 look like the only improvement, and
    # candidate 3's larger mean margin is all playout noise.
    rollout_baselines = {pid: [100.0] * 4 for pid in (1, 2, 3)}
    rollout_stats, rollout_take = rollout_decision(
        [1, 2, 3],
        {1: [160.0] * 4, 2: [120.0] * 4, 3: [300.0, 60.0, 300.0, 60.0]},
        rollout_baselines,
    )
    if rollout_stats[1]["edge"] != 60.0:
        fails.append("planning: the rollout base's own plan edge was not measured")
    if rollout_take != 1:
        fails.append("planning: a rollout candidate worth less than the base took the pick")
    # A smaller margin that is consistent across the shared draws does override it.
    if rollout_decision(
        [1, 2, 3],
        {1: [160.0] * 4, 2: [120.0] * 4, 3: [165.0, 166.0, 164.0, 165.0]},
        rollout_baselines,
    )[1] != 3:
        fails.append("planning: the rollout take did not beat the base's plan through the noise")

    # Even past an opponent's comfortable depth, my reported value_now is the raw
    # marginal expected-lineup value. The opponent heuristic must not leak into my policy.
    deep_board = fresh_board()
    wrs = [p for p in players if p.position == "WR"]
    deep_roster = wrs[: OPPONENT_DEPTH_TARGETS["WR"]]
    deep_board.rosters[deep_board.my_slot - 1] = deep_roster
    deep_board.picks_left[deep_board.my_slot - 1] -= len(deep_roster)
    deep_state = Draft(
        players,
        wire,
        deep_board,
        opponents=synthetic_opponents(players, deep_board),
    )
    deep_index = deep_board.pick_nos.index(deep_board.my_picks[0])
    deep_sorted = sorted_by_horizon(deep_roster)
    deep_base = team_value(deep_sorted, deep_state.wire)
    for now, _, candidate in deep_state.score_my_candidates(deep_index):
        raw_marginal = team_value(deep_sorted, deep_state.wire, candidate) - deep_base
        if abs(now - raw_marginal) > 1e-9:
            fails.append("planning: opponent depth heuristic changed my marginal value")
            break

    first_target = broad[-1][2]
    second_target = next(p for p in players if p.player_id != first_target.player_id)
    external = [second_target] + [p for p in players if p.player_id != second_target.player_id]
    opponents = synthetic_opponents(players, board, external)
    my_indices = [i for i, slot in enumerate(board.order) if slot == board.my_slot]
    planned = Draft(
        players,
        wire,
        board,
        opponents=opponents,
        targets={my_indices[0]: first_target, my_indices[1]: second_target},
    )
    planned.run(stop_before=my_indices[1] + 1)
    if planned.pick_of.get(first_target.player_id) != board.my_picks[0]:
        fails.append("planning: an available first target was not exercised")
    if planned.pick_of.get(second_target.player_id) != 1:
        fails.append("planning: the opponent did not take the later target first")
    if board.my_picks[1] not in planned.pick_of.values():
        fails.append("planning: an unavailable later target did not fall back")

    print(
        f"  planning: first-pick pool widened from {len(narrow)} to {len(broad)}; "
        "live candidates survived the deterministic prefix, the 5% floor removed long "
        "shots, the conditional take kept a distinct fallback, the rollout measured the "
        "base's own plan and only lost the take to a margin above the playout noise, my "
        "values stayed projection-only, and an unavailable later target fell back",
        file=sys.stderr,
    )
    return fails


def lineup_selftest(players: list[Player]) -> list[str]:
    """Expected lineup value is exact on small cases and monotone by construction."""
    fails: list[str] = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            fails.append(f"lineup: {message}")

    def player(player_id: int, name: str, position: str, yr1: int, yr23: int) -> Player:
        return Player(
            player_id=player_id,
            name=name,
            position=position,
            team="TEST",
            age=25.0,
            bye_week=None,
            is_rookie=False,
            points=yr1 + yr23,
            points_1yr=yr1,
            provider_adp=None,
        )

    qb1 = player(900001, "QB 1", "QB", 100, 100)
    qb2 = player(900002, "QB 2", "QB", 80, 80)
    # With one QB job: QB1 always supplies his unconditional projection, QB2 is used
    # when QB1 is unavailable, and the unique wire body is used only when both are out.
    one_qb = position_expected_value([qb1, qb2], 50.0, 1, "yr1")
    check(
        abs(one_qb - (100.0 + 0.08 * 80.0 + 0.08**2 * 50.0)) < 1e-9,
        f"one-QB expectation is {one_qb:.6f}",
    )
    two_qb = position_expected_value([qb1, qb2], 50.0, 2, "yr1")
    check(
        abs(two_qb - (180.0 + (1.0 - 0.92**2) * 50.0)) < 1e-9,
        f"two-QB expectation is {two_qb:.6f}",
    )

    zero_wire = {"QB": 100.0, "RB": 0.0, "WR": 0.0, "TE": 0.0}
    empty = expected_lineup_value([], zero_wire, "yr1")
    check(abs(empty - 100.0) < 1e-9, "one wire QB filled more than one lineup slot")

    # The closed-form weekly-reopt value against literal brute force: every availability
    # subset, weighted exactly, each solved by enumerating all legal compositions.
    def legal_compositions() -> list[dict[str, int]]:
        total = sum(STARTING_SLOTS.values())
        out = []
        for qb in range(DEDICATED_SLOTS["QB"], DEDICATED_SLOTS["QB"] + 2):
            for rb in range(DEDICATED_SLOTS["RB"], DEDICATED_SLOTS["RB"] + 4):
                for wr in range(DEDICATED_SLOTS["WR"], DEDICATED_SLOTS["WR"] + 4):
                    for te in range(DEDICATED_SLOTS["TE"], DEDICATED_SLOTS["TE"] + 4):
                        counts = {"QB": qb, "RB": rb, "WR": wr, "TE": te}
                        if sum(counts.values()) != total:
                            continue
                        qb_extra = qb - DEDICATED_SLOTS["QB"]
                        non_qb_extra = sum(
                            counts[p] - DEDICATED_SLOTS[p] for p in ("RB", "WR", "TE")
                        )
                        if qb_extra <= STARTING_SLOTS["SF"] and non_qb_extra <= (
                            STARTING_SLOTS["FLEX"] + STARTING_SLOTS["SF"] - qb_extra
                        ):
                            out.append(counts)
        return out

    def brute_weekly_value(roster: list[Player], wires: dict[str, float]) -> float:
        comps = legal_compositions()
        bodies = []
        for p in roster:
            if p.points_yr1 > 0:
                rate = 1.0 - UNAVAILABLE_RATE[p.position]
                bodies.append((p.position, p.points_yr1 / rate, rate))
        for pos in POSITIONS:
            if wires[pos] > 0:
                bodies.append((pos, wires[pos], 1.0))
        total = 0.0
        for mask in range(1 << len(bodies)):
            prob = 1.0
            avail: dict[str, list[float]] = {pos: [] for pos in POSITIONS}
            for i, (pos, w, rate) in enumerate(bodies):
                if mask >> i & 1:
                    prob *= rate
                    avail[pos].append(w)
                else:
                    prob *= 1.0 - rate
            for pos in POSITIONS:
                avail[pos].sort(reverse=True)
            total += prob * max(
                sum(sum(avail[pos][: c[pos]]) for pos in POSITIONS) for c in comps
            )
        return total

    brute_roster = [
        player(900030, "BF QB1", "QB", 300, 0),
        player(900031, "BF QB2", "QB", 280, 0),
        player(900032, "BF RB1", "RB", 240, 0),
        player(900033, "BF RB2", "RB", 220, 0),
        player(900034, "BF RB3", "RB", 190, 0),
        player(900035, "BF RB4", "RB", 180, 0),
        player(900036, "BF WR1", "WR", 230, 0),
        player(900037, "BF WR2", "WR", 200, 0),
        player(900038, "BF TE1", "TE", 190, 0),
        player(900039, "BF TE2", "TE", 185, 0),
    ]
    brute_wire = {"QB": 13.0, "RB": 45.0, "WR": 96.0, "TE": 92.0}
    brute = brute_weekly_value(brute_roster, brute_wire)
    closed = expected_lineup_value(brute_roster, brute_wire, "yr1")
    check(
        abs(brute - closed) < 1e-9,
        f"weekly-reopt closed form {closed:.6f} != brute force {brute:.6f}",
    )

    base = [
        player(900010, "Base QB", "QB", 327, 531),
        player(900011, "RB 1", "RB", 220, 400),
        player(900012, "RB 2", "RB", 200, 380),
        player(900013, "WR 1", "WR", 230, 440),
        player(900014, "WR 2", "WR", 210, 420),
        player(900015, "WR 3", "WR", 190, 400),
        player(900016, "TE 1", "TE", 180, 350),
    ]
    wire = {
        "yr1": {"QB": 13.0, "RB": 45.0, "WR": 92.0, "TE": 97.0},
        "yr23": {"QB": 191.0, "RB": 163.0, "WR": 263.0, "TE": 227.0},
    }
    low = player(900020, "Low QB", "QB", 325, 515)
    high = player(900021, "High QB", "QB", 325, 546)
    low_future = expected_lineup_value(base + [low], wire["yr23"], "yr23")
    high_future = expected_lineup_value(base + [high], wire["yr23"], "yr23")
    check(high_future > low_future, "a higher same-position future projection lost value")

    before = team_value(sorted_by_horizon(base), wire)
    after = team_value(sorted_by_horizon(base + [low]), wire)
    check(after >= before, "adding a player lowered roster value")
    improved = player(low.player_id, low.name, low.position, low.points_1yr, 532)
    low_threshold = player(low.player_id, low.name, low.position, low.points_1yr, 530)
    low_value = team_value(sorted_by_horizon(base + [low_threshold]), wire)
    improved_value = team_value(sorted_by_horizon(base + [improved]), wire)
    check(improved_value > low_value, "crossing the former stream threshold lowered value")

    # Regression for the live-board smell that motivated this model: Stafford's larger
    # years-2-3 projection must add more future-lineup value than Murray's to the roster
    # that currently owns Purdy. Their year-1 difference is deliberately excluded here.
    by_name = {p.name: p for p in players}
    current = [
        by_name[name]
        for name in (
            "Ja'Marr Chase",
            "Jeremiyah Love",
            "Nico Collins",
            "Brock Purdy",
            "TreVeyon Henderson",
            "Tucker Kraft",
            "Zay Flowers",
            "Josh Jacobs",
        )
    ]
    murray = by_name["Kyler Murray"]
    stafford = by_name["Matthew Stafford"]
    murray_future = expected_lineup_value(current + [murray], wire["yr23"], "yr23")
    stafford_future = expected_lineup_value(current + [stafford], wire["yr23"], "yr23")
    check(
        stafford_future > murray_future,
        "Stafford's higher years-2-3 projection valued below Murray's",
    )

    print(
        "  expected lineup: exact QB depth probabilities, unique wire capacity, and "
        "projection/addition monotonicity",
        file=sys.stderr,
    )
    return fails


def synthetic_draft(
    players: list[Player],
    made: int = 0,
    unrankable: dict[int, str] | None = None,
    trades: dict[int, int] | None = None,
) -> dict:
    """A `draft.json`-shaped board built offline, for the states the live file cannot reach.

    Today's live file has no traded picks and no selection outside the pool, so the two
    branches that handle them would go unexercised until the night they matter. Made picks
    take the pool in points order, which is a legal board and enough to check bookkeeping.
    `unrankable` maps a pick number to a position for a selection the pool does not carry;
    `trades` maps a pick number to the roster id that acquired it.
    """
    order = draft_order()
    slots = [
        {
            "draft_slot": s,
            "roster_id": 20 + s,  # deliberately not equal to the slot, as Sleeper's are not
            "user_id": None,
            "username": f"team{s}",
            "team_name": None,
            "is_mine": s == MY_SLOT,
        }
        for s in range(1, TEAMS + 1)
    ]
    roster_of_slot = {s["draft_slot"]: s["roster_id"] for s in slots}
    take = iter(players)
    picks: list[dict] = []
    for n, slot in enumerate(order, start=1):
        owner = (trades or {}).get(n, roster_of_slot[slot])
        pick = {
            "pick_no": n,
            "round": (n - 1) // TEAMS + 1,
            "pick_in_round": (n - 1) % TEAMS + 1,
            "draft_slot": slot,
            "roster_id": owner,
            "user_id": None,
            "username": None,
            "is_mine": owner == roster_of_slot[MY_SLOT],
            "status": "made" if n <= made else "pending",
            "sleeper_id": None,
            "name": None,
            "position": None,
            "team": None,
            "is_keeper": None,
        }
        if n <= made and (unrankable or {}).get(n):
            pick |= {
                "sleeper_id": f"not-in-pool-{n}",
                "name": f"Unrankable {n}",
                "position": unrankable[n],
                "team": "FA",
            }
        elif n <= made:
            p = next(take)
            pick |= {
                "sleeper_id": p.sleeper_id,
                "name": p.name,
                "position": p.position,
                "team": p.team,
            }
        picks.append(pick)

    pending = [p for p in picks if p["status"] == "pending"]
    mine = next((p for p in pending if p["is_mine"]), None)

    def summary(pick: dict | None) -> dict | None:
        if pick is None:
            return None
        return {
            "pick_no": pick["pick_no"],
            "round": pick["round"],
            "pick_in_round": pick["pick_in_round"],
            "draft_slot": pick["draft_slot"],
            "username": pick["username"],
            "slot": pick_label(pick["pick_no"]),
        }

    return {
        "source": "synthetic",
        "fetched_at": "2026-08-04T00:00:00+00:00",
        "draft_id": "synthetic",
        "league_name": "selftest",
        "status": "drafting",
        "format": {"type": "snake", "teams": TEAMS, "rounds": ROUNDS, "reversal_round": 3},
        "pick_count": TOTAL_PICKS,
        "picks_made": made,
        "picks_pending": TOTAL_PICKS - made,
        "on_the_clock": summary(pending[0] if pending else None),
        "me": {"username": "me", "draft_slot": MY_SLOT, "roster_id": roster_of_slot[MY_SLOT]},
        "my_next_pick": summary(mine),
        "slots": slots,
        "traded_picks": [{"round": (n - 1) // TEAMS + 1} for n in (trades or {})],
        "picks": picks,
    }


def board_selftest(players: list[Player]) -> list[str]:
    """The live-board loader, on states the real draft.json does not currently contain."""
    fails: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            fails.append(f"board: {msg}")

    # An untouched live board must be the static snake, or the live path and the offline
    # path disagree about the league before a single pick is made.
    board, problems = load_board(synthetic_draft(players), players, "synthetic")
    fresh = fresh_board()
    check(not problems, f"empty synthetic board complained: {problems}")
    check(board.order == fresh.order, "empty live board's order != the static snake")
    check(board.pick_nos == fresh.pick_nos, "empty live board's pick numbers != 1..290")
    check(board.my_picks == fresh.my_picks, "empty live board's picks for me != the snake's")
    check(board.picks_left == fresh.picks_left, f"picks left {board.picks_left} != all {ROUNDS}")
    check(not board.taken and board.picks_made == 0, "empty live board has players drafted")

    # Made picks leave the pool and land on the team that made them.
    board, problems = load_board(synthetic_draft(players, made=13), players, "synthetic")
    check(not problems, f"13-pick board complained: {problems}")
    check(board.picks_made == 13 and len(board.taken) == 13, "13 made picks did not come through")
    check(board.pick_nos[:1] == [14], f"simulation resumes at {board.pick_nos[:1]}, want 14")
    check(board.my_picks[:1] == [19], f"my next pick is {board.my_picks[:1]}, want 19 (2.09)")
    check(
        [p.name for p in board.rosters[MY_SLOT - 1]] == [players[1].name],
        "pick 1.02 did not land on my roster",
    )
    check(board.picks_left[MY_SLOT - 1] == ROUNDS - 1, "my remaining picks did not drop by one")
    check(sum(board.picks_left) == TOTAL_PICKS - 13, "remaining picks do not sum to the board")
    # Picks 10 and 11 are both slot 10 — the turn at the end of round 1 into round 2.
    check(len(board.rosters[9]) == 2, "slot 10 did not get both sides of its turn")

    # A traded pick is exercised by the roster that acquired it, not by its column.
    board, problems = load_board(
        synthetic_draft(players, trades={5: 20 + MY_SLOT}), players, "synthetic"
    )
    check(board.order[4] == MY_SLOT, f"traded pick 5 is exercised by slot {board.order[4]}")
    check(
        board.my_picks[:3] == [2, 5, 19],
        f"my picks start {board.my_picks[:3]}, want my own 1.02, the traded 5, then 2.09",
    )
    check(
        board.picks_left[MY_SLOT - 1] == ROUNDS + 1 and board.picks_left[4] == ROUNDS - 1,
        "a traded pick did not move between the two teams' pick counts",
    )
    check(board.owed_size(MY_SLOT) == ROUNDS + 1, "the acquiring team's roster size did not grow")

    # A selection the pool cannot value fills a spot and answers its mandatory position.
    board, problems = load_board(
        synthetic_draft(players, made=1, unrankable={1: "QB"}), players, "synthetic"
    )
    check(not board.taken, "an unrankable pick took a pool player off the board")
    check(len(board.off_pool[0]) == 1, "an unrankable pick was not held as a roster spot")
    check(board.picks_left[0] == ROUNDS - 1, "an unrankable pick did not cost its team a pick")
    wire = seed_wire(players)
    draft = Draft(players, wire, board, opponents=synthetic_opponents(players, board))
    owed = sum(DEDICATED_SLOTS.values()) - 1  # the QB is answered, six mandatory spots left
    with_qb = draft.candidates([], picks_left=owed, off=board.off_pool[0])
    without = draft.candidates([], picks_left=owed, off=[])
    check(
        {c.position for c in with_qb} == {"RB", "WR", "TE"},
        "an unrankable QB did not satisfy the QB requirement",
    )
    check(
        {c.position for c in without} == set(POSITIONS),
        "the same team without him should still owe a QB",
    )

    # Resuming: every made pick survives, every pending pick is played exactly once.
    board, _ = load_board(synthetic_draft(players, made=57), players, "synthetic")
    opponents = synthetic_opponents(players, board)
    partial = Draft(players, wire, board, opponents=opponents)
    partial.run(stop_before=5)
    check(
        set(partial.pick_of.values()) == set(board.pick_nos[:5]),
        "a short redraw did not stop immediately before its requested pick index",
    )
    draft = Draft(players, wire, board, opponents=opponents)
    draft.run()
    check(
        len(draft.taken) == len(board.taken) + len(board.order),
        "the resumed draft did not take one new player per pending pick",
    )
    check(
        set(draft.pick_of.values()) == set(board.pick_nos),
        "the simulated picks are not exactly the board's pending picks",
    )
    check(not (set(draft.pick_of) & board.taken), "a player already drafted was drafted again")
    for slot in range(1, TEAMS + 1):
        made = board.rosters[slot - 1]
        got = draft.rosters[slot - 1]
        check(got[: len(made)] == made, f"slot {slot} lost one of its made picks")
        check(
            len(got) == len(made) + board.picks_left[slot - 1],
            f"slot {slot} finished with {len(got)} players, not what it owns",
        )

    # A board that disagrees with this script must say so, not be quietly absorbed. Every
    # one of these is a way a wrong draft.json could otherwise produce a plausible board.
    def complains(raw: dict, about: str, pool: list[Player] = players) -> None:
        _, problems = load_board(raw, pool, "synthetic")
        check(bool(problems), f"a board with {about} was accepted without complaint")

    raw = synthetic_draft(players, made=2)
    raw["format"]["teams"] = 12
    complains(raw, "12 teams")
    raw = synthetic_draft(players, made=3)
    raw["picks"][2] |= {
        "sleeper_id": raw["picks"][0]["sleeper_id"],
        "name": raw["picks"][0]["name"],
    }
    complains(raw, "the same player drafted twice")
    raw = synthetic_draft(players, made=0)
    raw["picks"][4] |= {"roster_id": None, "draft_slot": None}
    complains(raw, "a pick nobody owns")
    raw = synthetic_draft(players, made=3)
    raw["picks_made"] = 5
    complains(raw, "a header contradicting its own picks")
    raw = synthetic_draft(players, made=0)
    raw["me"]["draft_slot"] = 7
    complains(raw, "a different draft slot for me")
    complains(
        synthetic_draft(players, made=6),
        "a pool carrying no sleeper ids to join on",
        [dataclasses.replace(p, sleeper_id=None) for p in players],
    )

    print(
        "  live board: static snake reproduced, made picks retained, traded pick and "
        "unvalued pick handled, 233 pending picks resumed, 6 bad boards rejected",
        file=sys.stderr,
    )
    return fails


def selftest(players: list[Player]) -> int:
    print("selftest:", file=sys.stderr)
    fails = (
        lineup_selftest(players)
        + opponent_selftest(players)
        + planning_selftest(players)
        + board_selftest(players)
    )
    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    verdict = f"{len(fails)} failure(s)" if fails else "all checks passed"
    print(f"selftest: {verdict}", file=sys.stderr)
    return 1 if fails else 0
