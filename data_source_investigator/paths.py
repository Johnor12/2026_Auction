#!/usr/bin/env python3
"""Paths owned or consumed by the data-source investigator."""

from __future__ import annotations

from pathlib import Path

PROCESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROCESS_DIR.parent
DATA = PROCESS_DIR / "data"
RAW = DATA / "raw"
MANUAL = DATA / "manual"

FETCH_META = RAW / "fetch_meta.json"
RANKINGS = DATA / "rankings.json"
REPORT = REPO_ROOT / "data_source_matches.json"

DRAFT = REPO_ROOT / "draft.json"
POOL = REPO_ROOT / "pool.json"


def display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
