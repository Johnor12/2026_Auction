# 2026 dynasty fantasy draft

A toolkit for the 12-team superflex draft of
[Gnosis Auction 2026](https://sleeper.com/leagues/1396606685107200000/predraft).
Independent processes publish stable JSON artifacts at the repository root; the ranker
consumes those artifacts and the static dashboard renders the result.

The league is a redraft with an auction draft; the toolkit still models a dynasty snake
startup. That rework is pending, so the pick-order assumptions below are placeholders
and `fetch_draft.py` refuses the auction draft until it lands. The data sources are
already redraft: `pool.json` carries FantasyPros' one-season projections scored for this
league, and the investigator snapshots redraft and auction boards in this format.

## League assumptions

Discovered from the Sleeper API (league `1396606685107200000`, draft
`1396606686923341824`).

- 0.5 PPR, no TE premium; otherwise standard (4 / 0.04 passing, 6 / 0.1 rushing and
  receiving, -2 per interception, -2 per lost fumble)
- Starters: 1 QB, 2 RB, 3 WR, 1 TE, 1 W/R/T flex, 1 W/R/T/Q superflex
- 5 bench, 2 IR, no taxi squad
- 12 teams and 14 drafted players per team (168 picks)
- Auction draft, $200 budget, no pick order. The ranker stands in a plain snake (no
  reversal) with me in slot 2, a placeholder carried over from the 2025 snake: 1.02,
  2.11, 3.02, 4.11, 5.02, 6.11, …, 13.02, 14.11

Projections are FantasyPros' 2026 consensus season projections, scored under these
settings by `pool_pipeline/`. The ranker's years-2–3 horizon is zero on this pool: there
is no multi-year projection in a redraft, and none is invented.

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

- `pool.json`: every QB/RB/WR/TE with a FantasyPros season projection that joins to a
  Sleeper player, scored for this league (about 480)
- `draft.json`: all 168 made and pending picks from Sleeper
- `data_source_matches.json`: the provider board closest to each opponent's picks
- `rankings.json`: undrafted-player rankings, recommendations, simulations, and validation

`sleeper_id` is the cross-process player key. `roster_id` and `draft_slot` connect
opponent source matches to the live board.

## Components

- [Pool pipeline](pool_pipeline/README.md): FantasyPros projection exports and Sleeper's
  player list to the league-specific pool
- [Draft pipeline](draft_pipeline/README.md): Sleeper API to the complete live board
- [Data-source investigator](data_source_investigator/README.md): normalize redraft and
  auction provider boards and infer opponent strategies
- [Ranker](ranker/README.md): wire-level solver, opponent simulation, planning,
  and output contracts
- `index.html`: dependency-free dashboard for `rankings.json`
- `data_source_investigator/index.html`: source-fit and pick-evidence dashboard
- `serve.py`: serves both dashboards at http://127.0.0.1:8123

Each component keeps its own paths, entry points, and implementation context. Offline
checks remain beside the draft, investigator, and ranker code they exercise. The pipelines
meet through their published JSON contracts rather than shared orchestration.

The ranker values a roster as expected optimal lineup points in separate year-one and
years-2–3 horizons (the latter zero on the redraft pool). Position-wide availability
determines when depth is called on, and one unique final waiver body per position
supplies the fallback. Personal and opponent strategies are intentionally separate: my
slot uses the projection-based roster objective, while each opponent follows its inferred
external board with roster-balance adjustments and fitted choice noise. Opponent picks
never use my projections or board.

## Common workflows

Rebuild the projection pool after re-exporting the FantasyPros CSVs:

```bash
uv run pool_pipeline/build_pool.py --report
```

Refresh ranking snapshots and opponent associations:

```bash
uv run data_source_investigator/pipeline.py --report
```

Until the auction `draft.json` exists, run only the snapshot stages:
`--only fetch`, then `--only build`.

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

`rank.py --selftest` reads `pool.json`, and its years-2–3 lineup regression fails on the
redraft pool until the ranker rework; the other checks pass.

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
