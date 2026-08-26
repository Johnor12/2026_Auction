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
  nominations, output, and auction self-tests

The older snake simulation modules remain in the repository for history but are not on
the `rank.py` execution path.

## Roster value

`lineup_gain` is the player's marginal expected optimal-lineup points on our current
roster. Higher players can cover missing starters; deeper players contribute according
to the probability they are called up when players above them are unavailable. One
projected post-draft waiver player per position is available as a fallback.

The waiver line is estimated by removing the players the field consensus expects the
league's remaining open roster spots to consume. Made purchases remain on their real
rosters and are removed from the available pool.

## Maximum bid

The FantasyPros 12-team, $200 auction values supply a dollar curve. Our projection-based
preseason value rank selects a point on that curve. The live maximum bid then applies:

1. current roster factor = current marginal lineup gain / empty-roster marginal gain;
2. league inflation = remaining league dollars / modeled price of remaining purchases;
3. our budget pace relative to league dollars per open slot;
4. the legal cap `remaining budget - $1 * (other open slots)`.

This makes the value personal without inventing a runtime risk setting or running a slow
portfolio simulation during the draft.

## Field price and nominations

Each opponent gets a modeled ceiling from its inferred source order. Before it has enough
purchases to infer a source, the consensus of all normalized boards is used. Remaining
budget, roster depth, and live inflation adjust the ceiling; a team cannot exceed its own
legal maximum.

An ascending auction is priced at one dollar above the second-highest modeled opponent
ceiling, capped by the highest. Nomination recommendations require a positive
`field_price - max_bid` and sort by that drain gap.

## Bounded live work

The ranker examines at most 240 available players: remaining league purchases plus a
72-player waiver buffer, capped at 240. The output board shrinks during the auction.
Everything is deterministic and batched through the lineup solver; there are no Monte
Carlo redraws or full-draft rollouts.

## Output contract

`rankings.json` contains:

- `my_auction`: remaining dollars, slots, legal ceiling, and a projection-only roster
  completion diagnostic;
- `nomination_strategy.recommendations`: the top positive drain gaps;
- `teams`: actual purchases and current budget state for all 12 rosters;
- `rankings`: available-player max bids, expected field prices, drain gaps, ranks, and
  the top modeled opposing bidders;
- `analysis`: pool bound, inflation, wire levels, and the pricing explanation;
- `validation`: every-run contract and budget checks.

Any validation problem makes `rank.py` exit nonzero.
