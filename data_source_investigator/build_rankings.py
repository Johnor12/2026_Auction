#!/usr/bin/env python3
"""Normalize public snapshots and manual CSV exports into one rankings file.

Built-in inputs come from ``fetch_rankings.py``. Any ``data/manual/*.csv`` file
is treated as another provider; its required columns are ``rank,name,position``
and its optional columns are ``team,sleeper_id,value``.

Usage:
    uv run data_source_investigator/build_rankings.py
    uv run data_source_investigator/build_rankings.py --report
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import paths
from identity import PlayerResolver
from providers import (
    PARSERS,
    draftsharks_adp,
    drop_ambiguous_identities,
    parse_manual,
    validate,
)

def write_json(path: Path, payload: dict, indent: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=indent, ensure_ascii=False) + "\n")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--raw", type=Path, default=paths.RAW)
    ap.add_argument("--manual", type=Path, default=paths.MANUAL)
    ap.add_argument("--pool", type=Path, default=paths.POOL)
    ap.add_argument("-o", "--output", type=Path, default=paths.RANKINGS)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)

    try:
        pool = json.loads(args.pool.read_text())
        resolver = PlayerResolver(pool)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"cannot load pool {args.pool}: {exc}", file=sys.stderr)
        return 1

    fetch_meta = {}
    meta_path = args.raw / "fetch_meta.json"
    if meta_path.exists():
        try:
            fetch_meta = json.loads(meta_path.read_text()).get("sources", {})
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot load fetch metadata {meta_path}: {exc}", file=sys.stderr)
            return 1

    sources: list[dict] = []
    failures: list[str] = []
    for source_id, (name, filename, parser) in PARSERS.items():
        source_path = args.raw / filename
        if not source_path.exists():
            failures.append(f"{source_id}: missing {source_path}")
            continue
        try:
            rows = parser(source_path)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{source_id}: {exc}")
            continue
        rows.sort(key=lambda row: row["rank"])
        rows, ambiguous = drop_ambiguous_identities(rows)
        if ambiguous:
            print(
                f"{source_id}: dropped unjoinable duplicate names: {', '.join(ambiguous)}",
                file=sys.stderr,
            )
        problems = validate(source_id, rows)
        if problems:
            failures.extend(problems)
            continue
        for row in rows:
            row["sleeper_id"] = resolver.resolve(row)
        metadata = fetch_meta.get(source_id, {})
        sources.append(
            {
                "id": source_id,
                "name": name,
                "url": metadata.get("url"),
                "format": metadata.get("format"),
                "fetched_at": metadata.get("fetched_at"),
                "player_count": len(rows),
                "matched_to_sleeper": sum(row["sleeper_id"] is not None for row in rows),
                "players": rows,
            }
        )

    manual_paths = sorted(args.manual.glob("*.csv")) if args.manual.exists() else []
    for source_path in manual_paths:
        source_id = source_path.stem
        if source_id in PARSERS or source_id == "draftsharks_adp":
            failures.append(f"manual source id {source_id!r} conflicts with a built-in source")
            continue
        try:
            rows = parse_manual(source_path)
            rows.sort(key=lambda row: row["rank"])
            rows, ambiguous = drop_ambiguous_identities(rows)
            if ambiguous:
                print(
                    f"{source_id}: dropped unjoinable duplicate names: {', '.join(ambiguous)}",
                    file=sys.stderr,
                )
            problems = validate(source_id, rows)
            if problems:
                failures.extend(problems)
                continue
            for row in rows:
                row["sleeper_id"] = resolver.resolve(row)
            sources.append(
                {
                    "id": source_id,
                    "name": source_id.replace("_", " ").title(),
                    "url": None,
                    "format": "user-supplied CSV; verify scoring format",
                    "fetched_at": None,
                    "player_count": len(rows),
                    "matched_to_sleeper": sum(row["sleeper_id"] is not None for row in rows),
                    "players": rows,
                }
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"{source_id}: {exc}")

    ds_rows = draftsharks_adp(pool)
    for row in ds_rows:
        row["sleeper_id"] = resolver.resolve(row)
    sources.append(
        {
            "id": "draftsharks_adp",
            "name": "DraftSharks superflex ADP",
            "url": None,
            "format": "dynasty superflex ADP from pool.json; TEP is not reflected in ADP",
            "fetched_at": None,
            "player_count": len(ds_rows),
            "matched_to_sleeper": sum(row["sleeper_id"] is not None for row in ds_rows),
            "players": ds_rows,
        }
    )

    if failures:
        print("ranking normalization failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    if len(sources) < 2:
        print("need at least two ranking sources to compare", file=sys.stderr)
        return 1

    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "league_format": {
            "teams": 10,
            "type": "dynasty superflex",
            "ppr": 0.5,
            "te_reception_bonus": 0.5,
        },
        "source_count": len(sources),
        "sources": sources,
    }
    write_json(args.output, payload, args.indent)

    if args.report:
        for source in sources:
            print(
                f"  {source['id']}: {source['player_count']} players, "
                f"{source['matched_to_sleeper']} matched to Sleeper",
                file=sys.stderr,
            )
    print(f"normalized {len(sources)} sources -> {paths.display(args.output)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
