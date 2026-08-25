# Pool pipeline

This independent, offline pipeline turns a saved DraftSharks projections page into the
league's `pool.json`. It is rerun when projections change, not during every live-draft
refresh.

## Stages

```text
data/projections.html
  -> parse_projections.py -> data/projections.json
  -> build_pool.py        -> ../pool.json
  -> match_sleeper.py     -> ../pool.json with sleeper_id
```

`pipeline.py` runs those stages in order and stops on the first failure. Every stage is
also a standalone CLI:

```bash
uv run pool_pipeline/pipeline.py --report
uv run pool_pipeline/pipeline.py --only pool
uv run pool_pipeline/parse_projections.py in.html -o out.json
uv run pool_pipeline/build_pool.py --limit 450 -o big.json
uv run pool_pipeline/match_sleeper.py --report
uv run pool_pipeline/fetch_sleeper.py
```

`fetch_sleeper.py` is manual and is not a pipeline stage. Sleeper's player dump is about
14 MB and should not be downloaded more than once per day. It is cached under `data/`;
the small metadata file records when it was fetched.

## File contracts

`projections.json` is a faithful provider export: identity fields, eight scoring
schemes, four horizons, displayed and derived ranks, ADP, and analysis text. The printed
ranks in the saved HTML are stale; consumers use the gap-free ranks derived from 3D value.

`pool.json` is the narrow draft input: every usable QB/RB/WR/TE player, with 13
fields per player. The ranker uses projected points, not DraftSharks' provider-scaled 3D
value. A player the source omits but the league drafts can be added as a fully
synthetic comp-median row (`SYNTHETIC_PLAYERS` in `build_pool.py`; currently Mac
Jones), and is dropped automatically once the provider publishes a real row.

- `points_1yr`: year-one points in this league's scoring
- `points_3yr`: cumulative three-year points in this league's scoring
- `adp`: overall superflex ADP decoded from the provider's 12-team round.pick notation
- `sleeper_id`: the join key used by the live draft and investigator
- `rank`: descending `points_3yr`, with provider dynasty rank breaking ties

For WR/RB/QB, league points come from DraftSharks' half-PPR projection. For TE, they come
from its PPR projection: 0.5 base PPR plus this league's 0.5 TE premium equals 1.0 per TE
reception. DraftSharks' named TE-premium column is 1.5 TE PPR and is not this league.

## Sleeper matching

There is no shared provider id, so `match_sleeper.py` uses three conservative name
tiers: full normalized name; name without suffix; then last name plus team and nearby
age. Position must always agree, ambiguity is left unmatched, and duplicate Sleeper ids
are fatal. Re-running is idempotent because ids are dropped and re-derived.

