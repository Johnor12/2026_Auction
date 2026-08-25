"""Provider-specific ranking parsers and normalized row validation."""

from __future__ import annotations

import csv
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from identity import normalized_name

POSITIONS = {"QB", "RB", "WR", "TE"}


def js_json(text: str, marker: str):
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"missing JavaScript marker {marker!r}")
    start += len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    value, _ = json.JSONDecoder().raw_decode(text, start)
    return value


def player(
    rank: int,
    name: str,
    position: str,
    team: str | None,
    value: int | float | None,
    sleeper_id: str | None = None,
) -> dict:
    return {
        "rank": int(rank),
        "name": name.strip(),
        "position": position.strip().upper(),
        "team": team.strip().upper() if team and team.strip() else None,
        "sleeper_id": str(sleeper_id) if sleeper_id not in (None, "") else None,
        "value": value,
    }


def parse_fantasycalc(path: Path) -> list[dict]:
    rows = json.loads(path.read_text())
    result = []
    for row in rows:
        raw = row["player"]
        if raw["position"] in POSITIONS:
            result.append(
                player(
                    row["overallRank"],
                    raw["name"],
                    raw["position"],
                    raw.get("maybeTeam"),
                    row.get("value"),
                    raw.get("sleeperId"),
                )
            )
    return result


def parse_keeptradecut(path: Path) -> list[dict]:
    rows = js_json(path.read_text(), "var playersArray =")
    ranked = []
    for raw in rows:
        if raw["position"] not in POSITIONS:
            continue
        # KTC calls its lightest TE-premium setting TE+. Its own description says
        # this includes a +0.5 PPR boost, which is the setting in this league.
        values = raw["superflexValues"]["tep"]
        ranked.append(
            player(
                values["rank"],
                raw["playerName"],
                raw["position"],
                raw.get("team"),
                values.get("value"),
            )
        )
    # KTC's TE+ overlay currently emits a few duplicate rank numbers even though
    # the values are distinct. Value is what drives its displayed board.
    ranked.sort(key=lambda row: (-row["value"], row["rank"], row["name"]))
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    return ranked


class RankingsTable(HTMLParser):
    """Capture the five-column SEO table published by Dynasty Nerds."""

    def __init__(self):
        super().__init__()
        self.active = False
        self.in_row = False
        self.in_cell = False
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div" and dict(attrs).get("id") == "dr-seo-table":
            self.active = True
        elif self.active and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag in {"th", "td"}:
            self.in_cell = True
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell and tag in {"th", "td"}:
            self.row.append("".join(self.text).strip())
            self.in_cell = False
        elif self.in_row and tag == "tr":
            self.rows.append(self.row)
            self.in_row = False


def parse_dynastynerds(path: Path) -> list[dict]:
    parser = RankingsTable()
    parser.feed(path.read_text())
    result = []
    for row in parser.rows:
        if len(row) != 5 or not row[0].isdigit():
            continue
        result.append(player(int(row[0]), row[1], row[3], row[2], int(row[4].replace(",", ""))))
    return result


def parse_fantasypros(path: Path) -> list[dict]:
    data = js_json(path.read_text(), "var ecrData =")
    result = []
    for raw in data["players"]:
        position = raw["player_position_id"]
        if position in POSITIONS:
            result.append(
                player(
                    raw["rank_ecr"],
                    raw["player_name"],
                    position,
                    raw.get("player_team_id"),
                    float(raw["rank_ave"]),
                )
            )
    return result


PARSERS: dict[str, tuple[str, str, Callable[[Path], list[dict]]]] = {
    "fantasycalc": ("FantasyCalc", "fantasycalc.json", parse_fantasycalc),
    "keeptradecut": ("KeepTradeCut", "keeptradecut.html", parse_keeptradecut),
    "dynastynerds": ("Dynasty Nerds", "dynastynerds.html", parse_dynastynerds),
    "fantasypros": ("FantasyPros ECR", "fantasypros.html", parse_fantasypros),
}


def parse_manual(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"rank", "name", "position"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing CSV columns: {', '.join(sorted(missing))}")
        rows = []
        for raw in reader:
            value: int | float | None = None
            if raw.get("value"):
                value = float(raw["value"])
                if value.is_integer():
                    value = int(value)
            rows.append(
                player(
                    int(raw["rank"]),
                    raw["name"],
                    raw["position"],
                    raw.get("team"),
                    value,
                    raw.get("sleeper_id"),
                )
            )
        return rows


def draftsharks_adp(pool: dict) -> list[dict]:
    available = [row for row in pool["players"] if row.get("adp") is not None]
    available.sort(key=lambda row: (row["adp"], row["rank"]))
    return [
        player(
            rank,
            raw["name"],
            raw["position"],
            raw.get("team"),
            raw["adp"],
            raw.get("sleeper_id"),
        )
        for rank, raw in enumerate(available, start=1)
    ]


def drop_ambiguous_identities(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Split off every row of a same-name-same-position collision.

    FantasyPros' 2026 ECR lists two distinct WRs named Isaiah Williams. A name-keyed
    identity cannot tell such rows apart, so none of them is joinable to the pool;
    dropping the collision (reported by the caller) beats failing the whole source.
    """
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        ident = (normalized_name(row["name"], drop_suffix=True), row["position"])
        counts[ident] = counts.get(ident, 0) + 1
    kept, dropped = [], []
    for row in rows:
        ident = (normalized_name(row["name"], drop_suffix=True), row["position"])
        (kept if counts[ident] == 1 else dropped).append(row)
    return kept, sorted(f"{row['name']} ({row['position']}, rank {row['rank']})" for row in dropped)


def validate(source_id: str, rows: list[dict]) -> list[str]:
    problems = []
    if len(rows) < 50:
        problems.append(f"{source_id}: only {len(rows)} players (expected at least 50)")
    ranks = [row["rank"] for row in rows]
    if any(rank < 1 for rank in ranks):
        problems.append(f"{source_id}: ranks must be positive")
    if len(ranks) != len(set(ranks)):
        problems.append(f"{source_id}: duplicate ranks")
    bad_positions = sorted({row["position"] for row in rows} - POSITIONS)
    if bad_positions:
        problems.append(f"{source_id}: unsupported positions {bad_positions}")
    identities = [
        (normalized_name(row["name"], drop_suffix=True), row["position"]) for row in rows
    ]
    if len(identities) != len(set(identities)):
        problems.append(f"{source_id}: duplicate player identities")
    return problems

