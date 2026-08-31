# Pool pipeline

This offline build turns FantasyPros' consensus season projections into the league's
`pool.json`, keyed to Sleeper. It is rerun when the projections are re-exported, not
during every live-draft refresh.

## Stages

```text
data/FantasyPros_Fantasy_Football_Projections_{QB,RB,WR,TE}.csv
data/sleeper_players.json
  -> build_pool.py -> ../pool.json
```

`build_pool.py` is the whole build; `match_sleeper.py` is the name join it imports.

```bash
uv run pool_pipeline/build_pool.py --report
uv run pool_pipeline/fetch_sleeper.py        # refresh the Sleeper dump (manual)
```

The CSVs are exported by hand: each position's page under
<https://www.fantasypros.com/nfl/projections/> (the season view) has an Export button,
and the files keep FantasyPros' own names. An export that includes average/high/low
also works: the high/low variant rows carry an empty Player cell and are skipped, so
only the consensus average is used. `fetch_sleeper.py` is manual too: Sleeper's
player dump is about 14 MB and should not be downloaded more than once per day. The small
metadata file beside it records when it was fetched, and the build warns when the dump is
more than two weeks old.

## File contract

`pool.json` is the narrow draft input: every QB/RB/WR/TE with a FantasyPros projection
that joins to a Sleeper player, 10 fields per player.

- `player_id` / `sleeper_id`: Sleeper's id, as an integer and as the API's string. The
  pool's identity is Sleeper's because the live draft is; a projected player Sleeper does
  not list at that position is left out and named in `excluded.no_sleeper_match`.
- `name`, `team`, `age`, `is_rookie`: from Sleeper.
- `points_1yr`: the 2026 projection scored under this league's settings, applied to the
  export's raw stat line (`scoring` in the file header). This matters for QBs: FantasyPros'
  own FPTS column assumes -1 per interception where the league scores -2. Everything else
  in the half-PPR export already agrees, and `--report` checks the column layout by
  recomputing FPTS under FantasyPros' weights.
- `rank` / `positional_rank`: descending `points_1yr`.

Rows FantasyPros zero-fills (no projection) are dropped. There is no bye week, ADP, or
multi-year horizon: the exports carry none, and the league is a redraft.

## Sleeper matching

There is no shared id, so `match_sleeper.py` uses three conservative name tiers: full
normalized name; name without suffix; then last name plus team. Position must always
agree with Sleeper's listing (which is what drops FantasyPros' fullback rows), an
ambiguous name is an error rather than a guess, and duplicate Sleeper ids are fatal.
`--report` lists every suffix and last-name join for eyeballing.
