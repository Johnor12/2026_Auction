"""Provider-specific ranking parsers and normalized row validation."""

from __future__ import annotations

import csv
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from identity import normalized_name

POSITIONS = {"QB", "RB", "WR", "TE"}

#: Provider team codes -> Sleeper's, so the last-name-plus-team identity tier can join.
#: FantasyPros writes JAC; KeepTradeCut also uses three-letter codes for a few teams.
TEAM_ALIASES = {
    "JAC": "JAX",
    "GBP": "GB",
    "KCC": "KC",
    "LVR": "LV",
    "NEP": "NE",
    "NOS": "NO",
    "SFO": "SF",
    "TBB": "TB",
}


def js_json(text: str, marker: str):
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"missing JavaScript marker {marker!r}")
    start += len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    value, _ = json.JSONDecoder().raw_decode(text, start)
    return value


def read_html(path: Path) -> str:
    # KeepTradeCut serves a stray non-UTF-8 byte or two in its page chrome; the embedded
    # JSON is clean, so a replacement character there costs nothing.
    return path.read_text(encoding="utf-8", errors="replace")


def player(
    rank: int,
    name: str,
    position: str,
    team: str | None,
    value: int | float | None,
    sleeper_id: str | None = None,
) -> dict:
    code = team.strip().upper() if team and team.strip() else None
    return {
        "rank": int(rank),
        "name": name.strip(),
        "position": position.strip().upper(),
        "team": None if code in (None, "FA") else TEAM_ALIASES.get(code, code),
        "sleeper_id": str(sleeper_id) if sleeper_id not in (None, "") else None,
        "value": value,
    }


def ranked_by_value(rows: list[dict], descending: bool) -> list[dict]:
    """Re-rank 1..N by value, keeping the provider's own order among ties."""
    sign = -1 if descending else 1
    rows.sort(key=lambda row: (sign * row["value"], row["rank"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def parse_fantasypros_ecr(path: Path) -> list[dict]:
    data = js_json(read_html(path), "var ecrData =")
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


class TableRows(HTMLParser):
    """Capture the cell text of every row of one <table>, chosen by id."""

    def __init__(self, table_id: str):
        super().__init__()
        self.table_id = table_id
        self.active = False
        self.in_row = False
        self.in_cell = False
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and dict(attrs).get("id") == self.table_id:
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
        elif self.active and tag == "table":
            self.active = False


#: "Josh Allen (BUF - QB)", "Travis Hunter (JAC - WR,CB)", "Elijah Mitchell ( - RB)"; an
#: injury tag such as DTD may trail the closing parenthesis.
AUCTION_NAME = re.compile(r"^(?P<name>.+?) \((?P<team>[A-Z]*) - (?P<positions>[A-Z,]+)\)")


def parse_fantasypros_auction(path: Path) -> list[dict]:
    """The Draft Wizard calculator's overall table: name cell, rounded $, unrounded value."""
    parser = TableRows("OverallTable")
    parser.feed(read_html(path))
    rows = []
    for cells in parser.rows:
        if len(cells) != 4 or not cells[2].startswith("$"):
            continue  # the header row
        found = AUCTION_NAME.match(cells[1])
        if found is None:
            raise ValueError(f"unexpected auction row {cells[1]!r}")
        position = found["positions"].split(",")[0]
        if position not in POSITIONS:
            continue
        row = player(len(rows) + 1, found["name"], position, found["team"], int(cells[2][1:]))
        # Dollars are rounded, so the board is ordered by the calculator's unrounded value.
        row["unrounded"] = float(cells[3])
        rows.append(row)
    rows.sort(key=lambda row: (-row.pop("unrounded"), row["rank"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def parse_keeptradecut(path: Path) -> list[dict]:
    rows = js_json(read_html(path), "var playersArray =")
    ranked = []
    for raw in rows:
        if raw["position"] not in POSITIONS:
            continue
        values = raw["superflexValues"]  # the plain (no TE premium) superflex board
        ranked.append(
            player(
                values["rank"],
                raw["playerName"],
                raw["position"],
                raw.get("team"),
                values["value"],
            )
        )
    return ranked_by_value(ranked, descending=True)


def parse_fantasycalc(path: Path) -> list[dict]:
    rows = json.loads(path.read_bytes())
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


def parse_sleeper_adp(path: Path) -> list[dict]:
    """Sleeper's projections feed, which carries the ADP its draft rooms display."""
    rows = json.loads(path.read_bytes())
    ranked = []
    for raw in rows:
        info = raw["player"]
        adp = (raw.get("stats") or {}).get("adp_2qb")
        if info.get("position") not in POSITIONS or adp is None or adp >= 999:
            continue  # 999 is Sleeper's "goes undrafted"
        ranked.append(
            player(
                len(ranked) + 1,
                f"{info['first_name']} {info['last_name']}",
                info["position"],
                info.get("team"),
                adp,
                raw["player_id"],
            )
        )
    return ranked_by_value(ranked, descending=False)


def parse_ffcalculator(path: Path) -> list[dict]:
    data = json.loads(path.read_bytes())
    ranked = [
        player(index, raw["name"], raw["position"], raw.get("team"), raw["adp"])
        for index, raw in enumerate(data["players"], start=1)
        if raw["position"] in POSITIONS
    ]
    return ranked_by_value(ranked, descending=False)


PARSERS: dict[str, tuple[str, str, Callable[[Path], list[dict]]]] = {
    "fantasypros_ecr": ("FantasyPros ECR", "fantasypros_ecr.html", parse_fantasypros_ecr),
    "fantasypros_auction": (
        "FantasyPros auction values",
        "fantasypros_auction.html",
        parse_fantasypros_auction,
    ),
    "keeptradecut": ("KeepTradeCut", "keeptradecut.html", parse_keeptradecut),
    "fantasycalc": ("FantasyCalc", "fantasycalc.json", parse_fantasycalc),
    "sleeper_adp": ("Sleeper ADP", "sleeper_projections.json", parse_sleeper_adp),
    "ffcalculator": ("FFCalculator ADP", "ffcalculator.json", parse_ffcalculator),
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


def drop_ambiguous_identities(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Split off every row of a same-name-same-position collision.

    A provider can list two distinct players under one name and position (FantasyPros'
    dynasty board once carried two WRs named Isaiah Williams). A name-keyed identity
    cannot tell such rows apart, so none of them is joinable to the pool; dropping the
    collision (reported by the caller) beats failing the whole source.
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
