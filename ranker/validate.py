"""Every-run invariants over the board, the simulation and the emitted rows.

These run on every invocation and land in `validation.problems`; a non-empty list is a
non-zero exit. `--selftest` (selftest.py) covers the states a real draft.json cannot
currently reach.
"""

from __future__ import annotations

from .board import Board
from .league import (
    DEDICATED_SLOTS,
    MAX_ITERS,
    NON_TAXI_SLOTS,
    POSITIONS,
    ROUNDS,
    STARTING_SLOTS,
    TEAMS,
    TOTAL_PICKS,
    draft_order,
    pick_label,
    picks_for_slot,
)
from .pool import Player
from .simulation import Draft
from .value import HORIZONS, horizon_points, pos_by_horizon, starting_positions


def validate(
    rows: list[dict],
    players: list[Player],
    stream: dict[str, dict[str, float]],
    draft: Draft,
    board: Board,
    history: dict,
) -> list[str]:
    problems: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            problems.append(msg)

    # The static snake is the yardstick even on a live board: check it against the README
    # first, then check that what the board says is still coming agrees with it.
    readme = ["1.02", "2.09", "3.09", "4.02", "5.09", "6.02", "28.02", "29.09"]
    full = picks_for_slot(board.my_slot, draft_order())
    labels = [pick_label(p) for p in full]
    check(labels[:6] == readme[:6], f"draft order head {labels[:6]} != README {readme[:6]}")
    check(labels[-2:] == readme[-2:], f"draft order tail {labels[-2:]} != README {readme[-2:]}")
    check(len(full) == ROUNDS, f"{len(full)} picks for slot {board.my_slot}, want {ROUNDS}")
    check(
        len(board.order) == len(board.pick_nos),
        f"board has {len(board.order)} owners for {len(board.pick_nos)} pending picks",
    )
    accounted = board.picks_made + len(board.order)
    check(accounted == TOTAL_PICKS, f"board accounts for {accounted} picks, want {TOTAL_PICKS}")
    check(
        sum(board.owed_size(s) for s in range(1, TEAMS + 1)) == TOTAL_PICKS,
        "the picks each team owns do not sum to the board",
    )

    if board.live:
        first = board.pick_nos[0] if board.pick_nos else TOTAL_PICKS + 1
        check(
            board.pick_nos == list(range(first, TOTAL_PICKS + 1)),
            "pending picks are not the contiguous tail of the board",
        )
        clock = (board.live.get("on_the_clock") or {}).get("pick_no")
        check(
            clock is None or clock == first,
            f"on the clock is pick {clock}, board resumes at {first}",
        )
        mine = (board.live.get("next_pick_of_mine") or {}).get("pick_no")
        check(
            mine is None or board.my_picks[:1] == [mine],
            f"draft.json says my next pick is {mine}, board says {board.my_picks[:1]}",
        )
        if not board.live.get("traded_picks"):
            want = [n for n in full if n >= first]
            check(
                board.my_picks == want,
                f"my {len(board.my_picks)} remaining picks are not the snake's tail "
                f"({len(want)} picks) even though no picks were traded",
            )

    want_taken = sum(len(r) for r in board.rosters) + len(board.order)
    check(
        len(draft.taken) == want_taken,
        f"{len(draft.taken)} unique pool players drafted, want {want_taken}",
    )
    for i, roster in enumerate(draft.rosters, start=1):
        made = board.rosters[i - 1]
        off = board.off_pool[i - 1]
        # Made picks are facts: they must come through the simulation untouched, in order.
        check(
            roster[: len(made)] == made,
            f"slot {i} lost or reordered one of its {len(made)} made picks",
        )
        want = len(made) + board.picks_left[i - 1]
        check(len(roster) == want, f"slot {i} ends with {len(roster)} players, want {want}")
        # The real constraint is the 25 non-taxi spots; which specific picks land on taxi is
        # not fixed, and a mandatory pick may have to be a non-rookie when no rookie at a
        # required position is left. Made picks are facts and may already exceed the cap
        # (owners drop players after the draft), so only the simulation's own additions
        # must fit the room left. Only players the pool carries are counted: `draft.json`
        # does not say whether a selection outside the pool is a rookie, so counting those
        # would assert something unknown. `Draft.candidates` still counts them as veterans
        # for its cap.
        made_non_rookies = sum(1 for p in made if not p.is_rookie)
        room = max(0, NON_TAXI_SLOTS - made_non_rookies)
        added_non_rookies = sum(1 for p in roster[len(made) :] if not p.is_rookie)
        check(
            added_non_rookies <= room,
            f"slot {i} was dealt {added_non_rookies} non-rookies with only {room} "
            f"non-taxi spots left",
        )
        # Whether 10 slots can be filled depends only on the roster's positions, not on
        # which horizon orders the pecking, so one horizon suffices here.
        starters = starting_positions(roster, "yr1")
        check(
            len(starters) == sum(STARTING_SLOTS.values()),
            f"slot {i} cannot field a full lineup ({len(starters)}/10)",
        )
        for pos, need in DEDICATED_SLOTS.items():
            have = sum(1 for p in roster if p.position == pos)
            have += sum(1 for o in off if o.get("position") == pos)
            check(have >= need, f"slot {i} has {have} {pos}, needs {need}")
    gains = [r["lineup_gain"] for r in rows]
    check(gains == sorted(gains, reverse=True), "rows are not sorted by lineup gain descending")
    source_ids = {strategy.source_id for strategy in draft.opponents.values()}
    check(len(source_ids) >= 2, f"opponents use only {len(source_ids)} distinct source board(s)")
    check(
        any(row["opponent_rank_delta"] != 0 for row in rows),
        "opponent consensus order does not diverge from my board order",
    )
    want_rows = len(players) - len(board.taken)
    check(
        len(rows) == want_rows,
        f"{len(rows)} rows for {want_rows} undrafted players in a {len(players)}-player pool",
    )
    check(
        not (board.taken & {r["player_id"] for r in rows}),
        "a player already drafted in draft.json is on the emitted board",
    )
    # The reported wire level is a mean over the limit cycle, so the invariant that
    # actually holds is that it lies inside the range the cycle spanned — not that it
    # coincides with any single draft's level, which a long cycle can genuinely straddle.
    pos_h = pos_by_horizon(players)
    for h in HORIZONS:
        for k in POSITIONS:
            lo, hi = history["cycle_wire_range"][h][k]
            check(
                lo - 1e-6 <= stream[h][k] <= hi + 1e-6,
                f"{h} {k} wire {stream[h][k]:.1f} outside its cycle range [{lo}, {hi}]",
            )
            check(
                horizon_points(pos_h[h][k][-1], h)
                <= stream[h][k]
                <= horizon_points(pos_h[h][k][0], h),
                f"{h} {k} wire {stream[h][k]:.1f} outside the {k} pool range",
            )
    check(
        history["iterations_run"] < MAX_ITERS,
        f"no limit cycle found within {MAX_ITERS} iterations",
    )
    return problems
