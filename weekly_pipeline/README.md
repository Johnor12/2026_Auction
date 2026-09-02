# Weekly pipeline

In-season: the best lineup for the current week and a free-agent check, from a blend of
two weekly projection sources.

```text
data/FantasyPros_Fantasy_Football_Projections_{QB,FLX}.csv   (hand export, this week)
api.sleeper.app/projections/nfl/<season>/<week>              (fetched live)
pool_pipeline/data/sleeper_players.json                      (name join)
../pool.json, ../draft.json, Sleeper league rosters
  -> lineup.py -> printed report
```

```bash
uv run weekly_pipeline/lineup.py
```

## Sources and blend

- **FantasyPros** weekly consensus: the QB page and the FLX page (RB, WR, TE in one
  file with a `POS` column) under <https://www.fantasypros.com/nfl/projections/>, weekly
  view, half-PPR, exported with the page's Export button into `data/` under FantasyPros'
  filenames. Unauthenticated page loads show ten rows, so the export is the only complete
  form. The export carries no week number: it is assumed to be the current Sleeper week.
- **Sleeper** weekly projections (Rotowire), fetched live for the current Sleeper week,
  which also supply opponent, game date, and injury status.

Both stat lines are scored under the league settings in `pool_pipeline/build_pool.py`
(`SCORING`; the exports' own FPTS column uses -1 per interception), then blended 60%
FantasyPros / 40% Sleeper (`WEIGHTS` in `lineup.py`): FantasyPros aggregates several
projection sets, Sleeper republishes one. A player only one source projects takes that
source alone. FantasyPros names join to Sleeper ids with `pool_pipeline/match_sleeper.py`;
an ambiguous name fails, an unmatched one is skipped.

## Output

- The best legal lineup (1 QB, 2 RB, 3 WR, 1 TE, FLEX, superflex) by blended points, each
  source's number beside the blend, and the start/sit changes versus the lineup Sleeper
  currently holds. Players on IR are excluded.
- Free agents: every player on no Sleeper roster, ranked by the best season-long swap
  under the ranker's expected-lineup model (`ranker/value.py`, with the waiver wire
  recomputed as if he were rostered), with his blended weekly points beside our worst
  bench player's. A negative season number means no drop is worth making for him.
