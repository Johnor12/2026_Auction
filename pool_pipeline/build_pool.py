#!/usr/bin/env python3
"""Cut projections.json down to this league's draft pool: one row, one value column.

Stage 2 of the build. ``parse_projections.py`` produces the full provider export —
900 players x 8 scoring schemes x 4 horizons, ~2.9 MB, most of it irrelevant to a
10-team superflex dynasty draft. This narrows it to what a draft board actually
consumes and drops everything else:

    900 players  ->  QB/RB/WR/TE only        (K and IDP have no roster slot)
                 ->  a usable 3-year point total (~444 players; all of them kept)

    9 schemes x 4 horizons  ->  two columns: 3-year and 1-year points in this
                                league's scoring
    8 ADP columns           ->  one column: superflex ADP, as an overall pick number

**Scoring.** The league is 0.5/rec with a 0.5/rec tight end premium (so 1.0/rec for
TEs) in superflex. Draftsharks publishes eight schemes and none of them is that one,
but for *points* no arithmetic is needed, because each position's target reception
rate is already priced exactly by one published column:

    TE       -> ppr        (1.0/rec)
    QB/RB/WR -> half_ppr   (0.5/rec)

Cells are copied from those columns, not computed. The algebraic route,
``half_ppr + (te_premium - ppr)``, is an identity on raw points — with B =
non-reception points and R = receptions, (B + 0.5R) + (B + 1.5R) - (B + R) = B + R,
i.e. exactly 1.0/rec for a TE, collapsing to half_ppr for everyone else because
te_premium == ppr there — but the published columns are pre-rounded integers, so
evaluating it drifts +-1 on 45 of 454 offensive 3-year cells for no gain. The 1QB
family is used because point totals cannot depend on roster settings and that family
is the internally consistent one; ``--report`` re-checks both identities.

Points are the only value columns kept — the 3-year total the ranker sorts on, and the
1-year total whose gap against it is the provider's implied growth (the ranker prices
bench upside off the years-2-3 pace). 3D value is deliberately not carried over: it
is a provider-scaled ordinal (best player pinned at 100, ~half the league negative)
that already bakes in someone else's roster assumptions, is not in points, and so
cannot enter a points-based expected-lineup value — which is how ``rank.py`` prices
the pool.

**ADP** is the superflex ADP, copied. All four superflex scoring styles are identical
here (the source's ADP responds only to 1QB vs superflex), so there is no TE-premium
signal to transfer and none is invented. The source encodes it as round.pick for a
12-team draft — "2.03" is round 2 pick 3, not a decimal — which is both a different
league size than ours and a trap for anything that sorts numerically, so it is decoded
to an integer overall pick. Its deep tail is provider noise: 48 pool players sit past
round 45 of a 12-team draft, so a pick number in the thousands means "effectively
undrafted", not a real slot.

**Ranking.** The pool is ordered by 3-year points descending, ties broken by the
provider's dynasty rank. So the emitted ``rank`` is verifiable from the emitted
``points_3yr`` column and the file references no quantity it does not contain. Every
eligible player is kept: an earlier top-350 cut dropped players the market drafts
inside this league's 290 picks — the retirement-discounted Aaron Rodgers (3yr 166 but
1yr 258, eligible rank 355) and near-miss RBs like Najee Harris and DJ Giddens — to
save ~90 rows nobody needed saved.

**Cleanups applied.** Players with a 0 or missing 3-year projection are dropped rather
than ranked last (the source uses 0 where a null belongs) — except the ones in
``SYNTHETIC_PROJECTIONS``, whose zeroes are a provider hole and get overwritten first.
A career-edge veteran can publish a 1-year projection above his 3-year total (Aaron
Rodgers 2026: 258 vs 166 — the source discounts future seasons by retirement odds, its
season projection does not). Downstream prices years 2-3 as ``points_3yr -
points_1yr``, and a negative future season is not a real quantity, so the 3-year cell
is raised to the 1-year value: a win-now season and zeroed future seasons. The 21 teamless players
carry ``bye_week: 18``, a sentinel — real byes run weeks 5-14 — so their bye is
nulled. Stale printed ranks, undocumented percent_low/percent_high, hidden_row,
analyst comments and profile paths are not carried: recover them from projections.json,
which this script only reads.

Usage:
    uv run build_pool.py [projections.json] [-o pool.json] [--limit 350] [--report]
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
from pathlib import Path

import paths

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCHEME = "half_ppr_te_premium_superflex"
HORIZON = "3yr"
POINTS_FIELD = f"points_{HORIZON}"

#: Roster slots exist for these only; K and IDP (DB/DL/LB) are dropped whole.
POSITIONS = ("QB", "RB", "WR", "TE")

#: Effectively no cut: only ~444 of the 900 source rows are offensive players with a
#: usable projection, and the old 350 cut dropped market-drafted players (see docstring).
RANK_LIMIT = 1000

#: The published 1QB column that already prices each position's reception rate.
POINTS_COLUMNS = {"TE": "ppr"}
POINTS_COLUMN_DEFAULT = "half_ppr"

#: ADP is roster-format dependent only. These four are identical in this export;
#: ADP_COLUMN is the one read, the rest are cross-checked by --report.
ADP_COLUMN = "half_ppr_superflex"
ADP_FAMILY = (
    "standard_superflex",
    "half_ppr_superflex",
    "ppr_superflex",
    "te_premium_superflex",
)

#: The source's ADP is round.pick for a 12-team draft. That is the provider's format,
#: not this league's size, and it is decoded rather than reinterpreted.
TEAMS_PER_ROUND = 12
PLAUSIBLE_ROUNDS = 45  # ~490 ranked players / 12 per round; beyond this is tail noise

#: Sentinels the source uses for unsigned players: no team, so no real bye week.
NO_TEAM = frozenset({"UNS", "RK"})
PLACEHOLDER_BYE = 18

#: Projections invented for players the source zero-fills but the market drafts.
#: Justin Fields (superflex ADP 214) is a Draftsharks data hole: 0 at every horizon.
#: Basis: median Draftsharks points of the QBs ranked within +-3 of him on each of the
#: four boards in data_source_investigator/data/rankings.json (FantasyCalc QB42,
#: KeepTradeCut QB43, DynastyNerds QB42, FantasyPros QB41; comps Cousins, Allar,
#: Richardson, Klubnik, Watson, Milroe, ...). Applied only while the source cell the
#: build reads is still 0/missing, so a real projection supersedes this on arrival.
SYNTHETIC_PROJECTIONS = {
    10754: {"name": "Justin Fields", "3yr": 388, "1yr": 28},
}

#: Players the source omits entirely but this league's market drafts. Same comp-median
#: basis as SYNTHETIC_PROJECTIONS, except the whole row is invented, not just the
#: points: Mac Jones (drafted pick 182 of this draft) sits QB35-39 on all four
#: investigator boards; his cells are the medians over the QBs within +-3 of him on
#: each board (Tua, McCarthy, Brissett, Rodgers, Watson, Cousins, Geno, Beck, Allar,
#: Richardson, Milroe, Klubnik): 3yr 406, 1yr 54, superflex ADP overall 222 -> 19.06
#: in the source's 12-team round.pick encoding. A QB's points are reception-blind, so
#: every scoring column carries the same number. Skipped once the source publishes a
#: real row under the same name and position.
_MAC_JONES_SCHEMES = [*ADP_FAMILY, "standard", "half_ppr", "ppr", "te_premium"]
SYNTHETIC_PLAYERS = [
    {
        "player_id": 900001,  # out-of-band: real source ids stay far below 900000
        "name": "Mac Jones",
        "position": "QB",
        "team": "SF",
        "age": 27.9,
        "bye_week": 8,
        "is_rookie": False,
        "rank_by_3d_value": None,
        "projections": {
            "3yr": {scheme: 406 for scheme in _MAC_JONES_SCHEMES},
            "1yr": {scheme: 54 for scheme in _MAC_JONES_SCHEMES},
        },
        "adp": {scheme: 19.06 for scheme in ADP_FAMILY},
    },
]

FIELD_DEFINITIONS = {
    "rank": (
        f"Pool rank, 1..N, by {POINTS_FIELD} descending (ties broken by the "
        "provider's dynasty rank). Unique and gap-free."
    ),
    "positional_rank": "Rank within position under the same ordering.",
    "player_id": "Provider player id. The only unique key — names collide.",
    "name": "Player name.",
    "position": "QB, RB, WR or TE.",
    "team": "NFL team abbreviation; 'UNS'/'RK' mean unsigned.",
    "age": "Age in years.",
    "bye_week": "Team bye week; null for unsigned players.",
    "is_rookie": "True for 2026 rookies (taxi-squad eligible).",
    POINTS_FIELD: (
        "Three-year projected fantasy points under this league's scoring: 0.5/rec "
        "with a 0.5/rec TE premium. Copied from the provider column that prices "
        "that rate exactly (TE: ppr, others: half_ppr). Never below points_1yr: a "
        "retirement-discounted 3yr cell is raised to the 1yr value (see 'adjustments')."
    ),
    "points_1yr": (
        "One-year projected points, same scoring and same source columns. The gap "
        f"between this and {POINTS_FIELD} is the provider's implied growth: the "
        "ranker prices bench upside off the years-2-3 pace, (points_3yr - "
        "points_1yr) / 2. A genuine 0 (e.g. a stashed rookie) is kept as 0."
    ),
    "adp": (
        "Superflex ADP as an overall pick number in the source's 12-team draft "
        f"(its round.pick value decoded: 2.03 -> 15). Past pick "
        f"{PLAUSIBLE_ROUNDS * TEAMS_PER_ROUND} the source's tail is noise, i.e. "
        "'effectively undrafted' rather than a real slot."
    ),
}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def points_column(position: str) -> str:
    return POINTS_COLUMNS.get(position, POINTS_COLUMN_DEFAULT)


def points_of(record: dict, horizon: str = HORIZON) -> int | None:
    """This league's point total at a horizon: a copy of the column that prices the rate."""
    return record["projections"][horizon].get(points_column(record["position"]))


def decode_adp(value: float, teams: int = TEAMS_PER_ROUND) -> int:
    """``2.03`` (round 2, pick 3) -> overall pick 15."""
    rnd = int(value)
    return (rnd - 1) * teams + round((value - rnd) * 100)


def adp_of(record: dict) -> int | None:
    value = record["adp"].get(ADP_COLUMN)
    return None if value is None else decode_adp(value)


def dynasty_rank(record: dict) -> float:
    """The provider's own overall rank, used only to break point ties."""
    rank = record.get("rank_by_3d_value")
    return math.inf if rank is None else rank


def apply_synthetic(records: list[dict]) -> list[str]:
    """Fill SYNTHETIC_PROJECTIONS into still-zero source cells. Returns audit notes."""
    by_id = {record["player_id"]: record for record in records}
    notes = []
    for player_id, synth in SYNTHETIC_PROJECTIONS.items():
        record = by_id.get(player_id)
        if record is None:
            notes.append(f"{synth['name']} ({player_id}): not in source, skipped")
            continue
        column = points_column(record["position"])
        if record["projections"]["3yr"].get(column):
            notes.append(f"{synth['name']}: source now has a real projection, skipped")
            continue
        record["projections"]["3yr"][column] = synth["3yr"]
        record["projections"]["1yr"][column] = synth["1yr"]
        notes.append(
            f"{synth['name']}: {column} 3yr={synth['3yr']} 1yr={synth['1yr']} "
            "(comp-median, see SYNTHETIC_PROJECTIONS)"
        )
    return notes


def add_synthetic_players(records: list[dict]) -> list[str]:
    """Append SYNTHETIC_PLAYERS the source still lacks. Returns audit notes."""
    present = {(record["name"].casefold(), record["position"]) for record in records}
    notes = []
    for synth in SYNTHETIC_PLAYERS:
        if (synth["name"].casefold(), synth["position"]) in present:
            notes.append(f"{synth['name']}: source now lists him, synthetic row skipped")
            continue
        records.append(synth)
        notes.append(
            f"{synth['name']}: fully synthetic row, absent from source "
            "(comp-median, see SYNTHETIC_PLAYERS)"
        )
    return notes


def zero_negative_futures(records: list[dict]) -> list[str]:
    """Raise a 3-year cell that sits below the 1-year one (see docstring). Notes what.

    Only a real 3-year projection is repaired — a 0 stays 0, meaning "no data", and the
    player is dropped by the usable-projection filter as before.
    """
    notes = []
    for record in records:
        if record.get("position") not in POSITIONS:
            continue  # K/IDP are full of these; they never reach the pool
        column = points_column(record["position"])
        horizons = record["projections"]
        p3, p1 = horizons["3yr"].get(column), horizons["1yr"].get(column)
        if p3 and p1 and p1 > p3:
            horizons["3yr"][column] = p1
            notes.append(
                f"{record['name']}: {column} 3yr {p3} -> {p1} (1yr exceeded the "
                "retirement-discounted 3yr; future seasons zeroed)"
            )
    return notes


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select(records: list[dict], limit: int = RANK_LIMIT) -> tuple[list[dict], dict]:
    """Filter to the relevant pool and order it. Returns (kept records, stats).

    Three cuts, in order: position, then a usable point total, then the rank limit.
    The rank cut has to come last — it is defined on the points column, so it is only
    meaningful once the rows without one are gone.
    """
    dropped_position: collections.Counter[str] = collections.Counter()
    unusable: list[str] = []
    eligible: list[dict] = []

    for record in records:
        position = record.get("position")
        if position not in POSITIONS:
            dropped_position[position or "?"] += 1
            continue
        points = points_of(record)
        if not points or points <= 0:
            unusable.append(record["name"])
            continue
        eligible.append(record)

    eligible.sort(key=lambda r: (-points_of(r), dynasty_rank(r), r["player_id"]))
    kept, cut = eligible[:limit], eligible[limit:]

    by_dynasty = sorted(eligible, key=lambda r: (dynasty_rank(r), r["player_id"]))[:limit]
    kept_ids = {record["player_id"] for record in kept}

    stats = {
        "dropped_position": dict(dropped_position.most_common()),
        "dropped_unusable": sorted(unusable),
        "right_position": len(eligible) + len(unusable),
        "eligible": len(eligible),
        "cut_by_rank": len(cut),
        "cut_at_points": points_of(kept[-1]) if kept else None,
        "best_points_cut": points_of(cut[0]) if cut else None,
        "dynasty_cut_would_swap": [
            (record["name"], record["position"], points_of(record))
            for record in by_dynasty
            if record["player_id"] not in kept_ids
        ],
    }
    return kept, stats


def build_rows(kept: list[dict]) -> list[dict]:
    """One flat, minimal record per player, in pool order."""
    seen: collections.Counter[str] = collections.Counter()
    rows = []
    for rank, record in enumerate(kept, start=1):
        position = record["position"]
        seen[position] += 1
        team = record.get("team")
        bye = record.get("bye_week")
        rows.append(
            {
                "rank": rank,
                "positional_rank": seen[position],
                "player_id": record["player_id"],
                "name": record["name"],
                "position": position,
                "team": team,
                "age": record.get("age"),
                "bye_week": None if team in NO_TEAM or bye == PLACEHOLDER_BYE else bye,
                "is_rookie": bool(record.get("is_rookie")),
                POINTS_FIELD: points_of(record),
                # 0 when the 1yr cell is missing: the source uses 0 for "no season", and
                # a player kept by the 3yr filter with no 1yr number is projected to sit.
                "points_1yr": points_of(record, "1yr") or 0,
                "adp": adp_of(record),
            }
        )
    return rows


def build_document(
    source: dict, source_path: Path, rows: list[dict], stats: dict, adjustments: list[str]
) -> dict:
    """The output file: a short provenance header plus the rows."""
    return {
        "source_file": source_path.name,
        "source_player_count": source.get("player_count", len(source["players"])),
        "scoring_scheme": {
            "name": SCHEME,
            "description": (
                "0.5 points per reception for every position plus a 0.5/rec tight end "
                "premium (so 1.0/rec for TEs), superflex roster format."
            ),
            "reception_points": {"non_te": 0.5, "te": 1.0},
            "points_copied_from": {"TE": POINTS_COLUMNS["TE"], "default": POINTS_COLUMN_DEFAULT},
            "adp_copied_from": ADP_COLUMN,
        },
        "horizon": HORIZON,
        "positions": list(POSITIONS),
        "player_count": len(rows),
        "excluded": {
            "by_position": stats["dropped_position"],
            "no_usable_projection": len(stats["dropped_unusable"]),
            "below_rank_limit": stats["cut_by_rank"],
        },
        "adjustments": adjustments,
        "fields": FIELD_DEFINITIONS,
        "players": rows,
    }


def check_sources(document: dict) -> None:
    """Fail loudly if a column this reads is gone. Only the ones read are required —
    the rest of ADP_FAMILY is a cross-check, and --report just compares fewer columns."""
    published = set(document.get("scoring_schemes") or [])
    required = {POINTS_COLUMN_DEFAULT, *POINTS_COLUMNS.values(), ADP_COLUMN}
    missing = sorted(required - published)
    if missing:
        raise ValueError(
            f"source scheme(s) {', '.join(missing)} absent from scoring_schemes — "
            "re-run parse_projections.py"
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(source: dict, kept: list[dict], rows: list[dict], stats: dict) -> None:
    out = sys.stderr
    total = len(source["players"])
    dropped = stats["dropped_position"]

    print(f"\ninput: {total} players", file=out)
    print(
        f"  positions {'/'.join(POSITIONS)}: kept {stats['right_position']}, "
        f"dropped {sum(dropped.values())} ("
        + ", ".join(f"{pos} {n}" for pos, n in dropped.items())
        + ")",
        file=out,
    )
    unusable = stats["dropped_unusable"]
    print(
        f"  usable {HORIZON} projection: kept {stats['eligible']}, dropped {len(unusable)}"
        + (f" (0 or missing: {', '.join(unusable[:4])}...)" if unusable else ""),
        file=out,
    )
    print(
        f"  top {len(rows)} by {POINTS_FIELD}: dropped {stats['cut_by_rank']} "
        f"(cut at {stats['cut_at_points']} pts"
        + (f"; best excluded {stats['best_points_cut']})" if stats["cut_by_rank"] else ")"),
        file=out,
    )
    counts = collections.Counter(row["position"] for row in rows)
    print(
        "pool: " + ", ".join(f"{pos} {counts[pos]}" for pos in POSITIONS)
        + f" = {len(rows)}, {sum(row['is_rookie'] for row in rows)} rookies",
        file=out,
    )

    # -- points: is the copied column really this league's scoring? --------
    print(
        f"\n{POINTS_FIELD}  [TE -> {POINTS_COLUMNS['TE']} (1.0/rec), others -> "
        f"{POINTS_COLUMN_DEFAULT} (0.5/rec)]",
        file=out,
    )
    tes = [r for r in kept if r["position"] == "TE"]
    others = [r for r in kept if r["position"] != "TE"]

    def flat(record: dict) -> dict:
        """The record's source cells for this horizon, all schemes."""
        return record["projections"][HORIZON]

    same = sum(1 for r in others if flat(r)["te_premium"] == flat(r)["ppr"])
    print(
        f"  non-TE: te_premium == ppr for {same}/{len(others)} — the premium is a "
        f"TE-only step, so {POINTS_COLUMN_DEFAULT} is exact at 0.5/rec",
        file=out,
    )
    step = sum(
        1
        for r in tes
        if flat(r)["ppr"] - flat(r)["half_ppr"] == flat(r)["te_premium"] - flat(r)["ppr"]
    )
    print(
        f"  TE: (ppr - half_ppr) == (te_premium - ppr) for {step}/{len(tes)}, rest "
        "+-1 (source columns are pre-rounded integers)",
        file=out,
    )
    drift = sum(
        1
        for r in kept
        for c in [flat(r)]
        if c["half_ppr"] + c["te_premium"] - c["ppr"] != points_of(r)
    )
    print(
        f"  cells where the three-term transfer would differ: {drift}/{len(kept)} "
        "(rounding drift, avoided by copying)",
        file=out,
    )
    copied = sum(
        1
        for row, r in zip(rows, kept)
        if row[POINTS_FIELD] == flat(r)[points_column(r["position"])]
    )
    print(f"  emitted == source column: {copied}/{len(rows)} — exact", file=out)
    one_ok = sum(
        1 for row, r in zip(rows, kept) if row["points_1yr"] == (points_of(r, "1yr") or 0)
    )
    bounded = sum(1 for row in rows if row["points_1yr"] <= row[POINTS_FIELD])
    print(
        f"  points_1yr: emitted == source column for {one_ok}/{len(rows)}; "
        f"<= {POINTS_FIELD} for {bounded}/{len(rows)}",
        file=out,
    )

    # -- adp ---------------------------------------------------------------
    print(
        f"\nadp  [{ADP_COLUMN}, round.pick -> overall pick, {TEAMS_PER_ROUND}-team source]",
        file=out,
    )
    family = [s for s in ADP_FAMILY if s in (source.get("scoring_schemes") or [])]
    agree = sum(1 for r in kept if len({r["adp"][s] for s in family}) == 1)
    print(
        f"  all {len(family)} superflex styles identical for {agree}/{len(kept)} — "
        "no TE-premium signal in the source ADP to transfer",
        file=out,
    )
    picks = [row["adp"] for row in rows if row["adp"] is not None]
    print(
        f"  present {len(picks)}/{len(rows)}, distinct {len(set(picks))}"
        + ("" if len(set(picks)) == len(picks) else "  <- COLLISIONS"),
        file=out,
    )
    roundtrip = sum(1 for r in kept if decode_adp(r["adp"][ADP_COLUMN]) == adp_of(r))
    tail = [pick for pick in picks if pick > PLAUSIBLE_ROUNDS * TEAMS_PER_ROUND]
    print(
        f"  decode round-trips for {roundtrip}/{len(kept)}; range "
        f"{min(picks, default=0)}..{max(picks, default=0)}, {len(tail)} past round "
        f"{PLAUSIBLE_ROUNDS} (provider tail noise, not a real slot)",
        file=out,
    )

    # -- integrity ---------------------------------------------------------
    print("\nintegrity", file=out)
    ranks = [row["rank"] for row in rows]
    ids = {row["player_id"] for row in rows}
    ordered = all(
        rows[i][POINTS_FIELD] >= rows[i + 1][POINTS_FIELD] for i in range(len(rows) - 1)
    )
    per_pos = collections.Counter()
    positional_ok = True
    for row in rows:
        per_pos[row["position"]] += 1
        positional_ok &= row["positional_rank"] == per_pos[row["position"]]
    print(
        f"  rank 1..{len(rows)} gap-free: {ranks == list(range(1, len(rows) + 1))}; "
        f"unique player_ids: {len(ids) == len(rows)}; "
        f"monotone in {POINTS_FIELD}: {ordered}; positional ranks consistent: {positional_ok}",
        file=out,
    )
    nulled = sum(1 for row, r in zip(rows, kept) if row["bye_week"] is None)
    print(
        f"  bye_week nulled for {nulled} unsigned players (source sentinel "
        f"{PLACEHOLDER_BYE}); age/team present for all {len(rows)}",
        file=out,
    )
    swap = stats["dynasty_cut_would_swap"]
    print(
        f"  cutting on the provider's dynasty rank instead would swap {len(swap)} at the "
        "boundary"
        + (
            ", e.g. " + ", ".join(f"{n} ({p}, {v} pts)" for n, p, v in swap[:3])
            if swap
            else ""
        ),
        file=out,
    )
    print(f"  fields per player: {len(rows[0])} ({', '.join(rows[0])})", file=out)

    # -- spot check --------------------------------------------------------
    print("\ntop 5 and the last 2 in the pool", file=out)
    for row in rows[:5] + rows[-2:]:
        print(
            f"  {row['rank']:>3} {row['name']:<22} {row['position']}{row['positional_rank']:<3} "
            f"{row[POINTS_FIELD]:>5} pts   adp {row['adp']}",
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
    ap.add_argument("input", nargs="?", default=paths.PROJECTIONS_JSON, type=Path)
    ap.add_argument("-o", "--output", default=paths.POOL, type=Path)
    ap.add_argument(
        "--limit", type=int, default=RANK_LIMIT, help=f"keep this many players (default {RANK_LIMIT})"
    )
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)

    if not args.input.is_file():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1
    with args.input.open(encoding="utf-8") as handle:
        source = json.load(handle)
    if not source.get("players"):
        print(f"error: no players in {args.input}", file=sys.stderr)
        return 1

    try:
        check_sources(source)
        adjustments = apply_synthetic(source["players"])
        adjustments += add_synthetic_players(source["players"])
        adjustments += zero_negative_futures(source["players"])
        kept, stats = select(source["players"], args.limit)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not kept:
        print("error: no players survived the filters", file=sys.stderr)
        return 1
    for note in adjustments:
        print(f"adjusted: {note}", file=sys.stderr)

    rows = build_rows(kept)
    document = build_document(source, args.input, rows, stats, adjustments)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    print(
        f"pool: {len(rows)} of {len(source['players'])} players, "
        f"{POINTS_FIELD} + adp -> {paths.display(args.output)}",
        file=sys.stderr,
    )
    if args.report:
        report(source, kept, rows, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
