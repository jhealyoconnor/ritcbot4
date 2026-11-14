"""THE compliance gate. Every order the bot sends passes through check(); there is no other path.

An order is allowed only if ALL of these hold:
  1. the case is ACTIVE;
  2. the request is well formed (ticker, BUY/SELL, MARKET/LIMIT, whole positive quantity,
     LIMIT price positive and on the tick grid);
  3. no tender in that stock is pending (strict front-running freeze);
  4. the stock has a non-zero server position AND the ledger holds accepted-tender inventory
     of that sign (so we only ever trade tender inventory);
  5. the order REDUCES the position (sell when long, buy when short);
  6. quantity <= max order size;
  7. quantity + every same-side order that might still fill <= |position| (cannot cross zero);
  8. the worst case over all partial fills of every open order stays inside the net/gross limits
     (or, if somehow already outside, does not make the breach worse).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple

from .marketdata import on_grid


@dataclass
class OrderRequest:
    ticker: str
    action: str
    qty: int
    type: str = "LIMIT"
    price: Optional[float] = None
    purpose: str = ""
    tender_ids: Tuple[str, ...] = ()


@dataclass
class GateContext:
    status: str
    positions: Dict[str, int]
    committed: Callable[[str, str], float]          # (ticker, action) -> shares that may still fill
    has_inventory: Callable[[str, int], bool]       # (ticker, sign) -> ledger holds tender inventory
    pending_tickers: Set[str]
    max_order: Dict[str, int]
    tick_size: Dict[str, float]
    net_limit: int
    gross_limit: int
    tickers: Tuple[str, ...] = field(default_factory=tuple)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def position_ranges(ctx: GateContext, extra: Optional[OrderRequest] = None) -> Dict[str, Tuple[float, float]]:
    """For each stock, the interval of positions reachable if any subset of open orders fills."""
    out: Dict[str, Tuple[float, float]] = {}
    for t in ctx.tickers or tuple(ctx.positions):
        p = ctx.positions.get(t, 0)
        buys = ctx.committed(t, "BUY")
        sells = ctx.committed(t, "SELL")
        if extra is not None and extra.ticker == t:
            if extra.action == "BUY":
                buys += extra.qty
            else:
                sells += extra.qty
        out[t] = (p - sells, p + buys)
    return out


def limits_ok(ranges: Dict[str, Tuple[float, float]], positions: Dict[str, int], net_limit: int,
              gross_limit: int) -> Tuple[bool, str]:
    names = list(ranges)
    # worst net is at a corner of the box; worst gross is max |x| per stock
    corners = [0.0]
    for t in names:
        lo, hi = ranges[t]
        corners = [c + lo for c in corners] + [c + hi for c in corners]
    worst_net = max(abs(c) for c in corners)
    worst_gross = sum(max(abs(ranges[t][0]), abs(ranges[t][1])) for t in names)
    cur_net = abs(sum(positions.get(t, 0) for t in names))
    cur_gross = sum(abs(positions.get(t, 0)) for t in names)
    if worst_net > max(net_limit, cur_net) + 1e-9:
        return False, f"worst-case net {worst_net:,.0f} > limit {net_limit:,}"
    if worst_gross > max(gross_limit, cur_gross) + 1e-9:
        return False, f"worst-case gross {worst_gross:,.0f} > limit {gross_limit:,}"
    return True, ""


def check(req: OrderRequest, ctx: GateContext) -> Tuple[bool, str]:
    if ctx.status != "ACTIVE":
        return False, f"case is {ctx.status}"
    if req.ticker not in ctx.positions:
        return False, f"unknown ticker {req.ticker}"
    if req.action not in ("BUY", "SELL"):
        return False, f"bad action {req.action}"
    if req.type not in ("MARKET", "LIMIT"):
        return False, f"bad order type {req.type}"
    if not isinstance(req.qty, int) or req.qty <= 0:
        return False, f"bad quantity {req.qty!r}"
    if req.type == "LIMIT":
        tick = ctx.tick_size.get(req.ticker, 0.01)
        if req.price is None or req.price <= 0:
            return False, "limit order without a valid price"
        if not on_grid(req.price, tick):
            return False, f"price {req.price} not on the {tick} tick grid"
    if req.ticker in ctx.pending_tickers:
        return False, f"FROZEN: tender pending in {req.ticker}"
    pos = ctx.positions.get(req.ticker, 0)
    if pos == 0:
        return False, f"no position in {req.ticker}"
    if not ctx.has_inventory(req.ticker, _sign(pos)):
        return False, f"no accepted-tender inventory in {req.ticker}"
    reducing = "SELL" if pos > 0 else "BUY"
    if req.action != reducing:
        return False, f"{req.action} would increase |position| ({pos:+,})"
    max_q = ctx.max_order.get(req.ticker)
    if max_q is not None and req.qty > max_q:
        return False, f"qty {req.qty:,} > max order size {max_q:,}"
    pending_same = ctx.committed(req.ticker, req.action)
    if req.qty + pending_same > abs(pos) + 1e-9:
        return False, (f"would cross zero: qty {req.qty:,} + open same-side {pending_same:,.0f} "
                       f"> |position| {abs(pos):,}")
    ok, why = limits_ok(position_ranges(ctx, req), ctx.positions, ctx.net_limit, ctx.gross_limit)
    if not ok:
        return False, why
    return True, "ok"
