"""Offline checks for draft formats, trades, ownership, and malformed picks."""

from __future__ import annotations

import collections
import sys

from draft_board import (
    Board,
    DOCUMENTED_SLOT_2_PICK_IN_ROUND,
    pick_number_problems,
    pick_rows,
    resolve_me,
)


def selftest() -> int:
    """Check the board geometry offline, against cases a live fetch cannot reach.

    The live cross-check in ``pick_rows`` is the real guard, but it can only test rounds
    that have been drafted and trades that have been made. These cases cover the rest:
    every supported format's slot order, this league's own pick sequence as README.md
    states it, and the trade logic in both directions.
    """
    failures: list[str] = []
    checked = 0

    def check(label: str, got, want) -> None:
        nonlocal checked
        checked += 1
        if got != want:
            failures.append(f"{label}\n    got  {got}\n    want {want}")

    def fake(teams=4, rounds=3, reversal=3, type_="snake", order=True) -> dict:
        # Slot n -> roster 100+n, so a slot/roster mix-up cannot accidentally pass.
        return {
            "draft_id": "X", "season": "2026", "type": type_,
            "settings": {"teams": teams, "rounds": rounds, "reversal_round": reversal},
            "slot_to_roster_id": {str(n): 100 + n for n in range(1, teams + 1)},
            "draft_order": {f"u{n}": n for n in range(1, teams + 1)} if order else None,
        }

    def order_of(draft: dict) -> list[list[int]]:
        board = Board(draft)
        return [
            [board.locate(n)[2] for n in range((r - 1) * board.teams + 1, r * board.teams + 1)]
            for r in range(1, board.rounds + 1)
        ]

    # Slot order per format. A reversal round repeats the round before it, so from
    # there on the parity is inverted — which is the whole subtlety.
    check("snake, reversal at 3", order_of(fake(rounds=6)),
          [[1, 2, 3, 4], [4, 3, 2, 1], [4, 3, 2, 1], [1, 2, 3, 4], [4, 3, 2, 1], [1, 2, 3, 4]])
    check("snake, reversal at 2", order_of(fake(rounds=4, reversal=2)),
          [[1, 2, 3, 4], [1, 2, 3, 4], [4, 3, 2, 1], [1, 2, 3, 4]])
    check("snake, no reversal", order_of(fake(rounds=4, reversal=0)),
          [[1, 2, 3, 4], [4, 3, 2, 1], [1, 2, 3, 4], [4, 3, 2, 1]])
    check("linear", order_of(fake(rounds=2, type_="linear")), [[1, 2, 3, 4], [1, 2, 3, 4]])
    check("auction has no board", Board(fake(type_="auction")).problems(),
          ["draft type 'auction' has no pick order to derive"])
    check("0 teams is refused", bool(Board(fake(teams=0)).problems()), True)

    # This league, against the sequence README.md documents for slot 2.
    real = Board({"type": "snake", "settings": {"teams": 10, "rounds": 29, "reversal_round": 3}})
    mine = {r: p for n in range(1, 291) for r, p, s in [real.locate(n)] if s == 2}
    check(
        "slot 2 matches the documented sequence",
        {r: mine.get(r) for r in DOCUMENTED_SLOT_2_PICK_IN_ROUND},
        DOCUMENTED_SLOT_2_PICK_IN_ROUND,
    )
    check("slot 2 picks once per round", len(mine), 29)
    check("every slot picks once per round",
          sorted(collections.Counter(real.locate(n)[2] for n in range(1, 291)).values()),
          [29] * 10)

    # Traded picks: slot 1's round-2 pick, originally roster 101, is now roster 103's.
    traded = [{"season": "2026", "round": 2, "roster_id": 101, "owner_id": 103}]
    board = Board(fake(), traded)
    check("traded pick goes to the acquirer", board.owner_roster(2, 1), 103)
    check("the same slot's other rounds are untouched",
          (board.owner_roster(1, 1), board.owner_roster(3, 1)), (101, 101))
    check("another team's round 2 is untouched", board.owner_roster(2, 2), 102)
    check("another season's trade is ignored",
          Board(fake(), [{**traded[0], "season": "2027"}]).owner_roster(2, 1), 101)
    check("a malformed trade is skipped", Board(fake(), [{"round": None}]).traded, {})

    users = {f"u{n}": {"username": f"name{n}", "team_name": None} for n in range(1, 5)}
    rows, _ = pick_rows(board, [], users, "u1")
    # Round 2 is reversed, so slot 1 picks last in it: pick 8 of 4 x 3.
    check("a pending traded pick is attributed to the acquirer",
          {k: rows[7][k] for k in ("draft_slot", "roster_id", "user_id", "is_mine")},
          {"draft_slot": 1, "roster_id": 103, "user_id": "u3", "is_mine": False})
    check("an untraded pick is still mine", rows[0]["is_mine"], True)

    # Once that traded pick is made, Sleeper's report must agree with the derivation...
    made = [{"pick_no": 8, "draft_slot": 1, "roster_id": 103, "picked_by": "u3",
             "player_id": "999", "is_keeper": None,
             "metadata": {"first_name": "A", "last_name": "B", "position": "WR", "team": "SF"}}]
    rows, checks = pick_rows(board, made, users, "u1")
    check("a made traded pick agrees with the derivation",
          (checks["slot_and_roster_agree"], checks["mismatches"]), (1, []))
    check("a made pick carries its player",
          {k: rows[7][k] for k in ("status", "sleeper_id", "name", "is_keeper")},
          {"status": "made", "sleeper_id": "999", "name": "A B", "is_keeper": False})
    # ...and the negative control: the same pick with the trade *not* applied is exactly
    # what the live check has to catch, or it is not checking anything.
    _, missed = pick_rows(Board(fake()), made, users, "u1")
    check("an unapplied trade is caught",
          (missed["slot_and_roster_agree"], len(missed["mismatches"])), (0, 1))

    # picked_by is empty when a pick was made for the team rather than by them.
    rows, _ = pick_rows(Board(fake()), [{"pick_no": 1, "draft_slot": 1, "roster_id": 101,
                                         "picked_by": "", "player_id": 42, "metadata": {}}],
                        users, "u1")
    check("an autopick still finds its owner", (rows[0]["user_id"], rows[0]["is_mine"]),
          ("u1", True))
    check("a player id is stringified", rows[0]["sleeper_id"], "42")

    # An unpublished draft order must leave picks unowned, not owned by everyone.
    rows, _ = pick_rows(Board(fake(order=False)), [], users, None)
    check("no draft order leaves owners null",
          ({row["user_id"] for row in rows}, {row["is_mine"] for row in rows}),
          ({None}, {False}))

    # Pick numbers that would corrupt an array indexed by pick_no — versus a gap, which
    # is legitimate in a keeper draft and must not be fatal.
    board = Board(fake())
    check("a pick_no off the board is fatal",
          bool(pick_number_problems([{"pick_no": 13}], board)[0]), True)
    check("a duplicate pick_no is fatal",
          bool(pick_number_problems([{"pick_no": 1}, {"pick_no": 1}], board)[0]), True)
    check("a gap is reported, not fatal",
          [bool(part) for part in pick_number_problems([{"pick_no": 1}, {"pick_no": 3}], board)],
          [False, True])
    check("a clean prefix is silent",
          pick_number_problems([{"pick_no": 1}, {"pick_no": 2}], board), ([], []))

    check("me resolves case-insensitively", resolve_me("NAME2", board, users),
          {"username": "name2", "user_id": "u2", "draft_slot": 2, "roster_id": 102})
    check("an unknown me resolves to nothing", resolve_me("nobody", board, users)["user_id"], None)

    for failure in failures:
        print(f"  MISMATCH {failure}", file=sys.stderr)
    print(
        f"selftest {'ok' if not failures else 'FAILED'}: {checked - len(failures)}/{checked} "
        "board-geometry checks passed (formats, this league's order, trades, autopicks)",
        file=sys.stderr,
    )
    return 1 if failures else 0
