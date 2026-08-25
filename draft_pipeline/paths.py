#!/usr/bin/env python3
"""Where the draft pipeline's files live. There are very few of them.

This pipeline has no inputs on disk and caches nothing: it asks Sleeper for the state
of the live draft and writes one artifact, ``draft.json`` at the repo root. So there is
no ``data/`` folder here — nothing is working material, because nothing is reused
between runs.

``pool_pipeline/`` is the other pipeline in this repo and keeps its own copy of this
file. The two share no code and no working files; they meet only at ``sleeper_id``,
which every pick here carries and which ``pool_pipeline/match_sleeper.py`` writes into
every pool player. Duplicating a dozen lines is the price of that independence, and it
is cheaper than a shared module that couples a network fetch to an html parse.

Paths are anchored to this file, not to the current directory, so it runs from anywhere:

    uv run draft_pipeline/fetch_draft.py
    cd draft_pipeline && uv run fetch_draft.py

Both write the same ``draft.json``. The script still takes explicit paths on the command
line; these are only the defaults.
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent

#: The one published artifact: the live board, every pick made and pending. Rewritten
#: from scratch on every run — this pipeline has no incremental state.
DRAFT = REPO_ROOT / "draft.json"

#: The *other* pipeline's artifact. Read-only, optional, and only by ``--report``, which
#: checks that the drafted players actually join onto it by ``sleeper_id``. Nothing in
#: the output depends on this file existing.
POOL = REPO_ROOT / "pool.json"


def display(path: Path) -> str:
    """Path relative to the repo root when it is inside it, for tidy log lines."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
