"""Parsing of server JSON into simple, validated structures (books, tenders, securities)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Level = Tuple[float, int]  # (price, remaining quantity), best first


@dataclass
class Book:
    ticker: str
    bids: List[Level] = field(default_factory=list)   # descending price, excludes our own orders
    asks: List[Level] = field(default_factory=list)   # ascending price, excludes our own orders
    own_bids: List[Level] = field(default_factory=list)
    own_asks: List[Level] = field(default_factory=list)
    keys: str = ""                                     # which JSON keys the server used ("bids/asks" or "bid/ask")
    recv: int = 0
    tick: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2.0
        if self.bids:
            return self.bids[0][0]
        if self.asks:
            return self.asks[0][0]
        return None

    def side(self, action: str) -> List[Level]:
        """Levels a market order with this action would hit (SELL hits bids)."""
        return self.bids if action == "SELL" else self.asks

    def depth(self, side: str, within: float) -> int:
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return 0
        best = levels[0][0]
        return int(sum(q for p, q in levels if abs(p - best) <= within + 1e-9))


def _aggregate(entries: List[Dict[str, Any]], descending: bool) -> List[Level]:
    agg: Dict[float, int] = {}
    for e in entries:
        price = e.get("price")
        if price is None:
            continue
        qty = int(round(float(e.get("quantity", 0)) - float(e.get("quantity_filled", 0) or 0)))
        if qty <= 0:
            continue
        p = round(float(price), 6)
        agg[p] = agg.get(p, 0) + qty
    return sorted(agg.items(), key=lambda kv: -kv[0] if descending else kv[0])


def parse_book(raw: Dict[str, Any], ticker: str, own_trader_id: Optional[str] = None,
               anon_only: bool = False) -> Book:
    """Accepts both 'bids'/'asks' (support script, live server) and 'bid'/'ask' (swagger spec)."""
    raw = raw or {}
    if "bids" in raw or "asks" in raw:
        bids_raw, asks_raw, keys = raw.get("bids") or [], raw.get("asks") or [], "bids/asks"
    else:
        bids_raw, asks_raw, keys = raw.get("bid") or [], raw.get("ask") or [], "bid/ask"

    def split(entries):
        mine, others = [], []
        for e in entries:
            tid = e.get("trader_id")
            if own_trader_id is not None and tid == own_trader_id:
                mine.append(e)
            elif anon_only and tid is not None and str(tid).upper() != "ANON":
                continue
            else:
                others.append(e)
        return mine, others

    own_b, other_b = split(bids_raw)
    own_a, other_a = split(asks_raw)
    return Book(ticker, _aggregate(other_b, True), _aggregate(other_a, False),
                _aggregate(own_b, True), _aggregate(own_a, False), keys)


# ---------------------------------------------------------------- tenders
_INST_RE = re.compile(r"\b(BUY|SELL)\b\s+([\d,]+)\s+shares?\s+of\s+([A-Za-z]{2,8})\b", re.I)
_PRICE_RE = re.compile(r"\$\s*([\d,]*\.?\d+)")
_OUR_RE = re.compile(r"would\s+you\s+like\s+to\s+(BUY|SELL)", re.I)


@dataclass
class Tender:
    tender_id: int
    ticker: str
    action: str            # OUR action if accepted: BUY (we go long) or SELL (we go short)
    quantity: int
    price: float
    expires: int
    tick: int
    caption: str
    is_fixed_bid: bool
    valid: bool = True
    issue: str = ""
    field_perspective: str = ""   # what the 'action' field turned out to mean: trader / institution / unknown
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def signed_qty(self) -> int:
        return self.quantity if self.action == "BUY" else -self.quantity

    @property
    def unwind_action(self) -> str:
        return "SELL" if self.action == "BUY" else "BUY"


def _opposite(action: str) -> str:
    return "SELL" if action == "BUY" else "BUY"


def parse_caption(caption: str) -> Optional[Dict[str, Any]]:
    """'An institution would like to SELL 75,000 shares of CRZY to you at a price of $9.75.
    Would you like to BUY the shares from them?' -> our action BUY, 75000, CRZY, 9.75."""
    if not caption:
        return None
    m = _INST_RE.search(caption)
    if not m:
        return None
    inst = m.group(1).upper()
    out: Dict[str, Any] = {"inst_action": inst, "quantity": int(m.group(2).replace(",", "")),
                           "ticker": m.group(3).upper(), "our_action": _opposite(inst)}
    pm = _PRICE_RE.search(caption, m.end())
    if pm:
        out["price"] = float(pm.group(1).replace(",", ""))
    om = _OUR_RE.search(caption)
    if om and om.group(1).upper() != out["our_action"]:
        out["conflict"] = f"caption says institution {inst}s but asks us to {om.group(1).upper()}"
    return out


def parse_tender(raw: Dict[str, Any], known_tickers: List[str], action_fallback: str = "trader") -> Tender:
    """Builds a Tender with OUR action. The caption is the authority; every field is cross-checked
    against it and any mismatch marks the tender invalid (it will be declined, never guessed)."""
    tid = int(raw.get("tender_id"))
    caption = str(raw.get("caption") or "")
    qty_field = raw.get("quantity")
    price_field = raw.get("price")
    action_field = (raw.get("action") or "").upper()
    ticker_field = raw.get("ticker")
    fixed = bool(raw.get("is_fixed_bid", True))
    issues = []
    cap = parse_caption(caption)

    ticker = ticker_field or (cap or {}).get("ticker")
    if ticker_field and cap and cap["ticker"] != str(ticker_field).upper():
        issues.append(f"ticker field {ticker_field} != caption {cap['ticker']}")
    if not ticker or str(ticker).upper() not in [t.upper() for t in known_tickers]:
        issues.append(f"unknown ticker {ticker!r}")

    perspective = "unknown"
    if cap:
        action = cap["our_action"]
        if "conflict" in cap:
            issues.append(cap["conflict"])
        if action_field == action:
            perspective = "trader"
        elif action_field == cap["inst_action"]:
            perspective = "institution"
        elif action_field:
            issues.append(f"action field {action_field!r} unreadable")
    else:
        if action_field not in ("BUY", "SELL"):
            issues.append("cannot determine side: no parseable caption and no action field")
            action = "BUY"
        else:
            action = action_field if action_fallback == "trader" else _opposite(action_field)
            perspective = action_fallback + " (fallback)"

    quantity = int(round(float(qty_field))) if qty_field is not None else int((cap or {}).get("quantity", 0))
    if cap and qty_field is not None and int(round(float(qty_field))) != cap["quantity"]:
        issues.append(f"quantity field {qty_field} != caption {cap['quantity']}")
    if price_field is not None:
        price = float(price_field)
    else:
        price = float((cap or {}).get("price") or float("nan"))
    if cap and price_field is not None and cap.get("price") is not None and abs(cap["price"] - float(price_field)) > 0.005:
        issues.append(f"price field {price_field} != caption {cap['price']}")
    if not fixed:
        issues.append("not a fixed-price tender (auction tenders are not part of this case)")
    if quantity <= 0:
        issues.append("non-positive quantity")
    if price is None or math.isnan(price) or price <= 0:
        issues.append("no valid price")

    return Tender(tid, str(ticker or "").upper(), action, quantity, price,
                  int(raw.get("expires") or 0), int(raw.get("tick") or 0), caption, fixed,
                  valid=not issues, issue="; ".join(issues), field_perspective=perspective, raw=dict(raw))


# ------------------------------------------------------------- securities
@dataclass
class SecurityInfo:
    ticker: str
    position: int = 0
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    max_trade_size: Optional[int] = None
    trading_fee: Optional[float] = None
    limit_order_rebate: Optional[float] = None
    quoted_decimals: Optional[int] = None
    api_orders_per_second: Optional[int] = None
    execution_delay_ms: Optional[int] = None
    realized: Optional[float] = None
    unrealized: Optional[float] = None
    vwap: Optional[float] = None


def _num(v, cast=float):
    try:
        return cast(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_securities(raw: List[Dict[str, Any]]) -> Dict[str, SecurityInfo]:
    out: Dict[str, SecurityInfo] = {}
    for s in raw or []:
        t = s.get("ticker")
        if not t:
            continue
        out[t] = SecurityInfo(
            ticker=t, position=int(round(float(s.get("position") or 0))),
            bid=_num(s.get("bid")), ask=_num(s.get("ask")), last=_num(s.get("last")),
            max_trade_size=_num(s.get("max_trade_size"), lambda x: int(float(x))),
            trading_fee=_num(s.get("trading_fee")), limit_order_rebate=_num(s.get("limit_order_rebate")),
            quoted_decimals=_num(s.get("quoted_decimals"), int),
            api_orders_per_second=_num(s.get("api_orders_per_second"), int),
            execution_delay_ms=_num(s.get("execution_delay_ms"), int),
            realized=_num(s.get("realized")), unrealized=_num(s.get("unrealized")), vwap=_num(s.get("vwap")))
    return out


def tick_size_from_decimals(decimals: Optional[int]) -> float:
    if decimals is None or decimals < 0 or decimals > 6:
        return 0.01
    return 10.0 ** (-decimals)


def round_price(price: float, tick: float, mode: str) -> float:
    """mode 'up' (ceil), 'down' (floor) or 'nearest' onto the tick grid."""
    n = price / tick
    if mode == "up":
        k = math.ceil(n - 1e-9)
    elif mode == "down":
        k = math.floor(n + 1e-9)
    else:
        k = round(n)
    decimals = max(0, int(round(-math.log10(tick)))) if tick < 1 else 0
    return round(k * tick, decimals + 2)


def on_grid(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6
