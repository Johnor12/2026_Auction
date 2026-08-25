#!/usr/bin/env python3
"""Where every file in the pool build lives.

This pipeline is a folder of scripts with one published artifact: ``pool.json`` at
the repo root, which is what the ranker (``rank.py``) reads. Everything else —
the 8 MB provider html, the 2.5 MB faithful parse of it, the 14 MB Sleeper dump — is
working material and stays inside ``pool_pipeline/data/``.

``draft_pipeline/`` is the other pipeline in this repo and keeps its own copy of this
file. The two share no code and no working files; they meet only at ``sleeper_id``,
the key ``match_sleeper.py`` writes into every pool player.

Paths are anchored to this file, not to the current directory, so every stage can
be run from anywhere:

    uv run pool_pipeline/pipeline.py
    cd pool_pipeline && uv run build_pool.py

Both write the same ``pool.json``. Every script still takes explicit paths on the
command line; these are only the defaults.
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
DATA_DIR = PIPELINE_DIR / "data"

#: Stage 1 input: hand-saved from the provider's site. Not machine-refreshable.
PROJECTIONS_HTML = DATA_DIR / "projections.html"

#: Stage 1 output / stage 2 input: the full provider export, never narrowed.
PROJECTIONS_JSON = DATA_DIR / "projections.json"

#: Sleeper's full player dump, exactly as the API returned it. Fetched by hand
#: (``fetch_sleeper.py``), large, gitignored.
SLEEPER_PLAYERS = DATA_DIR / "sleeper_players.json"

#: Small committed sidecar recording when the dump above was pulled.
SLEEPER_META = DATA_DIR / "sleeper_players.meta.json"

#: The one published artifact.
POOL = REPO_ROOT / "pool.json"


def display(path: Path) -> str:
    """Path relative to the repo root when it is inside it, for tidy log lines."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
