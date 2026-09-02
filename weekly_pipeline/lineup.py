#!/usr/bin/env python3
"""In-season lineup and waiver check from blended weekly projections.

Two weekly projection sources are blended per player:

- FantasyPros' weekly consensus, exported by hand into ``data/``: the QB page and the
  FLX page (RB, WR and TE in one file) of https://www.fantasypros.com/nfl/projections/,
  weekly view, half-PPR scoring. Unauthenticated page loads show ten rows, so the export
  is the only complete form. The export carries no week number: it is assumed to be the
  current Sleeper week.
- Sleeper's weekly projections (Rotowire), fetched live.

Both stat lines are scored under the league's settings (``pool_pipeline/build_pool.py``
``SCORING``), then combined by ``WEIGHTS``. A player only one source projects takes that
source alone. The lineup is the best legal one for the week. Waiver candidates are every
unrostered player, valued two ways: this week's blended points, and the season-long
change in expected lineup points from the ranker's roster model when he replaces each of
our players.

    uv run weekly_pipeline/lineup.py
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pool_pipeline"))

import match_sleeper  # noqa: E402
from build_pool import read_projections, score  # noqa: E402
from ranker.league import SLOT_ELIGIBLE, STARTING_SLOTS  # noqa: E402
from ranker.pool import by_position, load_pool  # noqa: E402
from ranker.value import sorted_by_horizon, team_value, wire_replacement  # noqa: E402

#: FantasyPros aggregates several projection sets; Sleeper republishes one (Rotowire).
WEIGHTS = {"fantasypros": 0.6, "sleeper": 0.4}
DATA_DIR = HERE / "data"
QB_CSV = DATA_DIR / "FantasyPros_Fantasy_Football_Projections_QB.csv"
FLX_CSV = DATA_DIR / "FantasyPros_Fantasy_Football_Projections_FLX.csv"
#: The FLX export adds a POS column ("WR12") to the RB layout, and lists the rare
#: rushing QB, which the QB file already covers.
FLX_HEADER = ["Player", "Team", "POS", "ATT", "YDS", "TDS", "REC", "YDS", "TDS", "FL", "FPTS"]
FLX_STATS = ("rush_att", "rush_yd", "rush_td", "rec", "rec_yd", "rec_td", "fum_lost", "fpts")
SLEEPER_PLAYERS = ROOT / "pool_pipeline" / "data" / "sleeper_players.json"
API = "https://api.sleeper.app"
WAIVER_CANDIDATES = 12


def get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as response:
        return json.load(response)


def read_flx(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] != FLX_HEADER:
        raise ValueError(f"{path.name}: header {rows[0] if rows else None} is not {FLX_HEADER}")
    out = []
    for row in rows[1:]:
        if len(row) != len(FLX_HEADER) or not row[0].strip():
            continue  # spacer row
        position = row[2].rstrip("0123456789")
        if position == "QB":
            continue
        out.append(
            {
                "name": row[0].strip(),
                "team": row[1].strip(),
                "position": position,
                "stats": dict(zip(FLX_STATS, (float(cell) for cell in row[3:]))),
            }
        )
    return out


def fantasypros_points(index: match_sleeper.SleeperIndex) -> dict[str, float]:
    for path in (QB_CSV, FLX_CSV):
        if not path.exists():
            sys.exit(f"missing {path.relative_to(ROOT)}: export this week's view")
    out: dict[str, float] = {}
    for row in read_projections("QB", QB_CSV) + read_flx(FLX_CSV):
        player, _, clash = match_sleeper.match(row, index)
        if player is None:
            if clash:
                sys.exit(f"ambiguous FantasyPros name {row['name']} {row['team']}")
            continue  # unmatched names are FantasyPros depth Sleeper does not list
        out[player["player_id"]] = score(row["stats"])
    return out


def sleeper_points(season: str, week: int) -> dict[str, dict]:
    positions = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE"))
    url = f"{API}/projections/nfl/{season}/{week}?season_type=regular&{positions}"
    out = {}
    for row in get(url):
        if row["stats"] and row["team"]:
            player = row["player"]
            out[row["player_id"]] = {
                "points": score(row["stats"]),
                "name": f"{player['first_name']} {player['last_name']}",
                "position": player["position"],
                "team": row["team"],
                "opponent": row.get("opponent"),
                "date": row.get("date"),
                "injury": player.get("injury_status"),
            }
    return out


def blend(fp: float | None, sl: float | None) -> float:
    if fp is None:
        return sl
    if sl is None:
        return fp
    return WEIGHTS["fantasypros"] * fp + WEIGHTS["sleeper"] * sl


def best_lineup(ids: list[str], position: dict[str, str], points: dict[str, float]):
    """Dedicated slots first, then FLEX, then SF: exact for this slot chain."""
    left = sorted(ids, key=lambda p: -points[p])
    out = []
    for slot in ("QB", "RB", "WR", "TE", "FLEX", "SF"):
        for _ in range(STARTING_SLOTS[slot]):
            pick = next(p for p in left if position[p] in SLOT_ELIGIBLE[slot])
            left.remove(pick)
            out.append((slot, pick))
    return out, left


def main() -> int:
    draft = json.loads((ROOT / "draft.json").read_text())
    league_id, my_roster = draft["league_id"], draft["me"]["roster_id"]
    state = get(f"{API}/v1/state/nfl")
    season, week = state["season"], state["week"]
    rosters = get(f"{API}/v1/league/{league_id}/rosters")
    me = next(t for t in rosters if t["roster_id"] == my_roster)
    active = [p for p in me["players"] if p not in (me.get("reserve") or [])]
    rostered = {p for t in rosters for p in (t["players"] or [])}

    dump = json.loads(SLEEPER_PLAYERS.read_text())
    index = match_sleeper.SleeperIndex(dump)
    fp = fantasypros_points(index)
    sl = sleeper_points(season, week)

    def info(pid: str) -> dict:
        if pid in sl:
            return sl[pid]
        player = dump[pid]
        return {
            "name": f"{player['first_name']} {player['last_name']}",
            "position": player["position"],
            "team": player.get("team"),
            "opponent": None,
            "date": None,
            "injury": player.get("injury_status"),
        }

    ids = set(fp) | set(sl)
    points = {p: blend(fp.get(p), sl[p]["points"] if p in sl else None) for p in ids}
    position = {p: info(p)["position"] for p in ids}
    for pid in active:
        if pid not in ids:
            sys.exit(f"no weekly projection for rostered {info(pid)['name']} ({pid})")

    def line(pid: str) -> str:
        i = info(pid)
        both = f"fp={fp.get(pid, float('nan')):5.1f} sl={sl[pid]['points'] if pid in sl else float('nan'):5.1f}"
        return (
            f"{i['position']:3} {i['name']:22} {i['team'] or '-':4} vs {i['opponent'] or '-':4} "
            f"{i['date'] or '':11} {points[pid]:5.1f}  ({both})  {i['injury'] or ''}"
        )

    starters, bench = best_lineup(active, position, points)
    print(f"Week {week} lineup, {WEIGHTS['fantasypros']:.0%} FantasyPros / {WEIGHTS['sleeper']:.0%} Sleeper")
    for slot, pid in starters:
        print(f"  {slot:4} {line(pid)}")
    print(f"  total {sum(points[p] for _, p in starters):.1f}")
    print("  bench")
    for pid in sorted(bench, key=lambda p: -points[p]):
        print(f"       {line(pid)}")
    chosen = {p for _, p in starters}
    current = set(me["starters"])
    if chosen != current:
        print("  start:", ", ".join(info(p)["name"] for p in chosen - current))
        print("  sit:  ", ", ".join(info(p)["name"] for p in current - chosen))
    else:
        print("  Sleeper lineup already matches")

    # Season-long check with the ranker's roster model, over Sleeper's real rosters.
    players, _ = load_pool(ROOT / "pool.json")
    by_sid = {p.sleeper_id: p for p in players}
    taken = {by_sid[p].player_id for p in rostered if p in by_sid}
    pos = by_position(players)
    roster = [by_sid[p] for p in active if p in by_sid]
    base = team_value(sorted_by_horizon(roster), wire_replacement(taken, pos))
    swaps = []
    for add in (p for p in players if p.player_id not in taken):
        wire = wire_replacement(taken | {add.player_id}, pos)
        for drop in roster:
            new = [p for p in roster if p is not drop] + [add]
            swaps.append((team_value(sorted_by_horizon(new), wire) - base, add, drop))
    swaps.sort(key=lambda s: -s[0])

    worst_bench = min(points[p] for p in bench)
    print(f"\nFree agents: best season swap per player (base {base:.1f} expected lineup points)")
    print(f"  weekly points vs our worst bench {worst_bench:.1f}")
    seen = set()
    shown = 0
    for delta, add, drop in swaps:
        if add.player_id in seen:
            continue
        seen.add(add.player_id)
        weekly = points.get(add.sleeper_id)
        weekly_text = f"{weekly:5.1f}" if weekly is not None else "  n/a"
        print(
            f"  {delta:+6.1f} season  {weekly_text} wk  add {add.name:22} {add.position} "
            f"{add.points_1yr:6.1f}  drop {drop.name}"
        )
        shown += 1
        if shown == WAIVER_CANDIDATES:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
