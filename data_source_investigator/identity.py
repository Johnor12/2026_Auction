"""Canonical player-name normalization and pool identity resolution."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def words(value: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", value).casefold()
    return re.findall(r"[a-z0-9]+", plain)


def normalized_name(value: str, *, drop_suffix: bool = False) -> str:
    parts = words(value)
    if drop_suffix:
        while parts and parts[-1] in SUFFIXES:
            parts.pop()
    return "".join(parts)


class PlayerResolver:
    """Resolve provider names onto the pool's canonical Sleeper ids."""

    def __init__(self, pool: dict):
        self.ids = {str(player["sleeper_id"]): player for player in pool["players"]}
        self.full: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.base: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.last_team: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for player in pool["players"]:
            position = player["position"]
            self.full[(normalized_name(player["name"]), position)].append(player)
            self.base[
                (normalized_name(player["name"], drop_suffix=True), position)
            ].append(player)
            parts = words(player["name"])
            while parts and parts[-1] in SUFFIXES:
                parts.pop()
            if len(parts) >= 2 and player.get("team"):
                self.last_team[(parts[-1], position, player["team"])].append(player)

    def resolve(self, player: dict) -> str | None:
        supplied = player.get("sleeper_id")
        if supplied is not None and str(supplied) in self.ids:
            return str(supplied)

        position = player["position"]
        name = player["name"]
        for index, key in (
            (self.full, (normalized_name(name), position)),
            (self.base, (normalized_name(name, drop_suffix=True), position)),
        ):
            matches = index.get(key, [])
            if len(matches) == 1:
                return str(matches[0]["sleeper_id"])

        parts = words(name)
        while parts and parts[-1] in SUFFIXES:
            parts.pop()
        team = player.get("team")
        if len(parts) >= 2 and team:
            matches = self.last_team.get((parts[-1], position, team), [])
            matches = [
                match
                for match in matches
                if (ours := words(match["name"]))
                and (parts[0].startswith(ours[0]) or ours[0].startswith(parts[0]))
            ]
            if len(matches) == 1:
                return str(matches[0]["sleeper_id"])
        return None

