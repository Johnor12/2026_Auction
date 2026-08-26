# Ranker

`rank.py` consumes `pool.json`, `draft.json`, normalized provider boards, and
`data_source_matches.json`, then publishes `rankings.json`.

```bash
uv run rank.py
uv run rank.py --report
uv run rank.py --no-draft
uv run rank.py --draft other.json
uv run rank.py --selftest
```

A live board is the simulation's starting state, not a post-processing filter. Made picks
remain on their rosters, only pending picks are played, and output ranking rows contain
undrafted players only.

## Modules

- `league.py`: league shape and hardcoded strategy constants
- `pool.py`: pool document to `Player` objects
- `board.py`: live `draft.json` to the immutable starting state
- `opponents.py`: inferred provider boards to complete opponent strategies
- `value.py`: horizon points, expected lineup value, and wire measurement
- `simulation.py`: one deterministic draft state and pick policies
- `convergence.py`: wire-level fixed point
- `planning.py`: Monte Carlo availability, candidate survival, lookahead, and rollouts
- `rankings.py`: ranking rows and serialized next-pick recommendations
- `output.py`: top-level `rankings.json` payload
- `report.py`: human-readable stderr diagnostics
- `validate.py`: every-run output and league invariants
- `selftest.py`: solver, opponent-separation, planning, and malformed-board checks

## Value model

The pool is split into year 1 and years 2–3 because a lineup is fielded each season.
The board is ranked by `lineup_gain`, each player's marginal expected-lineup value on
my current roster at the converged wire levels.

The final waiver bodies affect my expected-lineup choices, and those choices affect who
remains undrafted. This creates a fixed point:

```text
wire levels -> expected lineup value -> simulated draft -> wire levels
```

The map is discrete and can alternate between neighboring league shapes, so convergence
detects a repeated state and averages levels over that cycle. The expected-lineup solver
values the weekly re-optimized lineup: the starter composition is re-chosen per
availability draw, so a flex seat vacated at one position is refilled by the best
remaining body at any flex position. It is exact and closed-form — dedicated slots via a
per-position Bernoulli cascade ordered by points when active, the FLEX/superflex seats
via layer-cake integrals of the pooled cross-position marginal count (at most one QB,
the superflex) — and `--selftest` checks it against brute-force enumeration of every
availability subset. One always-available waiver body can fill one job at each position;
it is not an unlimited scalar. Expectation-of-max keeps value monotone when a projection
improves or a player is added. Years 2–3 use their own projections and lineup, with no
second growth bonus.

## Opponents and planning

Personal and opponent strategies are intentionally separate. My slot alone uses
projections and expected-lineup roster value. Each opponent uses the provider board
closest to its completed picks, with a soft boost for unfilled dedicated starters and a
compounding source-rank penalty for adding players beyond comfortable positional depth.
The depth targets sum to 12, so the last two spots remain source-driven rather than
forcing every opponent into one exact roster shape. These are preferences, not draft
limits: a large enough source-rank gap can still justify another player at a deep position.
Observed `mean_log2_loss` calibrates randomness around that preference, after shrinking
toward the cold-start prior as if that prior had been observed on two extra picks — one
or two on-board picks otherwise fit a near-deterministic policy. A flat RB tilt
(`OPPONENT_POSITION_TILT`) prices the RB dart-throwing that replays show every source
board under-predicts. Missing provider
players are appended in DraftSharks ADP order; opponents never fall back to my board.

My simulated pick policy does not use those targets or any other positional roster-size
heuristic. Positional depth is priced only by projected expected-lineup value, so a roster
shape that differs from the opponents' conventional behavior can be a source of value.

The bulk deterministic policy scores value now plus the expected best option at its next
pick. The live shortlist starts from the current board before intervening opponents pick,
then removes candidates below 5% survival to my turn. Candidate branches are evaluated
conditional on reaching that turn. Four-pick planning applies the same 5% floor to each
later target's conditional survival before playing finalists to the end of the draft.
The first `take` is the EV recommendation if available; when the noiseless example has
already removed it, `deterministic_fallback` identifies the example draft's legal choice.
Worker processes receive immutable inputs once, and seeded task ids keep results
deterministic across scheduling. Planning uses at most eight workers so a rerank does not
saturate every host CPU; this changes elapsed time, not the simulations or their output.

## Output contract

`rankings.json.rankings` is ranked by the roster-aware `lineup_gain` decision metric and
contains projected and simulated pick fields, opponent consensus deltas, and
availability estimates for each undrafted player.
`my_next_picks` is the full recommendation and can intentionally disagree with the
static board order. `example_draft` contains structured final-roster records for dashboard
lineup comparison. `validation.problems` is empty on success; any problem makes the CLI
exit nonzero.
