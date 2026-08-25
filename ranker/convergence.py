"""Solve wire levels from the league shape produced by a simulated draft."""

from __future__ import annotations

import sys

from .board import Board
from .league import MAX_ITERS, POSITIONS
from .opponents import OpponentStrategy
from .pool import Player, by_position
from .simulation import Draft
from .value import HORIZONS, seed_wire, wire_replacement


def converge(
    players: list[Player],
    board: Board,
    report: bool,
    opponents: dict[int, OpponentStrategy],
) -> tuple[dict[str, dict[str, float]], Draft, dict]:
    """Fixed point: wire value -> draft -> wire value.

    All levels are per horizon (value.HORIZONS): each iteration's draft is measured
    twice — once on year-1 points, once on years-2-3 points — and both sets of levels
    feed the next iteration's valuation.

    The map is piecewise-constant: the wire level jumps between adjacent players in the
    pool instead of moving continuously. That means gradient-style damping cannot settle
    it — it just orbits the discontinuity — while plain undamped iteration is eventually
    periodic.

    So iterate undamped, watch for a repeated state, and average the wire levels over
    the cycle once one closes. Each iteration's draft is a deterministic function of the
    previous one's wire levels, so the state key is the wire levels alone. A cycle of
    length 1 is an exact fixed point; a longer cycle means the draft genuinely
    alternates between neighbouring league shapes and the average across it is the
    honest answer. The cycle is reported rather than hidden.

    Every draft here starts from `board`, so on a live board the fixed point is over the
    rosters this league will actually finish with. The measurement is still league-wide:
    the wire is whatever the whole league leaves undrafted.
    """
    pos = by_position(players)
    stream = seed_wire(players)  # no draft to read a wire off yet
    trace = [
        {
            "iteration": 0,
            "source": "slot_assignment",
            "wire": {h: dict(stream[h]) for h in HORIZONS},
        }
    ]
    seen: dict[tuple[float, ...], int] = {}
    wire_observations: list[dict[str, dict[str, float]]] = []
    cycle_start: int | None = None

    for it in range(1, MAX_ITERS + 1):
        draft = Draft(players, stream, board, opponents=opponents)
        draft.run()
        wire = wire_replacement(draft.taken, pos)
        wire_observations.append(wire)
        key = tuple(wire[h][k] for h in HORIZONS for k in POSITIONS)
        trace.append(
            {
                "iteration": it,
                "source": "draft_simulation",
                "observed_wire": {
                    h: {k: round(v, 1) for k, v in wire[h].items()} for h in HORIZONS
                },
            }
        )
        if report:
            for h in HORIZONS:
                print(
                    f"  iter {it} {h}: wire="
                    + ", ".join(f"{k} {wire[h][k]:.0f}" for k in POSITIONS),
                    file=sys.stderr,
                )
        if key in seen:
            cycle_start = seen[key]
            break
        seen[key] = it
        stream = wire

    assert cycle_start is not None, f"no cycle within {MAX_ITERS} iterations"
    wire_cycle = wire_observations[cycle_start - 1 : -1] or wire_observations[cycle_start - 1 :]
    stream = {
        h: {k: sum(o[h][k] for o in wire_cycle) / len(wire_cycle) for k in POSITIONS}
        for h in HORIZONS
    }
    if report:
        print(
            f"  cycle of length {len(wire_cycle)} closed at iteration {cycle_start}; "
            "averaging wire levels across it",
            file=sys.stderr,
        )

    # Final deterministic draft at the settled levels, so sim_pick, my_decisions and the
    # reported wire levels describe one and the same draft.
    draft = Draft(players, stream, board, opponents=opponents)
    draft.run()
    history = {
        "method": "undamped iteration to a limit cycle, averaged over the cycle",
        "cycle_length": len(wire_cycle),
        "cycle_first_seen_at_iteration": cycle_start,
        "iterations_run": len(wire_observations),
        # Spread of the cycle the levels were averaged over. A wide band means the league
        # shape genuinely wobbles between neighbouring configurations rather than settling.
        "cycle_wire_range": {
            h: {
                k: [
                    round(min(o[h][k] for o in wire_cycle), 1),
                    round(max(o[h][k] for o in wire_cycle), 1),
                ]
                for k in POSITIONS
            }
            for h in HORIZONS
        },
        "trace": trace,
    }
    return stream, draft, history
