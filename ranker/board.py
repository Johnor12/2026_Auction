"""The draft's starting state: draft.json as a Board, or a fresh one.

By default the draft does not start empty: `draft.json` (written by
`draft_pipeline/fetch_draft.py`) is the live board, and it is the simulation's initial
state. Made picks are already on their teams' rosters and off the pool; the simulated
draft plays out only the picks that are still pending, in the order that file says they
will happen — which is where traded picks enter, since a pick's owner there is the roster
that will actually use it, not the slot that originally held it. `rankings.json` then
covers the undrafted players only, because a drafted player is not a decision any more.

Nothing about the method changes on a live board. The fixed point still measures the
wire over whole final rosters — made picks plus simulated ones — so wire levels are
levels for this league, not for the remainder of it. With no picks made the board is
the static snake and the output is identical to `--no-draft`; `--selftest` checks
exactly that.

Two facts about a live board this cannot value:

  * A pick can land on a player the pool does not carry — a kicker, an IDP, anyone past
    the pool's 350-player cut. There is no projection to price him with, so he is held
    as an `off_pool` roster entry: he fills a spot (so the team owes one fewer pick) and
    satisfies a mandatory position (a rostered QB means the team no longer needs one),
    but he never starts and is never worth anything. That is the right treatment for a
    kicker and a slight understatement for a real player just past the cut.
  * Made picks are facts, not decisions, so they are never re-valued. If a team reached,
    the board takes that as given and prices what is left.
"""

from __future__ import annotations

from dataclasses import dataclass

from .league import MY_SLOT, ROUNDS, TEAMS, TOTAL_PICKS, draft_order, pick_label, picks_for_slot
from .pool import Player


@dataclass(slots=True)
class Board:
    """What is already gone and what is left to happen.

    One type covers both cases so there is a single simulation path. `fresh_board()` is a
    board with nothing drafted and the static snake order; `load_board()` builds one from
    `draft.json`. The simulation only ever plays `order`, so an untouched board plays all
    168 picks and a live one plays the pending tail.

    Teams are indexed by draft slot (1..12, minus one), because that is what `MY_SLOT` and
    the snake are expressed in. `order` holds the slot of the team that *receives*
    each remaining pick, which is the acquirer for a traded pick, so a trade shows up as a
    slot appearing at a board position that is not its own column.
    """

    order: list[int]  # team slot receiving each remaining pick, in pick order
    pick_nos: list[int]  # overall pick number of each entry in `order`
    rosters: list[list[Player]]  # pool players already drafted, by slot - 1
    off_pool: list[list[dict]]  # drafted players the pool does not carry, by slot - 1
    picks_left: list[int]  # remaining picks per team, by slot - 1
    my_slot: int
    my_picks: list[int]  # my remaining picks, as overall pick numbers
    live: dict | None = None  # draft.json's header and join summary, when there is one

    @property
    def taken(self) -> set[int]:
        return {p.player_id for roster in self.rosters for p in roster}

    @property
    def picks_made(self) -> int:
        return sum(len(r) for r in self.rosters) + sum(len(o) for o in self.off_pool)

    def available(self, players: list[Player]) -> list[Player]:
        taken = self.taken
        return [p for p in players if p.player_id not in taken]

    def owed_size(self, slot: int) -> int:
        """How many players this team ends the draft with, made and pending together."""
        i = slot - 1
        return len(self.rosters[i]) + len(self.off_pool[i]) + self.picks_left[i]


def fresh_board(my_slot: int = MY_SLOT) -> Board:
    """An untouched board: the static snake, empty rosters, all 168 picks pending."""
    order = draft_order()
    return Board(
        order=order,
        pick_nos=list(range(1, len(order) + 1)),
        rosters=[[] for _ in range(TEAMS)],
        off_pool=[[] for _ in range(TEAMS)],
        picks_left=[ROUNDS] * TEAMS,
        my_slot=my_slot,
        my_picks=picks_for_slot(my_slot, order),
    )


def load_board(
    raw: dict, players: list[Player], source: str = "draft.json"
) -> tuple[Board, list[str]]:
    """Turn `draft.json` into a Board. Returns the board and any complaints about it.

    The join is on `sleeper_id`, the one key the two pipelines share — `match_sleeper.py`
    writes it into every pool player and every made pick in `draft.json` carries it. A
    made pick with no match in the pool is not an error: kickers, IDP and anyone past the
    pool's rank cut are draftable and unrankable at the same time, so they become
    `off_pool` entries (see the module docstring).

    Geometry disagreements with the league constants are reported rather than raised. This
    file is written from Sleeper's own answer about the draft, so if it says 12 teams then
    either the wrong draft was fetched or the constants in league.py are stale; both are
    worth seeing in `validation.problems` next to the numbers they broke.
    """
    problems: list[str] = []
    fmt = raw.get("format") or {}
    for label, got, want in (
        ("teams", fmt.get("teams"), TEAMS),
        ("rounds", fmt.get("rounds"), ROUNDS),
        ("pick_count", raw.get("pick_count"), TOTAL_PICKS),
    ):
        if got is not None and got != want:
            problems.append(f"{source} says {label}={got}, this script assumes {want}")

    slot_of_roster = {
        s["roster_id"]: s["draft_slot"] for s in raw.get("slots", []) if s.get("roster_id")
    }
    my_slot = (raw.get("me") or {}).get("draft_slot") or MY_SLOT
    if my_slot != MY_SLOT:
        problems.append(f"{source} says my draft slot is {my_slot}, README says {MY_SLOT}")

    by_sleeper: dict[str, Player] = {}
    for p in players:
        if p.sleeper_id:
            by_sleeper.setdefault(p.sleeper_id, p)

    rosters: list[list[Player]] = [[] for _ in range(TEAMS)]
    off_pool: list[list[dict]] = [[] for _ in range(TEAMS)]
    picks_left = [0] * TEAMS
    order: list[int] = []
    pick_nos: list[int] = []
    seen: set[int] = set()
    made = 0

    for pick in sorted(raw.get("picks", []), key=lambda p: p["pick_no"]):
        # The acquirer picks, not the column. With no trades these are the same team.
        slot = slot_of_roster.get(pick.get("roster_id")) or pick.get("draft_slot")
        if not slot or not 1 <= slot <= TEAMS:
            problems.append(f"pick {pick.get('pick_no')} has no usable owner in {source}")
            continue
        i = slot - 1
        if pick.get("status") != "made":
            order.append(slot)
            pick_nos.append(pick["pick_no"])
            picks_left[i] += 1
            continue
        made += 1
        player = by_sleeper.get(pick.get("sleeper_id") or "")
        if player is None:
            off_pool[i].append(
                {
                    "pick": pick_label(pick["pick_no"]),
                    "slot": slot,
                    "name": pick.get("name"),
                    "position": pick.get("position"),
                    "team": pick.get("team"),
                    "sleeper_id": pick.get("sleeper_id"),
                }
            )
        elif player.player_id in seen:
            problems.append(f"{player.name} appears twice in {source}'s made picks")
        else:
            seen.add(player.player_id)
            rosters[i].append(player)

    board = Board(
        order=order,
        pick_nos=pick_nos,
        rosters=rosters,
        off_pool=off_pool,
        picks_left=picks_left,
        my_slot=my_slot,
        my_picks=[n for n, s in zip(pick_nos, order) if s == my_slot],
        live={
            "source_file": source,
            "draft_id": raw.get("draft_id"),
            "league_name": raw.get("league_name"),
            "status": raw.get("status"),
            "fetched_at": raw.get("fetched_at"),
            "last_picked_at": raw.get("last_picked_at"),
            "me": raw.get("me"),
            "slots": raw.get("slots"),  # dropped from the output; only team names are kept
            "on_the_clock": raw.get("on_the_clock"),
            "next_pick_of_mine": raw.get("my_next_pick"),
            "traded_picks": len(raw.get("traded_picks") or []),
            "picks_made": made,
            "picks_pending": len(order),
            "matched_to_pool": sum(len(r) for r in rosters),
            "off_pool_picks": [o for team in off_pool for o in team],
        },
    )
    if raw.get("picks_made") is not None and raw["picks_made"] != made:
        problems.append(f"{source} header says {raw['picks_made']} picks made, picks say {made}")
    if made and not by_sleeper:
        # Otherwise this fails silently in the worst possible way: every made pick looks
        # unrankable, so drafted players stay on the emitted board as if available.
        problems.append(
            f"no pool player carries a sleeper_id, so no pick in {source} can be joined "
            "- run pool_pipeline/match_sleeper.py"
        )
    return board, problems
