#!/usr/bin/env python3
"""Build pool.json: FantasyPros season projections, scored for this league, keyed to Sleeper.

    data/FantasyPros_Fantasy_Football_Projections_{QB,RB,WR,TE}.csv
    data/sleeper_players.json                                     ->  ../pool.json

Sleeper hosts the draft, so its player ids are the pool's identity: every row is one
Sleeper player (id, name, team, age, rookie flag) carrying FantasyPros' projected stat
line, scored under this league's settings. A projection row that does not join to
exactly one Sleeper player at the same position is left out and reported — it cannot be
drafted in the room, so it is not on the board. ``match_sleeper.py`` owns the join.

**Scoring** is applied here, from the raw stat columns, rather than copied from the
export's FPTS column: FantasyPros scores an interception -1 where this league scores -2
(the rest of the export's half-PPR setting already agrees). ``SCORING`` mirrors the
league's Sleeper settings; two-point conversions are not projected and are ignored.
``--report`` recomputes FPTS under FantasyPros' own weights as a check that the column
layout is being read correctly.

**Positions** come from the file a row sits in and must agree with Sleeper's. That is
what separates same-named players, and it is why FantasyPros' fullback rows drop:
Sleeper lists them at TE.

Zero-point rows are FantasyPros' "no projection" and are dropped. Every other joined
player is kept; there is no rank cut. The pool carries one season of points because the
league is a redraft; nothing multi-year is invented.

Usage:
    uv run pool_pipeline/build_pool.py            # -> pool.json
    uv run pool_pipeline/build_pool.py --report   # + scoring check, join tiers, misses
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

import fetch_sleeper
import match_sleeper
import paths

POSITIONS = ("QB", "RB", "WR", "TE")
SEASON = 2026

#: This league's scoring (Sleeper league 1396606685107200000): 0.5 PPR, no TE bonus.
SCORING = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
}

#: What the export's own FPTS column was computed with: the same, at -1 per interception.
FANTASYPROS_SCORING = {**SCORING, "pass_int": -1.0}

#: Each export's header, and the stat every column after Player and Team carries. The
#: exports reuse ATT/YDS/TDS across passing, rushing and receiving, so columns are read
#: by position rather than by name, and a changed header fails loudly.
LAYOUTS = {
    "QB": (
        ["Player", "Team", "ATT", "CMP", "YDS", "TDS", "INTS", "ATT", "YDS", "TDS", "FL", "FPTS"],
        ("pass_att", "pass_cmp", "pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td", "fum_lost", "fpts"),
    ),
    "RB": (
        ["Player", "Team", "ATT", "YDS", "TDS", "REC", "YDS", "TDS", "FL", "FPTS"],
        ("rush_att", "rush_yd", "rush_td", "rec", "rec_yd", "rec_td", "fum_lost", "fpts"),
    ),
    "WR": (
        ["Player", "Team", "REC", "YDS", "TDS", "ATT", "YDS", "TDS", "FL", "FPTS"],
        ("rec", "rec_yd", "rec_td", "rush_att", "rush_yd", "rush_td", "fum_lost", "fpts"),
    ),
    "TE": (
        ["Player", "Team", "REC", "YDS", "TDS", "FL", "FPTS"],
        ("rec", "rec_yd", "rec_td", "fum_lost", "fpts"),
    ),
}

#: Sleeper ids are stable, so an old dump only misses players added since. Warn, don't fail.
STALE_AFTER_DAYS = 14

FIELD_DEFINITIONS = {
    "rank": "Pool rank, 1..N, by points_1yr descending (ties broken by name). Unique and gap-free.",
    "positional_rank": "Rank within position under the same ordering.",
    "player_id": "Sleeper's player id as an integer; the pool's unique key.",
    "sleeper_id": (
        "The same id as the string Sleeper's API uses; joins draft.json and the "
        "investigator's boards."
    ),
    "name": "Player name as Sleeper prints it.",
    "position": "QB, RB, WR or TE: FantasyPros' file, which agrees with Sleeper's listing.",
    "team": "NFL team abbreviation per Sleeper; null when unsigned.",
    "age": "Age in years per Sleeper; null when Sleeper does not know it.",
    "is_rookie": f"True for {SEASON} rookies (Sleeper years_exp == 0).",
    "points_1yr": (
        f"Projected {SEASON} fantasy points: FantasyPros' consensus stat line scored under "
        "this league's settings (see 'scoring')."
    ),
}


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def read_projections(position: str, path: Path) -> list[dict]:
    """One FantasyPros export -> rows of {name, team, position, stats}."""
    header, stat_names = LAYOUTS[position]
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] != header:
        raise ValueError(f"{path.name}: header {rows[0] if rows else None} is not {header}")
    out = []
    for row in rows[1:]:
        if len(row) != len(header):
            continue  # the export pads with a blank spacer row and trailing empty lines
        if not row[0].strip():
            continue  # avg/high/low exports add "high"/"low" rows under an empty Player
        out.append(
            {
                "name": row[0].strip(),
                "team": row[1].strip(),
                "position": position,
                "stats": dict(zip(stat_names, (float(cell) for cell in row[2:]))),
            }
        )
    return out


def score(stats: dict, weights: dict = SCORING) -> float:
    return round(sum(weight * stats.get(stat, 0.0) for stat, weight in weights.items()), 1)


# ---------------------------------------------------------------------------
# Join and rows
# ---------------------------------------------------------------------------


def join(rows: list[dict], index: match_sleeper.SleeperIndex) -> tuple[list[dict], dict]:
    """Attach each row's Sleeper player. Returns (joined rows, join diagnostics)."""
    joined: list[dict] = []
    tiers: collections.Counter[str] = collections.Counter()
    joins: dict[str, list] = {"name_without_suffix": [], "last_name_team": []}
    unmatched: list[dict] = []
    ambiguous: list[tuple[dict, str, list[dict]]] = []

    for row in rows:
        player, tier, clash = match_sleeper.match(row, index)
        if player is None:
            if clash:
                ambiguous.append((row, tier, clash))
            else:
                unmatched.append(row)
            continue
        tiers[tier] += 1
        if tier in joins:
            joins[tier].append((row, player))
        joined.append({**row, "sleeper": player})

    ids = collections.Counter(row["sleeper"]["player_id"] for row in joined)
    duplicates = sorted(player_id for player_id, count in ids.items() if count > 1)
    return joined, {
        "by_tier": dict(tiers),
        "joins": joins,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "duplicates": duplicates,
    }


def build_rows(joined: list[dict]) -> list[dict]:
    """One flat record per player, in pool order."""
    joined.sort(
        key=lambda row: (
            -row["points"],
            match_sleeper.full_name_of(row["sleeper"]),
            int(row["sleeper"]["player_id"]),
        )
    )
    seen: collections.Counter[str] = collections.Counter()
    rows = []
    for rank, row in enumerate(joined, start=1):
        player = row["sleeper"]
        seen[row["position"]] += 1
        rows.append(
            {
                "rank": rank,
                "positional_rank": seen[row["position"]],
                "player_id": int(player["player_id"]),
                "sleeper_id": str(player["player_id"]),
                "name": match_sleeper.full_name_of(player),
                "position": row["position"],
                "team": player.get("team"),
                "age": player.get("age"),
                "is_rookie": player.get("years_exp") == 0,
                "points_1yr": row["points"],
            }
        )
    return rows


def build_document(
    rows: list[dict],
    zero_projection: int,
    diagnostics: dict,
    index: match_sleeper.SleeperIndex,
    meta: dict | None,
) -> dict:
    return {
        "source": (
            "FantasyPros consensus season projections, one CSV export per position, "
            "scored under this league's settings"
        ),
        "source_files": [path.name for path in paths.PROJECTIONS_CSV.values()],
        "season": SEASON,
        "scoring": SCORING,
        "positions": list(POSITIONS),
        "player_count": len(rows),
        "excluded": {
            "zero_projection": zero_projection,
            "no_sleeper_match": [
                {
                    "name": row["name"],
                    "position": row["position"],
                    "team": row["team"],
                    "points_1yr": row["points"],
                }
                for row in diagnostics["unmatched"]
            ],
        },
        "sleeper": {
            "source": (meta or {}).get("url", fetch_sleeper.URL),
            "fetched_at": (meta or {}).get("fetched_at"),
            "dump_player_count": index.player_count,
            "matched_by": diagnostics["by_tier"],
        },
        "fields": FIELD_DEFINITIONS,
        "players": rows,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(
    rows: list[dict],
    projected: list[dict],
    pool: list[dict],
    diagnostics: dict,
    index: match_sleeper.SleeperIndex,
    meta: dict | None,
) -> None:
    out = sys.stderr
    counts = collections.Counter(row["position"] for row in rows)
    print(
        "\ninput: " + ", ".join(f"{pos} {counts[pos]}" for pos in POSITIONS)
        + f" = {len(rows)} rows; {len(rows) - len(projected)} with no projection dropped",
        file=out,
    )

    # -- scoring: is the column layout read right, and what does -2/INT change? ----
    gap, worst = max(
        (abs(score(row["stats"], FANTASYPROS_SCORING) - row["stats"]["fpts"]), row["name"])
        for row in projected
    )
    print(
        f"scoring check: FPTS recomputed under FantasyPros' weights (-1/INT) is within "
        f"{gap:.2f} of the export's own column for every row (worst {worst}; rounding)",
        file=out,
    )
    qb_delta = sum(
        row["points"] - row["stats"]["fpts"] for row in projected if row["position"] == "QB"
    )
    other_gap = max(
        abs(row["points"] - row["stats"]["fpts"])
        for row in projected
        if row["position"] != "QB"
    )
    print(
        f"  league scoring (-2/INT) moves the {counts['QB']} QBs by {qb_delta:+.1f} points in "
        f"total; RB/WR/TE agree with FPTS to within {other_gap:.2f}",
        file=out,
    )

    # -- join --------------------------------------------------------------
    age = fetch_sleeper.age_hours(meta)
    print(
        f"\nsleeper: dump {index.player_count} players, {index.considered} at a pool position"
        + (f", fetched {age / 24:.1f} days ago" if age is not None else ""),
        file=out,
    )
    by_tier = diagnostics["by_tier"]
    print(
        f"  matched {sum(by_tier.values())}/{len(projected)}: "
        + ", ".join(f"{tier} {count}" for tier, count in by_tier.items()),
        file=out,
    )
    suffix_joins = diagnostics["joins"]["name_without_suffix"]
    print(f"\ntier 2 — suffix dropped ({len(suffix_joins)})", file=out)
    for row, player in suffix_joins:
        print(
            f"  {row['name']:<24} -> {match_sleeper.full_name_of(player):<22} "
            f"{player.get('position')} {player.get('team')} id={player['player_id']}",
            file=out,
        )
    last_joins = diagnostics["joins"]["last_name_team"]
    print(f"\ntier 3 — last name + team, first names differ ({len(last_joins)})", file=out)
    for row, player in last_joins:
        print(
            f"  {row['name']:<24} ({row['position']} {row['team']}) -> "
            f"{match_sleeper.full_name_of(player):<22} {player.get('position')} "
            f"{player.get('team')} age {player.get('age')} id={player['player_id']}",
            file=out,
        )
    unmatched = diagnostics["unmatched"]
    print(f"\nunmatched ({len(unmatched)})", file=out)
    for row in unmatched:
        print(
            f"  {row['name']:<24} {row['position']} {row['team']} {row['points']:>6} pts",
            file=out,
        )

    # -- pool --------------------------------------------------------------
    per_pos = collections.Counter(row["position"] for row in pool)
    print(
        "\npool: " + ", ".join(f"{pos} {per_pos[pos]}" for pos in POSITIONS)
        + f" = {len(pool)}, {sum(row['is_rookie'] for row in pool)} rookies; "
        f"team known {sum(row['team'] is not None for row in pool)}/{len(pool)}, "
        f"age known {sum(row['age'] is not None for row in pool)}/{len(pool)}",
        file=out,
    )
    ranks = [row["rank"] for row in pool]
    monotone = all(a["points_1yr"] >= b["points_1yr"] for a, b in zip(pool, pool[1:]))
    print(
        f"integrity: rank 1..{len(pool)} gap-free: {ranks == list(range(1, len(pool) + 1))}; "
        f"unique ids: {len({row['player_id'] for row in pool}) == len(pool)}; "
        f"monotone in points_1yr: {monotone}; fields per player: {len(pool[0])}",
        file=out,
    )
    print("\ntop 5 and the last 2 in the pool", file=out)
    for row in pool[:5] + pool[-2:]:
        print(
            f"  {row['rank']:>3} {row['name']:<22} {row['position']}{row['positional_rank']:<3} "
            f"{row['points_1yr']:>6} pts  {row['team'] or '---'}  id={row['sleeper_id']}",
            file=out,
        )
    print(file=out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-o", "--output", default=paths.POOL, type=Path)
    ap.add_argument(
        "--players",
        default=paths.SLEEPER_PLAYERS,
        type=Path,
        help="the Sleeper dump (fetch_sleeper.py writes it)",
    )
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)

    if not args.players.is_file():
        print(
            f"error: sleeper dump {paths.display(args.players)} not found — run "
            "`uv run pool_pipeline/fetch_sleeper.py` (manual, ~14 MB)",
            file=sys.stderr,
        )
        return 1

    rows: list[dict] = []
    try:
        for position, path in paths.PROJECTIONS_CSV.items():
            rows.extend(read_projections(position, path))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for row in rows:
        row["points"] = score(row["stats"])
    projected = [row for row in rows if row["points"] > 0]

    with args.players.open(encoding="utf-8") as handle:
        dump = json.load(handle)
    meta = fetch_sleeper.load_meta(args.players.with_suffix(".meta.json"))
    age = fetch_sleeper.age_hours(meta)
    if age is None or age / 24 > STALE_AFTER_DAYS:
        print(
            "warning: sleeper dump is "
            + ("of unknown age" if age is None else f"{age / 24:.0f} days old")
            + "; players added since cannot match — re-run fetch_sleeper.py",
            file=sys.stderr,
        )
    index = match_sleeper.SleeperIndex(dump)

    joined, diagnostics = join(projected, index)
    for row, tier, clash in diagnostics["ambiguous"]:
        print(
            f"error: {row['name']} ({row['position']} {row['team']}, {row['points']} pts) is "
            f"ambiguous at tier {tier!r}: "
            + ", ".join(
                f"{match_sleeper.full_name_of(p)} ({p.get('position')} {p.get('team')} "
                f"age {p.get('age')}, id={p['player_id']})"
                for p in clash
            ),
            file=sys.stderr,
        )
    if diagnostics["duplicates"]:
        print(
            "error: sleeper id(s) matched more than one projection row: "
            f"{diagnostics['duplicates']}",
            file=sys.stderr,
        )
    if diagnostics["ambiguous"] or diagnostics["duplicates"]:
        return 1
    for row in diagnostics["unmatched"]:
        print(
            f"warning: no Sleeper {row['position']} named {row['name']} ({row['team']}, "
            f"{row['points']} pts) — left out",
            file=sys.stderr,
        )

    pool = build_rows(joined)
    document = build_document(pool, len(rows) - len(projected), diagnostics, index, meta)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    per_pos = collections.Counter(row["position"] for row in pool)
    print(
        f"pool: {len(pool)} players ("
        + ", ".join(f"{pos} {per_pos[pos]}" for pos in POSITIONS)
        + f") -> {paths.display(args.output)}",
        file=sys.stderr,
    )
    if args.report:
        report(rows, projected, pool, diagnostics, index, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
