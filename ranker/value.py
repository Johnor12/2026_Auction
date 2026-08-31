"""Expected lineup points and wire-level measurement.

The provider's cumulative projections are split into year 1 and years 2-3. A roster is
valued separately in both horizons, then the results are added. Within one horizon the
objective is the expectation, over position-wide Bernoulli availability, of the best
legal lineup each week: the composition is re-chosen per availability draw, so a flex
job vacated by an unavailable RB can be refilled by the best remaining body at any flex
position. (The previous solver locked one composition per horizon and let only
same-position depth cover its jobs — max-of-expectations instead of
expectation-of-max — which overvalued depth behind many locked slots.)

The weekly optimum decomposes exactly: each position fills its dedicated slots with its
best available bodies, then the FLEX+SF seats go to the best pooled leftovers, of which
at most one (the superflex) may be a QB. With F flex seats and one superflex that is
    dedicated tops  +  top-F pooled non-QB marginals  +  max(next marginal, backup QB).
Each expectation is computed in closed form: the dedicated term by a small Bernoulli
cascade per position, the pooled terms by layer-cake integrals of the marginal-count
distribution over value thresholds (positions are independent, so the pooled count is a
tiny convolution). The best waiver player at each position is inserted once as an
always-available body, so one free agent can fill one lineup job, never several
simultaneous holes.

This is one objective with one set of units: expected lineup points. There is no role
threshold and no separate bench bonus. A better projection can retain every role a worse
projection could fill, which makes roster value monotone when a player improves, is
replaced by a better same-position player, or is simply added.

The position-wide unavailability rate combines injury risk with one independently
averaged bye in 18 weeks. Team-specific bye correlations are not available in pool.json.
"""

from __future__ import annotations

from functools import lru_cache

from .league import (
    DEDICATED_SLOTS,
    HORIZON,
    POSITIONS,
    SLOT_CHAIN,
    STARTING_SLOTS,
    TEAMS,
    UNAVAILABLE_RATE,
)
from .pool import Player, by_position

# The two value horizons the provider's cumulative projections can distinguish:
# year 1, and years 2-3 as one block. Every wire-level dict in the ranker is keyed by
# horizon first, position second.
HORIZONS = ("yr1", "yr23")
# pool.py sets points == points_1yr under league.HORIZON == "1yr", so the years-2-3
# horizon is structurally zero here and the lineup solver skips it; wire dicts still
# carry both keys for the legacy snake modules.
ACTIVE_HORIZONS = ("yr1",) if HORIZON == "1yr" else HORIZONS


def horizon_points(p: Player, h: str) -> float:
    """A player's points in one horizon (precomputed on Player — see pool.py)."""
    return p.points_yr1 if h == "yr1" else p.points_yr23


# Sort keys per horizon, bound once: the lineup solver runs inside every marginal-value
# evaluation, so per-call lambda construction and string dispatch are worth avoiding.
_HKEY = {
    "yr1": lambda q: (-q.points_yr1, q.player_id),
    "yr23": lambda q: (-q.points_yr23, q.player_id),
}


def pos_by_horizon(players: list[Player]) -> dict[str, dict[str, list[Player]]]:
    """Per-horizon position lists, each sorted by that horizon's points descending."""
    pos = by_position(players)
    return {
        h: {k: sorted(v, key=_HKEY[h]) for k, v in pos.items()} for h in HORIZONS
    }


def sorted_by_horizon(roster: list[Player]) -> dict[str, list[Player]]:
    """A roster pre-sorted per horizon — the form `team_value` consumes.

    Valuing a candidate means re-valuing the roster with him added, thousands of times
    per pick, and re-sorting 25 players to place 1 was the single largest cost in the
    simulation. So the roster is sorted once per pick and each candidate is merged in
    by `insert_sorted`.
    """
    return {h: sorted(roster, key=_HKEY[h]) for h in HORIZONS}


def insert_sorted(seq: list[Player], p: Player, h: str) -> list[Player]:
    """A new list with `p` merged into an already-sorted horizon list."""
    points = horizon_points(p, h)
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        q = seq[mid]
        q_points = horizon_points(q, h)
        if q_points > points or (q_points == points and q.player_id < p.player_id):
            lo = mid + 1
        else:
            hi = mid
    out = seq.copy()
    out.insert(lo, p)
    return out


# --- expected lineup solver -------------------------------------------------------

# The position-flexible starting seats beyond the dedicated slots. The closed forms in
# `_extra_expected_value` are specialized to this league's exactly one FLEX seat and one
# superflex seat; it is the hottest arithmetic in the simulator.
_FLEX_SLOTS = STARTING_SLOTS["FLEX"]
_EXTRA_SLOTS = _FLEX_SLOTS + STARTING_SLOTS["SF"]
assert STARTING_SLOTS["SF"] == 1
assert _FLEX_SLOTS == 1


@lru_cache(maxsize=32_768)
def _position_expected_values(
    projections: tuple[tuple[int, float], ...],
    wire_points: float,
    unavailable: float,
    max_starters: int,
) -> tuple[float, ...]:
    """All starter-count values for one depth chart; cached across candidate branches."""
    available = 1.0 - unavailable
    entries = [
        (points / available, points, available, player_id)
        for player_id, points in projections
    ]
    # One actual free agent, not an unlimited scalar that can fill every open slot.
    entries.append((wire_points, wire_points, 1.0, -1))
    entries.sort(key=lambda row: (-row[0], row[3]))

    out = [0.0]
    for starter_count in range(1, max_starters + 1):
        # dist[k] = P(exactly k higher bodies are active), with the final cell
        # collecting saturated states that cannot leave a job for a lower body.
        dist = [1.0] + [0.0] * starter_count
        total = 0.0
        for _, expected_points, active_probability, _ in entries:
            total += expected_points * sum(dist[:starter_count])
            next_dist = [0.0] * (starter_count + 1)
            for active_higher, probability in enumerate(dist):
                next_dist[active_higher] += probability * (1.0 - active_probability)
                next_dist[min(active_higher + 1, starter_count)] += (
                    probability * active_probability
                )
            dist = next_dist
        out.append(total)
    return tuple(out)


def position_expected_value(
    players: list[Player], wire_points: float, starter_count: int, h: str
) -> float:
    """Expected points from one position with a unique, always-available wire body.

    Player projections are unconditional. Dividing by availability orders players by
    their points when active; multiplying that active rate by their own availability
    returns the original projection. The running Bernoulli distribution supplies the
    probability that fewer than ``starter_count`` higher bodies are active, which is the
    probability this depth-chart entry is called on.
    """
    unavailable = UNAVAILABLE_RATE[players[0].position] if players else 0.0
    projections = tuple(sorted((p.player_id, horizon_points(p, h)) for p in players))
    return _position_expected_values(
        projections, wire_points, unavailable, starter_count
    )[starter_count]


@lru_cache(maxsize=1024)
def _binomial_pmf(k: int, p: float) -> tuple[float, ...]:
    """P(j of k iid bodies are available), j = 0..k."""
    pmf = [1.0]
    for _ in range(k):
        nxt = [0.0] * (len(pmf) + 1)
        for j, prob in enumerate(pmf):
            nxt[j] += prob * (1.0 - p)
            nxt[j + 1] += prob * p
        pmf = nxt
    return tuple(pmf)


@lru_cache(maxsize=32_768)
def _extra_count_table(
    projections: tuple[tuple[int, float], ...],
    wire_points: float,
    unavailable: float,
    dedicated: int,
    cap: int,
) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    """Piecewise law of this position's marginal-body count above a value threshold.

    A marginal body is an available body ranked below the position's top-`dedicated`
    available ones — what a flex seat could use. Only bodies sorted at index >=
    `dedicated` can ever be marginal, so the law changes only at their values.
    Returns (breaks, rows), breaks ascending: rows[i] applies for thresholds in
    [breaks[i-1] (or 0), breaks[i]) and gives P(count = 0..cap), last cell >= cap.
    Past the last break the count is surely zero (omitted).
    """
    available = 1.0 - unavailable
    ws = [points / available for _, points in projections if points > 0]
    ws.append(wire_points)  # the unique always-available wire body
    ws.sort(reverse=True)
    breaks = sorted({w for w in ws[dedicated:] if w > 0})
    wire_w = wire_points
    rows = []
    for b in breaks:
        players_above = sum(1 for w in ws if w >= b) - (1 if wire_w >= b else 0)
        wire_above = 1 if wire_w >= b else 0
        row = [0.0] * (cap + 1)
        for j, prob in enumerate(_binomial_pmf(players_above, available)):
            row[min(max(j + wire_above - dedicated, 0), cap)] += prob
        rows.append(tuple(row))
    return tuple(breaks), tuple(rows)


@lru_cache(maxsize=65_536)
def _extra_expected_value(
    qb_table: tuple,
    flex_tables: tuple[tuple, ...],
) -> float:
    """E[points from the FLEX+SF seats], by layer-cake integrals over thresholds.

    Weekly, those seats take the top pooled non-QB marginal plus the better of the
    next marginal and the backup QB (the single superflex seat). With N(>t) the pooled
    marginal count — independent across positions, so a small capped convolution — and
    F_q(t) = P(no QB marginal above t):
        E[top marginal]     = integral of P(N(>t) >= 1)
        E[max(next, qb)]    = integral of 1 - P(N(>t) <= 1) * F_q(t)
    Both integrands are piecewise constant between body values and zero past the last.
    Only pooled cells 0 and 1 are needed, so the three-position convolution is unrolled
    to two running cells: this league has exactly one FLEX seat (asserted above), and
    this function is the hottest arithmetic in the simulator.
    """
    qb_brks, qb_rows = qb_table
    (brk0, row0), (brk1, row1), (brk2, row2) = flex_tables
    n0, n1, n2, nq = len(brk0), len(brk1), len(brk2), len(qb_brks)
    i0 = i1 = i2 = iq = 0
    total = 0.0
    prev = 0.0
    for x in sorted({*qb_brks, *brk0, *brk1, *brk2}):
        while iq < nq and qb_brks[iq] < x:
            iq += 1
        f_q = qb_rows[iq][0] if iq < nq else 1.0
        # pooled cells: P(no marginal above x) and P(exactly one); a table past its last
        # break contributes surely-zero marginals and leaves the cells unchanged.
        p0, p1 = 1.0, 0.0
        while i0 < n0 and brk0[i0] < x:
            i0 += 1
        if i0 < n0:
            row = row0[i0]
            p0, p1 = p0 * row[0], p0 * row[1] + p1 * row[0]
        while i1 < n1 and brk1[i1] < x:
            i1 += 1
        if i1 < n1:
            row = row1[i1]
            p0, p1 = p0 * row[0], p0 * row[1] + p1 * row[0]
        while i2 < n2 and brk2[i2] < x:
            i2 += 1
        if i2 < n2:
            row = row2[i2]
            p0, p1 = p0 * row[0], p0 * row[1] + p1 * row[0]
        total += (x - prev) * (1.0 - p0 + 1.0 - (p0 + p1) * f_q)
        prev = x
    return total

@lru_cache(maxsize=65_536)
def _position_tables(
    projections: tuple[tuple[int, float], ...], wire_points: float, pos: str
) -> tuple[float, tuple]:
    """A position's two closed-form pieces: dedicated-slot value and marginal law.

    Cached as a whole so the common repeat lookup hashes its key once instead of once
    per inner cache.
    """
    dedicated = DEDICATED_SLOTS[pos]
    unavailable = UNAVAILABLE_RATE[pos]
    ded_value = _position_expected_values(
        projections, wire_points, unavailable, dedicated
    )[dedicated]
    cap = 1 if pos == "QB" else _EXTRA_SLOTS
    table = _extra_count_table(projections, wire_points, unavailable, dedicated, cap)
    return ded_value, table


def expected_lineup_value(
    roster: list[Player],
    wire: dict[str, float],
    h: str,
) -> float:
    """Expected best weekly lineup over availability draws, in closed form."""
    by_pos = {pos: [] for pos in POSITIONS}
    for player in roster:
        by_pos[player.position].append(player)
    total = 0.0
    tables = {}
    for pos in POSITIONS:
        projections = tuple(
            sorted((p.player_id, horizon_points(p, h)) for p in by_pos[pos])
        )
        ded_value, tables[pos] = _position_tables(projections, wire[pos], pos)
        total += ded_value
    return total + _extra_expected_value(
        tables["QB"], (tables["RB"], tables["WR"], tables["TE"])
    )


def starting_positions(roster: list[Player], h: str) -> list[str]:
    """Which positions fill the lineup when a team must field everyone it can, per horizon.

    The horizon decides the pecking order into the flex slots. Validation uses this to
    check every simulated roster can field a full lineup.
    """
    caps = dict(STARTING_SLOTS)
    out: list[str] = []
    for p in sorted(roster, key=_HKEY[h]):
        for slot in SLOT_CHAIN[p.position]:
            if caps[slot]:
                caps[slot] -= 1
                out.append(p.position)
                break
    return out


def team_value(
    roster: dict[str, list[Player]],
    wire: dict[str, dict[str, float]],
    extra: Player | None = None,
) -> float:
    """Expected optimal lineup points over both horizons."""
    return sum(
        expected_lineup_value(
            roster[h]
            if extra is None
            else insert_sorted(roster[h], extra, h),
            wire[h],
            h,
        )
        for h in ACTIVE_HORIZONS
    )


def team_values_with_candidates(
    roster: list[Player],
    wire: dict[str, dict[str, float]],
    candidates: list[Player],
) -> tuple[float, dict[int, float]]:
    """Roster value and each one-player addition, sharing the unchanged positions.

    A candidate alters one position. Computing the other three depth charts once per
    horizon avoids rebuilding them for every personal-strategy option during rollouts.
    """
    by_pos = {pos: [] for pos in POSITIONS}
    for player in roster:
        by_pos[player.position].append(player)

    baseline = 0.0
    values = {candidate.player_id: 0.0 for candidate in candidates}
    for h in ACTIVE_HORIZONS:
        point_rows = {
            pos: tuple(
                sorted(
                    (player.player_id, horizon_points(player, h))
                    for player in by_pos[pos]
                )
            )
            for pos in POSITIONS
        }
        ded = {}
        tables = {}
        for pos in POSITIONS:
            ded[pos], tables[pos] = _position_tables(
                point_rows[pos], wire[h][pos], pos
            )
        baseline += sum(ded.values()) + _extra_expected_value(
            tables["QB"], (tables["RB"], tables["WR"], tables["TE"])
        )
        for candidate in candidates:
            pos = candidate.position
            candidate_rows = tuple(
                sorted(
                    point_rows[pos]
                    + ((candidate.player_id, horizon_points(candidate, h)),)
                )
            )
            cded, ctable = _position_tables(candidate_rows, wire[h][pos], pos)
            merged = {**tables, pos: ctable}
            values[candidate.player_id] += (
                sum(ded[p] for p in POSITIONS if p != pos)
                + cded
                + _extra_expected_value(
                    merged["QB"], (merged["RB"], merged["WR"], merged["TE"])
                )
            )
    return baseline, values


def upside_points(p: Player) -> float:
    """Reporting diagnostic: the 3-year total re-projected at the years-2-3 pace.

    Equal to `points` for a perfectly flat scorer, above it for a backloaded player,
    below it for a declining veteran. It is a reporting diagnostic, not a pricing input:
    future growth already lives in the years-2-3 projection and lineup.
    """
    return 1.5 * (p.points - p.points_1yr)


# --- wire levels --------------------------------------------------------------------


def _rep_at_rank(pos_players: list[Player], rank: int, h: str) -> float:
    """Horizon points of the rank-th best player at a position by that horizon (1-based),
    clamped to the pool."""
    if not pos_players:
        return 0.0
    return horizon_points(pos_players[min(max(rank, 1), len(pos_players)) - 1], h)


def seed_wire(players: list[Player]) -> dict[str, dict[str, float]]:
    """Iteration-0 wire levels from pure slot counting, no draft behaviour assumed.

    Per horizon: assign the top of the pool to the league's 108 starting slots the way a
    perfectly efficient market would — dedicated slots by that horizon's positional rank,
    then the 12 flex slots and 12 superflex slots to the best players still eligible —
    and take the best player at each position who did not earn a slot. Convergence
    replaces this with the best player actually left undrafted (`wire_replacement`).
    """
    pos_h = pos_by_horizon(players)
    out: dict[str, dict[str, float]] = {}
    for h in HORIZONS:
        caps = {slot: n * TEAMS for slot, n in STARTING_SLOTS.items()}
        used = {p: 0 for p in POSITIONS}
        for p in sorted(players, key=_HKEY[h]):
            for slot in SLOT_CHAIN[p.position]:
                if caps[slot]:
                    caps[slot] -= 1
                    used[p.position] += 1
                    break
        out[h] = {k: _rep_at_rank(pos_h[h][k], used[k] + 1, h) for k in POSITIONS}
    return out


def wire_replacement(
    taken: set[int], pos: dict[str, list[Player]]
) -> dict[str, dict[str, float]]:
    """The best player at each position actually left undrafted, per horizon.

    In this league only 168 of ~440 pool players get rostered, so the wire holds real
    bodies. It answers "how much better is he than what I could sign for nothing
    after the draft", and contributes one unique, always-available body per position to
    expected lineup value. The best year-1 add and the best years-2-3 stash can be
    different players, so each horizon takes its own max.
    """
    out: dict[str, dict[str, float]] = {h: {} for h in HORIZONS}
    for position, players in pos.items():
        left = [p for p in players if p.player_id not in taken]
        for h in HORIZONS:
            out[h][position] = max((horizon_points(p, h) for p in left), default=0.0)
    return out
