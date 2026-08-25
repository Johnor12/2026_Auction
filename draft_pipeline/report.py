"""Validation and human-readable diagnostics for a built draft board."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import paths
from draft_board import Board, DOCUMENTED_SLOT_2_PICK_IN_ROUND, round_pick


def documented_slot_check(rows: list[dict], draft_slot: int | None) -> tuple[list[str], list[str]]:
    """Compare a slot's pre-trade geometry with the sequence README.md documents."""
    slot_picks = {
        row["round"]: row["pick_in_round"]
        for row in rows
        if row["draft_slot"] == draft_slot
    }
    agree, disagree = [], []
    for round_no, expected in sorted(DOCUMENTED_SLOT_2_PICK_IN_ROUND.items()):
        got = slot_picks.get(round_no)
        line = f"{round_pick(round_no, expected)} expected, got " + (
            round_pick(round_no, got) if got else "no pick"
        )
        (agree if got == expected else disagree).append(line)
    return agree, disagree


def pool_join(rows: list[dict], pool_path: Path) -> dict | None:
    """How the made picks land in pool.json — the join this file exists to enable."""
    if not pool_path.is_file():
        return None
    try:
        with pool_path.open(encoding="utf-8") as handle:
            players = json.load(handle).get("players") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    by_id = {str(p["sleeper_id"]): p for p in players if p.get("sleeper_id")}
    made = [row for row in rows if row["status"] == "made" and row["sleeper_id"]]
    hits = [(row, by_id[row["sleeper_id"]]) for row in made if row["sleeper_id"] in by_id]
    return {
        "pool_size": len(players),
        "pool_with_id": len(by_id),
        "made": len(made),
        "matched": len(hits),
        "outside_pool": [row for row in made if row["sleeper_id"] not in by_id],
        "top_50_gone": sum(1 for _, player in hits if player.get("rank", 0) <= 50),
        # A position mismatch on a joined id would mean match_sleeper.py joined the
        # wrong player — the one failure mode a name-based join can hide.
        "disagreements": [
            (row, player) for row, player in hits if row["position"] != player["position"]
        ],
    }


def report(document: dict, rows: list[dict], board: Board, pool_path: Path) -> None:
    out = sys.stderr
    fmt = document["format"]

    print(
        f"\ndraft {document['draft_id']} — {document.get('league_name')} "
        f"{document.get('season')}, status {document['status']}",
        file=out,
    )
    print(
        f"  {fmt['type']}, {fmt['teams']} teams x {fmt['rounds']} rounds = "
        f"{document['pick_count']} picks"
        + (f", reversal at round {fmt['reversal_round']}" if fmt["reversal_round"] else "")
        + f"; last pick {document['last_picked_at']}",
        file=out,
    )

    print("\norder", file=out)
    for round_no in range(1, min(board.rounds, 6) + 1):
        first = board.locate((round_no - 1) * board.teams + 1)[2]
        last = board.locate(round_no * board.teams)[2]
        print(
            f"  round {round_no:>2}  slots {first} -> {last}  "
            f"({'reversed' if board.is_reversed(round_no) else 'forward'})",
            file=out,
        )
    if board.rounds > 6:
        print(f"  ... through round {board.rounds}", file=out)

    check = document["board_derivation"]
    print("\nderivation vs what Sleeper reported", file=out)
    print(
        f"  slot and roster agree on {check['slot_and_roster_agree']}/"
        f"{check['checked_against_made_picks']} made picks "
        f"(rounds exercised: {check['rounds_exercised'] or 'none yet'})",
        file=out,
    )
    for bad in check["mismatches"][:10]:
        print(
            f"  ^ pick {bad['pick_no']}: reported {bad['reported']} vs derived {bad['derived']}",
            file=out,
        )

    me = document["me"]
    agree, disagree = documented_slot_check(rows, me["draft_slot"])
    print(
        f"\nmy picks — {me['username']}, slot {me['draft_slot']}, roster {me['roster_id']}",
        file=out,
    )
    print(
        f"  documented slot geometry matches: {len(agree)}/{len(agree) + len(disagree)}",
        file=out,
    )
    for line in disagree:
        print(f"  ^ MISMATCH {line}", file=out)
    mine = [row for row in rows if row["is_mine"]]
    print(
        "  all: "
        + ", ".join(round_pick(row["round"], row["pick_in_round"]) for row in mine[:10])
        + (f", ... ({len(mine)} total)" if len(mine) > 10 else ""),
        file=out,
    )

    made = [row for row in rows if row["status"] == "made"]
    print(f"\npicks made ({len(made)})", file=out)
    for row in made[-12:]:
        print(
            f"  {round_pick(row['round'], row['pick_in_round']):>6} "
            f"#{row['pick_no']:<4} {(row['username'] or row['user_id'] or '?'):<16} "
            f"{(row['name'] or '?'):<24} {row['position'] or '?':<3} {row['team'] or '?':<4}"
            f"{'  <- mine' if row['is_mine'] else ''}",
            file=out,
        )
    if made:
        by_position = collections.Counter(row["position"] for row in made)
        print(
            "  by position: "
            + ", ".join(f"{pos} {n}" for pos, n in by_position.most_common()),
            file=out,
        )
    if document["on_the_clock"]:
        clock = document["on_the_clock"]
        print(
            f"  on the clock: #{clock['pick_no']} ({clock['slot']}) "
            f"{clock['username'] or clock['user_id']}",
            file=out,
        )
    if document["my_next_pick"]:
        mine_next = document["my_next_pick"]
        print(
            f"  my next: #{mine_next['pick_no']} ({mine_next['slot']}), "
            f"{mine_next.get('picks_away')} picks away",
            file=out,
        )

    print("\nintegrity", file=out)
    numbers = [row["pick_no"] for row in rows]
    print(
        f"  pick_no is 1..{len(rows)} gap-free: {numbers == list(range(1, len(rows) + 1))}",
        file=out,
    )
    per_slot = collections.Counter(row["draft_slot"] for row in rows)
    print(
        f"  every slot appears {board.rounds}x: "
        f"{set(per_slot.values()) == {board.rounds} and len(per_slot) == board.teams}",
        file=out,
    )
    drafted = [row["sleeper_id"] for row in made if row["sleeper_id"]]
    repeats = [i for i, n in collections.Counter(drafted).items() if n > 1]
    print(
        f"  no player drafted twice: {not repeats}"
        + (f" <- {repeats}" if repeats else "")
        + f"; every made pick has a player: {len(drafted) == len(made)}",
        file=out,
    )
    unowned = [row["pick_no"] for row in rows if row["user_id"] is None]
    print(
        f"  every pick has an owner: {not unowned}"
        + (f" <- {len(unowned)} without one, first {unowned[:5]}" if unowned else ""),
        file=out,
    )

    join = pool_join(rows, pool_path)
    print(f"\npool join ({paths.display(pool_path)})", file=out)
    if join is None:
        print("  pool.json not readable — skipped", file=out)
    else:
        print(
            f"  {join['matched']}/{join['made']} made picks are in the pool "
            f"({join['pool_with_id']}/{join['pool_size']} pool players carry a sleeper_id)",
            file=out,
        )
        print(f"  pool top 50 already gone: {join['top_50_gone']}", file=out)
        for row in join["outside_pool"][:15]:
            print(
                f"  ^ outside the pool: #{row['pick_no']} {row['name']} "
                f"{row['position']} {row['team']} (sleeper_id {row['sleeper_id']})",
                file=out,
            )
        for row, player in join["disagreements"][:10]:
            print(
                f"  ^ position disagrees on sleeper_id {row['sleeper_id']}: "
                f"sleeper {row['name']} {row['position']} vs pool {player['name']} "
                f"{player['position']}",
                file=out,
            )
    print(file=out)
