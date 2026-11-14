"""Tender valuation and the accept / wait / decline decision.

Plain English: accepting a tender changes our position. We value the whole book before and
after:
    EV(accept) = tender cash - tender fee + V(position after) - V(position now)
where V(x) is what we expect to net by unwinding x shares:
  * shares the visible book will take now at prices at least as good as working them later,
  * the rest worked as the book refills, at a reference price (VWAP of the top N shares)
    minus slippage and an inventory-risk buffer that grows with volatility and unwind time,
  * anything that cannot be worked before the end-of-heat deadline is dumped through the
    remaining book and beyond it at a penalty,
  * minus the per-share fee on every unwind share.
Using V(after) - V(now) handles existing inventory, netting against an opposite tender, shared
liquidity and late tenders with one formula.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .gate import limits_ok
from .marketdata import Level, Tender


@dataclass
class LiqParams:
    fee: float                 # $ per unwind share
    tick: float
    ref_depth: int             # shares for the reference VWAP
    sigma: float               # $ std of mid per tick
    refill: float              # shares/tick that come back near the touch
    competitors: float         # other traders sharing the refill
    risk_lambda: float
    slippage_ticks: float
    dump_penalty: float
    ticks_available: float     # ticks left to work the position before the end-of-heat deadline


def ref_price(levels: List[Level], depth: int) -> Optional[float]:
    if not levels:
        return None
    got, cash = 0, 0.0
    for p, q in levels:
        take = min(q, depth - got)
        got += take
        cash += take * p
        if got >= depth:
            break
    return cash / got if got else None


def walk(levels: List[Level], qty: float) -> Tuple[float, float]:
    """(filled, cash) from hitting levels best-first."""
    got, cash = 0.0, 0.0
    for p, q in levels:
        if got >= qty:
            break
        take = min(q, qty - got)
        got += take
        cash += take * p
    return got, cash


def _liquidate_long(q: float, levels: List[Level], p: LiqParams) -> Tuple[float, Dict]:
    """Value of selling q > 0 shares into `levels` (best first, higher is better)."""
    if q <= 0:
        return 0.0, {}
    if not levels:
        return -math.inf, {"reason": "empty book"}
    R = ref_price(levels, p.ref_depth)
    r_eff = max(p.refill, 1.0) / (1.0 + max(p.competitors, 0.0))
    rest_guess = q
    later_px = R
    for _ in range(3):
        unwind_ticks = rest_guess / r_eff
        c_later = p.slippage_ticks * p.tick + p.risk_lambda * p.sigma * math.sqrt(max(unwind_ticks, 0.0) / 3.0)
        later_px = R - c_later
        imm = sum(qq for pp, qq in levels if pp >= later_px - 1e-9)
        rest_guess = max(0.0, q - imm)
    imm_levels = [(pp, qq) for pp, qq in levels if pp >= later_px - 1e-9]
    imm_qty, imm_cash = walk(imm_levels, q)
    rest = q - imm_qty
    cap = r_eff * max(0.0, p.ticks_available)
    later_qty = min(rest, cap)
    dump_qty = rest - later_qty
    deep = [(pp, qq) for pp, qq in levels if pp < later_px - 1e-9]
    d_qty, d_cash = walk(deep, dump_qty)
    if dump_qty - d_qty > 1e-9:
        last = deep[-1][0] if deep else levels[-1][0]
        d_cash += (dump_qty - d_qty) * (last - p.dump_penalty)
    value = imm_cash + later_qty * later_px + d_cash - p.fee * q
    return value, {"ref": R, "later_px": later_px, "imm_qty": imm_qty, "later_qty": later_qty,
                   "dump_qty": dump_qty, "unwind_ticks": later_qty / r_eff, "r_eff": r_eff}


def liquidation_value(pos: float, bids: List[Level], asks: List[Level], p: LiqParams) -> Tuple[float, Dict]:
    """Expected net cash from unwinding signed position `pos` (long sells into bids, short buys asks)."""
    if pos > 0:
        return _liquidate_long(pos, bids, p)
    if pos < 0:
        neg = [(-pp, qq) for pp, qq in asks]          # buying at p == selling at -p
        v, d = _liquidate_long(-pos, neg, p)
        for k in ("ref", "later_px"):
            if k in d and d[k] is not None:
                d[k] = -d[k]
        return v, d
    return 0.0, {}


@dataclass
class Evaluation:
    tender_id: int
    ticker: str
    action: str
    quantity: int
    price: float
    ev: float = -math.inf
    ev_per_share: float = -math.inf
    threshold: float = 0.0
    feasible: bool = True
    reason: str = ""
    decision: str = "DECLINE"
    detail: Dict = field(default_factory=dict)


def evaluate_tender(t: Tender, positions: Dict[str, int], bids: List[Level], asks: List[Level],
                    params: LiqParams, fee_tender: float, net_limit: int, gross_limit: int,
                    per_stock_cap: bool, other_ranges: Optional[Dict[str, Tuple[float, float]]] = None) -> Evaluation:
    ev = Evaluation(t.tender_id, t.ticker, t.action, t.quantity, t.price)
    if not t.valid:
        ev.feasible, ev.reason = False, f"invalid tender: {t.issue}"
        return ev
    x = positions.get(t.ticker, 0)
    d = t.signed_qty
    after = dict(positions)
    after[t.ticker] = x + d
    # limits: the new position plus the worst case of open orders in the other stocks
    ranges = {k: (float(v), float(v)) for k, v in after.items()}
    for k, rng in (other_ranges or {}).items():
        if k != t.ticker:
            ranges[k] = rng
    ok, why = limits_ok(ranges, after, net_limit, gross_limit)
    # positions already beyond a limit before the tender may not be used as an excuse
    if ok:
        net_after = abs(sum(after.values()))
        gross_after = sum(abs(v) for v in after.values())
        if net_after > net_limit or gross_after > gross_limit:
            ok, why = False, "would breach limits"
    if ok and per_stock_cap and abs(x + d) > net_limit:
        ok, why = False, f"|{t.ticker}| would be {abs(x + d):,} > {net_limit:,} (per-stock cap)"
    if not ok:
        ev.feasible, ev.reason = False, why
        return ev
    v_now, _ = liquidation_value(x, bids, asks, params)
    v_after, det = liquidation_value(x + d, bids, asks, params)
    if math.isinf(v_now) or math.isinf(v_after):
        ev.feasible, ev.reason = False, "no book on the unwind side"
        return ev
    cash = -d * t.price - fee_tender * abs(d)
    ev.ev = cash + v_after - v_now
    ev.ev_per_share = ev.ev / abs(d)
    ev.detail = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in det.items()}
    ev.detail.update({"pos_before": x, "pos_after": x + d, "sigma": round(params.sigma, 4),
                      "refill": round(params.refill, 1), "competitors": round(params.competitors, 2),
                      "ticks_available": params.ticks_available, "fee_tender": fee_tender, "fee": params.fee})
    return ev


def decide(ev: Evaluation, tick: int, expires: int, cfg_tender, unwind_only: bool, degraded: bool) -> Evaluation:
    """ACCEPT immediately if the edge clearly clears the minimum; WAIT (re-evaluate every tick) if
    marginal and there is time; otherwise DECLINE explicitly before expiry."""
    ev.threshold = max(float(cfg_tender.min_ev_total), float(cfg_tender.min_ev_per_share) * ev.quantity)
    if unwind_only:
        ev.decision, ev.reason = "DECLINE", ev.reason or "unwind-only mode (risk stop or kill switch)"
        return ev
    if degraded:
        ev.decision, ev.reason = "DECLINE", ev.reason or "degraded: repeated errors or stale data"
        return ev
    if not ev.feasible:
        ev.decision = "DECLINE"
        return ev
    deadline = expires - int(cfg_tender.decide_margin_ticks)
    if ev.ev >= ev.threshold:
        ev.decision, ev.reason = "ACCEPT", f"EV ${ev.ev:,.0f} >= threshold ${ev.threshold:,.0f}"
    elif ev.ev >= float(cfg_tender.marginal_ratio) * ev.threshold and tick < deadline:
        ev.decision, ev.reason = "WAIT", f"marginal EV ${ev.ev:,.0f} < ${ev.threshold:,.0f}; re-checking until tick {deadline}"
    else:
        ev.decision, ev.reason = "DECLINE", f"EV ${ev.ev:,.0f} below threshold ${ev.threshold:,.0f}"
    return ev
