# Ranker

`rank.py` consumes `pool.json`, the live auction `draft.json`, normalized provider boards,
and the optional current `data_source_matches.json`, then publishes `rankings.json`.

```bash
uv run rank.py
uv run rank.py --report
uv run rank.py --no-draft
uv run rank.py --draft other.json
uv run rank.py --selftest
```

`--no-draft` keeps the auction's roster identities but ignores made purchases.

## Active modules

- `league.py`: league shape and hardcoded auction assumptions
- `pool.py`: `pool.json` to `Player` objects
- `value.py`: exact expected-lineup value with position availability and waiver fallbacks
- `auction.py`: live state, bounded candidate pool, maximum bids, field prices,
  full-auction rollouts, pursue/nomination recommendations, output, and auction self-tests

The older snake simulation modules remain in the repository for history but are not on
the `rank.py` execution path.

## Roster value

`lineup_gain` is the player's marginal expected optimal-lineup points on our current
roster. The best legal lineup is reselected, so a better player can demote a nominal
starter without an injury. Deeper players contribute only when that reselected lineup
calls on them. Position-level injury risk and one averaged bye in 18 weeks supply those
replacement opportunities; team-specific bye correlations are unavailable in
`pool.json`. One projected post-draft waiver player per position is a fallback.

The waiver line is estimated by removing the players the field consensus expects the
league's remaining open roster spots to consume. Made purchases remain on their real
rosters and are removed from the available pool.

## Maximum bid

The ranker greedily completes our current roster by marginal expected-lineup value. It
allocates all discretionary dollars across the gains in that completion, producing one
points-per-dollar rate. A maximum bid uses 1.8x the corresponding allocation because
mutually exclusive bid ceilings are not expected purchase prices. Fixed-seed full-draft
comparisons selected 1.8x by final expected-lineup value: lower ceilings stranded budget
and higher ceilings overpaid early. The result is capped by
`remaining budget - $1 * (other open slots)`.

This removes the FantasyPros curve's $57 top-player outlier from our personal budget. The
curve remains the field's dollar scale, where it represents likely market behavior rather
than our projection-valued willingness to pay.

## Field price and nominations

Each opponent gets a modeled ceiling from its inferred source order. Before it has enough
purchases to infer a source, cold-start owners are deterministically spread across the
normalized boards. Stable owner/player evaluation noise, remaining budget, roster depth,
and live inflation adjust the ceiling. A team cannot exceed its own legal maximum, buy a
player that makes its dedicated starter groups impossible to fill, or add more than two
players beyond a modeled completed-roster position target.

An ascending auction is priced at one dollar above the second-highest modeled opponent
ceiling, capped by the highest. Nomination recommendations require a positive
`field_price - max_bid` and sort by that drain gap.

## Cost-efficient targets

Forty fixed-seed rollouts finish the complete auction from the current state. They vary
nomination order, cold-start opponent sources, and opponent evaluation noise. The bidding
policy revalues whenever our roster or the projected completion changes, so savings and
missed targets change later bids.

Each row reports the 10th, 50th, and 90th percentile simulated closing price, its simulated
acquisition rate, and our average price when acquired. The **Pursue** list ranks players
that most often land on the roster. It is a sequential uncertainty-aware heuristic, not
a globally optimal roster chosen with knowledge of all future prices.

The simulation summary reports final spend and unused budget, nominal healthy-starter
points, and the additional expected-lineup value supplied by the bench. Its representative
completion marks each purchase as `starter` or `bench` for direct roster inspection. It
also includes all 12 complete teams from that same rollout, with position counts, prices,
individual one-season projections, starter/bench assignments, summed starter projections,
remaining budgets, and live purchases distinguished from simulated purchases.

## Bounded live work

The ranker examines at most 240 available players: remaining league purchases plus a
72-player waiver buffer, capped at 240. The output board shrinks during the auction.
Fixed seeds keep output deterministic. On the current 480-player input, pricing and 40
complete rollouts takes roughly 40 seconds on one CPU.

## Output contract

`rankings.json` contains:

- `my_auction`: remaining dollars, slots, legal ceiling, and a projection-only roster
  completion diagnostic;
- `purchase_strategy.recommendations`: recurring cost-efficient targets from full-auction
  rollouts;
- `nomination_strategy.recommendations`: the top positive drain gaps;
- `teams`: actual purchases and current budget state for all 12 rosters;
- `rankings`: available-player max bids, current field prices, rollout price ranges,
  acquisition rates, drain gaps, ranks, and the top modeled opposing bidders;
- `analysis`: pool bound, inflation, wire levels, rollout diagnostics, and the pricing
  explanation, including one representative rollout's 12 complete teams;
- `validation`: every-run contract and budget checks.

Any validation problem makes `rank.py` exit nonzero.
