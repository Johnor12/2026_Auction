#!/usr/bin/env python3
"""Rest-of-season waiver check: refetch projections and the league, then suggest FAAB claims.

Projections. Each run pulls Sleeper's weekly projections (Rotowire) for every week of the
season through ``LAST_WEEK``, scored as ``lineup.py`` scores them. A week with FantasyPros
exports in ``data/week<N>/`` (the QB and FLX files ``lineup.py`` reads) is blended with
them by ``lineup.WEIGHTS``; any other week is Sleeper alone. A week a player is not
projected counts as zero, which is how the sources mark byes and the absences they expect.

Roster value. Expected lineup points summed over the remaining weeks, each week solved by
the ranker's closed-form expected lineup (``ranker/value.py``): a projected week is the
player's if-active points, missed at his position's injury rate
(``INJURY_UNAVAILABLE_RATE``; byes are exact here, so the ranker's averaged bye is left
out). In later weeks the best other free agent at each position is one always-available
backup, as in the ranker, standing in for the streaming those weeks will allow; this week
has none, because this week's pickups are the claims being weighed. A player we pass on
is assumed claimed by someone else and one we drop is gone, so both sides of a move see
the same wire.

IR. A player is sidelined in a week he is not projected and his team plays, if he can go
on IR then: this week his Sleeper status must be one the league allows (``IR_STATUSES``),
later weeks need only a current injury tag. Up to ``IR_SLOTS`` sidelined players sit on
IR and everyone else needs one of the 14 active spots. Whenever that overflows (a claim
now, an IR player returning later) the drop is whoever leaves the roster worth the most
from that week on, which can be an IR player once a third is sidelined. This week a
projected player who is IR-eligible may instead sit on IR, deferring the drop a week.
This week's IR placements, activations and drops are printed as moves.

Claims. A free agent's gain is the change in roster value from adding him. ``market.py``
fits opponents' bidding to this season's claims and prices a point in dollars; each
suggested bid maximizes P(win) x (gain - bid in points).

    uv run weekly_pipeline/waivers.py
"""

from __future__ import annotations

import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pool_pipeline"))

import market  # noqa: E402
import match_sleeper  # noqa: E402
from build_pool import score  # noqa: E402
from lineup import (  # noqa: E402
    API,
    DATA_DIR,
    SLEEPER_PLAYERS,
    blend,
    fantasypros_points,
    get,
    sleeper_points,
)
from ranker.league import (  # noqa: E402
    DEDICATED_SLOTS,
    INJURY_UNAVAILABLE_RATE,
    NON_TAXI_SLOTS,
    POSITIONS,
)

# The pieces of ranker.value.expected_lineup_value, recomposed with an injury-only rate.
from ranker.value import (  # noqa: E402
    _EXTRA_SLOTS,
    _extra_count_table,
    _extra_expected_value,
    _position_expected_values,
)

#: Six playoff teams from week 14 with a two-week final.
LAST_WEEK = 17
IR_SLOTS = 2
#: What this league lets onto IR: Out (reserve_allow_out), and IR and PUP always.
IR_STATUSES = {"IR", "PUP", "Out"}
#: A current tag that can turn into one of those by a later week.
INJURY_TAGS = IR_STATUSES | {"Doubtful", "Questionable"}
#: Smaller season gains are rounding, not a pickup.
MIN_GAIN = 0.1
SHOWN = 15


def week_value(bodies: dict[str, list[tuple[int, float]]], backup: dict[str, float]) -> float:
    """One week's expected best lineup from unconditional (id, points) bodies."""
    total = 0.0
    tables = {}
    for pos in POSITIONS:
        rate = INJURY_UNAVAILABLE_RATE[pos]
        rows = tuple(sorted(bodies[pos]))
        dedicated = DEDICATED_SLOTS[pos]
        total += _position_expected_values(rows, backup[pos], rate, dedicated)[dedicated]
        cap = 1 if pos == "QB" else _EXTRA_SLOTS
        tables[pos] = _extra_count_table(rows, backup[pos], rate, dedicated, cap)
    return total + _extra_expected_value(tables["QB"], (tables["RB"], tables["WR"], tables["TE"]))


class Season:
    """Points in every week of the season (index week - 1), valued from decision week `now` on."""

    def __init__(self, points: dict[str, list[float]], position: dict[str, str], now: int, sidelined=None, parkable=()):
        self.points = points
        self.position = position
        self.now = now
        self.sidelined = sidelined  # pid -> can sit on IR, per week from `now`
        self.parkable = set(parkable)  # IR-eligible today, so may sit on IR even when projected

    def week(self, roster: list[str], wire: dict[str, str | None], w: int) -> float:
        # The solver takes unconditional points: if-active points times availability.
        bodies = {pos: [] for pos in POSITIONS}
        for p in roster:
            if self.points[p][w] > 0:
                keep = 1 - INJURY_UNAVAILABLE_RATE[self.position[p]]
                bodies[self.position[p]].append((int(p), self.points[p][w] * keep))
        # No free backup in the decision week: its pickups are the claims being weighed.
        backup = {
            pos: self.points[wire[pos]][w] * (1 - INJURY_UNAVAILABLE_RATE[pos])
            if wire[pos] and w != self.now
            else 0.0
            for pos in POSITIONS
        }
        return week_value(bodies, backup)

    def total(self, roster: list[str], wire: dict[str, str | None], start: int) -> float:
        return sum(self.week(roster, wire, w) for w in range(start, LAST_WEEK))

    def plan(
        self, roster: list[str], wire: dict[str, str | None], reserve: list[str]
    ) -> tuple[list[float], list[str], list[str]]:
        """Value per week from now under the roster limits, with this week's IR set and drops.

        Over the limit, the roster sheds whoever leaves it worth the most from that week on.
        This week a projected player who is IR-eligible may sit on IR instead, deferring
        the drop a week; that wins when his points this week are worth less than the drop's.
        """
        keep = list(roster)
        values, ir_now, drops, parked = [], [], [], []
        for w in range(self.now, LAST_WEEK):
            if w > self.now:
                parked = []
            while True:
                side = [p for p in keep if self.sidelined[p][w - self.now]]
                on_ir = min(IR_SLOTS, len(side)) + len(parked)
                if len(keep) - on_ir <= NON_TAXI_SLOTS:
                    break
                playing = [p for p in keep if p not in parked]
                # Dropping an IR player only helps when another sidelined one takes his slot.
                movable = [p for p in playing if p not in side] + (side if len(side) > IR_SLOTS else [])
                later = [self.total([q for q in keep if q != p], wire, w + 1) for p in movable]
                options = [
                    (self.week([q for q in playing if q != p], wire, w) + rest, p, False)
                    for p, rest in zip(movable, later)
                ]
                if w == self.now and on_ir < IR_SLOTS:
                    options += [
                        (self.week([q for q in playing if q != p], wire, w) + max(later), p, True)
                        for p in playing
                        if p in self.parkable and p not in side
                    ]
                _, p, park = max(options, key=lambda o: o[0])
                if park:
                    parked.append(p)
                else:
                    keep.remove(p)
                    if w == self.now:
                        drops.append(p)
            if w == self.now:
                ir_now = sorted(side, key=lambda p: p not in reserve)[:IR_SLOTS] + parked
            values.append(self.week([p for p in keep if p not in parked], wire, w))
        return values, ir_now, drops


def actual_points(season: str, week: int) -> dict[str, float]:
    positions = "&".join(f"position[]={p}" for p in POSITIONS)
    rows = get(f"{API}/stats/nfl/{season}/{week}?season_type=regular&{positions}")
    return {row["player_id"]: score(row["stats"]) for row in rows if row["stats"]}


def main() -> int:
    draft = json.loads((ROOT / "draft.json").read_text())
    league_id, my_roster = draft["league_id"], draft["me"]["roster_id"]
    state = get(f"{API}/v1/state/nfl")
    season, week = state["season"], state["week"]
    if state["season_type"] != "regular" or week > LAST_WEEK:
        sys.exit(f"no fantasy weeks left: Sleeper is at {state['season_type']} week {week}")
    league = get(f"{API}/v1/league/{league_id}")
    rosters = {t["roster_id"]: t for t in get(f"{API}/v1/league/{league_id}/rosters")}
    with ThreadPoolExecutor(16) as pool:
        weekly = list(pool.map(lambda w: sleeper_points(season, w), range(1, LAST_WEEK + 1)))
        actual = list(pool.map(lambda w: actual_points(season, w), range(1, week)))
        rounds = pool.map(lambda r: get(f"{API}/v1/league/{league_id}/transactions/{r}"), range(1, week + 1))
        transactions = [t for r in rounds for t in r]

    # Player info from the current week on first, so team and injury tag are today's.
    info: dict[str, dict] = {}
    for rows in weekly[week - 1 :] + weekly[: week - 1]:
        for pid, row in rows.items():
            info.setdefault(pid, row)
    # A bye week still lists the team's players, at zero points.
    teams = {row["team"] for rows in weekly for row in rows.values()}
    bye = [teams - {row["team"] for row in rows.values() if row["points"] > 0} for rows in weekly]

    dump = json.loads(SLEEPER_PLAYERS.read_text())
    index = match_sleeper.SleeperIndex(dump)
    points: dict[str, list[float]] = {}
    fp_weeks = []
    for w, rows in enumerate(weekly, start=1):
        folder = DATA_DIR / f"week{w}"
        fp = fantasypros_points(index, folder) if folder.exists() else {}
        if fp:
            fp_weeks.append(w)
            clash = sorted({info[p]["team"] for p in fp if p in info} & bye[w - 1])
            if clash:
                sys.exit(
                    f"{folder.relative_to(ROOT)} projects {', '.join(clash)}, on bye in week {w}: "
                    "is it another week's export?"
                )
        for pid in fp.keys() | rows.keys():
            sleeper = rows[pid]["points"] if pid in rows else None
            points.setdefault(pid, [0.0] * LAST_WEEK)[w - 1] = blend(fp.get(pid), sleeper)

    def known(pid: str) -> None:
        """Give a player Sleeper never projects (or only FantasyPros does) info and zero weeks."""
        if pid not in info:
            if pid not in dump:
                sys.exit(
                    f"Sleeper player {pid} is not in {SLEEPER_PLAYERS.relative_to(ROOT)}: "
                    "rerun `uv run pool_pipeline/fetch_sleeper.py`"
                )
            player = dump[pid]
            info[pid] = {
                "name": f"{player['first_name']} {player['last_name']}",
                "position": player["position"],
                "team": player.get("team"),
                "injury": player.get("injury_status"),
            }
        points.setdefault(pid, [0.0] * LAST_WEEK)

    me = rosters[my_roster]
    mine, reserve = me["players"], me.get("reserve") or []
    claims = [t for t in transactions if t["type"] == "waiver" and t["roster_ids"][0] != my_roster]
    rostered = {p for t in rosters.values() for p in t["players"] or []}
    moved = [p for t in transactions for p in {**(t["adds"] or {}), **(t["drops"] or {})}]
    for pid in [*points, *rostered, *moved]:
        known(pid)
    position = {p: i["position"] for p, i in info.items()}

    now = week - 1
    sidelined = {
        p: [
            points[p][w] == 0
            and info[p]["team"] not in bye[w]
            and info[p]["injury"] in (IR_STATUSES if w == now else INJURY_TAGS)
            for w in range(now, LAST_WEEK)
        ]
        for p in points
    }
    parkable = [p for p in points if info[p]["injury"] in IR_STATUSES]
    valuation = Season(points, position, now, sidelined, parkable)

    # --- the market, fit to every opponent claim so far --------------------------------
    start = [min(r["date"] for r in rows.values() if r["date"]) for rows in weekly]
    end = [max(r["date"] for r in rows.values() if r["date"]) for rows in weekly]
    current = {rid: t["players"] or [] for rid, t in rosters.items()}
    history = []
    for t in claims:
        rid, add = t["roster_ids"][0], next(iter(t["adds"]))
        if position[add] not in POSITIONS:
            continue  # no roster slot values him
        drop = next(iter(t["drops"] or {}), None)
        day = datetime.fromtimestamp(t["status_updated"] / 1000, timezone.utc).date().isoformat()
        then = market.rosters_before(current, transactions, t["status_updated"])
        taken = {p for players in then.values() for p in players}
        pool_then = [p for p in points if p not in taken and position[p] in POSITIONS]
        done = [w for w in range(week - 1) if end[w] < day]
        rank = None
        if done:
            last = actual[done[-1]]
            rank = 1 + sum(last.get(p, 0.0) > last.get(add, 0.0) for p in pool_then)
        s = next(w for w in range(LAST_WEEK) if start[w] >= day)
        free_then = sorted((p for p in pool_then if p != add), key=lambda p: -sum(points[p][s:]))
        wire = {pos: next((p for p in free_then if position[p] == pos), None) for pos in POSITIONS}
        roster = [p for p in then[rid] if position[p] in POSITIONS]
        new = [p for p in roster if p != drop] + [add]
        decided = Season(points, position, s)
        gain = decided.total(new, wire, s) - decided.total(roster, wire, s)
        history.append({"roster_id": rid, "bid": t["settings"]["waiver_bid"], "rank": rank, "gain": gain})
    fitted = market.fit(history, week - 1)

    # --- our claims ---------------------------------------------------------------------
    ros = {p: sum(v[now:]) for p, v in points.items()}
    free = sorted(
        (p for p in points if p not in rostered and ros[p] > 0 and position[p] in POSITIONS),
        key=lambda p: -ros[p],
    )

    def wire_without(add: str | None) -> dict[str, str | None]:
        return {pos: next((p for p in free if position[p] == pos and p != add), None) for pos in POSITIONS}

    def moves(ir_now: list[str], drops: list[str]) -> list[str]:
        out = [f"IR {info[p]['name']}" for p in ir_now if p not in reserve]
        out += [f"activate {info[p]['name']}" for p in reserve if p not in ir_now and p not in drops]
        return out + [f"drop {info[p]['name']}" for p in drops]

    bases: dict[tuple, tuple] = {}

    def base(wire: dict[str, str | None]) -> tuple:
        key = tuple(wire.values())
        if key not in bases:
            bases[key] = valuation.plan(mine, wire, reserve)
        return bases[key]

    candidates = []
    for add in free:
        wire = wire_without(add)
        old, old_ir, old_drops = base(wire)
        values, ir_now, drops = valuation.plan(mine + [add], wire, reserve)
        gain = sum(values) - sum(old)
        if gain > MIN_GAIN and add not in drops:
            # A claim's moves replace the no-claim ones, so list all of them.
            kept = [f"leave {info[p]['name']} on IR" for p in ir_now if p in reserve and p not in old_ir]
            candidates.append((gain, values[0] - old[0], add, kept + moves(ir_now, drops)))

    last = actual[week - 2]
    pool = sorted(
        (p for p in points if p not in rostered and position[p] in POSITIONS),
        key=lambda p: (-last.get(p, 0.0), -ros[p]),
    )
    rank_of = {p: i for i, p in enumerate(pool)}
    budget = league["settings"]["waiver_budget"]
    faab = budget - me["settings"]["waiver_budget_used"]
    priority = {rid: t["settings"]["waiver_position"] for rid, t in rosters.items()}
    chances = market.win_chances(
        fitted, len(pool), [rank_of[c[2]] for c in candidates], priority, priority[my_roster], faab
    )
    suggested = []
    for (gain, now_gain, add, extra), chance in zip(candidates, chances):
        bid, win, ev = market.best_bid(chance, gain, fitted.price)
        if ev > 0:
            suggested.append((ev, bid, win, gain, now_gain, add, extra))
    suggested.sort(key=lambda c: -c[0])

    # --- report -------------------------------------------------------------------------
    base_values, base_ir, base_drops = base(wire_without(None))
    blended = ", ".join(str(w) for w in fp_weeks if w >= week)
    print(
        f"Weeks {week}-{LAST_WEEK}: {sum(base_values):.1f} expected lineup points "
        f"({f'FantasyPros blended into weeks {blended}' if blended else 'Sleeper projections only'}); "
        f"FAAB ${faab} of ${budget}, waiver priority {priority[my_roster]}"
    )
    print("Without a claim: " + ("; ".join(moves(base_ir, base_drops)) or "no moves"))
    print(
        f"Market: {len(history)} opponent claims, {sum(fitted.rates.values()):.1f} a week, bids "
        f"${min(fitted.bids)}-${max(fitted.bids)} (median ${statistics.median(fitted.bids):g}); "
        f"targets follow last week's free-agent scoring (q {fitted.q:.2f}); "
        f"the league pays ${fitted.price:.2f} a point"
    )
    print(f"Claims by expected value (points net of the bid; gains over the season and week {week})")
    for ev, bid, win, gain, now_gain, add, extra in suggested[:SHOWN]:
        i = info[add]
        print(
            f"  ${bid:<3} win {win:4.0%}  EV {ev:+5.1f}  gain {gain:+5.1f} {now_gain:+5.1f}  "
            f"add {i['name']:22} {i['position']:2} {i['team'] or '-':3} {ros[add]:6.1f} ros  "
            f"{i['injury'] or '':12} {'; '.join(extra) or '-'}"
        )
    if not suggested:
        print("  none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
