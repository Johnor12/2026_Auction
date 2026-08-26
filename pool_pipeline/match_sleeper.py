"""Join FantasyPros projection rows to Sleeper's player dump by name.

Sleeper hosts the league; its ids are what a roster or a draft pick is expressed in
over its API, so they are the pool's identity. There is no shared key with FantasyPros,
and names are neither unique nor spelled the same way, so the join runs in three tiers.
Each is stricter about what it may assume, and each requires exactly one survivor — an
ambiguous player is reported, never guessed:

    1. full name            "Josh Allen"          -> joshallen, QB
    2. name without suffix  "Patrick Mahomes II"  -> patrickmahomes
    3. last name + team     "Hollywood Brown"     -> Marquise Brown, PHI

Tier 2 exists because the suffix is editorial: Sleeper lists Michael Penix Jr. as
"Michael Penix". Tier 3 exists because first names are too (Bam/Zonovan Knight,
Hollywood/Marquise Brown); it cannot lean on the first name at all, so it demands the
team instead, and ``build_pool.py --report`` prints every one of its joins for eyeballing.

Position must agree in every tier (against Sleeper's ``position`` or its
``fantasy_positions``), which is what separates the two Kenneth Walkers — and what
drops FantasyPros' fullback rows, which Sleeper lists at TE. Where several same-named
candidates survive, the tie is broken only by hard facts: team, then active status.
"""

from __future__ import annotations

import collections
import re

POSITIONS = ("QB", "RB", "WR", "TE")

#: FantasyPros team code -> Sleeper team code. Everything else is already identical.
TEAM_ALIASES = {"JAC": "JAX"}

#: Name suffixes that one source prints and the other does not.
SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "v")


def norm(value: str | None) -> str:
    """Lowercase, alphanumerics only — the form Sleeper's own search fields use."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def strip_suffix(normalized: str) -> str:
    """``kennethwalkeriii`` -> ``kennethwalker``; left alone if nothing sane remains."""
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 4:
            return normalized[: -len(suffix)]
    return normalized


def last_name(name: str) -> str:
    """The last word that isn't a suffix. ``Dont'e Thornton Jr.`` -> ``thornton``."""
    words = [norm(word) for word in (name or "").split()]
    words = [word for word in words if word] or [norm(name)]
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return words[-1]


def sleeper_team(code: str | None) -> str | None:
    return TEAM_ALIASES.get(code, code) or None


def full_name_of(player: dict) -> str:
    return player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()


def normalized_name_of(player: dict) -> str:
    """Sleeper's precomputed key when present, recomputed identically when not."""
    return player.get("search_full_name") or norm(full_name_of(player))


def positions_of(player: dict) -> set[str]:
    listed = player.get("fantasy_positions") or []
    return {p for p in [player.get("position"), *listed] if p}


class SleeperIndex:
    """Sleeper's QB/RB/WR/TE players, keyed the three ways the tiers look them up."""

    def __init__(self, dump: dict):
        self.by_name: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_base: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_last: dict[str, list[dict]] = collections.defaultdict(list)
        self.player_count = len(dump)
        self.considered = 0

        for player in dump.values():
            if not isinstance(player, dict) or not positions_of(player) & set(POSITIONS):
                continue
            self.considered += 1
            name = normalized_name_of(player)
            if not name:
                continue
            self.by_name[name].append(player)
            self.by_base[strip_suffix(name)].append(player)
            self.by_last[last_name(full_name_of(player))].append(player)


def narrow(row: dict, candidates: list[dict]) -> list[dict]:
    """Drop candidates on hard facts only, and only while something survives.

    Position must agree, always. Beyond that, team and active status are tiebreakers
    rather than filters: a lone candidate on the wrong team is still the answer (the
    provider and Sleeper disagree about who plays where mid-offseason), but between two
    same-named players the one on the right team is.
    """
    survivors = [p for p in candidates if row["position"] in positions_of(p)]
    if len(survivors) <= 1:
        return survivors

    team = sleeper_team(row.get("team"))
    if team:
        on_team = [p for p in survivors if p.get("team") == team]
        if on_team:
            survivors = on_team
    if len(survivors) <= 1:
        return survivors

    active = [p for p in survivors if p.get("active")]
    return active or survivors


def match(row: dict, index: SleeperIndex) -> tuple[dict | None, str, list[dict]]:
    """Return (player or None, tier, the candidates that caused an ambiguity)."""
    name = norm(row["name"])

    for tier, candidates in (
        ("name", index.by_name.get(name, [])),
        ("name_without_suffix", index.by_base.get(strip_suffix(name), [])),
    ):
        survivors = narrow(row, candidates)
        if len(survivors) == 1:
            return survivors[0], tier, []
        if survivors:
            return None, tier, survivors

    # Tier 3: the first name is unusable, so the team carries the whole join.
    team = sleeper_team(row.get("team"))
    if team:
        candidates = [
            p for p in index.by_last.get(last_name(row["name"]), []) if p.get("team") == team
        ]
        survivors = narrow(row, candidates)
        if len(survivors) == 1:
            return survivors[0], "last_name_team", []
        if survivors:
            return None, "last_name_team", survivors

    return None, "none", []
