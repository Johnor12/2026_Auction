"""FAAB market model: opponents' waiver claims so far -> this week's competing bids.

Fit from every opponent claim Sleeper shows this season, won or failed:

- how often each opponent claims: its claims per completed week, as a Poisson rate;
- whom a claim targets: free agents ranked by the last completed week's actual points,
  rank k drawn with geometric probability (1 - q) q^(k-1), q fit to the claimed players'
  ranks. That ordering predicts this league's claims far better than projections do.
  Claims made before any week was complete carry no rank and are left out of q;
- what it bids: a draw from every opponent bid so far. The bids show no relation to the
  target's rank or projection, so none is modeled, and no bid above the season's
  highest is ever simulated;
- the league's price of a point: dollars bid over the claimants' modeled gains, which
  prices our own FAAB dollars.

A Monte Carlo of this week's run then gives, per free agent, the chance a bid of b dollars
wins: it beats every opponent bid on him, or ties the highest and our waiver position,
Sleeper's FAAB tiebreak, is better.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

SIMS = 20_000
SEED = 20260922


@dataclass
class Market:
    rates: dict[int, float]  # opponent roster_id -> claims per week
    q: float
    bids: list[int]
    price: float  # dollars per expected lineup point


def rosters_before(rosters: dict[int, list[str]], transactions: list[dict], ms: int) -> dict[int, list[str]]:
    """League rosters just before ``ms``: every completed move since is undone, newest first."""
    out = {rid: list(players) for rid, players in rosters.items()}
    later = [t for t in transactions if t["status"] == "complete" and t["status_updated"] >= ms]
    for t in sorted(later, key=lambda t: -t["status_updated"]):
        for p, rid in (t["adds"] or {}).items():
            out[rid].remove(p)
        for p, rid in (t["drops"] or {}).items():
            out[rid].append(p)
    return out


def fit(claims: list[dict], weeks_done: int) -> Market:
    """claims: one {"roster_id", "bid", "rank" or None, "gain"} per opponent claim."""
    ranks = [c["rank"] for c in claims if c["rank"] is not None]
    paid = [c for c in claims if c["bid"] > 0]
    gained = sum(max(c["gain"], 0.0) for c in paid)
    if not ranks or not gained:
        raise SystemExit("too few opponent claims to fit the FAAB market yet")
    mean = sum(r - 1 for r in ranks) / len(ranks)
    counts = Counter(c["roster_id"] for c in claims)
    return Market(
        rates={rid: n / weeks_done for rid, n in counts.items()},
        q=mean / (mean + 1),
        bids=[c["bid"] for c in claims],
        price=sum(c["bid"] for c in paid) / gained,
    )


def win_chances(
    market: Market,
    pool_size: int,
    targets: list[int],
    priority: dict[int, int],
    ours: int,
    budget: int,
) -> list[np.ndarray]:
    """P(win) at each bid 0..budget for each target, an index into the pool's rank order."""
    rng = np.random.default_rng(SEED)
    rank_p = market.q ** np.arange(pool_size)
    rank_p /= rank_p.sum()
    parts = []
    for rid, rate in market.rates.items():
        sims = np.repeat(np.arange(SIMS), rng.poisson(rate, SIMS))
        parts.append(
            (
                sims,
                rng.choice(pool_size, sims.size, p=rank_p),
                rng.choice(market.bids, sims.size),
                np.full(sims.size, priority[rid]),
            )
        )
    sims, target, bid, prio = (np.concatenate(column) for column in zip(*parts))
    bids = np.arange(budget + 1)[:, None]
    out = []
    for t in targets:
        hit = target == t
        top = np.full(SIMS, -1)
        np.maximum.at(top, sims[hit], bid[hit])
        tied = hit & (bid == top[sims])
        first = np.full(SIMS, np.iinfo(np.int64).max)
        np.minimum.at(first, sims[tied], prio[tied])
        out.append(((top < bids) | ((top == bids) & (ours < first))).mean(axis=1))
    return out


def best_bid(chances: np.ndarray, gain: float, price: float) -> tuple[int, float, float]:
    """The bid maximizing P(win) x (gain - bid in points): (bid, P(win), expected points)."""
    ev = chances * (gain - np.arange(chances.size) / price)
    b = int(np.argmax(ev))
    return b, float(chances[b]), float(ev[b])
