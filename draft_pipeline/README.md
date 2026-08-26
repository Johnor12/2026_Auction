# Draft pipeline

`fetch_draft.py` is the network-only entry point that publishes `draft.json` on demand.
It does not cache responses because a stale live board is worse than another small fetch.

```bash
uv run draft_pipeline/fetch_draft.py
uv run draft_pipeline/fetch_draft.py --report
uv run draft_pipeline/fetch_draft.py --selftest
uv run draft_pipeline/fetch_draft.py --me someone
```

## Internal boundaries

- `fetch_draft.py`: Sleeper requests and CLI orchestration
- `draft_board.py`: draft geometry, ownership, pick rows, and JSON document construction
- `report.py`: integrity diagnostics and the optional `pool.json` join report
- `selftest.py`: offline formats, reversal, trade, autopick, and malformed-board checks
- `paths.py`: this process's input/output locations

## Inputs and output

The fetch reads four Sleeper endpoints: the draft, made picks, traded picks, and league
users. Draft, picks, and trades are load-bearing; user lookup may fail with only names
becoming null.

`draft.json.picks` always contains all 168 picks, indexed by gap-free `pick_no`.
Made rows carry Sleeper's player fields. Pending rows carry null player fields but already
identify the current owner, so consumers can answer who picks next and when my next pick
occurs. `sleeper_id` joins made picks to `pool.json`; Sleeper's name, position, and NFL
team are informational.

## Derived pending picks

Sleeper reports slot and roster only after a pick is made. Pending picks are derived from
the draft settings:

- normal snake rounds alternate direction;
- a reversal round repeats the prior round's direction and flips parity thereafter;
- traded ownership is applied by round and the pick's original roster.

This league's 2026 draft is an auction (12 teams × 14 rounds = 168 picks, no reversal),
which has no pick order to derive, so `fetch_draft.py` currently refuses it. Auction
support is a pending rework; the derivation above and the offline self-test still
describe the snake case.

Every fetch compares derived slot and roster against every made pick Sleeper reports.
Disagreements are warnings and are retained in `board_derivation`. The offline self-test
covers later rounds and trade cases the current live board may not yet exercise.

