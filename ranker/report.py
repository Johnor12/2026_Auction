"""Human-readable ranker diagnostics written to stderr."""

from __future__ import annotations

import sys

from .board import Board
from .league import TEAMS, TOTAL_PICKS, pick_label
from .output import team_names
from .rankings import my_next_picks
from .simulation import Draft


def report_board(board: Board) -> None:
    """The starting state, on stderr: what is gone, who holds it, what is still coming."""
    if not board.live:
        print(
            f"board: no live draft; all {len(board.order)} picks simulated from the static "
            "snake",
            file=sys.stderr,
        )
        return
    live = board.live
    print(
        f"board: {live['picks_made']}/{TOTAL_PICKS} picks made, {live['picks_pending']} "
        f"pending ({live['matched_to_pool']} made picks joined to the pool, "
        f"{len(live['off_pool_picks'])} outside it); {live['status']}, "
        f"fetched {live['fetched_at']}",
        file=sys.stderr,
    )
    if live["traded_picks"]:
        print(
            f"  {live['traded_picks']} traded pick(s), applied by the draft pipeline",
            file=sys.stderr,
        )
    for o in live["off_pool_picks"]:
        print(
            f"  {o['pick']} slot {o['slot']}: {o['name']} ({o['position']}) is not in the "
            "pool - held as a filled roster spot with no value",
            file=sys.stderr,
        )
    names = team_names(board)
    for slot in range(1, TEAMS + 1):
        made, off = board.rosters[slot - 1], board.off_pool[slot - 1]
        if not made and not off:
            continue
        who = names.get(slot) or f"slot {slot}"
        held = [f"{p.name} ({p.position})" for p in made]
        held += [f"{o['name']} ({o['position']}, unvalued)" for o in off]
        print(
            f"  {'*' if slot == board.my_slot else ' '} slot {slot:>2} {who[:20]:<20}"
            f" {board.picks_left[slot - 1]:>2} picks left: " + ", ".join(held),
            file=sys.stderr,
        )
    clock = live.get("on_the_clock") or {}
    mine = live.get("next_pick_of_mine") or {}
    print(
        f"  on the clock {clock.get('slot')} ({clock.get('username')}); my next "
        f"{mine.get('slot')} ({mine.get('picks_away')} away), "
        f"{len(board.my_picks)} of my picks remain",
        file=sys.stderr,
    )


def report_summary(
    rows: list[dict],
    draft: Draft,
    board: Board,
    rollout: dict | None = None,
    survival: dict[int, dict[int, float]] | None = None,
) -> None:
    """Top of the board and the recommendation, on stderr."""
    top = rows[:12]
    if top:
        width = max(len(r["name"]) for r in top)
        print(
            "\ntop of the board" + (f", {len(rows)} undrafted:" if board.live else ":"),
            file=sys.stderr,
        )
    for r in top:
        print(
            f"  {r['rank']:>3}. {r['name']:<{width}}  {r['position']}"
            f"{r['positional_rank']:<3} gain {r['lineup_gain']:>6.1f}"
            f"  pts {r['points_3yr']:>5}  sim {r['sim_pick_label'] or '--':>6}"
            f"  provider adp {r['provider_adp'] or float('nan'):>5}",
            file=sys.stderr,
        )
    recs = my_next_picks(draft, board, rollout)
    if recs:
        print("\nmy next picks (four-pick plan; two-pick scores shown):", file=sys.stderr)
        for rec in recs:
            cands = ", ".join(
                f"{c['name']} {c['score']:.0f} ({c['value_now']:.0f}+{c['next_pick_ev']:.0f})"
                for c in rec["candidates"]
            )
            print(f"  {rec['pick']}: take {rec['take']}  |  {cands}", file=sys.stderr)
        first = recs[0]
        if "rollout_ev" in first["candidates"][0]:
            cands = ", ".join(
                f"{c['name']} edge {c['rollout_edge']:+.0f}±{c['rollout_se']:.0f}"
                for c in first["candidates"]
            )
            print(f"  {first['pick']} full-horizon rollout: {cands}", file=sys.stderr)
            chosen = next(c for c in first["candidates"] if c["player_id"] == first["take_id"])
            plan = " -> ".join(
                f"{target['pick']} {target['name']}" for target in chosen.get("four_pick_plan", [])
            )
            if plan:
                print(f"  selected four-pick plan: {plan}", file=sys.stderr)
        if survival:
            # Last of my picks each candidate survives to at even odds if I keep passing
            # on him ('--' = likely gone before my current pick comes back around).
            parts = []
            for _, _, c in draft.my_decisions.get(board.my_picks[0], []):
                sv = survival.get(c.player_id)
                if not sv:
                    continue
                last = None
                for pk in board.my_picks:
                    if sv[pk] < 0.5:
                        break
                    last = pk
                parts.append(f"{c.name} {pick_label(last) if last else '--'}")
            print(
                f"  {first['pick']} last even-odds pick if I pass: " + ", ".join(parts),
                file=sys.stderr,
            )
