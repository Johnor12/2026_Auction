#!/usr/bin/env python3
"""Where every file in the pool build lives.

This pipeline is a folder of scripts with one published artifact: ``pool.json`` at
the repo root, which is what the ranker (``rank.py``) and the investigator read.
Everything else — the four FantasyPros CSV exports and the 14 MB Sleeper dump — is
working material and stays inside ``pool_pipeline/data/``.

``draft_pipeline/`` is the other pipeline in this repo and keeps its own copy of this
file. The two share no code and no working files; they meet only at ``sleeper_id``,
the key every pool player and every made pick carries.

Paths are anchored to this file, not to the current directory, so the build can be run
from anywhere:

    uv run pool_pipeline/build_pool.py
    cd pool_pipeline && uv run build_pool.py
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
DATA_DIR = PIPELINE_DIR / "data"

#: Inputs: FantasyPros' consensus season projections, one CSV per position, exported by
#: hand with the "Export" button on https://www.fantasypros.com/nfl/projections/<pos>.php
#: (the Draft / full-season view). Not machine-refreshable; the filenames are FantasyPros'.
PROJECTIONS_CSV = {
    position: DATA_DIR / f"FantasyPros_Fantasy_Football_Projections_{position}.csv"
    for position in ("QB", "RB", "WR", "TE")
}

#: Sleeper's full player dump, exactly as the API returned it. Fetched by hand
#: (``fetch_sleeper.py``), at most once a day.
SLEEPER_PLAYERS = DATA_DIR / "sleeper_players.json"

#: Small sidecar recording when the dump above was pulled.
SLEEPER_META = DATA_DIR / "sleeper_players.meta.json"

#: The one published artifact.
POOL = REPO_ROOT / "pool.json"


def display(path: Path) -> str:
    """Path relative to the repo root when it is inside it, for tidy log lines."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
