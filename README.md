# 2026 auction draft assistant

A live assistant for the 12-team superflex auction in
[Gnosis Auction 2026](https://sleeper.com/leagues/1396606685107200000/predraft).
Independent processes publish JSON artifacts at the repository root; the ranker consumes
them and the dependency-free dashboard renders the result.

The two live decisions are:

- **Maximum bid:** the most this roster should pay for each available player, after its
  current players, remaining dollars, open slots, the $1-per-slot reserve, and the
  remaining market are accounted for.
- **Nominate next:** players whose modeled field price is above our maximum bid, ordered
  by the dollars they should drain from opponents.

## League assumptions

Discovered from Sleeper league `1396606685107200000` and draft
`1396606686923341824`:

- 0.5 PPR, no TE premium; 4-point passing TDs and otherwise standard scoring
- Starters: 1 QB, 2 RB, 3 WR, 1 TE, 1 W/R/T flex, 1 W/R/T/Q superflex
- 5 bench, 2 IR, no taxi squad
- 12 teams, 14 auction purchases per team, $200 budget
- $1 minimum purchase; a bid must leave $1 for every other open roster spot

Projections are FantasyPros' 2026 consensus season projections scored by
`pool_pipeline/` under these settings. These are project assumptions, not runtime flags;
the constants live in `ranker/league.py`.

## Setup

[uv](https://docs.astral.sh/uv/) pins Python 3.12.

```bash
uv sync
uv run <script>
```

Commands work from the repository root.

## Data flow

```text
pool_pipeline/ ----------------------> pool.json ----------------------+
                                                                     |
draft_pipeline/ ---------------------> draft.json --------------------+--> rank.py --> rankings.json
                                                                     |
data_source_investigator/ --> data_source_matches.json --------------+
                          `-> data/rankings.json ---------------------+
```

The published files have distinct owners:

- `pool.json`: 2026 projections joined to Sleeper players
- `draft.json`: completed auction purchases, their winning `amount`, team identities,
  and remaining budgets; auctions have no fabricated pending pick order
- `data_source_matches.json`: the provider board closest to each opponent's purchases;
  it is a valid cold-start document with no owners before the first purchase
- `rankings.json`: bid ceilings, field prices, nomination recommendations, team budgets,
  and validation results

`sleeper_id` joins players across processes. `roster_id` is the auction team identity;
`draft_slot` is not used as a future turn because an auction has no snake geometry.

## Auction pricing

The ranker starts with the provider's 12-team, $200 auction dollar curve but does not copy
its player values onto our board. Our projection-based preseason value rank determines
where a player lands on that curve. The value is then discounted by the player's current
marginal expected-lineup value relative to an empty roster, and adjusted for:

- our current roster and the projected post-draft waiver level;
- actual league dollars and roster spots remaining (market inflation);
- our dollars-per-open-slot pace relative to the league;
- the hard legal ceiling that reserves $1 for every other open slot.

The field model gives every opponent a ceiling from its inferred provider order, roster
depth, remaining budget, and the same live inflation. An open ascending auction is modeled
to close one dollar above the second-highest opponent ceiling, capped by the highest.
`field_price - max_bid` is the nomination drain gap.

### Live-speed limit

At most 240 available players are examined. That is the 168-player draft plus six waiver
candidates per team. As the draft fills, the board shrinks to the number of purchases
still needed plus the same 72-player waiver buffer. Drafted players outside that analysis
horizon still count on their roster and against their team's budget.

The pricing path is deterministic and contains no Monte Carlo or full-draft rollout. On
the current 480-player source pool the pricing pass runs in a fraction of a second; the
network-bound full refresh is normally the slower part.

## Components

- [Pool pipeline](pool_pipeline/README.md): FantasyPros projections to `pool.json`
- [Draft pipeline](draft_pipeline/README.md): Sleeper auction state to `draft.json`
- [Data-source investigator](data_source_investigator/README.md): normalize redraft and
  auction sources and infer opponent preferences
- [Ranker](ranker/README.md): roster valuation, auction pricing, nomination strategy,
  and output contracts
- `index.html`: the main auction dashboard
- `data_source_investigator/index.html`: opponent source-fit evidence
- `serve.py`: serves both dashboards at http://127.0.0.1:8123

## Workflows

Refresh live state and recommendations between nominations:

```bash
uv run refresh.py --report
```

`refresh.py` fetches Sleeper, applies the existing provider snapshot to completed
purchases, then rebuilds bids. It deliberately does not refetch projections or public
ranking sources during the timed draft.

Refresh the offline source snapshot separately:

```bash
uv run pool_pipeline/build_pool.py --report
uv run data_source_investigator/pipeline.py --report
```

Run offline checks:

```bash
uv run rank.py --selftest
uv run draft_pipeline/fetch_draft.py --selftest
uv run data_source_investigator/investigate.py --selftest
```

Run `uv run serve.py` and open the local URL; direct `file://` access cannot fetch JSON.
The dashboard polls Sleeper for purchase-count staleness and can dispatch the repository's
refresh workflow. The workflow commits `draft.json`, `data_source_matches.json`, and
`rankings.json`; Pages publishes both dashboards.

## Important source assumptions

FantasyPros' auction calculator supports a second QB starter rather than a superflex
slot, so that is the closest available dollar curve. It supplies the dollar scale only;
our league-scored projections supply the personal player order and roster value. Public
opponent boards are also correlated, and an inferred source is evidence rather than proof
of an owner's strategy. Exact source formats and timestamps remain in
`data_source_investigator/data/rankings.json`.
