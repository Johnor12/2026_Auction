# Weekly pipeline

In-season: the best lineup for the current week with a free-agent check (`lineup.py`),
and a rest-of-season waiver check with IR moves and suggested FAAB bids (`waivers.py`),
both from a blend of two weekly projection sources.

```text
data/week<N>/FantasyPros_Fantasy_Football_Projections_{QB,FLX}.csv   (hand export)
api.sleeper.app/projections/nfl/<season>/<week>                     (fetched live)
pool_pipeline/data/sleeper_players.json                             (name join)
../pool.json, ../draft.json, Sleeper league rosters
  -> lineup.py -> printed report          (the current week's exports; required)

the same exports and projections, every week through 17
Sleeper actual stats, league settings, rosters, transactions         (fetched live)
../draft.json, pool_pipeline/data/sleeper_players.json
  -> waivers.py + market.py -> printed report   (exports optional, per week)
```

```bash
uv run weekly_pipeline/lineup.py
uv run weekly_pipeline/waivers.py
```

## Sources and blend

- **FantasyPros** weekly consensus: the QB page and the FLX page (RB, WR, TE in one
  file with a `POS` column) under <https://www.fantasypros.com/nfl/projections/>, weekly
  view, half-PPR, exported with the page's Export button under FantasyPros' filenames.
  Unauthenticated page loads show ten rows, so the export is the only complete form. The
  export carries no week number, so each week's pair goes in its own folder:
  `qb.php?week=6` and `flx.php?week=6` save to `data/week6/`. FantasyPros publishes future
  weeks the same way; its season view is the preseason full-season total, not rest of
  season, so it is not used.
- **Sleeper** weekly projections (Rotowire), fetched live, which also supply opponent,
  game date, and injury status.

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

## Waiver check

`waivers.py` refetches Sleeper's weekly projections for every week through week 17
(`LAST_WEEK`: six playoff teams from week 14 with a two-week final), the league's rosters,
transactions and last week's actual stats on each run. A week with both FantasyPros
exports in `data/week<N>/` is blended as above; a week without them is Sleeper alone, so
exporting is optional and can cover any subset of weeks. An export listing a team that is
on bye that week fails as another week's file. A week a player is not projected counts as
zero, which is how the sources mark byes and the absences they expect. Run it before the
week's games: the current week counts in full.

### Roster value and IR

A roster is worth its expected lineup points summed over the remaining weeks, each week
solved by the ranker's closed-form expected lineup (`ranker/value.py`): a projected week is
the player's if-active number, missed at his position's `INJURY_UNAVAILABLE_RATE` (byes are
exact here, so the ranker's averaged bye is not added). In later weeks the best other free
agent at each position is one always-available backup, standing in for the streaming
those weeks will allow. This week has none: an empty slot this week stays empty unless a
claim fills it. A player we pass on is assumed claimed by someone else and a dropped
player is gone. Weeks count equally; playoff odds are not modeled.

Roster limits are applied week by week. A player is sidelined in a week he is not
projected and his team plays, if he can be on IR then: this week his Sleeper status must
be one the league allows (IR, PUP, Out; `IR_STATUSES`), later weeks need only a current
injury tag. Up to two sidelined players sit on IR and everyone else needs one of the 14
active spots. When that overflows (a claim now, an IR player returning later) the drop is
whoever leaves the roster worth the most from that week on, which can be an IR player once
a third is sidelined. This week a projected player who is still IR-eligible (say, listed
Out but expected back) may instead stay on IR, deferring that drop a week, when his points
this week are worth less than the dropped player's. So an injured free agent can be
stashed on IR, and a pickup that will be cut when a starter returns is worth only the
weeks before. Only this week's moves are printed; later ones are assumed, not advised. A
player left on IR must still be IR-eligible when waivers process, or Sleeper blocks the
claim.

### Suggested bids

`market.py` fits opponents' behavior to every opponent claim Sleeper shows this season,
won or failed (their bids are visible either way):

- **How often:** each opponent's claims per completed week, as a Poisson rate.
- **Whom:** free agents ranked by the last completed week's actual points; a claim takes
  rank k with probability (1 - q) q^(k-1), q fit to claimed players' ranks when claimed.
  That ordering explains this league's claims far better than projections do.
- **How much:** a draw from every opponent bid so far. Bids show no relation to the
  target's rank or projection, so none is modeled, and no bid above the season's highest
  is simulated.
- **Price of a point:** dollars opponents bid over their claims' modeled gains, which is
  what our dollars are worth.

A Monte Carlo of this week's run (20,000 draws, fixed seed) gives each free agent's chance
of going to a bid of b dollars: it beats every opponent bid on him, or ties the highest
while our waiver position (Sleeper's FAAB tiebreak) is better. The suggested bid maximizes
P(win) x (gain - bid / price).

### Output

Our expected lineup points, FAAB and waiver position; the moves the model makes without a
claim (activations, IR placements, drops); the fitted market; then up to 15 claims by
expected value, each with its bid, win chance, season and current-week gains, and every
move it needs, which replace the no-claim moves. Each claim is valued alone: claims
sharing a drop are alternatives, so order them in Sleeper by expected value. A gain under
0.1 points is left out.
