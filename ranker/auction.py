"""Fast, roster-aware auction pricing and nomination recommendations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .league import (
    ANALYSIS_POOL_MAX,
    ANALYSIS_WAIVER_BUFFER,
    AUCTION_BUDGET,
    AUCTION_POSITION_TARGETS,
    DEDICATED_SLOTS,
    MIN_BID,
    POSITIONS,
    ROSTER_SLOTS,
    STARTING_SLOTS,
    TEAMS,
)
from .pool import Player
from .value import HORIZONS, team_values_with_candidates


@dataclass(slots=True)
class Purchase:
    pick_no: int
    sleeper_id: str | None
    name: str | None
    position: str | None
    nfl_team: str | None
    amount: int
    player: Player | None


@dataclass(slots=True)
class Team:
    roster_id: int
    username: str | None
    team_name: str | None
    is_mine: bool
    budget: int
    purchases: list[Purchase] = field(default_factory=list)

    @property
    def players(self) -> list[Player]:
        return [purchase.player for purchase in self.purchases if purchase.player is not None]

    @property
    def spent(self) -> int:
        return sum(purchase.amount for purchase in self.purchases)

    @property
    def remaining_budget(self) -> int:
        return self.budget - self.spent

    @property
    def slots_left(self) -> int:
        return ROSTER_SLOTS - len(self.purchases)

    @property
    def max_legal_bid(self) -> int:
        if self.slots_left <= 0:
            return 0
        return self.remaining_budget - MIN_BID * (self.slots_left - 1)

    def position_counts(self) -> dict[str, int]:
        counts = {position: 0 for position in POSITIONS}
        for purchase in self.purchases:
            if purchase.position in counts:
                counts[purchase.position] += 1
        return counts


@dataclass(slots=True)
class AuctionState:
    raw: dict
    teams: list[Team]
    mine: Team
    taken: set[int]
    problems: list[str]

    @property
    def picks_made(self) -> int:
        return sum(len(team.purchases) for team in self.teams)

    @property
    def open_slots(self) -> int:
        return sum(team.slots_left for team in self.teams)


def load_state(raw: dict, players: list[Player]) -> AuctionState:
    """Load the made purchases, rosters, and remaining dollars from ``draft.json``."""
    problems: list[str] = []
    fmt = raw.get("format") or {}
    if fmt.get("type") != "auction":
        problems.append(f"draft type is {fmt.get('type')!r}, want 'auction'")
    for label, got, want in (
        ("teams", fmt.get("teams"), TEAMS),
        ("rounds", fmt.get("rounds"), ROSTER_SLOTS),
        ("budget", fmt.get("budget"), AUCTION_BUDGET),
    ):
        if got is not None and int(got) != want:
            problems.append(f"draft says {label}={got}, ranker assumes {want}")

    slots = raw.get("slots") or []
    if len(slots) != TEAMS:
        problems.append(f"draft has {len(slots)} rosters, want {TEAMS}")
    teams = [
        Team(
            roster_id=int(slot["roster_id"]),
            username=slot.get("username"),
            team_name=slot.get("team_name"),
            is_mine=bool(slot.get("is_mine")),
            budget=int(fmt.get("budget") or AUCTION_BUDGET),
        )
        for slot in slots
        if slot.get("roster_id") is not None
    ]
    by_roster = {team.roster_id: team for team in teams}
    by_sleeper = {str(player.sleeper_id): player for player in players if player.sleeper_id}
    seen_sleeper: set[str] = set()
    taken: set[int] = set()
    for row in sorted(raw.get("picks") or [], key=lambda pick: int(pick["pick_no"])):
        if row.get("status") != "made":
            continue
        roster_id = int(row["roster_id"])
        team = by_roster.get(roster_id)
        if team is None:
            problems.append(f"pick {row.get('pick_no')} belongs to unknown roster {roster_id}")
            continue
        sleeper_id = str(row["sleeper_id"]) if row.get("sleeper_id") else None
        if sleeper_id and sleeper_id in seen_sleeper:
            problems.append(f"Sleeper player {sleeper_id} was purchased twice")
        if sleeper_id:
            seen_sleeper.add(sleeper_id)
        amount = row.get("amount")
        if amount is None:
            problems.append(f"auction pick {row.get('pick_no')} has no amount")
            amount = 0
        amount = int(amount)
        if amount < MIN_BID:
            problems.append(f"auction pick {row.get('pick_no')} cost ${amount}, below ${MIN_BID}")
        player = by_sleeper.get(sleeper_id) if sleeper_id else None
        if player is not None:
            taken.add(player.player_id)
        team.purchases.append(
            Purchase(
                pick_no=int(row["pick_no"]),
                sleeper_id=sleeper_id,
                name=row.get("name"),
                position=row.get("position"),
                nfl_team=row.get("team"),
                amount=amount,
                player=player,
            )
        )

    my_roster = (raw.get("me") or {}).get("roster_id")
    mine = by_roster.get(int(my_roster)) if my_roster is not None else None
    if mine is None:
        mine = next((team for team in teams if team.is_mine), None)
    if mine is None:
        raise ValueError("draft.json does not identify my auction roster")
    for team in teams:
        team.is_mine = team.roster_id == mine.roster_id
        if team.spent > team.budget:
            problems.append(f"roster {team.roster_id} spent ${team.spent} of ${team.budget}")
        if team.slots_left < 0:
            problems.append(f"roster {team.roster_id} has {-team.slots_left} too many players")
        if team.slots_left and team.remaining_budget < team.slots_left * MIN_BID:
            problems.append(
                f"roster {team.roster_id} has ${team.remaining_budget} for "
                f"{team.slots_left} open slots"
            )
    return AuctionState(raw=raw, teams=teams, mine=mine, taken=taken, problems=problems)


def _source_orders(players: list[Player], rankings: dict) -> dict[str, tuple[int, ...]]:
    """Complete each provider's joined order with the projection order as its tail."""
    player_ids = {player.player_id for player in players}
    by_sleeper = {str(player.sleeper_id): player.player_id for player in players if player.sleeper_id}
    tail = [player.player_id for player in sorted(players, key=lambda p: (-p.points_1yr, p.player_id))]
    orders: dict[str, tuple[int, ...]] = {}
    for source in rankings["sources"]:
        primary: list[int] = []
        seen: set[int] = set()
        for row in source["players"]:
            sleeper_id = row.get("sleeper_id")
            player_id = by_sleeper.get(str(sleeper_id)) if sleeper_id is not None else None
            if player_id in player_ids and player_id not in seen:
                seen.add(player_id)
                primary.append(player_id)
        orders[source["id"]] = tuple(primary + [player_id for player_id in tail if player_id not in seen])
    return orders


def _market_inputs(
    players: list[Player], rankings: dict
) -> tuple[dict[int, int], list[float], dict[str, tuple[int, ...]], dict[int, int]]:
    orders = _source_orders(players, rankings)
    ranks = {
        source_id: {player_id: rank for rank, player_id in enumerate(order, start=1)}
        for source_id, order in orders.items()
    }
    consensus_score = {
        player.player_id: sum(source[player.player_id] for source in ranks.values()) / len(ranks)
        for player in players
    }
    consensus_order = sorted(players, key=lambda player: (consensus_score[player.player_id], player.player_id))
    consensus_rank = {
        player.player_id: rank for rank, player in enumerate(consensus_order, start=1)
    }

    auction = next(
        (source for source in rankings["sources"] if source["id"] == "fantasypros_auction"),
        None,
    )
    if auction is None:
        raise ValueError("provider snapshot has no fantasypros_auction dollar curve")
    by_sleeper = {str(player.sleeper_id): player.player_id for player in players if player.sleeper_id}
    values: dict[int, int] = {}
    curve: list[float] = []
    for row in auction["players"]:
        value = float(row.get("value") or 0)
        curve.append(value)
        sleeper_id = row.get("sleeper_id")
        player_id = by_sleeper.get(str(sleeper_id)) if sleeper_id is not None else None
        if player_id is not None:
            values[player_id] = int(round(value))
    return values, curve, orders, consensus_rank


def _wire_levels(
    players: list[Player], state: AuctionState, consensus_rank: dict[int, int]
) -> dict[str, dict[str, float]]:
    available = [player for player in players if player.player_id not in state.taken]
    available.sort(key=lambda player: (consensus_rank[player.player_id], player.player_id))
    projected_taken = {player.player_id for player in available[: state.open_slots]}
    free_agents = [player for player in available if player.player_id not in projected_taken]
    yr1 = {}
    for position in POSITIONS:
        at_position = [player.points_1yr for player in free_agents if player.position == position]
        yr1[position] = float(max(at_position) if at_position else 0.0)
    return {"yr1": yr1, "yr23": {position: 0.0 for position in POSITIONS}}


def _pre_draft_wire(
    players: list[Player], consensus_rank: dict[int, int]
) -> dict[str, dict[str, float]]:
    ordered = sorted(players, key=lambda player: (consensus_rank[player.player_id], player.player_id))
    projected_taken = {player.player_id for player in ordered[: TEAMS * ROSTER_SLOTS]}
    free_agents = [player for player in ordered if player.player_id not in projected_taken]
    return {
        "yr1": {
            position: float(
                max(
                    (player.points_1yr for player in free_agents if player.position == position),
                    default=0.0,
                )
            )
            for position in POSITIONS
        },
        "yr23": {position: 0.0 for position in POSITIONS},
    }


def _eligible_for_completion(team: Team, roster: list[Player], candidates: list[Player], left: int):
    counts = team.position_counts()
    for player in roster[len(team.players) :]:
        counts[player.position] += 1
    owed = {
        position: max(0, DEDICATED_SLOTS[position] - counts[position])
        for position in POSITIONS
    }
    if left <= sum(owed.values()):
        required = {position for position, count in owed.items() if count}
        narrowed = [player for player in candidates if player.position in required]
        if narrowed:
            return narrowed
    return candidates


def _completion_gain(team: Team, candidates: list[Player], wire: dict) -> tuple[float, list[dict]]:
    """Greedy marginal-value completion used only to set the point-to-dollar rate."""
    roster = list(team.players)
    remaining = list(candidates)
    initial_value, _ = team_values_with_candidates(roster, wire, [])
    plan: list[dict] = []
    for step in range(min(team.slots_left, len(remaining))):
        legal = _eligible_for_completion(team, roster, remaining, team.slots_left - step)
        base, values = team_values_with_candidates(roster, wire, legal)
        chosen = min(
            legal,
            key=lambda player: (-(values[player.player_id] - base), player.player_id),
        )
        gain = values[chosen.player_id] - base
        plan.append(
            {
                "player_id": chosen.player_id,
                "name": chosen.name,
                "position": chosen.position,
                "lineup_gain": round(gain, 1),
            }
        )
        roster.append(chosen)
        remaining = [player for player in remaining if player.player_id != chosen.player_id]
    final_value, _ = team_values_with_candidates(roster, wire, [])
    return max(0.0, final_value - initial_value), plan


def _curve_value(curve: list[float], rank: int) -> float:
    return curve[rank - 1] if 1 <= rank <= len(curve) else 0.0


def _field_base(
    player_id: int,
    rank: int,
    auction_values: dict[int, int],
    curve: list[float],
) -> float:
    direct = float(auction_values.get(player_id, 0))
    return max(float(MIN_BID), (direct + _curve_value(curve, rank)) / 2.0)


def _field_inflation(
    state: AuctionState,
    available: list[Player],
    consensus_rank: dict[int, int],
    auction_values: dict[int, int],
    curve: list[float],
) -> float:
    projected = sorted(
        available, key=lambda player: (consensus_rank[player.player_id], player.player_id)
    )[: state.open_slots]
    baseline = sum(
        _field_base(player.player_id, consensus_rank[player.player_id], auction_values, curve)
        for player in projected
    )
    remaining = sum(team.remaining_budget for team in state.teams)
    if baseline <= 0:
        return 1.0
    return min(2.0, max(0.5, remaining / baseline))


def _team_position_factor(team: Team, position: str) -> float:
    counts = team.position_counts()
    if counts[position] >= AUCTION_POSITION_TARGETS[position]:
        return 0.65
    owed = sum(
        max(0, DEDICATED_SLOTS[pos] - counts[pos]) for pos in POSITIONS
    )
    if team.slots_left <= owed and counts[position] < DEDICATED_SLOTS[position]:
        return 1.15
    return 1.0


def _personal_purchase_is_legal(team: Team, player: Player) -> bool:
    """A purchase must leave enough spots to fill every dedicated starter group."""
    if team.slots_left <= 0:
        return False
    counts = team.position_counts()
    counts[player.position] += 1
    owed_after = sum(
        max(0, DEDICATED_SLOTS[position] - counts[position]) for position in POSITIONS
    )
    return owed_after <= team.slots_left - 1


def _field_price(
    player: Player,
    state: AuctionState,
    source_by_roster: dict[int, str],
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    auction_values: dict[int, int],
    curve: list[float],
    inflation: float,
) -> tuple[int, list[dict]]:
    open_slots = sum(team.slots_left for team in state.teams)
    remaining = sum(team.remaining_budget for team in state.teams)
    league_per_slot = remaining / open_slots if open_slots else 0.0
    bids = []
    for team in state.teams:
        if team.is_mine or team.slots_left <= 0 or team.max_legal_bid < MIN_BID:
            continue
        source_id = source_by_roster.get(team.roster_id)
        rank = (
            source_ranks[source_id][player.player_id]
            if source_id in source_ranks
            else consensus_rank[player.player_id]
        )
        base = _field_base(player.player_id, rank, auction_values, curve)
        team_per_slot = team.remaining_budget / team.slots_left
        pace = math.sqrt(team_per_slot / league_per_slot) if league_per_slot else 1.0
        pace = min(1.25, max(0.75, pace))
        modeled = math.floor(
            base * inflation * pace * _team_position_factor(team, player.position) + 0.5
        )
        bid = min(team.max_legal_bid, max(MIN_BID, modeled))
        bids.append(
            {
                "roster_id": team.roster_id,
                "team": team.team_name or team.username or f"Roster {team.roster_id}",
                "max_bid": bid,
            }
        )
    bids.sort(key=lambda row: (-row["max_bid"], row["roster_id"]))
    if not bids:
        return MIN_BID, []
    if len(bids) == 1:
        return MIN_BID, bids
    winning = min(bids[0]["max_bid"], bids[1]["max_bid"] + MIN_BID)
    return winning, bids[:3]


def _source_by_roster(state: AuctionState, matches: dict | None) -> dict[int, str]:
    if not matches or (matches.get("draft") or {}).get("draft_id") != state.raw.get("draft_id"):
        return {}
    return {
        int(owner["roster_id"]): owner["inferred_source"]["source_id"]
        for owner in matches.get("owners") or []
        if owner.get("roster_id") is not None and owner.get("inferred_source")
    }


def analyze(
    players: list[Player],
    state: AuctionState,
    rankings: dict,
    matches: dict | None,
) -> dict:
    auction_values, curve, orders, consensus_rank = _market_inputs(players, rankings)
    source_ranks = {
        source_id: {player_id: rank for rank, player_id in enumerate(order, start=1)}
        for source_id, order in orders.items()
    }
    # Preseason projection value supplies the personal side of candidate relevance and
    # the dollar-curve rank. Compute it over the full pool so a player we value much more
    # than the field cannot disappear outside a field-only 240-player cutoff.
    generic_wire = _pre_draft_wire(players, consensus_rank)
    generic_base, generic_values = team_values_with_candidates([], generic_wire, players)
    generic_gains = {
        player.player_id: max(0.0, generic_values[player.player_id] - generic_base)
        for player in players
    }
    projection_order = sorted(
        players, key=lambda player: (-generic_gains[player.player_id], player.player_id)
    )
    projection_rank = {
        player.player_id: rank for rank, player in enumerate(projection_order, start=1)
    }
    all_available = [player for player in players if player.player_id not in state.taken]
    all_available.sort(key=lambda player: (consensus_rank[player.player_id], player.player_id))
    target_size = min(
        ANALYSIS_POOL_MAX,
        max(ANALYSIS_WAIVER_BUFFER, state.open_slots + ANALYSIS_WAIVER_BUFFER),
        len(all_available),
    )
    candidates = sorted(
        all_available,
        key=lambda player: (
            min(consensus_rank[player.player_id], projection_rank[player.player_id]),
            consensus_rank[player.player_id] + projection_rank[player.player_id],
            player.player_id,
        ),
    )[:target_size]
    wire = _wire_levels(players, state, consensus_rank)

    base, candidate_values = team_values_with_candidates(state.mine.players, wire, candidates)
    gains = {
        player.player_id: max(0.0, candidate_values[player.player_id] - base)
        for player in candidates
    }
    completion_gain, plan = _completion_gain(state.mine, candidates, wire)
    discretionary = max(
        0, state.mine.remaining_budget - MIN_BID * state.mine.slots_left
    )
    inflation = _field_inflation(
        state, all_available, consensus_rank, auction_values, curve
    )
    source_by_roster = _source_by_roster(state, matches)

    # The auction curve supplies only the dollar scale. Current-roster marginal value
    # discounts the personal preseason anchor as a position fills.
    league_per_slot = (
        sum(team.remaining_budget for team in state.teams) / state.open_slots
        if state.open_slots
        else 0.0
    )
    my_per_slot = (
        state.mine.remaining_budget / state.mine.slots_left
        if state.mine.slots_left
        else 0.0
    )
    budget_pace = math.sqrt(my_per_slot / league_per_slot) if league_per_slot else 1.0
    budget_pace = min(1.5, max(0.5, budget_pace))

    provisional = []
    for player in candidates:
        if not _personal_purchase_is_legal(state.mine, player):
            max_bid = 0
        else:
            generic_gain = generic_gains[player.player_id]
            roster_factor = (
                gains[player.player_id] / generic_gain if generic_gain > 0 else 0.0
            )
            personal_anchor = max(
                float(MIN_BID), _curve_value(curve, projection_rank[player.player_id])
            )
            max_bid = min(
                state.mine.max_legal_bid,
                max(
                    MIN_BID,
                    math.floor(personal_anchor * inflation * budget_pace * roster_factor + 0.5),
                ),
            )
        field_price, top_bidders = _field_price(
            player,
            state,
            source_by_roster,
            source_ranks,
            consensus_rank,
            auction_values,
            curve,
            inflation,
        )
        provisional.append(
            {
                "player": player,
                "lineup_gain": gains[player.player_id],
                "max_bid": max_bid,
                "field_price": field_price,
                "nomination_edge": field_price - max_bid,
                "top_bidders": top_bidders,
            }
        )
    provisional.sort(
        key=lambda item: (
            -item["max_bid"],
            -item["lineup_gain"],
            consensus_rank[item["player"].player_id],
            item["player"].player_id,
        )
    )
    position_ranks = {position: 0 for position in POSITIONS}
    rows = []
    for rank, item in enumerate(provisional, start=1):
        player = item.pop("player")
        position_ranks[player.position] += 1
        rows.append(
            {
                "rank": rank,
                "player_id": player.player_id,
                "name": player.name,
                "position": player.position,
                "positional_rank": position_ranks[player.position],
                "team": player.team,
                "age": player.age,
                "bye_week": player.bye_week,
                "is_rookie": player.is_rookie,
                "points_1yr": player.points_1yr,
                "lineup_gain": round(item["lineup_gain"], 1),
                "max_bid": item["max_bid"],
                "field_price": item["field_price"],
                "nomination_edge": item["nomination_edge"],
                "fantasypros_auction_value": auction_values.get(player.player_id, 0),
                "field_rank": consensus_rank[player.player_id],
                "field_rank_delta": consensus_rank[player.player_id] - rank,
                "projection_value_rank": projection_rank[player.player_id],
                "roster_value_factor": (
                    round(gains[player.player_id] / generic_gains[player.player_id], 3)
                    if generic_gains[player.player_id] > 0
                    else 0.0
                ),
                "top_field_bidders": item["top_bidders"],
            }
        )

    nominations = sorted(
        (
            row for row in rows
            if state.mine.slots_left > 0
            and row["nomination_edge"] > 0
            and row["field_price"] > MIN_BID
        ),
        key=lambda row: (-row["nomination_edge"], -row["field_price"], row["field_rank"]),
    )[:8]
    teams = []
    for team in sorted(state.teams, key=lambda item: item.roster_id):
        teams.append(
            {
                "roster_id": team.roster_id,
                "team": team.team_name or team.username or f"Roster {team.roster_id}",
                "username": team.username,
                "is_mine": team.is_mine,
                "spent": team.spent,
                "remaining_budget": team.remaining_budget,
                "slots_filled": len(team.purchases),
                "slots_left": team.slots_left,
                "max_legal_bid": team.max_legal_bid,
                "positions": team.position_counts(),
                "players": [
                    {
                        "pick_no": purchase.pick_no,
                        "player_id": purchase.player.player_id if purchase.player else None,
                        "name": purchase.player.name if purchase.player else purchase.name,
                        "position": purchase.position,
                        "nfl_team": purchase.nfl_team,
                        "amount": purchase.amount,
                        "points_1yr": purchase.player.points_1yr if purchase.player else None,
                        "off_pool": purchase.player is None,
                    }
                    for purchase in team.purchases
                ],
            }
        )

    problems = list(state.problems)
    if len(rows) > ANALYSIS_POOL_MAX:
        problems.append(f"analysis emitted {len(rows)} players, cap is {ANALYSIS_POOL_MAX}")
    if state.picks_made + state.open_slots != TEAMS * ROSTER_SLOTS:
        problems.append("made purchases plus open roster slots do not equal the draft size")
    for row in rows:
        if row["player_id"] in state.taken:
            problems.append(f"drafted player {row['name']} remains on the bid board")
        if not 0 <= row["max_bid"] <= max(0, state.mine.max_legal_bid):
            problems.append(f"{row['name']} max bid violates my legal budget ceiling")

    return {
        "league": {
            "teams": TEAMS,
            "starting_slots": STARTING_SLOTS,
            "bench_slots": ROSTER_SLOTS - sum(STARTING_SLOTS.values()),
            "roster_slots": ROSTER_SLOTS,
            "total_picks": TEAMS * ROSTER_SLOTS,
            "draft_type": "auction",
            "budget": AUCTION_BUDGET,
            "minimum_bid": MIN_BID,
        },
        "draft": {
            "draft_id": state.raw.get("draft_id"),
            "league_name": state.raw.get("league_name"),
            "status": state.raw.get("status"),
            "fetched_at": state.raw.get("fetched_at"),
            "last_picked_at": state.raw.get("last_picked_at"),
            "picks_made": state.picks_made,
            "picks_pending": state.open_slots,
            "my_roster_id": state.mine.roster_id,
        },
        "analysis": {
            "player_cap": ANALYSIS_POOL_MAX,
            "waiver_buffer": ANALYSIS_WAIVER_BUFFER,
            "players_examined": len(rows),
            "available_pool_players": len(all_available),
            "field_inflation": round(inflation, 3),
            "wire": {h: {p: round(v, 1) for p, v in levels.items()} for h, levels in wire.items()},
            "pricing_note": (
                "Max bid maps my projection-based preseason value rank onto the league's "
                "$200 auction curve, discounts it by the player's marginal value on my "
                "current roster, adjusts for live inflation and my budget pace, then applies "
                "the hard $1 reserve for every other open slot. Field price is the modeled ascending-"
                "auction result: one dollar above the second-highest opponent max, capped by "
                "the highest, with provider ranks, roster depth, remaining budgets, and live "
                "inflation applied."
            ),
        },
        "my_auction": {
            "roster_id": state.mine.roster_id,
            "team": state.mine.team_name or state.mine.username or f"Roster {state.mine.roster_id}",
            "spent": state.mine.spent,
            "remaining_budget": state.mine.remaining_budget,
            "slots_filled": len(state.mine.purchases),
            "slots_left": state.mine.slots_left,
            "max_legal_bid": state.mine.max_legal_bid,
            "discretionary_budget": discretionary,
            "budget_pace": round(budget_pace, 3),
            "completion_gain": round(completion_gain, 1),
            "completion_plan": plan,
        },
        "nomination_strategy": {
            "note": (
                "Nominate the largest positive field-price-minus-max-bid gaps. These are "
                "players the modeled opponents should buy for more than this roster should pay."
            ),
            "recommendations": nominations,
        },
        "teams": teams,
        "rankings_note": (
            "Available players only, ordered by my roster-aware max bid. The live analysis "
            "examines at most 240 players: every remaining roster purchase plus a six-player-"
            "per-team waiver buffer."
        ),
        "validation": {"ok": not problems, "problems": problems},
        "count": len(rows),
        "rankings": rows,
    }


def selftest(players: list[Player], rankings: dict) -> list[str]:
    problems: list[str] = []
    slots = [
        {
            "draft_slot": roster_id,
            "roster_id": roster_id,
            "username": "me" if roster_id == 1 else f"owner{roster_id}",
            "team_name": None,
            "is_mine": roster_id == 1,
        }
        for roster_id in range(1, TEAMS + 1)
    ]
    raw = {
        "draft_id": "auction-selftest",
        "format": {"type": "auction", "teams": TEAMS, "rounds": ROSTER_SLOTS, "budget": AUCTION_BUDGET},
        "me": {"roster_id": 1},
        "slots": slots,
        "picks": [],
    }
    state = load_state(raw, players)
    result = analyze(players, state, rankings, None)
    if state.mine.max_legal_bid != AUCTION_BUDGET - (ROSTER_SLOTS - 1) * MIN_BID:
        problems.append("empty-roster legal max did not reserve $1 for every other slot")
    if result["count"] != min(ANALYSIS_POOL_MAX, len(players)):
        problems.append("analysis pool cap or waiver horizon is wrong")
    if any(row["max_bid"] > state.mine.max_legal_bid for row in result["rankings"]):
        problems.append("a player max bid exceeds the hard budget ceiling")
    if any(row["nomination_edge"] <= 0 for row in result["nomination_strategy"]["recommendations"]):
        problems.append("nomination list contains a non-draining player")
    if not result["validation"]["ok"]:
        problems.extend(result["validation"]["problems"])

    bought = players[0]
    purchased_raw = {
        **raw,
        "picks": [
            {
                "pick_no": 1,
                "status": "made",
                "roster_id": 1,
                "sleeper_id": bought.sleeper_id,
                "name": bought.name,
                "position": bought.position,
                "team": bought.team,
                "amount": 47,
            }
        ],
    }
    purchased = load_state(purchased_raw, players)
    if (purchased.mine.remaining_budget, purchased.mine.slots_left) != (153, 13):
        problems.append("a made $47 purchase did not update my budget and open slots")
    if bought.player_id not in purchased.taken:
        problems.append("a made purchase was not removed from the available pool")
    purchased_result = analyze(players, purchased, rankings, None)
    empty_rows = {row["player_id"]: row for row in result["rankings"]}
    same_position = next(
        row
        for row in purchased_result["rankings"]
        if row["position"] == bought.position and row["player_id"] in empty_rows
    )
    if same_position["max_bid"] >= empty_rows[same_position["player_id"]]["max_bid"]:
        problems.append("a purchase did not reduce the next same-position maximum bid")

    one_qb = next(player for player in players if player.position == "QB")
    two_rbs = [player for player in players if player.position == "RB"][:2]
    ten_wrs = [player for player in players if player.position == "WR"][:10]
    last_slot_team = Team(99, None, None, True, AUCTION_BUDGET)
    for pick_no, player in enumerate([one_qb, *two_rbs, *ten_wrs], start=1):
        last_slot_team.purchases.append(
            Purchase(pick_no, player.sleeper_id, player.name, player.position, player.team, 1, player)
        )
    tight_end = next(player for player in players if player.position == "TE")
    extra_receiver = next(player for player in players if player.position == "WR")
    if not _personal_purchase_is_legal(last_slot_team, tight_end):
        problems.append("a final-slot tight end was rejected when tight end was still owed")
    if _personal_purchase_is_legal(last_slot_team, extra_receiver):
        problems.append("a final-slot receiver was allowed when tight end was still owed")
    return problems
