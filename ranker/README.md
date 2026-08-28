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
- `auction.py`: live state, bounded candidate pool, expected prices, the completion plan,
  maximum bids, field prices, parallel full-auction rollouts, pursue/nomination
  recommendations, output, and auction self-tests

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

## Expected price

Each opponent gets a modeled ceiling from its inferred source order on the FantasyPros
dollar curve, scaled by its purse: dollars per open spot over curve dollars per remaining
purchase. Before it has enough purchases to infer a source, cold-start owners are
deterministically spread across the normalized boards. Roster depth and stable
owner/player evaluation noise adjust the ceiling. A team cannot exceed its own legal
maximum, buy a player that makes its dedicated starter groups impossible to fill, or add
more than two players beyond a modeled completed-roster position target.

A player's expected price is what beating the field should cost us: one dollar above the
expected highest opponent ceiling, averaged over fixed evaluation-noise draws because the
maximum of eleven noisy ceilings sits above the maximum of their medians whenever owners
contend. The rollouts then calibrate it: a first pass records what beating the field
actually cost at each player's nomination, and the live plan and the second pass use the
realized-over-static ratio, which fades inside a rollout as its own state matures.

## Completion plan and maximum bid

The plan is the completion of our roster that adds the most expected-lineup points at
expected prices. A greedy completion adds, at a shadow price per discretionary dollar,
the candidate with the largest marginal lineup gain net of the dollars he costs; the
shadow price is bisected until the plan just fits the budget. Expected lineup value is
submodular, so a candidate's gain on the bare roster bounds his later gain and the greedy
is lazy. Spend jumps when a star enters, so the budget-constrained plan just below the
binding rate competes with the free plan just above it and the better one is kept. The
plan reserves the cheapest real price (normally $2) for every unfilled spot; when the
budget cannot cover that it reserves the legal $1 and goes short.

A maximum bid is the price at which the player and the plan's best alternative use of
that money tie: for a planned player, the best substitute's price plus the value lost by
swapping; for anyone else, the best planned player he could displace, that player's price
plus the value gained. Unplanned spots take any improvement for $1. The result is capped
by `remaining budget - $1 * (other open slots)`.

## Field price and nominations

An ascending auction without us is priced at one dollar above the second-highest modeled
opponent ceiling, capped by the highest. Nomination recommendations require a positive
`field_price - max_bid` and sort by that drain gap.

## Cost-efficient targets

Forty-eight fixed-seed rollouts finish the complete auction from the current state, in
parallel across CPU cores, twice (see expected price). They vary nomination order,
cold-start opponent sources, and opponent evaluation noise. After every purchase the
bidding policy reprices its planned players, because those prices are what its bids
consume; it substitutes at the plan's shadow price when an opponent takes a planned
player, and reprices the whole market and redraws the plan whenever it buys or the plan's
cost drifts more than $3 from what it planned. A roster spot no policy filled is
autofilled at $1 and reported; validation fails on it.

Each row reports the 10th, 50th, and 90th percentile simulated closing price, the median
realized acquisition price, its simulated acquisition rate, and our average price when
acquired. The **Pursue** list ranks players that most often land on the roster. It is a
sequential uncertainty-aware heuristic, not a globally optimal roster chosen with
knowledge of all future prices.

The simulation summary reports final spend and unused budget, our rank among the 12 teams
by expected points, opponents' unused budget, nominal healthy-starter points, and the
additional expected-lineup value supplied by the bench. Its representative completion
marks each purchase as `starter` or `bench` for direct roster inspection. It also
includes all 12 complete teams from that same rollout, with position counts, prices,
individual one-season projections, starter/bench assignments, summed starter projections,
expected lineup points under the same backup/unavailability assumptions used by the
ranker, starter/bench spend, remaining budgets, and live purchases distinguished from
simulated purchases.

## Bounded live work

The ranker examines at most 240 available players: remaining league purchases plus a
72-player waiver buffer, capped at 240. The output board shrinks during the auction.
Fixed seeds keep output deterministic regardless of the worker count. The rollouts use
every core up to eight, at low priority. On the current 480-player input, pricing and two
passes of 48 rollouts take roughly 20 seconds on eight workers and 25 seconds on the
4-core GitHub runner.

## Output contract

`rankings.json` contains:

- `my_auction`: remaining dollars, slots, legal ceiling, the plan's shadow price, and the
  completion plan with each member's expected price and in-plan lineup gain;
- `purchase_strategy.recommendations`: recurring cost-efficient targets from full-auction
  rollouts;
- `nomination_strategy.recommendations`: the top positive drain gaps;
- `teams`: actual purchases and current budget state for all 12 rosters;
- `rankings`: available-player max bids, expected (learned) and static prices, plan
  membership, current field prices, rollout price ranges, acquisition rates, drain gaps,
  ranks, and the top modeled opposing bidders;
- `analysis`: pool bound, inflation, wire levels, rollout diagnostics, and the pricing
  explanation, including one representative rollout's 12 complete teams;
- `validation`: every-run contract and budget checks.

Any validation problem makes `rank.py` exit nonzero.
