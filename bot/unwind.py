"""Unwind planner: decides, for one stock, which orders to cancel and which to place this loop.

Plain English:
  * SWEEP: right after a tender is accepted, take every visible share priced at least as well
    as break-even (tender price +/- both fees) with marketable limits capped at that price. This
    is free edge and immediately cuts market risk.
  * SCHEDULE: work the rest over a short horizon (shorter when volatility is high or competitors
    are draining the same side), once per tick, with marketable limits a tick through the touch,
    never more than a fraction of visible depth, and never chasing far below the recent mid.
  * PASSIVE: while there is time, rest one limit order inside the spread to earn better prices
    from incoming buyers/sellers.
  * ESCALATE (last ~30 ticks): finish the schedule by the market-order deadline, pricing further
    through the book and dropping the mid guard.
  * FLATTEN (last ~10 ticks): market orders capped at the exact remaining position, every loop,
    until flat. Being flat at the end beats any slippage saving.
Every order this module proposes still has to pass the compliance gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .gate import OrderRequest
from .marketdata import Level, round_price
from .tracker import TrackedOrder

NORMAL, ESCALATE, FLATTEN = "NORMAL", "ESCALATE", "FLATTEN"


@dataclass
class UnwindContext:
    ticker: str
    tick: int
    end_tick: int
    pos: int
    committed: float                      # same-side shares that may still fill (gate's view)
    live: List[TrackedOrder]              # our non-terminal orders in this stock
    foreign: List[Dict]                   # open orders on the server we did not send
    bids: List[Level]
    asks: List[Level]
    tick_size: float
    max_order: int
    breakeven: Optional[float]
    sigma: float
    sigma_prior: float
    competitors: float
    start_tick: int
    last_schedule_tick: int
    ref_mid: Optional[float]
    tender_ids: tuple
    cfg: object                           # cfg.unwind
    force_flatten: bool = False


@dataclass
class Plan:
    phase: str
    cancels: List[TrackedOrder] = field(default_factory=list)
    cancel_foreign: List[int] = field(default_factory=list)
    places: List[OrderRequest] = field(default_factory=list)
    scheduled: bool = False
    notes: List[str] = field(default_factory=list)


def phase_for(tick: int, end_tick: int, cfg) -> str:
    if tick >= end_tick - int(cfg.market_ticks_before_end):
        return FLATTEN
    if tick >= end_tick - int(cfg.escalate_ticks_before_end):
        return ESCALATE
    return NORMAL


def horizon(ctx: UnwindContext) -> int:
    cfg = ctx.cfg
    base = float(getattr(cfg.horizon_ticks, ctx.ticker, 20))
    if ctx.sigma > 0 and ctx.sigma_prior > 0:
        base *= (ctx.sigma_prior / ctx.sigma) ** float(cfg.vol_urgency)
    base /= (1.0 + max(0.0, ctx.competitors))
    h = int(round(min(max(base, float(cfg.min_horizon_ticks)), float(cfg.max_horizon_ticks))))
    deadline = ctx.end_tick - int(cfg.market_ticks_before_end)
    return max(1, min(h, deadline - ctx.start_tick))


def _depth_at_or_better(levels: List[Level], price: float, selling: bool) -> int:
    if selling:
        return int(sum(q for p, q in levels if p >= price - 1e-9))
    return int(sum(q for p, q in levels if p <= price + 1e-9))


def plan(ctx: UnwindContext) -> Plan:
    cfg = ctx.cfg
    ph = FLATTEN if ctx.force_flatten else phase_for(ctx.tick, ctx.end_tick, cfg)
    out = Plan(ph)
    out.cancel_foreign = [int(o["order_id"]) for o in ctx.foreign if o.get("order_id") is not None]
    if ctx.pos == 0:
        out.cancels = [o for o in ctx.live if o.order_id is not None]
        return out
    selling = ctx.pos > 0
    side = "SELL" if selling else "BUY"
    size = abs(ctx.pos)
    keep: List[TrackedOrder] = []
    for o in ctx.live:
        if o.action != side:
            out.cancels.append(o)           # wrong direction (e.g. after a netting tender)
        else:
            keep.append(o)

    if ph == FLATTEN:
        out.cancels += [o for o in keep if o.type == "LIMIT"]
        avail = size - ctx.committed
        while avail >= 1 and len(out.places) < int(cfg.max_places_per_loop):
            q = int(min(avail, ctx.max_order))
            out.places.append(OrderRequest(ctx.ticker, side, q, "MARKET", None, "flatten", ctx.tender_ids))
            avail -= q
        return out

    # trim: same-side orders that could exceed the position (e.g. after a netting tender)
    committed = ctx.committed
    if committed > size:
        for o in sorted(keep, key=lambda o: -o.local_id):
            if committed <= size:
                break
            out.cancels.append(o)
            committed -= o.remaining
    # stale orders
    best_same = (ctx.asks[0][0] if ctx.asks else None) if selling else (ctx.bids[0][0] if ctx.bids else None)
    for o in keep:
        if o in out.cancels or o.type != "LIMIT" or o.order_id is None:
            continue
        age = ctx.tick - o.created_tick
        if o.purpose == "passive":
            off_touch = best_same is not None and o.price is not None and (
                (selling and o.price > best_same + 1e-9) or (not selling and o.price < best_same - 1e-9))
            if ph != NORMAL or age >= int(cfg.passive_max_age_ticks) or off_touch:
                out.cancels.append(o)
        elif age >= int(cfg.stale_ticks):
            out.cancels.append(o)

    avail = size - ctx.committed            # cancels are not confirmed yet: stay conservative
    levels = ctx.bids if selling else ctx.asks
    if avail < 1 or not levels:
        return out
    tick = ctx.tick_size
    min_child = int(cfg.min_child)

    # 1. SWEEP everything priced better than break-even
    if cfg.sweep and ctx.breakeven is not None and ph == NORMAL:
        edge = float(cfg.min_edge)
        limit = round_price(ctx.breakeven + edge, tick, "up") if selling else round_price(ctx.breakeven - edge, tick, "down")
        depth = _depth_at_or_better(levels, limit, selling)
        depth -= sum(o.remaining for o in keep if o.purpose == "sweep" and o not in out.cancels)
        while avail >= 1 and depth >= min(min_child, avail) and len(out.places) < int(cfg.max_places_per_loop):
            q = int(min(avail, depth, ctx.max_order))
            if q < 1:
                break
            out.places.append(OrderRequest(ctx.ticker, side, q, "LIMIT", limit, "sweep", ctx.tender_ids))
            avail -= q
            depth -= q

    # 2. SCHEDULE: once per tick
    if avail >= 1 and ctx.tick > ctx.last_schedule_tick and len(out.places) < int(cfg.max_places_per_loop):
        if ph == NORMAL:
            ticks_left = ctx.start_tick + horizon(ctx) - ctx.tick
        else:
            ticks_left = ctx.end_tick - int(cfg.market_ticks_before_end) - ctx.tick
        ticks_left = max(1, ticks_left)
        desired = int(math.ceil(size / ticks_left))
        best = levels[0][0]
        slip = int(cfg.slip_ticks) if ph == NORMAL else int(cfg.escalate_slip_ticks)
        price = best - slip * tick if selling else best + slip * tick
        if ph == NORMAL and ctx.ref_mid is not None:
            max_slip = float(getattr(cfg.max_slip, ctx.ticker, 0.08))
            guard = ctx.ref_mid - max_slip if selling else ctx.ref_mid + max_slip
            price = max(price, guard) if selling else min(price, guard)
        price = round_price(price, tick, "up" if selling else "down")
        frac = float(cfg.depth_fraction) if ph == NORMAL else 1.0
        depth = _depth_at_or_better(levels, price, selling)
        q = int(min(desired, avail, ctx.max_order, math.floor(frac * depth)))
        if q >= min(min_child, avail) and q >= 1:
            out.places.append(OrderRequest(ctx.ticker, side, q, "LIMIT", price, "schedule", ctx.tender_ids))
            avail -= q
            out.scheduled = True
        else:
            out.notes.append(f"schedule waiting for depth (wanted {desired:,}, depth {depth:,} at {price})")

    # 3. PASSIVE: one resting order inside the spread while there is time
    if cfg.passive and ph == NORMAL and avail >= 1 and ctx.bids and ctx.asks \
            and not any(o.purpose == "passive" and o not in out.cancels for o in keep) \
            and len(out.places) < int(cfg.max_places_per_loop):
        bb, ba = ctx.bids[0][0], ctx.asks[0][0]
        if selling:
            px = ba - tick if ba - bb > tick + 1e-9 else ba
            px = round_price(px, tick, "up")
            ok = px > bb + 1e-9
        else:
            px = bb + tick if ba - bb > tick + 1e-9 else bb
            px = round_price(px, tick, "down")
            ok = px < ba - 1e-9
        q = int(min(avail, ctx.max_order, math.ceil(float(cfg.passive_fraction) * size)))
        if ok and q >= min(min_child, avail):
            out.places.append(OrderRequest(ctx.ticker, side, q, "LIMIT", px, "passive", ctx.tender_ids))
    return out
