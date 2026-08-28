"""Fast, roster-aware auction pricing and nomination recommendations."""

from __future__ import annotations

import math
import os
import random
import statistics
from dataclasses import dataclass, field
from hashlib import blake2s
from multiprocessing import Pool

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
_AUCTION_SIMULATIONS = 48
# The plan's shadow price is bisected inside this bracket. No player returns 64 lineup
# points per discretionary dollar, so the top of the bracket is the all-$1 completion.
_MAX_POINT_RATE = 64.0
_POINT_RATE_STEPS = 9
# A replan brackets the previous shadow price this widely and bisects this many times.
_WARM_RATE_SPREAD = 1.3
_WARM_RATE_STEPS = 3
# A replan at the previous shadow price that lands this close to the budget stands, and a
# repriced plan whose cost has drifted no further than this from its planned cost keeps
# its shadow price.
_REPLAN_SLACK = 3
# The expected highest opposing ceiling averages these fixed noise draws: the maximum of
# eleven noisy ceilings sits above the maximum of their medians whenever owners contend.
_ACQUISITION_DRAWS = 8
# Rollouts use every core up to eight. The 4-core GitHub runner is fine flat out; the
# 16-core development machine crashed under repeated all-core bursts.
_MAX_WORKERS = 8


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


def _copy_state(state: AuctionState) -> AuctionState:
    """A rollout's private teams and purchase lists; players and purchases are shared."""
    teams = [
        Team(team.roster_id, team.username, team.team_name, team.is_mine, team.budget, list(team.purchases))
        for team in state.teams
    ]
    mine = next(team for team in teams if team.is_mine)
    return AuctionState(state.raw, teams, mine, set(state.taken), list(state.problems))


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


def _curve_value(curve: list[float], rank: int) -> float:
    return curve[rank - 1] if 1 <= rank <= len(curve) else 0.0


def _field_base(rank: int, curve: list[float]) -> float:
    return max(float(MIN_BID), _curve_value(curve, rank))


def _curve_per_slot(
    state: AuctionState, available: list[Player], consensus_rank: dict[int, int]
    , curve: list[float]
) -> float:
    """Curve dollars per remaining league purchase, over the consensus-projected purchases.

    ``available`` must already be in consensus order. The provider curve only supplies
    relative dollar scale; every owner's purse rescales it, so this is the denominator.
    """
    if not state.open_slots:
        return float(MIN_BID)
    projected = available[: state.open_slots]
    total = sum(_field_base(consensus_rank[player.player_id], curve) for player in projected)
    return max(float(MIN_BID), total / state.open_slots)


def _legal_positions(team: Team, planned: list[Player] = ()) -> frozenset[str]:
    """Positions a purchase may add while every dedicated starter group stays fillable.

    ``planned`` are completion members treated as already bought, so a plan obeys the
    same position caps as a live purchase.
    """
    slots_left = team.slots_left - len(planned)
    if slots_left <= 0:
        return frozenset()
    counts = team.position_counts()
    for member in planned:
        counts[member.position] += 1
    owed = sum(max(0, DEDICATED_SLOTS[pos] - counts[pos]) for pos in POSITIONS)
    legal = []
    for position in POSITIONS:
        if counts[position] >= AUCTION_POSITION_TARGETS[position] + 2:
            continue
        owed_after = owed - (1 if counts[position] < DEDICATED_SLOTS[position] else 0)
        if owed_after <= slots_left - 1:
            legal.append(position)
    return frozenset(legal)


def _purchase_is_legal(team: Team, player: Player, planned: list[Player] = ()) -> bool:
    return player.position in _legal_positions(team, planned)


@dataclass(slots=True)
class _Bidder:
    """One opponent's bidding state, computed once per nomination rather than per player."""

    team: Team
    legal: frozenset[str]
    purse: float
    factor: dict[str, float]
    max_legal_bid: int


def _bidder(team: Team, curve_per_slot: float) -> _Bidder:
    counts = team.position_counts()
    owed = sum(max(0, DEDICATED_SLOTS[pos] - counts[pos]) for pos in POSITIONS)
    factor = {}
    for position in POSITIONS:
        if team.slots_left <= owed and counts[position] < DEDICATED_SLOTS[position]:
            factor[position] = 1.15
        else:
            excess_after = counts[position] + 1 - AUCTION_POSITION_TARGETS[position]
            factor[position] = 0.55**excess_after if excess_after > 0 else 1.0
    # An owner's purse rescales the market curve: dollars per open spot over curve
    # dollars per remaining purchase. Owners who are rich for what is left bid up well
    # before the end and dump the rest on their favorites; owners who spent early fill
    # with $1 players. Unspent money is not a realistic outcome, so nothing damps this.
    purse = team.remaining_budget / team.slots_left / curve_per_slot if team.slots_left else 0.0
    return _Bidder(team, _legal_positions(team), purse, factor, team.max_legal_bid)


def _stable_field_noise(state: AuctionState, team: Team, player: Player) -> float:
    key = f"{state.raw.get('draft_id')}|{team.roster_id}|{player.player_id}".encode()
    digest = blake2s(key, digest_size=16).digest()
    scale = 1 << 64
    u1 = (int.from_bytes(digest[:8], "big") + 0.5) / scale
    u2 = (int.from_bytes(digest[8:], "big") + 0.5) / scale
    normal = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return math.exp(_FIELD_NOISE_LOG_MEAN + _FIELD_NOISE_SIGMA * normal)


def _opponent_ceiling(
    player: Player,
    bidder: _Bidder,
    source_id: str,
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
) -> float:
    """The owner's median ceiling before evaluation noise; zero when he cannot buy."""
    if player.position not in bidder.legal or bidder.max_legal_bid < MIN_BID:
        return 0.0
    rank = (
        source_ranks[source_id][player.player_id]
        if source_id in source_ranks
        else consensus_rank[player.player_id]
    )
    return _field_base(rank, curve) * bidder.purse * bidder.factor[player.position]


def _noisy_bid(ceiling: float, cap: int, noise: float) -> int:
    if ceiling <= 0.0:
        return 0
    return min(cap, max(MIN_BID, math.floor(ceiling * noise + 0.5)))


def _opponent_bid(
    player: Player,
    bidder: _Bidder,
    source_id: str,
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
    noise: float,
) -> int:
    return _noisy_bid(
        _opponent_ceiling(player, bidder, source_id, source_ranks, consensus_rank, curve),
        bidder.max_legal_bid,
        noise,
    )


def _acquisition_noise() -> list[list[float]]:
    rng = random.Random(SEED)
    return [
        [rng.lognormvariate(_FIELD_NOISE_LOG_MEAN, _FIELD_NOISE_SIGMA) for _ in range(TEAMS)]
        for _ in range(_ACQUISITION_DRAWS)
    ]


_ACQUISITION_NOISE = _acquisition_noise()


def _field_price(
    player: Player,
    state: AuctionState,
    bidders: list[_Bidder],
    source_by_roster: dict[int, str],
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
) -> tuple[int, list[dict]]:
    """The open-auction price without us: one dollar above the second-highest ceiling."""
    bids = []
    for bidder in bidders:
        team = bidder.team
        source_id = source_by_roster.get(team.roster_id)
        bid = _opponent_bid(
            player,
            bidder,
            source_id or "",
            source_ranks,
            consensus_rank,
            curve,
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


def _opponent_bidders(state: AuctionState, curve_per_slot: float) -> list[_Bidder]:
    return [
        _bidder(team, curve_per_slot)
        for team in state.teams
        if not team.is_mine and team.slots_left > 0
    ]


def _acquisition_prices(
    candidates: list[Player],
    bidders: list[_Bidder],
    source_by_roster: dict[int, str],
    source_ranks: dict[str, dict[int, int]],
    consensus_rank: dict[int, int],
    curve: list[float],
) -> dict[int, int]:
    """What beating the field should cost us: one dollar above the expected highest ceiling."""
    prices: dict[int, int] = {}
    for player in candidates:
        ceilings = [
            (
                _opponent_ceiling(
                    player,
                    bidder,
                    source_by_roster[bidder.team.roster_id],
                    source_ranks,
                    consensus_rank,
                    curve,
                ),
                bidder.max_legal_bid,
            )
            for bidder in bidders
        ]
        if not any(ceiling > 0.0 for ceiling, _ in ceilings):
            prices[player.player_id] = MIN_BID
            continue
        total = 0
        for draw in _ACQUISITION_NOISE:
            total += max(
                _noisy_bid(ceiling, cap, noise)
                for (ceiling, cap), noise in zip(ceilings, draw)
            )
        prices[player.player_id] = math.floor(total / len(_ACQUISITION_NOISE) + 0.5) + MIN_BID
    return prices


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


# --- our policy: a completion planned against expected prices --------------------------


@dataclass(slots=True)
class Plan:
    """The completion worth the most at expected prices, and the money's shadow price.

    ``point_rate`` is the expected-lineup points the last discretionary dollar buys. A
    candidate's maximum bid is the price at which he and the plan's best alternative use
    of that money tie, so the bids are mutually consistent limits rather than a flat
    conversion of points into dollars.
    """

    members: list[Player]
    prices: dict[int, int]
    point_rate: float
    value: float
    cost: int

    def has(self, player_id: int) -> bool:
        return any(member.player_id == player_id for member in self.members)


def _greedy_completion(
    team: Team,
    roster: list[Player],
    slots: int,
    budget: int,
    rate: float,
    affordable: list[Player],
    prices: dict[int, int],
    wire: dict,
    bare_gain: dict[int, float],
    extra: dict[int, int],
    reserve: int,
) -> tuple[list[Player], int, bool]:
    """Members, their cost, and whether the budget ever overrode the rate's choice."""
    members: list[Player] = []
    spent = 0
    constrained = False
    pool = list(affordable)
    bound = dict(bare_gain)
    for step in range(slots):
        legal_positions = _legal_positions(team, members)
        ceiling = budget - spent - reserve * (slots - step - 1)
        legal = [player for player in pool if player.position in legal_positions]
        if not legal:
            break
        legal.sort(
            key=lambda player: (
                -(bound[player.player_id] - rate * extra[player.player_id]),
                player.player_id,
            )
        )
        best: Player | None = None
        best_score = -math.inf
        skipped_score = -math.inf
        index = 0
        while index < len(legal):
            batch = []
            while index < len(legal) and len(batch) < 6:
                player = legal[index]
                if bound[player.player_id] - rate * extra[player.player_id] <= best_score:
                    index = len(legal)
                    break
                batch.append(player)
                index += 1
            if not batch:
                break
            step_base, step_values = team_values_with_candidates(
                roster + members, wire, batch
            )
            for player in batch:
                gain = step_values[player.player_id] - step_base
                bound[player.player_id] = gain
                score = gain - rate * extra[player.player_id]
                if prices[player.player_id] > ceiling:
                    skipped_score = max(skipped_score, score)
                elif score > best_score or (
                    score == best_score and player.player_id < best.player_id
                ):
                    best, best_score = player, score
        if best is None:
            break
        if skipped_score > best_score:
            constrained = True
        members.append(best)
        spent += prices[best.player_id]
        pool.remove(best)
    return members, spent, constrained


def _plan_completion(
    team: Team,
    candidates: list[Player],
    prices: dict[int, int],
    wire: dict,
    rate_hint: float | None = None,
) -> Plan:
    """Greedy completion at a shadow price, bisected until the plan just fits the budget.

    Each step adds the candidate with the largest marginal lineup gain net of the
    discretionary dollars he costs at that rate. Spend falls as the rate rises, so the
    smallest affordable rate is the one at which the budget binds. Expected lineup value
    is submodular (a matroid's max-weight independent set, averaged over availability),
    so a candidate's gain on the bare roster bounds his gain on any completion of it and
    most candidates never need re-evaluating: the greedy is lazy.
    """
    roster = team.players
    slots = team.slots_left
    budget = team.remaining_budget
    if slots <= 0 or not candidates:
        value, _ = team_values_with_candidates(roster, wire, [])
        return Plan([], prices, 0.0, value, 0)
    # Beating even one $1 opponent bid costs $2, so a plan reserves the cheapest real
    # price, not the legal $1, for every spot it has not filled yet. Once the budget
    # cannot cover that, the reserve drops to the legal $1 and the plan goes short:
    # unplanned spots are then filled by $1 bids that win uncontested nominations.
    prices = {
        player.player_id: min(prices[player.player_id], team.max_legal_bid)
        for player in candidates
    }
    reserve = min(prices.values())
    if reserve * slots > budget:
        reserve = MIN_BID
    affordable = [
        player for player in candidates
        if prices[player.player_id] <= budget - reserve * (slots - 1)
    ]
    base, values = team_values_with_candidates(roster, wire, affordable)
    bare_gain = {player.player_id: values[player.player_id] - base for player in affordable}
    extra = {player.player_id: prices[player.player_id] - reserve for player in affordable}

    def greedy(rate: float) -> tuple[list[Player], int, bool]:
        return _greedy_completion(
            team, roster, slots, budget, rate, affordable, prices, wire, bare_gain, extra, reserve
        )

    def plan_at(rate: float, fit: tuple[list[Player], int, bool]) -> Plan:
        members, spent, _ = fit
        value, _ = team_values_with_candidates(roster + members, wire, [])
        return Plan(members, prices, rate, value, spent)

    # The smallest rate at which the budget never overrides the greedy's own choice is the
    # rate at which the budget binds. Spend falls as the rate rises, so bisect for it.
    # Spend also jumps when a star enters, so the budget-constrained plan just below that
    # rate (the star plus cheaper company) competes with the free plan just above it.
    if rate_hint:
        fit = greedy(rate_hint)
        if not fit[2] and fit[1] >= budget - _REPLAN_SLACK:
            return plan_at(rate_hint, fit)
        low, high = rate_hint / _WARM_RATE_SPREAD, rate_hint * _WARM_RATE_SPREAD
        bound_fit = greedy(low)
        while (fit := greedy(high))[2] and high < _MAX_POINT_RATE:
            low, high, bound_fit = high, high * _WARM_RATE_SPREAD, fit
        while low > 0.02 and not (lower := greedy(low))[2]:
            high, low, fit = low, low / _WARM_RATE_SPREAD, lower
            bound_fit = greedy(low)
        steps = _WARM_RATE_STEPS
    else:
        low, high = 0.0, _MAX_POINT_RATE
        fit = greedy(high)
        bound_fit = greedy(low)
        steps = _POINT_RATE_STEPS
    for _ in range(steps):
        rate = (low + high) / 2
        attempt = greedy(rate)
        if attempt[2]:
            low, bound_fit = rate, attempt
        else:
            high, fit = rate, attempt
    if fit[2]:
        # No rate lets the greedy pick freely: every expected price beats the remaining
        # dollars. The budget-constrained plan at the top rate is still a legal completion.
        high = _MAX_POINT_RATE
        fit = greedy(high)
    free = plan_at(high, fit)
    if not bound_fit[2] or bound_fit[1] > budget:
        return free
    bound = plan_at(high, bound_fit)
    return bound if bound.value > free.value else free



def _repriced(team: Team, plan: Plan, prices: dict[int, int]) -> Plan:
    """The same plan at today's prices; members above the legal maximum are still planned."""
    prices = {
        player_id: min(price, team.max_legal_bid) for player_id, price in prices.items()
    }
    return Plan(
        plan.members,
        prices,
        plan.point_rate,
        plan.value,
        sum(prices[member.player_id] for member in plan.members),
    )


def _substitute(
    team: Team, plan: Plan, gone_id: int, candidates: list[Player], wire: dict
) -> Plan:
    """Refill one lost plan spot at the plan's shadow price without moving the price."""
    others = [member for member in plan.members if member.player_id != gone_id]
    member_ids = {member.player_id for member in others}
    spare = team.remaining_budget - sum(plan.prices[member.player_id] for member in others)
    legal_positions = _legal_positions(team, others)
    options = [
        candidate for candidate in candidates
        if candidate.player_id not in member_ids
        and candidate.position in legal_positions
        and plan.prices[candidate.player_id] <= spare
    ]
    roster = team.players + others
    base, values = team_values_with_candidates(roster, wire, options)
    if not options:
        return Plan(others, plan.prices, plan.point_rate, base, sum(plan.prices[m.player_id] for m in others))
    chosen = max(
        options,
        key=lambda player: (
            values[player.player_id] - plan.point_rate * (plan.prices[player.player_id] - MIN_BID),
            -player.player_id,
        ),
    )
    members = others + [chosen]
    return Plan(
        members,
        plan.prices,
        plan.point_rate,
        values[chosen.player_id],
        sum(plan.prices[member.player_id] for member in members),
    )


def _plan_max_bid(
    team: Team, player: Player, plan: Plan, candidates: list[Player], wire: dict
) -> int:
    """The price at which this player and the plan's best alternative use of the money tie."""
    if player.position not in _legal_positions(team):
        return 0
    rate = max(plan.point_rate, 1e-9)
    roster = team.players
    member_ids = {member.player_id for member in plan.members}
    if player.player_id in member_ids:
        others = [member for member in plan.members if member.player_id != player.player_id]
        legal_positions = _legal_positions(team, others)
        substitutes = [
            candidate for candidate in candidates
            if candidate.player_id not in member_ids and candidate.position in legal_positions
        ]
        base, values = team_values_with_candidates(roster + others, wire, substitutes)
        # The best replacement plan, net of what it costs; with no replacement the spot
        # simply stays empty.
        alternative = max(
            (values[s.player_id] - rate * plan.prices[s.player_id] for s in substitutes),
            default=base,
        )
        bid = (plan.value - alternative) / rate
    else:
        bid = 0.0
        if len(plan.members) < team.slots_left and player.position in _legal_positions(
            team, plan.members
        ):
            # An unplanned spot has no alternative use, so any improvement is worth the
            # $1 that wins an uncontested nomination.
            _, values = team_values_with_candidates(roster + plan.members, wire, [player])
            improvement = values[player.player_id] - plan.value
            if improvement > 0:
                bid = max(float(MIN_BID), improvement / rate)
        for member in plan.members:
            others = [other for other in plan.members if other is not member]
            if player.position not in _legal_positions(team, others):
                continue
            _, values = team_values_with_candidates(roster + others, wire, [player])
            bid = max(
                bid,
                plan.prices[member.player_id] + (values[player.player_id] - plan.value) / rate,
            )
    return min(team.max_legal_bid, max(0, math.floor(bid + 0.5)))


def _plan_rows(team: Team, plan: Plan, wire: dict) -> list[dict]:
    roster = team.players
    rows = []
    for member in plan.members:
        others = [other for other in plan.members if other is not member]
        without, _ = team_values_with_candidates(roster + others, wire, [])
        rows.append(
            {
                "player_id": member.player_id,
                "name": member.name,
                "position": member.position,
                "points_1yr": member.points_1yr,
                "lineup_gain": round(plan.value - without, 1),
                "expected_price": plan.prices[member.player_id],
            }
        )
    return rows


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


def _rollout_teams(
    state: AuctionState, sim_state: AuctionState, wire: dict
) -> list[dict]:
    """Complete teams from one rollout, including purchases already made live."""
    live_counts = {team.roster_id: len(team.purchases) for team in state.teams}
    teams = []
    for team in sorted(sim_state.teams, key=lambda item: item.roster_id):
        starter_ids = _nominal_starters(team.players)
        expected_points, _ = team_values_with_candidates(team.players, wire, [])
        players = []
        for index, purchase in enumerate(team.purchases):
            player = purchase.player
            players.append(
                {
                    "player_id": player.player_id if player else None,
                    "name": player.name if player else purchase.name,
                    "position": purchase.position,
                    "nfl_team": player.team if player else purchase.nfl_team,
                    "amount": purchase.amount,
                    "points_1yr": round(player.points_1yr, 1) if player else None,
                    "role": (
                        "starter"
                        if player and player.player_id in starter_ids
                        else "bench"
                    ),
                    "is_simulated": index >= live_counts[team.roster_id],
                    "off_pool": player is None,
                }
            )
        teams.append(
            {
                "roster_id": team.roster_id,
                "team": team.team_name or team.username or f"Roster {team.roster_id}",
                "is_mine": team.is_mine,
                "spent": team.spent,
                "remaining_budget": team.remaining_budget,
                "projected_starter_points_1yr": round(
                    sum(
                        player.points_1yr
                        for player in team.players
                        if player.player_id in starter_ids
                    ),
                    1,
                ),
                "expected_lineup_points_1yr": round(expected_points, 1),
                "starter_spend": sum(
                    purchase.amount
                    for purchase in team.purchases
                    if purchase.player
                    and purchase.player.player_id in starter_ids
                ),
                "bench_spend": sum(
                    purchase.amount
                    for purchase in team.purchases
                    if not purchase.player
                    or purchase.player.player_id not in starter_ids
                ),
                "positions": team.position_counts(),
                "players": players,
            }
        )
    return teams


# --- full-auction rollouts ------------------------------------------------------------


@dataclass(slots=True)
class _RolloutContext:
    state: AuctionState
    candidates: list[Player]
    wire: dict
    curve: list[float]
    source_ranks: dict[str, dict[int, int]]
    consensus_rank: dict[int, int]
    source_by_roster: dict[int, str]
    matched_rosters: set[int]
    plan: Plan
    # Realized-over-static acquisition price per player, learned from a first rollout
    # pass: a static estimate cannot see that a player nominated later meets owners who
    # have filled his position or spent down. It fades as a rollout's own state matures.
    price_ratio: dict[int, float]


def _learned_prices(
    static: dict[int, int], ratio: dict[int, float], remaining_fraction: float
) -> dict[int, int]:
    return {
        player_id: max(MIN_BID, math.floor(price * ratio.get(player_id, 1.0) ** remaining_fraction + 0.5))
        for player_id, price in static.items()
    }


_ROLLOUT_CONTEXT: _RolloutContext | None = None


def _init_rollout_worker(context: _RolloutContext) -> None:
    global _ROLLOUT_CONTEXT
    _ROLLOUT_CONTEXT = context
    if hasattr(os, "nice"):
        os.nice(10)


def _rollout_worker(simulation: int) -> dict:
    assert _ROLLOUT_CONTEXT is not None
    return _run_rollout(_ROLLOUT_CONTEXT, simulation)


def _run_rollout(context: _RolloutContext, simulation: int) -> dict:
    """One complete auction from the live state under a fixed per-rollout seed."""
    rng = random.Random(SEED + simulation)
    state = _copy_state(context.state)
    mine = state.mine
    consensus_rank = context.consensus_rank
    source_ids = sorted(context.source_ranks)
    source_by_roster = dict(context.source_by_roster)
    for team in state.teams:
        if not team.is_mine and team.roster_id not in context.matched_rosters:
            source_by_roster[team.roster_id] = rng.choice(source_ids)
    # Top players normally surface earlier, but nomination order has substantial room
    # for price-enforcement nominations and personal favorites.
    nominations = sorted(
        context.candidates,
        key=lambda player: (
            consensus_rank[player.player_id] + rng.gauss(0.0, 36.0),
            player.player_id,
        ),
    )
    available = sorted(
        context.candidates, key=lambda player: (consensus_rank[player.player_id], player.player_id)
    )

    initial_open = context.state.open_slots

    def reprice(players: list[Player]) -> dict[int, int]:
        per_slot = _curve_per_slot(state, available, consensus_rank, context.curve)
        static = _acquisition_prices(
            players, _opponent_bidders(state, per_slot), source_by_roster,
            context.source_ranks, consensus_rank, context.curve,
        )
        return _learned_prices(static, context.price_ratio, state.open_slots / initial_open)

    def replan(previous: Plan) -> Plan:
        return _plan_completion(
            mine, available, reprice(available), context.wire, previous.point_rate
        )

    plan = context.plan
    planned_cost = plan.cost
    closing: dict[int, int] = {}
    acquisition: dict[int, int] = {}
    mine_prices: dict[int, int] = {}
    considered: set[int] = set()
    affordable: set[int] = set()

    for pick_no, player in enumerate(nominations, start=context.state.picks_made + 1):
        if not state.open_slots:
            break
        curve_per_slot = _curve_per_slot(state, available, consensus_rank, context.curve)
        bids: list[tuple[int, float, Team]] = []
        my_bid = 0
        if mine.slots_left:
            my_bid = _plan_max_bid(mine, player, plan, available, context.wire)
            if my_bid:
                considered.add(player.player_id)
                bids.append((my_bid, rng.random(), mine))
        highest_opponent = 0
        for bidder in _opponent_bidders(state, curve_per_slot):
            bid = _opponent_bid(
                player,
                bidder,
                source_by_roster[bidder.team.roster_id],
                context.source_ranks,
                consensus_rank,
                context.curve,
                rng.lognormvariate(_FIELD_NOISE_LOG_MEAN, _FIELD_NOISE_SIGMA),
            )
            if bid:
                highest_opponent = max(highest_opponent, bid)
                bids.append((bid, rng.random(), bidder.team))
        if _purchase_is_legal(mine, player):
            acquisition[player.player_id] = highest_opponent + MIN_BID
        if my_bid and my_bid >= highest_opponent + MIN_BID:
            affordable.add(player.player_id)
        if not bids:
            continue
        bids.sort(key=lambda item: (-item[0], item[1]))
        ceiling, _, winner = bids[0]
        price = MIN_BID if len(bids) == 1 else min(ceiling, bids[1][0] + MIN_BID)
        closing[player.player_id] = price
        winner.purchases.append(
            Purchase(
                pick_no, player.sleeper_id, player.name, player.position, player.team, price, player
            )
        )
        available.remove(player)
        state.taken.add(player.player_id)
        if winner.is_mine:
            mine_prices[player.player_id] = price
        if not mine.slots_left:
            continue
        # Every purchase moves the market. Bids consume the planned players' prices, so
        # those are refreshed after each one; the whole market is repriced and the plan
        # redrawn when that matters: we bought, a planned player is gone, or the plan
        # no longer costs what it planned.
        if winner.is_mine:
            plan = replan(plan)
            planned_cost = plan.cost
            continue
        if plan.has(player.player_id):
            plan = _substitute(mine, plan, player.player_id, available, context.wire)
        plan = _repriced(mine, plan, {**plan.prices, **reprice(plan.members)})
        if abs(plan.cost - planned_cost) > _REPLAN_SLACK or plan.cost > mine.remaining_budget:
            plan = replan(plan)
            planned_cost = plan.cost

    # Sleeper autodrafts a roster that fails to fill. Nothing should reach this; it is
    # counted so a policy that passes on everything fails validation instead of hiding.
    autofilled: list[str] = []
    for team in state.teams:
        while team.slots_left > 0:
            player = next((p for p in available if _purchase_is_legal(team, p)), None)
            if player is None:
                break
            team.purchases.append(
                Purchase(0, player.sleeper_id, player.name, player.position, player.team, MIN_BID, player)
            )
            available.remove(player)
            state.taken.add(player.player_id)
            autofilled.append(
                f"rollout {simulation + 1} roster {team.roster_id} {player.position} "
                f"with ${team.remaining_budget + MIN_BID}"
            )

    valid = state.open_slots == 0
    pathological = 0
    position_max = {position: 0 for position in POSITIONS}
    for team in state.teams:
        counts = team.position_counts()
        for position, count in counts.items():
            position_max[position] = max(position_max[position], count)
            if count > AUCTION_POSITION_TARGETS[position] + 2:
                pathological += 1
        valid = valid and team.spent <= team.budget
        valid = valid and all(
            counts[position] >= DEDICATED_SLOTS[position] for position in POSITIONS
        )
    result = {
        "simulation": simulation + 1,
        "valid": valid,
        "pathological": pathological,
        "position_max": position_max,
        "autofilled": autofilled,
        "closing": closing,
        "acquisition": acquisition,
        "mine_prices": mine_prices,
        "considered": considered,
        "affordable": affordable,
    }
    if not valid:
        return result
    value, _ = team_values_with_candidates(mine.players, context.wire, [])
    starter_ids = _nominal_starters(mine.players)
    starters = [player for player in mine.players if player.player_id in starter_ids]
    starter_value, _ = team_values_with_candidates(starters, context.wire, [])
    teams = _rollout_teams(context.state, state, context.wire)
    result.update(
        {
            "value": value,
            "spent": mine.spent,
            "remaining_budget": mine.remaining_budget,
            "nominal_starter_points": sum(player.points_yr1 for player in starters),
            "depth_lineup_points": max(0.0, value - starter_value),
            "position_counts": mine.position_counts(),
            "roster": [
                {
                    "player_id": purchase.player.player_id,
                    "name": purchase.player.name,
                    "position": purchase.player.position,
                    "amount": purchase.amount,
                    "role": (
                        "starter" if purchase.player.player_id in starter_ids else "bench"
                    ),
                }
                for purchase in mine.purchases[len(context.state.mine.purchases) :]
                if purchase.player is not None
            ],
            "teams": teams,
            "rank": 1 + sum(
                1 for team in teams
                if not team["is_mine"] and team["expected_lineup_points_1yr"] > round(value, 1)
            ),
            "opponent_unused": [
                team["remaining_budget"] for team in teams if not team["is_mine"]
            ],
        }
    )
    return result


def _stats(values: list, digits: int = 1) -> dict:
    if not values:
        return {"mean": None, "low": None, "high": None}
    return {
        "mean": round(statistics.mean(values), digits),
        "low": round(min(values), digits),
        "high": round(max(values), digits),
    }


def _simulate_auctions(context: _RolloutContext) -> tuple[dict, dict[int, dict]]:
    """Roll out complete auctions under uncertain nominations and field evaluations."""
    workers = min(_AUCTION_SIMULATIONS, _MAX_WORKERS, os.cpu_count() or 1)
    if workers > 1:
        with Pool(workers, initializer=_init_rollout_worker, initargs=(context,)) as pool:
            results = pool.map(_rollout_worker, range(_AUCTION_SIMULATIONS), chunksize=1)
    else:
        results = [_run_rollout(context, simulation) for simulation in range(_AUCTION_SIMULATIONS)]

    candidates = context.candidates
    closing = {player.player_id: [] for player in candidates}
    acquisition = {player.player_id: [] for player in candidates}
    wins = {player.player_id: [] for player in candidates}
    considered = {player.player_id: 0 for player in candidates}
    affordable = {player.player_id: 0 for player in candidates}
    league_position_max = {position: 0 for position in POSITIONS}
    pathological_rosters = 0
    autofilled: list[str] = []
    outcomes = []
    for result in results:
        for player_id, price in result["closing"].items():
            closing[player_id].append(price)
        for player_id, price in result["acquisition"].items():
            acquisition[player_id].append(price)
        for player_id, price in result["mine_prices"].items():
            wins[player_id].append(price)
        for player_id in result["considered"]:
            considered[player_id] += 1
        for player_id in result["affordable"]:
            affordable[player_id] += 1
        for position, count in result["position_max"].items():
            league_position_max[position] = max(league_position_max[position], count)
        pathological_rosters += result["pathological"]
        autofilled.extend(result["autofilled"])
        if result["valid"]:
            outcomes.append(result)

    representative = None
    if outcomes:
        median_value = statistics.median(outcome["value"] for outcome in outcomes)
        representative = min(outcomes, key=lambda outcome: abs(outcome["value"] - median_value))

    player_results = {}
    for player in candidates:
        player_id = player.player_id
        player_results[player_id] = {
            "simulated_roster_rate": round(len(wins[player_id]) / _AUCTION_SIMULATIONS, 3),
            "simulated_affordable_rate": (
                round(affordable[player_id] / considered[player_id], 3)
                if considered[player_id]
                else 0.0
            ),
            "simulated_price_low": _percentile(closing[player_id], 0.1) if closing[player_id] else None,
            "simulated_price_median": _percentile(closing[player_id], 0.5) if closing[player_id] else None,
            "simulated_price_high": _percentile(closing[player_id], 0.9) if closing[player_id] else None,
            "simulated_acquisition_price": (
                _percentile(acquisition[player_id], 0.5) if acquisition[player_id] else None
            ),
            "simulated_purchase_price": (
                round(statistics.mean(wins[player_id]), 1) if wins[player_id] else None
            ),
        }

    opponent_unused = [dollars for outcome in outcomes for dollars in outcome["opponent_unused"]]
    summary = {
        "simulations": _AUCTION_SIMULATIONS,
        "completed": len(outcomes),
        "my_projected_lineup_points": _stats([outcome["value"] for outcome in outcomes]),
        "my_expected_points_rank": _stats([outcome["rank"] for outcome in outcomes], 2),
        "my_spend": _stats([outcome["spent"] for outcome in outcomes]),
        "my_unused_budget": _stats([outcome["remaining_budget"] for outcome in outcomes]),
        "opponent_unused_budget": {
            **_stats(opponent_unused),
            "share_at_least_10": (
                round(sum(1 for dollars in opponent_unused if dollars >= 10) / len(opponent_unused), 3)
                if opponent_unused
                else None
            ),
        },
        "my_nominal_starter_points": _stats(
            [outcome["nominal_starter_points"] for outcome in outcomes]
        ),
        "my_depth_lineup_points": _stats([outcome["depth_lineup_points"] for outcome in outcomes]),
        "my_position_ranges": {
            position: {
                "low": min((o["position_counts"][position] for o in outcomes), default=None),
                "high": max((o["position_counts"][position] for o in outcomes), default=None),
            }
            for position in POSITIONS
        },
        "largest_simulated_position_counts": league_position_max,
        "pathological_rosters": pathological_rosters,
        "autofilled_slots": len(autofilled),
        "autofilled_detail": autofilled[:8],
        "representative_remaining_budget": (
            representative["remaining_budget"] if representative else None
        ),
        "representative_nominal_starter_points": (
            round(representative["nominal_starter_points"], 1) if representative else None
        ),
        "representative_depth_lineup_points": (
            round(representative["depth_lineup_points"], 1) if representative else None
        ),
        "representative_simulation": representative["simulation"] if representative else None,
        "representative_completion": representative["roster"] if representative else [],
        "representative_teams": representative["teams"] if representative else [],
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
    curve_per_slot = _curve_per_slot(state, all_available, consensus_rank, curve)
    remaining_dollars = sum(team.remaining_budget for team in state.teams)
    inflation = (
        remaining_dollars / state.open_slots / curve_per_slot if state.open_slots else 1.0
    )
    source_by_roster, matched_rosters = _source_by_roster(
        state, matches, sorted(source_ranks)
    )
    bidders = _opponent_bidders(state, curve_per_slot)
    static_prices = _acquisition_prices(
        candidates, bidders, source_by_roster, source_ranks, consensus_rank, curve
    )
    discretionary = max(
        0, state.mine.remaining_budget - MIN_BID * state.mine.slots_left
    )

    def board(prices: dict[int, int], price_ratio: dict[int, float]):
        plan = _plan_completion(state.mine, candidates, prices, wire)
        max_bids = {
            player.player_id: _plan_max_bid(state.mine, player, plan, candidates, wire)
            for player in candidates
        }
        simulation, simulated_players = _simulate_auctions(
            _RolloutContext(
                state,
                candidates,
                wire,
                curve,
                source_ranks,
                consensus_rank,
                source_by_roster,
                matched_rosters,
                plan,
                price_ratio,
            )
        )
        return plan, max_bids, simulation, simulated_players

    # Pass one rolls the auction out at static prices and records what beating the field
    # actually cost at each player's nomination; pass two plans and bids with that.
    _, _, _, first_pass = board(static_prices, {})
    price_ratio = {}
    for player in candidates:
        observed = first_pass[player.player_id]["simulated_acquisition_price"]
        if observed is not None:
            price_ratio[player.player_id] = min(
                2.0, max(0.2, observed / static_prices[player.player_id])
            )
    prices = _learned_prices(static_prices, price_ratio, 1.0)
    plan, max_bids, simulation, simulated_players = board(prices, price_ratio)

    provisional = []
    for player in candidates:
        max_bid = max_bids[player.player_id]
        field_price, top_bidders = _field_price(
            player,
            state,
            bidders,
            source_by_roster,
            source_ranks,
            consensus_rank,
            curve,
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
                "expected_price": prices[player.player_id],
                "static_price": static_prices[player.player_id],
                "in_plan": plan.has(player.player_id),
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
    if simulation["autofilled_slots"]:
        problems.append(
            f"simulated auctions autofilled {simulation['autofilled_slots']} roster spots "
            f"that no bidding policy filled: {'; '.join(simulation['autofilled_detail'])}"
        )
    if state.mine.slots_left and len(plan.members) != state.mine.slots_left:
        problems.append(
            f"completion plan holds {len(plan.members)} players for "
            f"{state.mine.slots_left} open spots"
        )
    if plan.cost > state.mine.remaining_budget:
        problems.append("completion plan costs more than the remaining budget")
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
            "curve_dollars_per_slot": round(curve_per_slot, 2),
            "matched_opponent_sources": len(matched_rosters),
            "cold_start_opponent_sources": len(source_by_roster) - len(matched_rosters),
            "wire": {h: {p: round(v, 1) for p, v in levels.items()} for h, levels in wire.items()},
            "simulation": simulation,
            "pricing_note": (
                "The completion plan is the roster that adds the most expected-lineup "
                "points at expected prices (one dollar above the strongest modeled opponent "
                "ceiling), found by bisecting the shadow price of a discretionary dollar until "
                "the plan just fits the budget. A maximum bid is the price at which the player "
                "and the plan's best alternative use of that money tie. Every bid still "
                "reserves $1 for every other open slot. Field price is one dollar above the "
                "second-highest legal opponent ceiling, capped by the highest; each opponent "
                "scales the market curve by its own purse, so owners who are rich for what is "
                "left bid up and spend out."
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
            "points_per_discretionary_dollar": round(plan.point_rate, 2),
            "completion_gain": round(max(0.0, plan.value - base), 1),
            "completion_cost": plan.cost,
            "completion_value": round(plan.value, 1),
            "completion_plan": _plan_rows(state.mine, plan, wire),
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
                "in which the plan-driven bidding policy actually acquired the player; "
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
    plan_rows = result["my_auction"]["completion_plan"]
    if len(plan_rows) != ROSTER_SLOTS:
        problems.append("completion plan does not fill every open roster spot")
    plan_cost = sum(row["expected_price"] for row in plan_rows)
    if not AUCTION_BUDGET - 12 <= plan_cost <= AUCTION_BUDGET:
        problems.append(f"completion plan costs ${plan_cost}, not roughly the full budget")
    if not 1.0 <= result["my_auction"]["points_per_discretionary_dollar"] <= 12.0:
        problems.append("the plan's shadow price is outside any plausible market rate")
    planned = {row["player_id"] for row in plan_rows}
    # A greedy plan is not swap-optimal, so a member prices below his expected cost when
    # a better substitute exists once the rest of the plan is in; most must not.
    underpriced = [
        row["name"] for row in result["rankings"]
        if row["in_plan"] and row["max_bid"] < row["expected_price"]
    ]
    if len(underpriced) > len(plan_rows) // 2:
        problems.append(f"most planned players price below their expected cost: {underpriced}")
    if not any(row["in_plan"] for row in result["rankings"]) or planned != {
        row["player_id"] for row in result["rankings"] if row["in_plan"]
    }:
        problems.append("board rows do not flag exactly the planned players")
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
    if simulation["autofilled_slots"]:
        problems.append("selftest auction simulations needed autofilled roster spots")
    if simulation["my_unused_budget"]["mean"] > 8:
        problems.append("auction policy strands more than $8 on average")
    if simulation["opponent_unused_budget"]["mean"] > 3:
        problems.append("modeled opponents strand more than $3 on average")
    if simulation["my_expected_points_rank"]["mean"] > 4:
        problems.append("our policy finishes outside the top four on expected points on average")
    roles = [row["role"] for row in simulation["representative_completion"]]
    if roles.count("starter") != sum(STARTING_SLOTS.values()):
        problems.append("representative completion does not identify nine nominal starters")
    if roles.count("bench") != ROSTER_SLOTS - sum(STARTING_SLOTS.values()):
        problems.append("representative completion does not identify five bench players")
    representative_teams = simulation["representative_teams"]
    if len(representative_teams) != TEAMS:
        problems.append("representative rollout does not contain all twelve teams")
    for team in representative_teams:
        if len(team["players"]) != ROSTER_SLOTS:
            problems.append(
                f"representative roster {team['roster_id']} does not contain fourteen players"
            )
        if sum(player["amount"] for player in team["players"]) != team["spent"]:
            problems.append(
                f"representative roster {team['roster_id']} purchase prices do not match spend"
            )
        if team["spent"] + team["remaining_budget"] != AUCTION_BUDGET:
            problems.append(
                f"representative roster {team['roster_id']} budget does not balance"
            )
        projected_points = round(
            sum(
                player["points_1yr"] or 0.0
                for player in team["players"]
                if player["role"] == "starter"
            ),
            1,
        )
        if projected_points != team["projected_starter_points_1yr"]:
            problems.append(
                f"representative roster {team['roster_id']} starter projections do not add up"
            )
        starter_spend = sum(
            player["amount"]
            for player in team["players"]
            if player["role"] == "starter"
        )
        bench_spend = sum(
            player["amount"]
            for player in team["players"]
            if player["role"] == "bench"
        )
        if (starter_spend, bench_spend) != (
            team["starter_spend"],
            team["bench_spend"],
        ):
            problems.append(
                f"representative roster {team['roster_id']} role spending does not add up"
            )
        if team["expected_lineup_points_1yr"] < projected_points:
            problems.append(
                f"representative roster {team['roster_id']} backups reduced expected points"
            )
        roles = [player["role"] for player in team["players"]]
        if roles.count("starter") != sum(STARTING_SLOTS.values()):
            problems.append(
                f"representative roster {team['roster_id']} does not identify nine starters"
            )
        if roles.count("bench") != ROSTER_SLOTS - sum(STARTING_SLOTS.values()):
            problems.append(
                f"representative roster {team['roster_id']} does not identify five bench players"
            )
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
    rollout_mine = next(
        team
        for team in purchased_result["analysis"]["simulation"]["representative_teams"]
        if team["is_mine"]
    )
    live_rows = [
        player for player in rollout_mine["players"] if not player["is_simulated"]
    ]
    if len(live_rows) != 1 or live_rows[0]["player_id"] != bought.player_id:
        problems.append("representative rollout did not distinguish the made purchase")
    if len(purchased_result["my_auction"]["completion_plan"]) != ROSTER_SLOTS - 1:
        problems.append("a made purchase did not shorten the completion plan by one spot")
    empty_rows = {row["player_id"]: row for row in result["rankings"]}
    same_position = [
        row
        for row in purchased_result["rankings"]
        if row["position"] == bought.position and row["player_id"] in empty_rows
    ][:5]
    if not any(
        row["max_bid"] < empty_rows[row["player_id"]]["max_bid"] for row in same_position
    ):
        problems.append("a purchase did not reduce the next same-position maximum bids")

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
    planned_team = Team(98, None, None, True, AUCTION_BUDGET)
    for pick_no, player in enumerate([one_qb, *two_rbs], start=1):
        planned_team.purchases.append(
            Purchase(pick_no, player.sleeper_id, player.name, player.position, player.team, 1, player)
        )
    if _purchase_is_legal(planned_team, extra_receiver, ten_wrs[:8]):
        problems.append("a plan was allowed to exceed the receiver position cap")
    return problems
