#!/usr/bin/env python3
"""Pull auction purchases and rebuild bids between nominations.

Three steps, all of which already stand alone; this only fixes the order and stops on
the first failure:

    1. draft_pipeline/fetch_draft.py   Sleeper's draft API -> draft.json
    2. data_source_investigator/investigate.py
                                        existing source rankings + draft.json
                                        -> data_source_matches.json
    3. rank.py                         pool + auction state + source evidence
                                        -> rankings.json

The pool pipeline and the investigator's source fetch/build stages are deliberately not
steps. They are re-run only when their underlying rankings change; this loop applies the
existing snapshots to the current draft without hitting any ranking provider. Source
investigation precedes ranking because each opponent price consumes its latest match.

Each step is a separate ``uv run``, not an import. All run with the repo root as their
working directory, so their own default paths apply and this works from anywhere
(``rank.py`` resolves ``pool.json`` and ``draft.json`` from the shell's cwd, not from
the script).

Usage:
    uv run refresh.py            # draft.json, rankings.json, data_source_matches.json
    uv run refresh.py --report   # + every step's validation summary on stderr
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--report", action="store_true", help="pass --report to every step")
    args = ap.parse_args(argv)

    report = ["--report"] if args.report else []
    steps = [
        ("draft", ["draft_pipeline/fetch_draft.py", *report]),
        ("investigate", ["data_source_investigator/investigate.py", *report]),
        ("rank", ["rank.py", *report]),
    ]

    for number, (name, command) in enumerate(steps, start=1):
        print(f"\n=== [{number}/{len(steps)}] {name} ===", file=sys.stderr)
        started = time.monotonic()
        code = subprocess.run(["uv", "run", *command], cwd=REPO_ROOT).returncode
        if code != 0:
            print(f"refresh failed at step '{name}' (exit {code})", file=sys.stderr)
            return code
        print(f"--- {name} ok in {time.monotonic() - started:.1f}s", file=sys.stderr)

    print(
        "\nrefresh complete -> rankings.json + data_source_matches.json",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
