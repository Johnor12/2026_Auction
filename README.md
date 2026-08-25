# 2026 dynasty fantasy draft

A toolkit for a 10-team superflex dynasty startup draft. Independent
processes publish stable JSON artifacts at the repository root; the ranker consumes
those artifacts and the static dashboard renders the result.

## League assumptions

- 0.5 PPR, plus another 0.5 points per TE reception
- Starters: 1 QB, 2 RB, 3 WR, 1 TE, 2 W/R/T flex, 1 W/R/T/Q superflex
- 15 bench, 3 IR, and 4 rookie-only taxi spots
- 10 teams and 29 drafted players per team (290 picks)
- Snake draft with a third-round reversal
- Before trades, my slot is 1.02: 1.02, 2.09, 3.09, 4.02, 5.09, 6.02, …,
  28.02, 29.09

These are project assumptions, not runtime configuration. Ranker constants live in
`ranker/league.py`.

## Setup

[uv](https://docs.astral.sh/uv/) pins Python 3.12.

```bash
uv sync
uv run <script>
```

Commands work from the repository root. Pipeline defaults are anchored to their own
directories, so their documented direct invocations also work from inside the pipeline.

## Data flow

```text
pool_pipeline/ ───────────────> pool.json ──────────┐
                                                   │
draft_pipeline/ ──────────────> draft.json ─────────┼─> rank.py ────> rankings.json
                                                   │
data_source_investigator/ ────> data_source_matches.json
                         └────> data/rankings.json ─┘
```

The published files have distinct owners:

- `pool.json`: 350 projection-backed QB/RB/WR/TE players, keyed to Sleeper
- `draft.json`: all 290 made and pending picks from Sleeper
- `data_source_matches.json`: the provider board closest to each opponent's picks
- `rankings.json`: undrafted-player rankings, recommendations, simulations, and validation

`sleeper_id` is the cross-process player key. `roster_id` and `draft_slot` connect
opponent source matches to the live board.

## Components

- [Pool pipeline](pool_pipeline/README.md): provider HTML to the league-specific pool
- [Draft pipeline](draft_pipeline/README.md): Sleeper API to the complete live board
- [Data-source investigator](data_source_investigator/README.md): normalize provider
  boards and infer opponent strategies
- [Ranker](ranker/README.md): wire-level solver, opponent simulation, planning,
  and output contracts
- `index.html`: dependency-free dashboard for `rankings.json`
- `data_source_investigator/index.html`: source-fit and pick-evidence dashboard
- `serve.py`: serves both dashboards at http://127.0.0.1:8123

Each component keeps its own paths, entry points, and implementation context. Offline
checks remain beside the draft, investigator, and ranker code they exercise. The pipelines
meet through their published JSON contracts rather than shared orchestration.

The ranker values a roster as expected optimal lineup points in separate year-one and
years-2–3 horizons. Position-wide availability determines when depth is called on, and
one unique final waiver body per position supplies the fallback. Personal and
opponent strategies are intentionally separate: my slot uses the projection-based roster
objective, while each opponent follows its inferred external board with roster-balance
adjustments and fitted choice noise. Opponent picks never use my projections or board.

## Common workflows

Rebuild the projection pool after saving updated provider HTML:

```bash
uv run pool_pipeline/pipeline.py --report
```

Refresh ranking snapshots and opponent associations:

```bash
uv run data_source_investigator/pipeline.py --report
```

Refresh the live board and recommendations between picks:

```bash
uv run refresh.py --report
```

`refresh.py` deliberately does only three live steps: fetch the draft, re-run source
inference against the existing provider snapshot, then rank. It does not rebuild the
offline pool or fetch provider boards.

Run offline checks:

```bash
uv run rank.py --selftest
uv run draft_pipeline/fetch_draft.py --selftest
uv run data_source_investigator/investigate.py --selftest
uv run evaluate_opponents.py
```

Before and after changing an opponent model or pick policy, run
`uv run evaluate_opponents.py` and compare its replay accuracy.

## Dashboard and automation

Run `uv run serve.py` and open the local URL; direct `file://` access cannot fetch the
JSON files. The main dashboard also polls Sleeper for a compact live-status strip, shows
when its `draft.json` snapshot is stale, and pins the status of any active refresh or
deploy workflow run in that strip.

`.github/workflows/refresh.yml` runs the live refresh on manual dispatch and commits the
generated board, rankings, and source matches. `.github/workflows/deploy-pages.yml`
publishes both dashboards plus `rankings.json` and `data_source_matches.json` to GitHub
Pages, with the investigator at `data_source_investigator/`. They share one concurrency
group so refresh and deploy do not overlap.
