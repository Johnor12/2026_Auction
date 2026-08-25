#!/usr/bin/env python3
"""Join pool.json to Sleeper's player dump and write each player's Sleeper id back.

Stage 3 of the build, and the only one that touches an already-finished pool.json:
it re-reads the file, adds one field per player, and writes it back in place. It has
to run last, because ``build_pool.py`` regenerates pool.json from scratch — a rebuild
drops the ids, and this puts them back.

    pool.json + data/sleeper_players.json  ->  pool.json  (+ sleeper_id)

Sleeper is the league host; its ids are what a roster, a trade or a draft pick is
expressed in over their API. The provider's ``player_id`` is a Draftsharks number
and means nothing there, so without this join nothing in pool.json can be looked up
against the actual league. Only the id is taken: name, team, age and position are
already in the pool from the projections source, and importing a second, disagreeing
copy of them would just raise the question of which one to trust.

**The join is by name, because there is no shared key.** Names are not unique and the
two sources spell them differently, so it runs in three tiers, each stricter about
what it is allowed to assume, and each requiring exactly one survivor — an ambiguous
player is left unmatched rather than guessed:

    1. full name            "Josh Allen"          -> josh allen, QB      321 of 350
    2. name without suffix  "Marvin Harrison Jr." -> marvin harrison      21
    3. last name + team     "Nathaniel Dell"      -> Tank Dell, HOU        7

Tier 2 exists because the suffix is editorial: Sleeper lists Michael Penix Jr. as
"Michael Penix" and Kenneth Walker III as "Kenneth Walker". Tier 3 exists because
first names are too: Cam(eron) Ward, Chig(oziem) Okonkwo, Kenny/Kenneth Gainwell,
and Tank Dell, whose given name is Nathaniel. That tier can't lean on the first name
at all, so it demands a team match and an age within ``AGE_TOLERANCE`` instead, and
``--report`` prints every one of its matches for eyeballing — it is the tier where a
wrong join would be plausible enough to slip through.

Position is required to agree in every tier (against Sleeper's ``position`` or its
``fantasy_positions``), which is what separates the two Kenneth Walkers and the three
Kyle Williamses. Where several candidates still survive, the tie is broken only by
hard facts — team, then active status — never by "closest age" or list order.

Team codes are translated (the provider writes JAC/LVR where Sleeper writes JAX/LV);
the provider's UNS/RK sentinels mean unsigned, so those players simply carry no team
constraint.

Unmatched players keep ``sleeper_id: null`` — a real answer, not a failure. Sleeper
lists NFL players, and a deep-dynasty pool legitimately contains people who are not
in Sleeper yet.

Usage:
    uv run match_sleeper.py                     # pool.json in place
    uv run match_sleeper.py --report            # + every tier-3 join and every miss
    uv run match_sleeper.py -o annotated.json   # write elsewhere, leave pool.json alone
    uv run match_sleeper.py --skip-if-missing   # no dump -> warn and exit 0 (pipeline use)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

import fetch_sleeper
import paths

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POSITIONS = ("QB", "RB", "WR", "TE")

ID_FIELD = "sleeper_id"

#: Inserted directly after this pool field, so the two ids sit together.
ID_AFTER = "player_id"

#: Provider team code -> Sleeper team code. Everything else is already identical.
TEAM_ALIASES = {"JAC": "JAX", "LVR": "LV"}

#: Provider sentinels for an unsigned player: no team, so no team constraint.
NO_TEAM = frozenset({"UNS", "RK"})

#: Name suffixes that one source prints and the other does not.
SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "v")

#: Years. Sleeper's age is a truncated integer computed at its own fetch time and
#: the provider's is a decimal at its own; a real person's two ages sit inside this.
AGE_TOLERANCE = 2.0

#: Sleeper ids are stable, so an old dump is only a problem for players who joined
#: the league since. Warn, don't fail.
STALE_AFTER_DAYS = 14

TIERS = ("name", "name_without_suffix", "last_name_team")

FIELD_DEFINITION = (
    "Sleeper's player id (a string), for looking this player up against the league "
    "on Sleeper's API. Null when no unambiguous match exists in their player dump."
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


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
    if not code or code in NO_TEAM:
        return None
    return TEAM_ALIASES.get(code, code)


def full_name_of(player: dict) -> str:
    return player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()


def normalized_name_of(player: dict) -> str:
    """Sleeper's precomputed key when present, recomputed identically when not."""
    return player.get("search_full_name") or norm(full_name_of(player))


def positions_of(player: dict) -> set[str]:
    listed = player.get("fantasy_positions") or []
    return {p for p in [player.get("position"), *listed] if p}


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def age_ok(row: dict, player: dict) -> bool:
    """True when the ages agree or one side doesn't know."""
    theirs, ours = player.get("age"), row.get("age")
    if theirs is None or ours is None:
        return True
    return abs(float(theirs) - float(ours)) <= AGE_TOLERANCE


def narrow(row: dict, candidates: list[dict]) -> list[dict]:
    """Drop candidates on hard facts only, and only while something survives.

    Position must agree, always. Beyond that, team and active status are applied as
    tiebreakers rather than filters: a lone candidate on the wrong team is still the
    answer (the provider and Sleeper disagree about who plays where mid-offseason),
    but between two same-named players the one on the right team is.
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
    if active:
        survivors = active
    if len(survivors) <= 1:
        return survivors

    plausible = [p for p in survivors if age_ok(row, p)]
    return plausible or survivors


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

    # Tier 3: the first name is unusable, so the team and age carry the whole join.
    team = sleeper_team(row.get("team"))
    if team:
        candidates = [
            p
            for p in index.by_last.get(last_name(row["name"]), [])
            if p.get("team") == team and age_ok(row, p)
        ]
        survivors = narrow(row, candidates)
        if len(survivors) == 1:
            return survivors[0], "last_name_team", []
        if survivors:
            return None, "last_name_team", survivors

    return None, "none", []


def annotate(rows: list[dict], index: SleeperIndex) -> tuple[list[dict], dict]:
    """Rebuild every row with sleeper_id in place. Returns (rows, stats)."""
    tiers: collections.Counter[str] = collections.Counter()
    joins: dict[str, list] = {tier: [] for tier in TIERS}
    unmatched: list[dict] = []
    ambiguous: list[dict] = []
    out: list[dict] = []

    for row in rows:
        player, tier, clash = match(row, index)
        sleeper_id = player["player_id"] if player else None
        if player:
            tiers[tier] += 1
            joins[tier].append((row, player))
        else:
            unmatched.append(row)
            if clash:
                ambiguous.append({"row": row, "tier": tier, "candidates": clash})

        rebuilt = {}
        for key, value in row.items():
            if key == ID_FIELD:
                continue  # dropped and re-derived, so re-running is idempotent
            rebuilt[key] = value
            if key == ID_AFTER:
                rebuilt[ID_FIELD] = sleeper_id
        if ID_FIELD not in rebuilt:
            rebuilt[ID_FIELD] = sleeper_id
        out.append(rebuilt)

    ids = [row[ID_FIELD] for row in out if row[ID_FIELD]]
    duplicates = [i for i, n in collections.Counter(ids).items() if n > 1]

    stats = {
        "matched": len(ids),
        "unmatched": len(unmatched),
        "by_tier": {tier: tiers[tier] for tier in TIERS if tiers[tier]},
        "joins": joins,
        "unmatched_players": unmatched,
        "ambiguous": ambiguous,
        "duplicate_ids": duplicates,
    }
    return out, stats


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def provenance(index: SleeperIndex, meta: dict | None, stats: dict) -> dict:
    return {
        "source": (meta or {}).get("url", fetch_sleeper.URL),
        "fetched_at": (meta or {}).get("fetched_at"),
        "dump_player_count": index.player_count,
        "dump_offensive_count": index.considered,
        "matched": stats["matched"],
        "unmatched": stats["unmatched"],
        "matched_by": stats["by_tier"],
        "unmatched_players": [
            {"name": row["name"], "position": row["position"], "team": row["team"]}
            for row in stats["unmatched_players"]
        ],
    }


def update_document(document: dict, rows: list[dict], index: SleeperIndex, meta, stats) -> dict:
    document["players"] = rows
    fields = document.get("fields")
    if isinstance(fields, dict) and ID_FIELD not in fields:
        # Same insertion point as in the rows, so the header still reads in order.
        document["fields"] = {
            key: value
            for pair in fields.items()
            for key, value in ([pair] + ([(ID_FIELD, FIELD_DEFINITION)] if pair[0] == ID_AFTER else []))
        }
    document["sleeper"] = provenance(index, meta, stats)
    return document


def staleness_warning(meta: dict | None) -> str | None:
    age = fetch_sleeper.age_hours(meta)
    if age is None:
        return "sleeper dump has no fetch timestamp — age unknown"
    if age / 24 > STALE_AFTER_DAYS:
        return (
            f"sleeper dump is {age / 24:.0f} days old; players added since then cannot "
            "match — re-run fetch_sleeper.py"
        )
    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(rows: list[dict], index: SleeperIndex, meta: dict | None, stats: dict) -> None:
    out = sys.stderr
    age = fetch_sleeper.age_hours(meta)
    print(
        f"\ndump: {index.player_count} players, {index.considered} at a pool position"
        + (f", fetched {age / 24:.1f} days ago" if age is not None else ""),
        file=out,
    )
    print(
        f"matched {stats['matched']}/{len(rows)} by tier: "
        + ", ".join(f"{tier} {count}" for tier, count in stats["by_tier"].items()),
        file=out,
    )

    exact = stats["joins"]["name"]
    agree_team = sum(1 for row, p in exact if sleeper_team(row["team"]) == p.get("team"))
    agree_age = sum(1 for row, p in exact if age_ok(row, p))
    print(
        f"  tier 1 cross-check (not used to match): team agrees {agree_team}/{len(exact)}, "
        f"age within {AGE_TOLERANCE:g}y {agree_age}/{len(exact)}",
        file=out,
    )

    suffix_joins = stats["joins"]["name_without_suffix"]
    print(f"\ntier 2 — suffix dropped ({len(suffix_joins)})", file=out)
    for row, player in suffix_joins:
        print(
            f"  {row['name']:<24} -> {full_name_of(player):<22} {player.get('position')} "
            f"{player.get('team')} id={player['player_id']}",
            file=out,
        )

    last_joins = stats["joins"]["last_name_team"]
    print(f"\ntier 3 — last name + team, first names differ ({len(last_joins)})", file=out)
    for row, player in last_joins:
        print(
            f"  {row['name']:<24} ({row['position']} {row['team']}, age {row['age']}) -> "
            f"{full_name_of(player):<22} {player.get('position')} {player.get('team')} "
            f"age {player.get('age')} id={player['player_id']}",
            file=out,
        )

    print(f"\nunmatched ({stats['unmatched']})", file=out)
    for row in stats["unmatched_players"]:
        print(
            f"  rank {row['rank']:>3}  {row['name']:<24} {row['position']} {row['team']} "
            f"age {row['age']}{'  (rookie)' if row.get('is_rookie') else ''}",
            file=out,
        )
    for clash in stats["ambiguous"]:
        row = clash["row"]
        print(
            f"  ^ {row['name']} was ambiguous at tier '{clash['tier']}': "
            + ", ".join(
                f"{full_name_of(p)} ({p.get('position')} {p.get('team')} age {p.get('age')}, "
                f"id={p['player_id']})"
                for p in clash["candidates"]
            ),
            file=out,
        )

    print("\nintegrity", file=out)
    print(
        f"  ids unique: {not stats['duplicate_ids']}"
        + (f" <- COLLISIONS {stats['duplicate_ids']}" if stats["duplicate_ids"] else "")
        + f"; all ids are strings: {all(isinstance(r[ID_FIELD], str) for r in rows if r[ID_FIELD])}",
        file=out,
    )
    missing_pos = [
        row["name"]
        for row, player in [pair for tier in TIERS for pair in stats["joins"][tier]]
        if row["position"] not in positions_of(player)
    ]
    print(f"  position agrees on every join: {not missing_pos}", file=out)
    covered = [row for row in rows if row[ID_FIELD]]
    top = [row for row in rows[:100] if row[ID_FIELD]]
    print(
        f"  coverage: {len(covered)}/{len(rows)} overall, {len(top)}/100 in the top 100",
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
    ap.add_argument("input", nargs="?", default=paths.POOL, type=Path)
    ap.add_argument(
        "-o", "--output", type=Path, help="default: overwrite the input in place"
    )
    ap.add_argument("--players", default=paths.SLEEPER_PLAYERS, type=Path, help="the dump")
    ap.add_argument(
        "--skip-if-missing",
        action="store_true",
        help="exit 0 with a warning when the dump has not been downloaded yet",
    )
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)
    output = args.output or args.input

    if not args.players.is_file():
        message = (
            f"sleeper dump {paths.display(args.players)} not found — run "
            "`uv run pool_pipeline/fetch_sleeper.py` (manual, ~14 MB)"
        )
        if args.skip_if_missing:
            print(f"warning: {message}; {ID_FIELD} not added", file=sys.stderr)
            return 0
        print(f"error: {message}", file=sys.stderr)
        return 1
    if not args.input.is_file():
        print(f"error: {args.input} not found — run the pool stage first", file=sys.stderr)
        return 1

    with args.input.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not document.get("players"):
        print(f"error: no players in {args.input}", file=sys.stderr)
        return 1
    with args.players.open(encoding="utf-8") as handle:
        dump = json.load(handle)

    meta = fetch_sleeper.load_meta(args.players.with_suffix(".meta.json"))
    warning = staleness_warning(meta)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    index = SleeperIndex(dump)
    if not index.considered:
        print("error: no QB/RB/WR/TE players in the dump — is it the right file?", file=sys.stderr)
        return 1

    rows, stats = annotate(document["players"], index)
    if stats["duplicate_ids"]:
        print(
            f"error: {len(stats['duplicate_ids'])} sleeper id(s) matched more than one "
            f"pool player: {stats['duplicate_ids']}",
            file=sys.stderr,
        )
        return 1

    document = update_document(document, rows, index, meta, stats)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    print(
        f"sleeper: {stats['matched']}/{len(rows)} matched "
        f"({', '.join(f'{t} {n}' for t, n in stats['by_tier'].items())}), "
        f"{stats['unmatched']} without an id -> {paths.display(output)}",
        file=sys.stderr,
    )
    if args.report:
        report(rows, index, meta, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
