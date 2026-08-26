"""Fast, roster-aware auction pricing and nomination recommendations."""

from __future__ import annotations

import math
import random
import statistics
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import blake2s

from .league import (
    ANALYSIS_POOL_MAX,
    ANALYSIS_WAIVER_BUFFER,
    AUCTION_BUDGET,
    AUCTION_POSITION_TARGETS,
    DEDICATED_SLOTS,
    MIN_BID,
    POSITIONS,
    ROSTER_SLOTS,
    SEED,
    STARTING_SLOTS,
    TEAMS,
)
from .pool import Player
from .value import team_values_with_candidates


# Cold-start owners should disagree without making the live board jump between refreshes.
# A 16% lognormal spread is enough to separate similar tiers while leaving source order
# and roster/budget constraints as the dominant inputs. The negative center keeps the
# second-highest of eleven noisy ceilings near the original market curve.
_FIELD_NOISE_SIGMA = 0.16
_FIELD_NOISE_LOG_MEAN = -0.14
_AUCTION_SIMULATIONS = 40
# A maximum is a ceiling on one of many alternative purchases, not expected spend.
# Full-auction rollouts peak at 1.8x; lower ceilings strand cash and higher ones lose
# expected-lineup value to early overpays.
_MAX_BID_HEADROOM = 1.8


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
    """Greedy marginal-value completion used to set the point-to-dollar rate."""
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
                "_gain": gain,
            }
        )
        roster.append(chosen)
        remaining = [player for player in remaining if player.player_id != chosen.player_id]
    final_value, _ = team_values_with_candidates(roster, wire, [])
    total_gain = max(0.0, final_value - initial_value)
    discretionary = max(0, team.remaining_budget - MIN_BID * team.slots_left)
    if plan and total_gain > 0:
        exact = [discretionary * row["_gain"] / total_gain for row in plan]
        extras = [math.floor(value) for value in exact]
        left = discretionary - sum(extras)
        order = sorted(
            range(len(plan)), key=lambda i: (-(exact[i] - extras[i]), i)
        )
        for index in order[:left]:
            extras[index] += 1
        for row, extra in zip(plan, extras):
            row["value_budget"] = MIN_BID + extra
            del row["_gain"]
    else:
        for row in plan:
            del row["_gain"]
    return total_gain, plan


def _curve_value(curve: list[float], rank: int) -> float:
    return curve[rank - 1] if 1 <= rank <= len(curve) else 0.0


def _field_base(
    rank: int,
    curve: list[float],
) -> float:
    return max(float(MIN_BID), _curve_value(curve, rank))


def _field_inflation(
    state: AuctionState,
    available: list[Player],
    consensus_rank: dict[int, int],
    curve: list[float],
) -> float:
    projected = sorted(
        available, key=lambda player: (consensus_rank[player.player_id], player.player_id)
    )[: state.open_slots]
    baseline = sum(
        _field_base(consensus_rank[player.player_id], curve)
        for player in projected
    )
    remaining = sum(team.remaining_budget for team in state.teams)
    if baseline <= 0:
        return 1.0
    return min(2.0, max(0.5, remaining / baseline))


def _team_position_factor(team: Team, position: str) -> float:
    counts = team.position_counts()
    owed = sum(
        max(0, DEDICATED_SLOTS[pos] - counts[pos]) for pos in POSITIONS
    )
    if team.slots_left <= owed and counts[position] < DEDICATED_SLOTS[position]:
        return 1.15
    excess_after = counts[position] + 1 - AUCTION_POSITION_TARGETS[position]
    if excess_after > 0:
        return 0.55**excess_after
    return 1.0


def _purchase_is_legal(team: Team, player: Player) -> bool:
    """A purchase must leave enough spots to fill every dedicated starter group."""
    if team.slots_left <= 0:
        return False
    counts = team.position_counts()
    if counts[player.position] >= AUCTION_POSITION_TARGETS[player.position] + 2:
        return False
    counts[player.position] += 1
    owed_after = sum(
        max(0, DEDICATED_SLOTS[position] - counts[position]) for position in POSITIONS
    )
    return owed_after <= team.slots_left - 1


def _stable_field_noise(state: AuctionState, team: Team, player: Player) -> float:
    key = f"{state.raw.get('draft_id')}|{team.roster_id}|{player.player_id}".encode()
    digest = blake2s(key, digest_size=16).digest()
    scale = 1 << 64
    u1 = (int.from_bytes(digest[:8], "big") + 0.5) / scale
    u2 = (int.from_bytes(digest[8:], "big") + 0.5) / scale
    normal = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return math.exp(_FIELD_NOISE_LOG_MEAN + _FIELD_NOISE_SIGMA * normal)


def _opponent_bid(
    player: Player,
    team: Team,
    source_id: str,
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
    inflation: float,
    league_per_slot: float,
    noise: float,
) -> int:
    if not _purchase_is_legal(team, player) or team.max_legal_bid < MIN_BID:
        return 0
    rank = (
        source_ranks[source_id][player.player_id]
        if source_id in source_ranks
        else consensus_rank[player.player_id]
    )
    base = _field_base(rank, curve)
    team_per_slot = team.remaining_budget / team.slots_left
    pace = math.sqrt(team_per_slot / league_per_slot) if league_per_slot else 1.0
    pace = min(1.25, max(0.75, pace))
    modeled = math.floor(
        base * inflation * pace * _team_position_factor(team, player.position) * noise + 0.5
    )
    return min(team.max_legal_bid, max(MIN_BID, modeled))


def _field_price(
    player: Player,
    state: AuctionState,
    source_by_roster: dict[int, str],
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
    inflation: float,
) -> tuple[int, list[dict]]:
    open_slots = sum(team.slots_left for team in state.teams)
    remaining = sum(team.remaining_budget for team in state.teams)
    league_per_slot = remaining / open_slots if open_slots else 0.0
    bids = []
    for team in state.teams:
        if team.is_mine or team.slots_left <= 0:
            continue
        source_id = source_by_roster.get(team.roster_id)
        bid = _opponent_bid(
            player,
            team,
            source_id or "",
            source_ranks,
            consensus_rank,
            curve,
            inflation,
            league_per_slot,
            _stable_field_noise(state, team, player),
        )
        if bid < MIN_BID:
            continue
        bids.append(
            {
                "roster_id": team.roster_id,
                "team": team.team_name or team.username or f"Roster {team.roster_id}",
                "max_bid": bid,
                "source_id": source_id,
            }
        )
    bids.sort(key=lambda row: (-row["max_bid"], row["roster_id"]))
    if not bids:
        return MIN_BID, []
    if len(bids) == 1:
        return MIN_BID, bids
    winning = min(bids[0]["max_bid"], bids[1]["max_bid"] + MIN_BID)
    return winning, bids[:3]


def _source_by_roster(
    state: AuctionState, matches: dict | None, source_ids: list[str]
) -> tuple[dict[int, str], set[int]]:
    matched = {
        int(owner["roster_id"]): owner["inferred_source"]["source_id"]
        for owner in (matches or {}).get("owners") or []
        if owner.get("roster_id") is not None and owner.get("inferred_source")
        and (matches.get("draft") or {}).get("draft_id") == state.raw.get("draft_id")
    }
    if not source_ids:
        raise ValueError("provider snapshot has no source boards")
    offset = int.from_bytes(
        blake2s(str(state.raw.get("draft_id")).encode(), digest_size=2).digest(), "big"
    ) % len(source_ids)
    unknown = [
        team for team in sorted(state.teams, key=lambda item: item.roster_id)
        if not team.is_mine and team.roster_id not in matched
    ]
    assigned = dict(matched)
    for index, team in enumerate(unknown):
        assigned[team.roster_id] = source_ids[(offset + index) % len(source_ids)]
    return assigned, set(matched)


def _value_max_bid(team: Team, player: Player, gain: float, point_rate: float) -> int:
    if not _purchase_is_legal(team, player):
        return 0
    share = _MAX_BID_HEADROOM * gain / point_rate if point_rate > 0 else 0.0
    modeled = math.floor(MIN_BID + share + 0.5)
    return min(team.max_legal_bid, max(MIN_BID, modeled))


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def _nominal_starters(roster: list[Player]) -> set[int]:
    """Healthy year-one starters; expected value still reselects the weekly lineup."""
    caps = dict(STARTING_SLOTS)
    starters: set[int] = set()
    for player in sorted(roster, key=lambda item: (-item.points_yr1, item.player_id)):
        for slot in (
            ("QB", "SF")
            if player.position == "QB"
            else (player.position, "FLEX", "SF")
        ):
            if caps[slot]:
                caps[slot] -= 1
                starters.add(player.player_id)
                break
    return starters


def _simulate_auctions(
    state: AuctionState,
    candidates: list[Player],
    wire: dict,
    initial_completion_gain: float,
    initial_completion_plan: list[dict],
    curve: list[float],
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    source_by_roster: dict[int, str],
    matched_rosters: set[int],
) -> tuple[dict, dict[int, dict]]:
    """Roll out complete auctions under uncertain nominations and field evaluations."""
    acquired: dict[int, list[int]] = {player.player_id: [] for player in candidates}
    prices: dict[int, list[int]] = {player.player_id: [] for player in candidates}
    affordable = {player.player_id: 0 for player in candidates}
    considered = {player.player_id: 0 for player in candidates}
    completed = 0
    roster_values: list[float] = []
    mine_spent: list[int] = []
    mine_unused: list[int] = []
    nominal_starter_points: list[float] = []
    depth_lineup_points: list[float] = []
    mine_position_counts = {position: [] for position in POSITIONS}
    league_position_max = {position: 0 for position in POSITIONS}
    pathological_rosters = 0
    representative: list[dict] = []
    outcomes: list[dict] = []
    source_ids = sorted(source_ranks)

    for simulation in range(_AUCTION_SIMULATIONS):
        rng = random.Random(SEED + simulation)
        sim_state = deepcopy(state)
        for team in sim_state.teams:
            if not team.is_mine and team.roster_id not in matched_rosters:
                source_by_roster = {
                    **source_by_roster,
                    team.roster_id: rng.choice(source_ids),
                }
        # Top players normally surface earlier, but nomination order has substantial room
        # for price-enforcement nominations and personal favorites.
        nominations = sorted(
            candidates,
            key=lambda player: (
                consensus_rank[player.player_id] + rng.gauss(0.0, 36.0),
                player.player_id,
            ),
        )
        available = list(candidates)
        completion_gain = initial_completion_gain
        completion_ids = {row["player_id"] for row in initial_completion_plan}
        discretionary = max(
            0, sim_state.mine.remaining_budget - MIN_BID * sim_state.mine.slots_left
        )
        point_rate = completion_gain / discretionary if discretionary else 0.0

        for pick_no, player in enumerate(nominations, start=state.picks_made + 1):
            if not sim_state.open_slots:
                break
            inflation = _field_inflation(sim_state, available, consensus_rank, curve)
            league_per_slot = (
                sum(team.remaining_budget for team in sim_state.teams) / sim_state.open_slots
            )
            bids: list[tuple[int, float, Team]] = []
            my_bid = 0
            my_gain = 0.0
            highest_opponent = 0
            for team in sim_state.teams:
                if team.is_mine:
                    if _purchase_is_legal(team, player):
                        base, values = team_values_with_candidates(team.players, wire, [player])
                        my_gain = max(0.0, values[player.player_id] - base)
                        my_bid = _value_max_bid(team, player, my_gain, point_rate)
                        if my_bid:
                            considered[player.player_id] += 1
                            bids.append((my_bid, rng.random(), team))
                    continue
                noise = rng.lognormvariate(
                    _FIELD_NOISE_LOG_MEAN, _FIELD_NOISE_SIGMA
                )
                bid = _opponent_bid(
                    player,
                    team,
                    source_by_roster[team.roster_id],
                    source_ranks,
                    consensus_rank,
                    curve,
                    inflation,
                    league_per_slot,
                    noise,
                )
                if bid:
                    highest_opponent = max(highest_opponent, bid)
                    bids.append((bid, rng.random(), team))
            if my_bid and my_bid >= highest_opponent + MIN_BID:
                affordable[player.player_id] += 1
            if not bids:
                continue
            bids.sort(key=lambda item: (-item[0], item[1]))
            ceiling, _, winner = bids[0]
            price = MIN_BID if len(bids) == 1 else min(ceiling, bids[1][0] + MIN_BID)
            prices[player.player_id].append(price)
            winner.purchases.append(
                Purchase(
                    pick_no,
                    player.sleeper_id,
                    player.name,
                    player.position,
                    player.team,
                    price,
                    player,
                )
            )
            available.remove(player)
            sim_state.taken.add(player.player_id)
            if winner.is_mine:
                acquired[player.player_id].append(price)
            # Removing any other player leaves the existing greedy completion unchanged.
            if winner.is_mine or player.player_id in completion_ids:
                completion_gain, completion_plan = _completion_gain(
                    sim_state.mine, available, wire
                )
                completion_ids = {row["player_id"] for row in completion_plan}
                discretionary = max(
                    0,
                    sim_state.mine.remaining_budget
                    - MIN_BID * sim_state.mine.slots_left,
                )
                point_rate = completion_gain / discretionary if discretionary else 0.0

        valid = sim_state.open_slots == 0
        for team in sim_state.teams:
            counts = team.position_counts()
            for position, count in counts.items():
                league_position_max[position] = max(league_position_max[position], count)
                if count > AUCTION_POSITION_TARGETS[position] + 2:
                    pathological_rosters += 1
            valid = valid and team.spent <= team.budget
            valid = valid and all(
                counts[position] >= DEDICATED_SLOTS[position] for position in POSITIONS
            )
        if not valid:
            continue
        completed += 1
        value, _ = team_values_with_candidates(sim_state.mine.players, wire, [])
        roster_values.append(value)
        mine_spent.append(sim_state.mine.spent)
        mine_unused.append(sim_state.mine.remaining_budget)
        starter_ids = _nominal_starters(sim_state.mine.players)
        starters = [
            player for player in sim_state.mine.players if player.player_id in starter_ids
        ]
        starter_value, _ = team_values_with_candidates(starters, wire, [])
        nominal_starter_points.append(sum(player.points_yr1 for player in starters))
        depth_lineup_points.append(max(0.0, value - starter_value))
        counts = sim_state.mine.position_counts()
        for position in POSITIONS:
            mine_position_counts[position].append(counts[position])
        roster = [
            {
                "player_id": purchase.player.player_id,
                "name": purchase.player.name,
                "position": purchase.player.position,
                "amount": purchase.amount,
                "role": (
                    "starter"
                    if purchase.player.player_id in starter_ids
                    else "bench"
                ),
            }
            for purchase in sim_state.mine.purchases[len(state.mine.purchases) :]
            if purchase.player is not None
        ]
        outcomes.append(
            {
                "value": value,
                "roster": roster,
                "remaining_budget": sim_state.mine.remaining_budget,
                "nominal_starter_points": nominal_starter_points[-1],
                "depth_lineup_points": depth_lineup_points[-1],
            }
        )

    if outcomes:
        median_value = statistics.median(outcome["value"] for outcome in outcomes)
        representative_outcome = min(
            outcomes, key=lambda outcome: abs(outcome["value"] - median_value)
        )
        representative = representative_outcome["roster"]
    else:
        representative_outcome = None

    player_results = {}
    for player in candidates:
        player_prices = prices[player.player_id]
        wins = acquired[player.player_id]
        opportunities = considered[player.player_id]
        player_results[player.player_id] = {
            "simulated_roster_rate": round(len(wins) / _AUCTION_SIMULATIONS, 3),
            "simulated_affordable_rate": round(
                affordable[player.player_id] / opportunities, 3
            ) if opportunities else 0.0,
            "simulated_price_low": _percentile(player_prices, 0.1) if player_prices else None,
            "simulated_price_median": _percentile(player_prices, 0.5) if player_prices else None,
            "simulated_price_high": _percentile(player_prices, 0.9) if player_prices else None,
            "simulated_purchase_price": round(statistics.mean(wins), 1) if wins else None,
        }

    summary = {
        "simulations": _AUCTION_SIMULATIONS,
        "completed": completed,
        "my_projected_lineup_points": {
            "mean": round(statistics.mean(roster_values), 1) if roster_values else None,
            "low": round(min(roster_values), 1) if roster_values else None,
            "high": round(max(roster_values), 1) if roster_values else None,
        },
        "my_spend": {
            "mean": round(statistics.mean(mine_spent), 1) if mine_spent else None,
            "low": min(mine_spent) if mine_spent else None,
            "high": max(mine_spent) if mine_spent else None,
        },
        "my_unused_budget": {
            "mean": round(statistics.mean(mine_unused), 1) if mine_unused else None,
            "low": min(mine_unused) if mine_unused else None,
            "high": max(mine_unused) if mine_unused else None,
        },
        "my_nominal_starter_points": {
            "mean": round(statistics.mean(nominal_starter_points), 1)
            if nominal_starter_points
            else None,
            "low": round(min(nominal_starter_points), 1)
            if nominal_starter_points
            else None,
            "high": round(max(nominal_starter_points), 1)
            if nominal_starter_points
            else None,
        },
        "my_depth_lineup_points": {
            "mean": round(statistics.mean(depth_lineup_points), 1)
            if depth_lineup_points
            else None,
            "low": round(min(depth_lineup_points), 1) if depth_lineup_points else None,
            "high": round(max(depth_lineup_points), 1) if depth_lineup_points else None,
        },
        "my_position_ranges": {
            position: {
                "low": min(counts) if counts else None,
                "high": max(counts) if counts else None,
            }
            for position, counts in mine_position_counts.items()
        },
        "largest_simulated_position_counts": league_position_max,
        "pathological_rosters": pathological_rosters,
        "representative_remaining_budget": (
            representative_outcome["remaining_budget"]
            if representative_outcome
            else None
        ),
        "representative_nominal_starter_points": (
            round(representative_outcome["nominal_starter_points"], 1)
            if representative_outcome
            else None
        ),
        "representative_depth_lineup_points": (
            round(representative_outcome["depth_lineup_points"], 1)
            if representative_outcome
            else None
        ),
        "representative_completion": representative,
    }
    return summary, player_results


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
    inflation = _field_inflation(state, all_available, consensus_rank, curve)
    source_by_roster, matched_rosters = _source_by_roster(
        state, matches, sorted(source_ranks)
    )

    # Allocate this roster's discretionary dollars over the projected completion value.
    # Unlike the market curve, this scale cannot spend the same dollar on fourteen
    # independent "maximums" and it does not inherit a provider's top-player outlier.
    point_rate = completion_gain / discretionary if discretionary else 0.0

    provisional = []
    for player in candidates:
        max_bid = _value_max_bid(
            state.mine, player, gains[player.player_id], point_rate
        )
        field_price, top_bidders = _field_price(
            player,
            state,
            source_by_roster,
            source_ranks,
            consensus_rank,
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

    simulation, simulated_players = _simulate_auctions(
        state,
        candidates,
        wire,
        completion_gain,
        plan,
        curve,
        source_ranks,
        consensus_rank,
        source_by_roster,
        matched_rosters,
    )
    for row in rows:
        row.update(simulated_players[row["player_id"]])
        row["value_edge"] = row["max_bid"] - row["field_price"]

    nominations = sorted(
        (
            row for row in rows
            if state.mine.slots_left > 0
            and row["nomination_edge"] > 0
            and row["field_price"] > MIN_BID
        ),
        key=lambda row: (-row["nomination_edge"], -row["field_price"], row["field_rank"]),
    )[:8]
    purchase_targets = sorted(
        (
            row for row in rows
            if row["simulated_roster_rate"] > 0
        ),
        key=lambda row: (
            -row["simulated_roster_rate"],
            -row["value_edge"],
            -row["lineup_gain"],
            row["field_rank"],
        ),
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
    if simulation["completed"] != simulation["simulations"]:
        problems.append(
            f"only {simulation['completed']}/{simulation['simulations']} simulated auctions "
            "finished with legal budgets and lineups"
        )
    if simulation["pathological_rosters"]:
        problems.append(
            f"simulated auctions built {simulation['pathological_rosters']} rosters with a "
            "position more than two players beyond its modeled target"
        )
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
            "matched_opponent_sources": len(matched_rosters),
            "cold_start_opponent_sources": len(source_by_roster) - len(matched_rosters),
            "wire": {h: {p: round(v, 1) for p, v in levels.items()} for h, levels in wire.items()},
            "simulation": simulation,
            "pricing_note": (
                "Max bid converts current marginal expected-lineup value at the completion "
                "plan's point-to-dollar rate. Bid ceilings receive 1.8x headroom because "
                "they are alternative limits rather than expected prices; full-auction rollouts "
                "selected that level by final expected-lineup value and spend. Every bid "
                "still reserves $1 for every other open slot. Field price is one dollar above "
                "the second-highest legal opponent ceiling, capped by the highest."
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
            "points_per_discretionary_dollar": round(
                point_rate / _MAX_BID_HEADROOM, 2
            ),
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
        "purchase_strategy": {
            "note": (
                "Targets that fit the projection-valued budget and recur on this roster "
                "across complete auction rollouts. Roster rate is the share of simulations "
                "in which the budget-balanced bidding policy actually acquired the player; "
                "it includes nomination order, opponent-source uncertainty, and bid noise."
            ),
            "recommendations": purchase_targets,
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
    if sum(row["value_budget"] for row in result["my_auction"]["completion_plan"]) != AUCTION_BUDGET:
        problems.append("completion value budgets do not allocate the full auction budget")
    diverse_rows = [
        row for row in result["rankings"][:40]
        if len(row["top_field_bidders"]) >= 3
        and len({bid["max_bid"] for bid in row["top_field_bidders"]}) > 1
        and len({bid["source_id"] for bid in row["top_field_bidders"]}) > 1
    ]
    if not diverse_rows:
        problems.append("cold-start field bidders did not have diverse sources and ceilings")
    simulation = result["analysis"]["simulation"]
    if simulation["completed"] != simulation["simulations"]:
        problems.append("not every selftest auction simulation completed legally")
    if simulation["pathological_rosters"]:
        problems.append("selftest auction simulations produced a pathological position count")
    if simulation["my_unused_budget"]["mean"] > 8:
        problems.append("auction policy strands more than $8 on average")
    roles = [row["role"] for row in simulation["representative_completion"]]
    if roles.count("starter") != sum(STARTING_SLOTS.values()):
        problems.append("representative completion does not identify nine nominal starters")
    if roles.count("bench") != ROSTER_SLOTS - sum(STARTING_SLOTS.values()):
        problems.append("representative completion does not identify five bench players")
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
    if not _purchase_is_legal(last_slot_team, tight_end):
        problems.append("a final-slot tight end was rejected when tight end was still owed")
    if _purchase_is_legal(last_slot_team, extra_receiver):
        problems.append("a final-slot receiver was allowed when tight end was still owed")
    return problems
