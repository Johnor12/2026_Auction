"""The draft pool: pool.json rows as Player objects.

The value inputs are `points_3yr` — three-year points at 0.5/rec with a 0.5 TE
premium, which is this league's scoring — and `points_1yr`, whose gap against it is
the provider's implied growth. The ranker values the horizons separately.
Draftsharks' 3D value is ignored entirely and
is not even carried into the pool: it is a provider-scaled ordinal that already bakes in
someone else's roster assumptions, and it is not in points, so it cannot enter a
points-denominated lineup objective. Kickers and IDP are already dropped upstream
because the roster has no slot for them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .league import POINTS_FIELD, POSITIONS


@dataclass(slots=True)
class Player:
    player_id: int
    name: str
    position: str
    team: str
    age: float | None
    bye_week: int | None
    is_rookie: bool
    points: int
    points_1yr: int
    provider_adp: float | None
    sleeper_id: str | None = None  # the only key draft.json shares with the pool
    availability_index: int = 0  # rank on the opponents' consensus board, 0-based
    # The two value horizons, precomputed because the lineup solver reads them millions
    # of times per run (value.HORIZONS; yr23 = points - points_1yr, never negative —
    # build_pool raises a retirement-discounted 3yr cell to the 1yr value, and
    # load_pool rejects a pool that predates that repair).
    points_yr1: float = field(init=False, default=0.0)
    points_yr23: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.points_yr1 = float(self.points_1yr)
        self.points_yr23 = float(self.points - self.points_1yr)


def load_pool(path: Path) -> tuple[list[Player], dict]:
    """Read pool.json: already filtered to QB/RB/WR/TE with a usable 3-year projection.

    build_pool.py does the filtering — positions with no roster slot, the source's zeros
    where a null belongs — so this only re-checks the
    invariants it promises rather than re-deriving them. The guards stay because a
    hand-edited or stale pool is the likeliest way this ever gets bad input.
    """
    raw = json.loads(path.read_text())
    players: list[Player] = []
    dropped = {"non_offense": 0, "zero_projection": []}
    for rec in raw["players"]:
        if rec["position"] not in POSITIONS:
            dropped["non_offense"] += 1
            continue
        points = rec[POINTS_FIELD]
        if not points or points <= 0:
            dropped["zero_projection"].append(rec["name"])
            continue
        if rec["points_1yr"] > points:
            raise ValueError(
                f"{rec['name']}: points_1yr {rec['points_1yr']} > {POINTS_FIELD} "
                f"{points} — a negative years-2-3 projection; rebuild pool.json "
                "(build_pool zeroes negative future seasons)"
            )
        players.append(
            Player(
                player_id=rec["player_id"],
                name=rec["name"],
                position=rec["position"],
                team=rec["team"],
                age=rec.get("age"),
                bye_week=rec.get("bye_week"),
                is_rookie=bool(rec.get("is_rookie")),
                points=points,
                # Direct key access: a pool.json without it predates the upside pricing
                # and must be rebuilt, not silently valued as if every player were flat.
                points_1yr=rec["points_1yr"],
                provider_adp=rec.get("adp"),
                sleeper_id=rec.get("sleeper_id"),
            )
        )

    players.sort(key=lambda p: (-p.points, p.player_id))
    counts = {pos: 0 for pos in POSITIONS}
    for p in players:
        counts[p.position] += 1

    meta = {
        "source_file": str(path),
        "source_player_count": raw.get("player_count", len(raw["players"])),
        "source_of_pool": raw.get("source_file"),
        "pool_size": len(players),
        "by_position": counts,
        # The join key to draft.json. A pool player without one can never be recognised
        # as drafted, so a shortfall here is a silent way for the live board to go wrong.
        "with_sleeper_id": sum(1 for p in players if p.sleeper_id),
        "dropped_non_offense": dropped["non_offense"],
        "dropped_zero_projection": sorted(dropped["zero_projection"]),
    }
    return players, meta


def by_position(players: list[Player]) -> dict[str, list[Player]]:
    out: dict[str, list[Player]] = {pos: [] for pos in POSITIONS}
    for p in players:
        out[p.position].append(p)
    return out
