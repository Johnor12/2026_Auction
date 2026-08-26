# Data-source investigator

This process snapshots public redraft boards in this league's format, normalizes player
identity, and asks which board best explains each drafter's picks. It evaluates every
selection at the moment it was made: drafted players above it are removed before its
availability rank is measured.

```bash
uv run data_source_investigator/pipeline.py --report
uv run data_source_investigator/pipeline.py --only investigate  # reuse the snapshots
uv run data_source_investigator/investigate.py --selftest
uv run evaluate_opponents.py  # forward replay, 100 prediction draws per opponent pick
uv run refresh.py --report  # refresh the draft, ranking board, and evaluation only
uv run serve.py  # open http://127.0.0.1:8123/data_source_investigator/
```

The built-in sources are the closest public match to a 12-team, $200 auction, superflex,
0.5 PPR redraft with no TE premium:

- FantasyPros' half-PPR superflex expert consensus draft rankings.
- FantasyPros' auction values from its Draft Wizard calculator at 12 teams, $200, and
  half PPR, with two QB starters standing in for the superflex slot the calculator lacks.
  This is the one source in dollars, i.e. baseline bid targets.
- KeepTradeCut's redraft superflex crowd values (KTC states 12 teams, 0.5 PPR).
- FantasyCalc's redraft superflex trade values at 12 teams and 0.5 PPR.
- Sleeper's own 2QB-league ADP, which is what this draft room displays.
- FantasyFootballCalculator's 12-team 2QB mock-draft ADP.

Provider formats are not identical; `data/rankings.json` and the final report retain the
exact format for each source, and each row's `value` is in the source's native unit
(dollars, crowd value, or ADP). Raw web responses stay in `data/raw/`, so parsing can be
repeated without replacing the snapshot.

Implementation context is separated by concern: `fetch_rankings.py` owns network
snapshots, `providers.py` owns provider-specific formats, `identity.py` owns canonical
player resolution, `build_rankings.py` assembles normalized boards, and `investigate.py`
scores those boards against the draft.

The investigate stage reads `draft.json`. This league's draft is an auction, which
`draft_pipeline/fetch_draft.py` does not publish yet, so until that rework lands only the
fetch and build stages run against the new league (`--only fetch`, `--only build`).

`data_source_matches.json` is the published report. For each drafter it includes the
closest and second-closest source, a separation-based confidence label, every provider's
score, and pick-level evidence showing the higher-ranked available players they passed.
The label is an inference, not proof: rankings are correlated, roster construction and
personal preferences cause intentional deviations, and a current ranking snapshot may
have changed since an earlier draft pick.

The ranker consumes the closest source as each opponent's player order and
`mean_log2_loss` as that manager's adherence estimate. Personal and opponent strategies
remain separate: the opponent picker never translates the source order into personal
projections or board value. It checks that the report's draft id and ranking-snapshot timestamp
match its other inputs, so `refresh.py` always runs this investigation after fetching the
draft and before simulating the remaining picks.

`index.html` visualizes that report in two connected views: a league-wide team/source
fit heatmap and a selected team's pick/source availability matrix. Selecting any matrix
cell shows the provider's overall rank, the player's rank among those still available,
and up to five higher-ranked players the team passed.

Provider identities join by Sleeper id first (FantasyCalc and Sleeper carry it) and
normalized name second. When a source lacks the id, a final conservative tier accepts
first-name abbreviations only when the first names are prefixes and last name, team, and
position all agree (`Cam Ward` to `Cameron Ward`). It intentionally does not make
last-name-only guesses such as matching Tahj Washington to Malik Washington.

## Manual sources

For a paid, login-only, or user-exported provider, place one CSV in `data/manual/` and run
the build and investigate stages. The filename becomes the source id (for example,
`beersheet.csv` becomes `beersheet`). Required columns are:

```csv
rank,name,position
1,Josh Allen,QB
2,Jahmyr Gibbs,RB
```

Optional columns are `team,sleeper_id,value`. Names are joined to `pool.json` by normalized
name and position when `sleeper_id` is absent. A manual board must contain at least 50
unique QB/RB/WR/TE rows. Raw and manual inputs are ignored by git by default.
