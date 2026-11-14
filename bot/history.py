"""Rolling per-stock history: volatility, book refill rate, and competitor detection.

Competitor detection (to learn whether other traders get the same tender): when a tender
arrives we note the market volume on that stock for the next `window` ticks, excluding our own
fills, and compare it with the normal volume. Excess volume divided by the tender size is a rough
count of other traders unwinding a block of the same size. That estimate (smoothed across
tenders) feeds the valuation (refill is shared) and the unwind urgency.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class TenderWatch:
    tender_id: int
    tick0: int
    quantity: int
    side: str                  # book side the unwind would hit: 'bid' or 'ask'
    depth0: int
    base_volume: float         # normal market volume per tick before the tender
    window: int
    done: bool = False
    result: Dict = field(default_factory=dict)


class TickerHistory:
    def __init__(self, ticker: str, vol_prior: float, refill_prior: float, window: int = 90,
                 prior_weight: float = 20.0, depth_ticks: int = 10) -> None:
        self.ticker = ticker
        self.vol_prior = vol_prior
        self.refill_prior = refill_prior
        self.prior_weight = prior_weight
        self.depth_ticks = depth_ticks
        self.mids: Deque[Tuple[int, float]] = deque(maxlen=window)
        self.diffs: Deque[float] = deque(maxlen=window)
        self.refills: Deque[float] = deque(maxlen=window)
        self.last_depth: Optional[Tuple[int, int, int]] = None
        self.ewma_mid: Optional[float] = None
        self.volume: Dict[int, float] = {}       # tick -> market volume (time & sales)
        self.own: Dict[int, float] = {}          # tick -> our filled volume
        self.watches: List[TenderWatch] = []
        self.competitor_est: Optional[float] = None
        self.last_tick = -1

    # --------------------------------------------------------------- books
    def on_book(self, tick: int, bids, asks, tick_size: float) -> None:
        if not bids or not asks:
            return
        mid = (bids[0][0] + asks[0][0]) / 2.0
        if tick != self.last_tick:
            if self.mids:
                t_prev, m_prev = self.mids[-1]
                gap = tick - t_prev
                if 0 < gap <= 10:
                    self.diffs.append((mid - m_prev) / math.sqrt(gap))
            self.mids.append((tick, mid))
            self.ewma_mid = mid if self.ewma_mid is None else 0.7 * self.ewma_mid + 0.3 * mid
            within = self.depth_ticks * tick_size
            bd = sum(q for p, q in bids if p >= bids[0][0] - within - 1e-9)
            ad = sum(q for p, q in asks if p <= asks[0][0] + within + 1e-9)
            if self.last_depth is not None:
                t_prev, bd0, ad0 = self.last_depth
                gap = max(1, tick - t_prev)
                if gap <= 5:
                    own = self.own.get(tick, 0.0) + self.own.get(t_prev, 0.0)
                    vol = self.volume.get(tick, 0.0)
                    inc = (max(0, bd - bd0) + max(0, ad - ad0) + max(0.0, vol - own)) / 2.0 / gap
                    self.refills.append(inc)
            self.last_depth = (tick, bd, ad)
            self.last_tick = tick

    def sigma(self) -> float:
        n = len(self.diffs)
        if n < 2:
            return self.vol_prior
        s2 = sum(d * d for d in self.diffs) / n
        w = self.prior_weight
        return math.sqrt((n * s2 + w * self.vol_prior ** 2) / (n + w))

    def refill(self) -> float:
        n = len(self.refills)
        if n == 0:
            return self.refill_prior
        m = sum(self.refills) / n
        w = self.prior_weight
        return (n * m + w * self.refill_prior) / (n + w)

    # ------------------------------------------------------------- volume
    def on_tas(self, trades) -> None:
        for tr in trades or []:
            t = int(tr.get("tick", 0))
            self.volume[t] = self.volume.get(t, 0.0) + float(tr.get("quantity", 0) or 0)

    def on_own_fill(self, tick: int, qty: float) -> None:
        self.own[tick] = self.own.get(tick, 0.0) + qty

    def base_volume(self, tick: int, lookback: int = 20) -> float:
        ticks = [t for t in range(tick - lookback, tick)]
        vols = [max(0.0, self.volume.get(t, 0.0) - self.own.get(t, 0.0)) for t in ticks]
        return sum(vols) / len(vols) if vols else 0.0

    # --------------------------------------------------------- competitors
    def watch_tender(self, tender_id: int, tick: int, quantity: int, unwind_action: str, bids, asks,
                     window: int = 10) -> None:
        side = "bid" if unwind_action == "SELL" else "ask"
        levels = bids if side == "bid" else asks
        depth0 = int(sum(q for _, q in levels[:10])) if levels else 0
        self.watches.append(TenderWatch(tender_id, tick, quantity, side, depth0, self.base_volume(tick), window))

    def update_watches(self, tick: int, bids, asks) -> List[Dict]:
        finished = []
        for w in self.watches:
            if w.done or tick < w.tick0 + w.window:
                continue
            others = sum(max(0.0, self.volume.get(t, 0.0) - self.own.get(t, 0.0))
                         for t in range(w.tick0, w.tick0 + w.window + 1))
            excess = max(0.0, others - w.base_volume * (w.window + 1))
            levels = bids if w.side == "bid" else asks
            depth_now = int(sum(q for _, q in levels[:10])) if levels else 0
            est = excess / w.quantity if w.quantity else 0.0
            w.done = True
            w.result = {"tender_id": w.tender_id, "other_volume": round(others), "excess_volume": round(excess),
                        "competitor_estimate": round(est, 2), "depth0": w.depth0, "depth_now": depth_now}
            self.competitor_est = est if self.competitor_est is None else 0.5 * self.competitor_est + 0.5 * est
            finished.append(w.result)
        return finished
