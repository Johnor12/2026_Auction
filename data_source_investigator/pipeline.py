#!/usr/bin/env python3
"""Fetch ranking snapshots, normalize them, then investigate the live draft.

Usage:
    uv run data_source_investigator/pipeline.py
    uv run data_source_investigator/pipeline.py --report
    uv run data_source_investigator/pipeline.py --only investigate
"""

from __future__ import annotations

import argparse
import sys
import time

import build_rankings
import fetch_rankings
import investigate

STAGES = ("fetch", "build", "investigate")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", choices=STAGES)
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)

    shared = ["--indent", str(args.indent)] + (["--report"] if args.report else [])
    stages = {
        "fetch": (fetch_rankings.main, shared),
        "build": (build_rankings.main, shared),
        "investigate": (investigate.main, shared),
    }
    selected = [args.only] if args.only else list(STAGES)
    for number, stage in enumerate(selected, start=1):
        print(f"\n=== [{number}/{len(selected)}] {stage} ===", file=sys.stderr)
        started = time.monotonic()
        code = stages[stage][0](stages[stage][1])
        if code:
            print(f"pipeline failed at stage {stage!r} (exit {code})", file=sys.stderr)
            return code
        print(f"--- {stage} ok in {time.monotonic() - started:.1f}s", file=sys.stderr)
    print("\ndata-source investigation complete", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
