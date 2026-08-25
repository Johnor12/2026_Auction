#!/usr/bin/env python3
"""Run the data build: projections.html -> projections.json -> pool.json (+ sleeper ids).

Three stages, in order:

    1. parse_projections.py   html  -> json   full provider export, 900 players, 8 schemes
    2. build_pool.py          json  -> json   this league's 350-player pool, one value column
    3. match_sleeper.py       pool.json       adds each player's Sleeper id, in place

Everything the build reads and every intermediate it writes lives in this folder;
the one file it publishes is ``pool.json`` at the repo root, which is what
``rank.py`` consumes.

Stage 1 is the faithful record of what the provider published and is never narrowed;
stage 2 is the narrow, draft-ready view. Keeping them separate is what makes stage 2
re-runnable (different rank limit, different scoring) without re-parsing 8 MB of html,
and what leaves the dropped columns recoverable. Stage 3 has to come last because
stage 2 rewrites pool.json from scratch, dropping the ids stage 3 adds.

**Stage 3 never downloads.** It joins against a cached copy of Sleeper's ~14 MB player
dump, pulled by hand with ``fetch_sleeper.py`` — Sleeper asks for at most one call a
day, and a roster of NFL players is not something a rebuild of local projections needs
to re-ask for. With no dump present the stage warns and is skipped, and pool.json is
still complete except for ``sleeper_id``. Running ``--only sleeper`` makes it an error
instead, since there the dump is the whole point of the run.

All four scripts remain usable as standalone CLIs — this only fixes the order and stops
on the first failure.

Ranking (`rank.py`) is deliberately not a stage here: it consumes pool.json, takes a
simulation seed and strategy knobs, and is re-run far more often than the data is rebuilt.

Usage:
    uv run pool_pipeline/pipeline.py                      # html -> projections.json -> pool.json
    uv run pool_pipeline/pipeline.py --report             # + every stage's validation summary
    uv run pool_pipeline/pipeline.py --only pool          # single stage
    uv run pool_pipeline/pipeline.py --only sleeper       # re-join ids onto an existing pool.json
    uv run pool_pipeline/fetch_sleeper.py                 # refresh the Sleeper dump (manual)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import build_pool
import match_sleeper
import parse_projections as parse
import paths

STAGES = ("parse", "pool", "sleeper")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", nargs="?", default=paths.PROJECTIONS_HTML, type=Path)
    ap.add_argument(
        "--projections",
        default=paths.PROJECTIONS_JSON,
        type=Path,
        help="stage 1 output / stage 2 input (default: pool_pipeline/data/projections.json)",
    )
    ap.add_argument("-o", "--output", default=paths.POOL, type=Path)
    ap.add_argument(
        "--players",
        default=paths.SLEEPER_PLAYERS,
        type=Path,
        help="stage 3 input: the cached Sleeper dump (fetch_sleeper.py writes it)",
    )
    ap.add_argument("--report", action="store_true", help="pass --report to every stage")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    ap.add_argument(
        "--only",
        choices=STAGES,
        help="run a single stage (equivalent to invoking that script directly)",
    )
    args = ap.parse_args(argv)

    shared = ["--indent", str(args.indent)] + (["--report"] if args.report else [])
    # A missing dump is fatal only when the sleeper stage is what was asked for.
    sleeper_argv = [str(args.output), "--players", str(args.players), *shared]
    if args.only != "sleeper":
        sleeper_argv.append("--skip-if-missing")

    stages = {
        "parse": (parse.main, [str(args.input), "-o", str(args.projections), *shared]),
        "pool": (build_pool.main, [str(args.projections), "-o", str(args.output), *shared]),
        "sleeper": (match_sleeper.main, sleeper_argv),
    }
    selected = [args.only] if args.only else list(STAGES)

    for number, name in enumerate(selected, start=1):
        entry, stage_argv = stages[name]
        print(f"\n=== [{number}/{len(selected)}] {name} ===", file=sys.stderr)
        started = time.monotonic()
        code = entry(stage_argv)
        if code != 0:
            print(f"pipeline failed at stage '{name}' (exit {code})", file=sys.stderr)
            return code
        print(f"--- {name} ok in {time.monotonic() - started:.1f}s", file=sys.stderr)

    print(f"\npipeline complete -> {paths.display(args.output)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
