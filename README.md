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
- **Pursue:** cost-efficient players that repeatedly land on our roster in complete
  simulated auctions, with closing-price ranges and acquisition rates.

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

Roster value is the expected best legal lineup, reselected whenever a better healthy
player can demote a nominal starter. Position-level injury risk and one bye in 18 weeks
give useful depth its replacement value. The pool does not carry team bye weeks, so bye
unavailability is averaged independently rather than correlated by NFL team.

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
- `rankings.json`: bid ceilings, field prices, rollout price ranges and roster rates,
  pursue/nomination recommendations, team budgets, one representative full-league
  rollout, and validation results

`sleeper_id` joins players across processes. `roster_id` is the auction team identity;
`draft_slot` is not used as a future turn because an auction has no snake geometry.

## Auction pricing

Our maximum bid does not copy the provider's player prices. The ranker greedily completes
our current roster by marginal expected-lineup value, then allocates every remaining
discretionary dollar across that completion value. Maximum bids use 1.8 times that
allocation: a ceiling is one of many mutually exclusive limits, not expected spend, and
purchases normally close below it. Fixed-seed full-auction comparisons selected 1.8x
because lower ceilings stranded cash while higher ceilings reduced final expected-lineup
value. Every bid remains capped by the hard legal ceiling that reserves $1 for every other
open slot.

The FantasyPros 12-team, $200 curve supplies only the field's dollar scale. Each opponent
maps its inferred provider order onto that curve, with roster depth, remaining budget,
live inflation, and stable owner/player evaluation noise. Before purchases reveal an
owner's likely source, cold-start owners are spread across the available public boards;
the assignments and noise are deterministic so refreshes do not make the live board jump.
An open ascending auction is modeled to close one dollar above the second-highest legal
opponent ceiling, capped by the highest. `field_price - max_bid` is the nomination drain
gap.

The ranker also rolls out 40 complete auctions. Nomination order, cold-start source
assignments, and opponent evaluation noise vary per rollout. Our policy repeatedly
revalues the remaining completion whenever a purchase changes it and bids
projection-valued dollars; it is a practical sequential policy, not a clairvoyant global
optimizer. Each player's 10th–90th percentile closing-price range and the fraction of
rollouts in which we acquire him drive the **Pursue** list. Rollout diagnostics include
unused budget, nominal starter points, the expected-lineup value added by useful depth,
and starter/bench roles on a representative final roster. The dashboard also exposes all
12 completed teams from that same representative rollout, including every purchase and
price, individual projected points, starter/bench assignments, and each starting lineup's
total projection for simulator sanity checks. Each team also reports expected lineup
points under the backup/unavailability model and its auction spend split between starters
and bench.

Every simulated purchase preserves enough slots to fill the dedicated starter groups.
The model also refuses additions beyond two players over its plausible completed-roster
position target; that is a guard against simulated 9-RB or 11-WR rosters, not a Sleeper
league rule.

### Live-speed limit

At most 240 available players are examined. That is the 168-player draft plus six waiver
candidates per team. As the draft fills, the board shrinks to the number of purchases
still needed plus the same 72-player waiver buffer. Drafted players outside that analysis
horizon still count on their roster and against their team's budget.

The published board remains deterministic because the rollouts use a fixed seed. On the
current 480-player source pool, pricing plus 40 complete rollouts takes roughly 40 seconds
on a single CPU; the network-bound full refresh can still be slower.

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
