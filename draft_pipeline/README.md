# Draft pipeline

`fetch_draft.py` reads Sleeper's draft, picks, traded picks, league users, and league
rosters, then publishes `draft.json`.

```bash
uv run draft_pipeline/fetch_draft.py
uv run draft_pipeline/fetch_draft.py --report
uv run draft_pipeline/fetch_draft.py --selftest
uv run draft_pipeline/fetch_draft.py --me someone
```

## Auction contract

Sleeper auctions have no pending pick order. `draft.json.picks` therefore contains made
purchases only, ordered by `pick_no`; the pipeline does not fabricate 168 future turns.
Each row carries:

- the winning `roster_id` and owner;
- `sleeper_id`, name, position, and NFL team;
- the winning `amount` from `pick.metadata.amount`.

The header contains the $200 budget, all 12 roster identities, purchase counts, and a
`budgets` summary with spend, dollars remaining, and open spots. The ranker recomputes
the same budget facts from purchases and validates them.

League rosters are load-bearing for auctions because `draft_order` may be null; they map
each `roster_id` to its Sleeper owner. User lookup only supplies display and team names.

## Snake compatibility

`draft_board.py` still supports snake and linear fixtures. For those formats it derives
pending slots from round geometry and traded-pick ownership and compares them with made
picks. Auction mode takes a separate made-purchases-only path.

## Internal boundaries

- `fetch_draft.py`: Sleeper requests and CLI orchestration
- `draft_board.py`: format handling and the `draft.json` document
- `report.py`: integrity diagnostics and the optional `pool.json` join report
- `selftest.py`: auction amounts plus legacy snake geometry, trades, and malformed picks
- `paths.py`: input/output locations
