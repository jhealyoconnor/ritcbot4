"""Simulated RIT LT3 market: random-walk prices, ANON liquidity with dynamic order arrival,
competitor traders that get the same tenders, 5 tenders per heat, 2c fee, 25k/10k order caps.

It also acts as the compliance referee for the bot's account and records every violation:
  speculation      a bot fill that increases |position| or crosses zero
  order_pending    the bot submitted an order in a stock while a tender in it was pending
  fill_pending     a bot fill on the other side of a pending tender more than 1 tick after it appeared
  limit            net/gross limit breached
  lapsed           a tender expired without an explicit accept/decline
  end_position     non-zero position when the heat ends
All API methods return the same JSON shapes as the RIT DMA REST API.
"""
from __future__ import annotations

import copy
import itertools
import math
import random
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SimError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class TickerParams:
    start: float
    sigma: float                 # $ per tick
    max_trade: int
    half_spread_ticks: int = 2
    levels: int = 20
    level_qty: int = 3000        # mean ANON size per level
    refill_per_level: int = 600  # ANON shares added per level per tick
    flow_rate: float = 1.5       # ANON market orders per tick
    flow_min: int = 300
    flow_max: int = 3000
    perm_impact: float = 0.002   # $ permanent move per 10k shares of net aggressive volume
    # "sparse" model, calibrated to the live practice logs: limit orders arrive at random distances
    # from the fundamental, live for a while and are hit by random market flow. Gives wide, gappy
    # books (median spread ~10c, ~25k within 10c of the touch, ~150k over ~17 levels per side).
    model: str = "ladder"        # "ladder" (v1 tests) or "sparse" (live-like)
    arrivals: float = 2.4        # new limit orders per side per tick
    offset_mean: float = 0.22    # mean distance of a new order from the fundamental ($)
    near_frac: float = 0.0       # share of new orders placed close to the fundamental ...
    near_mean: float = 0.04      # ... at this mean distance
    size_median: int = 5500      # lognormal order size
    size_sigma: float = 1.0
    size_cap: int = 25000
    cancel_p: float = 0.07       # chance per tick that a resting order is cancelled
    mflow_rate: float = 3.5      # market orders per tick
    mflow_min: int = 500
    mflow_max: int = 6000
    burst_p: float = 0.0         # chance per tick of one large market order (other traders' blocks) ...
    burst_min: int = 8000        # ... which causes the short-lived price swings seen live
    burst_max: int = 25000


@dataclass
class SimParams:
    ticks: int = 300
    fee: float = 0.02
    tender_fee: bool = True
    net_limit: int = 100000
    gross_limit: int = 250000
    n_tenders: int = 5
    competitors: int = 3
    competitor_accept_prob: float = 0.7
    book_keys: str = "bids"      # "bids" (bids/asks) or "bid" (bid/ask, as in the swagger spec)
    tender_ticker_field: bool = True
    tender_action_perspective: str = "trader"
    teammates: int = 0           # other accounts (MATE1..) that receive the same tenders; driven by real engines
    competitor_min_edge: float = 0.03
    tender_mode: str = "default" # "default" (v1 tests) or "live" (5 windows, 65-75k, edge 0-45c off the book mid)
    tx_default_limit: int = 50   # the live server returns only the latest 20 rows unless 'limit' is passed
    tickers: Dict[str, TickerParams] = field(default_factory=lambda: {
        "CRZY": TickerParams(10.00, 0.035, 25000),
        "TAME": TickerParams(25.00, 0.020, 10000, level_qty=2000, refill_per_level=400, flow_rate=1.2,
                             flow_max=2000),
    })


def live_params(competitors: int = 2, teammates: int = 0, sigma: float = 0.055) -> "SimParams":
    """Market calibrated to the live practice heats (see README, 'Simulator calibration')."""
    p = SimParams(competitors=competitors, teammates=teammates, tender_fee=False, tender_mode="live",
                  tx_default_limit=20, competitor_accept_prob=0.75, competitor_min_edge=0.06)
    common = dict(model="sparse", near_frac=0.38, near_mean=0.03, offset_mean=0.28, arrivals=2.8, burst_p=0.20)
    p.tickers = {
        "CRZY": TickerParams(10.00, sigma, 25000, size_cap=25000, **common),
        "TAME": TickerParams(25.00, sigma, 10000, size_cap=20000, size_median=5200, **common),
    }
    return p


LIVE_TENDER_WINDOWS = [(24, 30), (77, 87), (137, 142), (201, 212), (260, 269)]


def live_tenders(rng: random.Random) -> List["TenderSpec"]:
    """Tender pattern seen live: one per window, 65-75k shares, 0-45c better than the mid."""
    out = []
    for lo, hi in LIVE_TENDER_WINDOWS:
        out.append(TenderSpec(rng.randint(lo, hi), rng.choice(["CRZY", "TAME"]), rng.choice(["BUY", "SELL"]),
                              rng.randrange(65000, 75001, 1000), round(rng.triangular(0.0, 0.45, 0.15), 3)))
    return out


@dataclass
class SOrder:
    oid: int
    trader: str
    ticker: str
    action: str
    type: str
    qty: int
    price: Optional[float]
    tick: int
    filled: int = 0
    cash: float = 0.0
    status: str = "OPEN"

    def as_json(self, period: int = 1) -> Dict[str, Any]:
        return {"order_id": self.oid, "period": period, "tick": self.tick, "trader_id": self.trader,
                "ticker": self.ticker, "type": self.type, "quantity": self.qty, "action": self.action,
                "price": self.price, "quantity_filled": self.filled,
                "vwap": (self.cash / self.filled) if self.filled else None, "status": self.status}


@dataclass
class TenderSpec:
    tick: int
    ticker: str
    action: str           # the TRADER's action if accepted
    qty: int
    discount: float       # $ better than mid for the trader
    tid: int = 0
    price: float = 0.0
    expires: int = 0
    caption: str = ""


@dataclass
class Account:
    tid: str
    positions: Dict[str, int]
    cash: float = 0.0
    fees: float = 0.0
    pending: Dict[int, TenderSpec] = field(default_factory=dict)
    tx: List[Dict] = field(default_factory=list)
    comp_state: Dict = field(default_factory=dict)


def round_tick(x: float, tick: float = 0.01) -> float:
    return round(round(x / tick) * tick, 2)


def default_tenders(rng: random.Random, ticks: int, n: int) -> List[TenderSpec]:
    slots = sorted(rng.sample(range(8, ticks - 35), n))
    out = []
    for s in slots:
        tk = "CRZY" if rng.random() < 0.6 else "TAME"
        action = rng.choice(["BUY", "SELL"])
        if tk == "CRZY":
            qty = rng.choice(range(20000, 100001, 5000))
            disc = rng.uniform(0.0, 0.03) if rng.random() < 0.25 else rng.uniform(0.05, 0.30)
        else:
            qty = rng.choice(range(10000, 80001, 5000))
            disc = rng.uniform(0.0, 0.03) if rng.random() < 0.25 else rng.uniform(0.04, 0.20)
        out.append(TenderSpec(s, tk, action, qty, disc))
    return out


class SimMarket:
    BOT = "BOT"

    def __init__(self, seed: int = 0, params: Optional[SimParams] = None,
                 tenders: Optional[List[TenderSpec]] = None, bot_id: str = "BOT",
                 initial_bot_positions: Optional[Dict[str, int]] = None) -> None:
        self.rng = random.Random(seed)
        self.p = params or SimParams()
        self.lock = threading.RLock()
        self.bot = bot_id
        self.tick = 0
        self.period = 1
        self.status = "ACTIVE"
        self.ids = itertools.count(1)
        self.tas_ids = itertools.count(1)
        self.tx_ids = itertools.count(1)
        self.tickers = list(self.p.tickers)
        self.F = {t: tp.start for t, tp in self.p.tickers.items()}
        self.last = dict(self.F)
        self.bids: Dict[str, List[SOrder]] = {t: [] for t in self.tickers}
        self.asks: Dict[str, List[SOrder]] = {t: [] for t in self.tickers}
        self.orders: Dict[int, SOrder] = {}
        self.tas: Dict[str, List[Dict]] = {t: [] for t in self.tickers}
        self.aggr_volume: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.accounts: Dict[str, Account] = {bot_id: Account(bot_id, {t: 0 for t in self.tickers})}
        if initial_bot_positions:
            self.accounts[bot_id].positions.update(initial_bot_positions)
        for i in range(self.p.competitors):
            cid = f"COMP{i + 1}"
            self.accounts[cid] = Account(cid, {t: 0 for t in self.tickers})
        for i in range(self.p.teammates):
            mid_ = f"MATE{i + 1}"
            self.accounts[mid_] = Account(mid_, {t: 0 for t in self.tickers})
        self.accounts["ANON"] = Account("ANON", {t: 0 for t in self.tickers})
        self.level_targets = {t: [max(200, int(self.rng.gauss(tp.level_qty, tp.level_qty * 0.4)))
                                  for _ in range(tp.levels)] for t, tp in self.p.tickers.items()}
        if tenders is not None:
            specs = tenders
        elif self.p.tender_mode == "live":
            specs = live_tenders(self.rng)
        else:
            specs = default_tenders(self.rng, self.p.ticks, self.p.n_tenders)
        self.tender_specs = sorted((copy.copy(s) for s in specs), key=lambda s: s.tick)
        self.tender_ids = itertools.count(101)
        self.violations: List[Dict] = []
        self.tender_log: List[Dict] = []        # what happened to each tender for the bot
        self.bot_fills: List[Dict] = []
        self.end_positions: Optional[Dict[str, int]] = None
        self.start_nlv = 0.0
        self.max_net = 0
        self.max_gross = 0
        for t in self.tickers:
            if self.p.tickers[t].model == "sparse":
                for _ in range(40):
                    self._sparse_step(t, flow=False)
            else:
                for _ in range(8):
                    self._anon_replenish(t)

    # ============================================================ dynamics
    def step(self) -> None:
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            for t in self.tickers:
                tp = self.p.tickers[t]
                imp = tp.perm_impact * self.aggr_volume[t] / 10000.0
                self.aggr_volume[t] = 0.0
                if tp.model == "sparse":
                    self.F[t] = max(0.5, self.F[t] + self.rng.gauss(0, tp.sigma))
                    self._sparse_step(t)
                    continue
                self.F[t] = max(0.5, self.F[t] + self.rng.gauss(0, tp.sigma) + imp)
                self._anon_pull(t)
                self._anon_replenish(t)
                self._anon_flow(t)
            self._competitors_act()
            self._tenders_arrive_and_expire()
            if self.tick >= self.p.ticks:
                self._end()

    def _sparse_step(self, t: str, flow: bool = True) -> None:
        """Live-like book: random cancels, orders that the fundamental has moved through are
        pulled, new limit orders arrive at random distances, then random market orders trade."""
        tp = self.p.tickers[t]
        f = self.F[t]
        for o in list(self.bids[t]):
            if o.trader == "ANON" and (o.price >= f or self.rng.random() < tp.cancel_p):
                self._remove(o, "CANCELLED")
        for o in list(self.asks[t]):
            if o.trader == "ANON" and (o.price <= f or self.rng.random() < tp.cancel_p):
                self._remove(o, "CANCELLED")
        mu = math.log(tp.size_median)
        for side in ("BUY", "SELL"):
            for _ in range(self._poisson(tp.arrivals)):
                near = tp.near_frac > 0 and self.rng.random() < tp.near_frac
                off = 0.01 + self.rng.expovariate(1.0 / (tp.near_mean if near else tp.offset_mean))
                price = round_tick(f - off if side == "BUY" else f + off)
                if price <= 0.01:
                    continue
                size = int(min(tp.size_cap, max(100, self.rng.lognormvariate(mu, tp.size_sigma))) // 100 * 100) or 100
                self._match(SOrder(next(self.ids), "ANON", t, side, "LIMIT", size, price, self.tick))
        if flow:
            for _ in range(self._poisson(tp.mflow_rate)):
                q = self.rng.randint(tp.mflow_min, tp.mflow_max)
                self._match(SOrder(next(self.ids), "ANON", t, self.rng.choice(["BUY", "SELL"]), "MARKET", q, None,
                                   self.tick))
            if tp.burst_p > 0 and self.rng.random() < tp.burst_p:
                q = self.rng.randint(tp.burst_min, tp.burst_max)
                self._match(SOrder(next(self.ids), "ANON", t, self.rng.choice(["BUY", "SELL"]), "MARKET", q, None,
                                   self.tick))

    def book_mid(self, t: str) -> float:
        b, a = self.bids[t], self.asks[t]
        if b and a:
            return (b[0].price + a[0].price) / 2.0
        return self.F[t]

    def _anon_pull(self, t: str) -> None:
        tp = self.p.tickers[t]
        tick = 0.01
        top_bid = self.F[t] - tp.half_spread_ticks * tick
        top_ask = self.F[t] + tp.half_spread_ticks * tick
        low = top_bid - tp.levels * tick
        high = top_ask + tp.levels * tick
        for o in list(self.bids[t]):
            if o.trader == "ANON" and (o.price > top_bid + 1e-9 or o.price < low - 1e-9):
                self._remove(o, "CANCELLED")
        for o in list(self.asks[t]):
            if o.trader == "ANON" and (o.price < top_ask - 1e-9 or o.price > high + 1e-9):
                self._remove(o, "CANCELLED")

    def _anon_replenish(self, t: str) -> None:
        tp = self.p.tickers[t]
        tick = 0.01
        top_bid = round_tick(math.floor(round((self.F[t] - tp.half_spread_ticks * tick) / tick, 6)) * tick)
        top_ask = round_tick(math.ceil(round((self.F[t] + tp.half_spread_ticks * tick) / tick, 6)) * tick)
        for k in range(tp.levels):
            target = self.level_targets[t][k]
            for side, price in (("BUY", round_tick(top_bid - k * tick)), ("SELL", round_tick(top_ask + k * tick))):
                book = self.bids[t] if side == "BUY" else self.asks[t]
                have = sum(o.qty - o.filled for o in book if o.trader == "ANON" and abs(o.price - price) < 1e-9)
                add = min(target - have, tp.refill_per_level)
                if add > 0 and price > 0:
                    self._rest(SOrder(next(self.ids), "ANON", t, side, "LIMIT", int(add), price, self.tick))

    def _anon_flow(self, t: str) -> None:
        tp = self.p.tickers[t]
        n = self._poisson(tp.flow_rate)
        for _ in range(n):
            q = self.rng.randint(tp.flow_min, tp.flow_max)
            side = self.rng.choice(["BUY", "SELL"])
            o = SOrder(next(self.ids), "ANON", t, side, "MARKET", q, None, self.tick)
            self._match(o)

    def _poisson(self, lam: float) -> int:
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L:
                return k
            k += 1

    # ======================================================= competitors
    def _competitors_act(self) -> None:
        for acc in self.accounts.values():
            if not acc.tid.startswith("COMP"):
                continue
            st = acc.comp_state
            for tid, spec in list(acc.pending.items()):
                d = st.setdefault(("delay", tid), self.rng.randint(1, 6))
                if self.tick - spec.tick >= d:
                    if self.rng.random() < self.p.competitor_accept_prob and spec.discount > self.p.competitor_min_edge:
                        self._apply_tender(acc, spec)
                    del acc.pending[tid]
            for t in self.tickers:
                pos = acc.positions[t]
                if pos != 0 and self.rng.random() < 0.7:
                    q = min(abs(pos), self.rng.randint(3000, 10000), self.p.tickers[t].max_trade)
                    o = SOrder(next(self.ids), acc.tid, t, "SELL" if pos > 0 else "BUY", "MARKET", q, None, self.tick)
                    self._match(o)
                    if self.tick >= self.p.ticks - 3 and acc.positions[t] != 0:
                        acc.positions[t] = 0     # competitors are flattened by fiat at the end

    # ============================================================ tenders
    def _tenders_arrive_and_expire(self) -> None:
        for spec in self.tender_specs:
            if spec.tick == self.tick and spec.tid == 0:
                spec.tid = next(self.tender_ids)
                mid = self.book_mid(spec.ticker) if self.p.tender_mode == "live" else self.F[spec.ticker]
                spec.price = round_tick(mid - spec.discount if spec.action == "BUY" else mid + spec.discount)
                spec.expires = self.tick + 30
                inst = "SELL" if spec.action == "BUY" else "BUY"
                prep = "to" if inst == "SELL" else "from"
                spec.caption = (f"An institution would like to {inst} {spec.qty:,} shares of {spec.ticker} {prep} you "
                                f"at a price of ${spec.price:.2f}. Would you like to {spec.action} the shares "
                                f"{'from' if spec.action == 'BUY' else 'to'} them?")
                for acc in self.accounts.values():
                    if acc.tid == "ANON":
                        continue
                    acc.pending[spec.tid] = spec
                self.tender_log.append({"tid": spec.tid, "tick": spec.tick, "ticker": spec.ticker,
                                        "action": spec.action, "qty": spec.qty, "price": spec.price,
                                        "discount": round(spec.discount, 4), "outcome": "PENDING"})
        bot = self.accounts[self.bot]
        for tid, spec in list(bot.pending.items()):
            if self.tick >= spec.expires:
                del bot.pending[tid]
                self._tlog(tid, "LAPSED")
                self._violate("lapsed", tender_id=tid)
        for acc in self.accounts.values():
            if acc.tid.startswith("MATE"):
                for tid, spec in list(acc.pending.items()):
                    if self.tick >= spec.expires:
                        del acc.pending[tid]

    def _tlog(self, tid: int, outcome: str) -> None:
        for r in self.tender_log:
            if r["tid"] == tid:
                r["outcome"] = outcome

    def _apply_tender(self, acc: Account, spec: TenderSpec) -> None:
        signed = spec.qty if spec.action == "BUY" else -spec.qty
        acc.positions[spec.ticker] += signed
        acc.cash -= signed * spec.price
        rec = {"id": next(self.tx_ids), "period": self.period, "tick": self.tick, "ticker": spec.ticker,
               "type": "TENDER", "price": spec.price, "quantity": signed, "currency": "CAD",
               "cash_flow": -signed * spec.price, "balance": round(acc.cash, 2)}
        acc.tx.append(rec)
        if self.p.tender_fee:
            fee = spec.qty * self.p.fee
            acc.cash -= fee
            acc.fees += fee
            acc.tx.append({"id": next(self.tx_ids), "period": self.period, "tick": self.tick, "ticker": spec.ticker,
                           "type": "COMMISSION", "price": self.p.fee, "quantity": spec.qty, "currency": "CAD",
                           "cash_flow": -fee, "balance": round(acc.cash, 2)})

    # ========================================================== matching
    def _rest(self, o: SOrder) -> None:
        self.orders[o.oid] = o
        if o.action == "BUY":
            book = self.bids[o.ticker]
            book.append(o)
            book.sort(key=lambda x: (-x.price, x.oid))
        else:
            book = self.asks[o.ticker]
            book.append(o)
            book.sort(key=lambda x: (x.price, x.oid))

    def _remove(self, o: SOrder, status: str) -> None:
        book = self.bids[o.ticker] if o.action == "BUY" else self.asks[o.ticker]
        if o in book:
            book.remove(o)
        o.status = status

    def _fill(self, o: SOrder, q: int, price: float) -> None:
        acc = self.accounts[o.trader]
        signed = q if o.action == "BUY" else -q
        if acc.tid == self.bot:
            pre = acc.positions[o.ticker]
            post = pre + signed
            if abs(post) > abs(pre) or (pre != 0 and post != 0 and (pre > 0) != (post > 0)):
                self._violate("speculation", ticker=o.ticker, pre=pre, post=post, order_id=o.oid)
            for spec in acc.pending.values():
                if spec.ticker == o.ticker:
                    other_side = "SELL" if spec.action == "BUY" else "BUY"
                    if o.action == other_side and self.tick > spec.tick + 1:
                        self._violate("fill_pending", ticker=o.ticker, tender_id=spec.tid, order_id=o.oid)
            self.bot_fills.append({"tick": self.tick, "ticker": o.ticker, "action": o.action, "qty": q,
                                   "price": price, "pre": pre, "post": post, "order_id": o.oid})
        acc.positions[o.ticker] += signed
        acc.cash -= signed * price
        if acc.tid != "ANON":
            fee = q * self.p.fee
            acc.cash -= fee
            acc.fees += fee
            acc.tx.append({"id": next(self.tx_ids), "period": self.period, "tick": self.tick, "ticker": o.ticker,
                           "type": "ORDER", "price": price, "quantity": signed, "currency": "CAD",
                           "cash_flow": -signed * price, "balance": round(acc.cash + fee, 2)})
            acc.tx.append({"id": next(self.tx_ids), "period": self.period, "tick": self.tick, "ticker": o.ticker,
                           "type": "COMMISSION", "price": self.p.fee, "quantity": q, "currency": "CAD",
                           "cash_flow": -fee, "balance": round(acc.cash, 2)})
        o.filled += q
        o.cash += q * price
        if acc.tid == self.bot:
            self._check_limits()

    def _match(self, o: SOrder) -> None:
        book = self.asks[o.ticker] if o.action == "BUY" else self.bids[o.ticker]
        while o.filled < o.qty and book:
            best = book[0]
            if o.type == "LIMIT":
                if o.action == "BUY" and best.price > o.price + 1e-9:
                    break
                if o.action == "SELL" and best.price < o.price - 1e-9:
                    break
            if best.trader == o.trader and o.trader != "ANON":   # no self-trade (ANON is many traders)
                self._remove(best, "CANCELLED")
                continue
            q = min(o.qty - o.filled, best.qty - best.filled)
            self._fill(o, q, best.price)
            self._fill(best, q, best.price)
            self.last[o.ticker] = best.price
            self.tas[o.ticker].append({"id": next(self.tas_ids), "period": self.period, "tick": self.tick,
                                       "price": best.price, "quantity": q})
            if o.trader != "ANON":
                self.aggr_volume[o.ticker] += q if o.action == "BUY" else -q
            if best.filled >= best.qty:
                self._remove(best, "TRANSACTED")
        if o.type == "MARKET" or o.filled >= o.qty:
            o.status = "TRANSACTED" if o.filled > 0 or o.type == "LIMIT" else "CANCELLED"
            self.orders[o.oid] = o
        else:
            self._rest(o)

    def _violate(self, kind: str, **data) -> None:
        self.violations.append({"kind": kind, "tick": self.tick, **data})

    def _check_limits(self) -> None:
        pos = self.accounts[self.bot].positions
        net = abs(sum(pos.values()))
        gross = sum(abs(v) for v in pos.values())
        self.max_net, self.max_gross = max(self.max_net, net), max(self.max_gross, gross)
        if net > self.p.net_limit or gross > self.p.gross_limit:
            self._violate("limit", net=net, gross=gross)

    def _end(self) -> None:
        self.status = "STOPPED"
        bot = self.accounts[self.bot]
        self.end_positions = dict(bot.positions)
        if any(v != 0 for v in bot.positions.values()):
            self._violate("end_position", positions=dict(bot.positions))
        for tid in list(bot.pending):
            self._tlog(tid, "LAPSED")
        for t in self.tickers:
            for o in list(self.bids[t]) + list(self.asks[t]):
                if o.trader != "ANON":
                    self._remove(o, "CANCELLED")

    # ============================================================== views
    def nlv(self, tid: str) -> float:
        acc = self.accounts[tid]
        return acc.cash + sum(acc.positions[t] * self.last[t] for t in self.tickers)

    def bot_pnl(self) -> float:
        return self.nlv(self.bot) - self.start_nlv

    # ================================================================ API
    def _acc(self, tid: str) -> Account:
        if tid not in self.accounts:
            raise SimError(401, "UNAUTHORIZED")
        return self.accounts[tid]

    def api_case(self) -> Dict:
        with self.lock:
            return {"name": "RITCx Liability Trading (sim)", "period": self.period, "tick": self.tick,
                    "ticks_per_period": self.p.ticks, "total_periods": 1, "status": self.status,
                    "is_enforce_trading_limits": False}

    def api_trader(self, tid: str) -> Dict:
        with self.lock:
            acc = self._acc(tid)
            return {"trader_id": tid, "first_name": "Sim", "last_name": tid, "nlv": round(self.nlv(tid), 2),
                    "total_fines": 0}

    def api_limits(self, tid: str) -> List[Dict]:
        with self.lock:
            pos = self._acc(tid).positions
            return [{"name": "LIMIT-STOCK", "gross": sum(abs(v) for v in pos.values()), "net": sum(pos.values()),
                     "gross_limit": self.p.gross_limit, "net_limit": self.p.net_limit, "gross_fine": 0, "net_fine": 0}]

    def api_securities(self, tid: str) -> List[Dict]:
        with self.lock:
            acc = self._acc(tid)
            out = []
            for t in self.tickers:
                b, a = self.bids[t], self.asks[t]
                out.append({"ticker": t, "type": "STOCK", "size": 1, "position": acc.positions[t],
                            "last": self.last[t], "bid": b[0].price if b else 0, "bid_size": (b[0].qty - b[0].filled) if b else 0,
                            "ask": a[0].price if a else 0, "ask_size": (a[0].qty - a[0].filled) if a else 0,
                            "volume": 0, "unrealized": 0, "realized": 0, "currency": "CAD",
                            "trading_fee": self.p.fee, "limit_order_rebate": 0, "min_trade_size": 0,
                            "max_trade_size": self.p.tickers[t].max_trade, "quoted_decimals": 2,
                            "api_orders_per_second": 20, "execution_delay_ms": 0, "is_tradeable": True,
                            "is_shortable": True})
            return out

    def api_book(self, ticker: str, limit: int = 20) -> Dict:
        with self.lock:
            if ticker not in self.tickers:
                raise SimError(400, f"unknown ticker {ticker}")
            bids = [o.as_json(self.period) for o in self.bids[ticker][: int(limit)]]
            asks = [o.as_json(self.period) for o in self.asks[ticker][: int(limit)]]
            if self.p.book_keys == "bid":
                return {"bid": bids, "ask": asks}
            return {"bids": bids, "asks": asks}

    def api_tas(self, ticker: str, after: Optional[int] = None, limit: Optional[int] = None) -> List[Dict]:
        with self.lock:
            return self._window(self.tas.get(ticker, []), after, limit)

    def api_orders(self, tid: str, status: str = "OPEN") -> List[Dict]:
        with self.lock:
            self._acc(tid)
            return [o.as_json(self.period) for o in self.orders.values() if o.trader == tid and o.status == status]

    def api_order(self, tid: str, oid: int) -> Dict:
        with self.lock:
            o = self.orders.get(int(oid))
            if o is None or o.trader != tid:
                raise SimError(404, "order not found")
            return o.as_json(self.period)

    def api_post_order(self, tid: str, ticker: str, type_: str, qty, action: str, price=None) -> Dict:
        with self.lock:
            acc = self._acc(tid)
            if self.status != "ACTIVE":
                raise SimError(400, "case is not active")
            if ticker not in self.tickers:
                raise SimError(400, "unknown ticker")
            try:
                q = int(float(qty))
            except (TypeError, ValueError):
                raise SimError(400, "bad quantity")
            if q <= 0 or q != float(qty):
                raise SimError(400, "bad quantity")
            if q > self.p.tickers[ticker].max_trade:
                raise SimError(400, f"Order quantity exceeds max trade size of {self.p.tickers[ticker].max_trade}")
            action = str(action).upper()
            type_ = str(type_).upper()
            if action not in ("BUY", "SELL") or type_ not in ("MARKET", "LIMIT"):
                raise SimError(400, "bad action/type")
            px = None
            if type_ == "LIMIT":
                if price is None:
                    raise SimError(400, "price required")
                px = round(float(price), 2)
            if tid == self.bot:
                for spec in acc.pending.values():
                    if spec.ticker == ticker:
                        other_side = "SELL" if spec.action == "BUY" else "BUY"
                        self._violate("order_pending", ticker=ticker, tender_id=spec.tid, action=action,
                                      other_side=(action == other_side))
            o = SOrder(next(self.ids), tid, ticker, action, type_, q, px, self.tick)
            self._match(o)
            return o.as_json(self.period)

    def api_cancel(self, tid: str, oid: int) -> Dict:
        with self.lock:
            o = self.orders.get(int(oid))
            if o is None or o.trader != tid:
                raise SimError(404, "order not found")
            if o.status == "OPEN":
                self._remove(o, "CANCELLED")
            return {"success": True}

    def api_cancel_bulk(self, tid: str, all_: Optional[int] = None, ticker: Optional[str] = None,
                        ids: Optional[str] = None) -> Dict:
        with self.lock:
            self._acc(tid)
            done = []
            if all_ is not None and int(all_) == 1:
                cands = [o for o in self.orders.values() if o.trader == tid and o.status == "OPEN"]
            elif ticker:
                cands = [o for o in self.orders.values() if o.trader == tid and o.status == "OPEN" and o.ticker == ticker]
            elif ids:
                want = {int(x) for x in str(ids).split(",") if x.strip()}
                cands = [o for o in self.orders.values() if o.trader == tid and o.status == "OPEN" and o.oid in want]
            else:
                raise SimError(400, "one of all/ticker/ids required")
            for o in cands:
                self._remove(o, "CANCELLED")
                done.append(o.oid)
            return {"cancelled_order_ids": done}

    def api_tenders(self, tid: str) -> List[Dict]:
        with self.lock:
            acc = self._acc(tid)
            out = []
            for spec in acc.pending.values():
                action = spec.action if self.p.tender_action_perspective == "trader" else (
                    "SELL" if spec.action == "BUY" else "BUY")
                row = {"tender_id": spec.tid, "period": self.period, "tick": spec.tick, "expires": spec.expires,
                       "caption": spec.caption, "quantity": spec.qty, "action": action, "is_fixed_bid": True,
                       "price": spec.price}
                if self.p.tender_ticker_field:
                    row["ticker"] = spec.ticker
                out.append(row)
            return out

    def api_accept(self, tid: str, tender_id: int, price) -> Dict:
        with self.lock:
            acc = self._acc(tid)
            spec = acc.pending.get(int(tender_id))
            if spec is None:
                raise SimError(404, "tender not found")
            if price is None or abs(float(price) - spec.price) > 1e-6:
                raise SimError(400, "price must match the fixed tender price")
            del acc.pending[spec.tid]
            self._apply_tender(acc, spec)
            if tid == self.bot:
                self._tlog(spec.tid, "ACCEPTED")
                self._check_limits()
            return {"success": True}

    def api_decline(self, tid: str, tender_id: int) -> Dict:
        with self.lock:
            acc = self._acc(tid)
            spec = acc.pending.pop(int(tender_id), None)
            if spec is None:
                raise SimError(404, "tender not found")
            if tid == self.bot:
                self._tlog(spec.tid, "DECLINED")
            return {"success": True}

    def _window(self, rows: List[Dict], after, limit) -> List[Dict]:
        """v1 behaviour: 'after' is exclusive and unlimited. Live behaviour (seen in the practice
        logs): rows with id >= after, only the latest `limit` (default 20), newest first."""
        if self.p.tender_mode != "live":
            if after is not None:
                return [r for r in rows if r["id"] > int(after)]
            return rows[-int(limit if limit is not None else self.p.tx_default_limit):]
        out = [r for r in rows if after is None or r["id"] >= int(after)]
        out = out[-int(limit if limit is not None else self.p.tx_default_limit):]
        return list(reversed(out))

    def api_transactions(self, tid: str, after: Optional[int] = None, limit: Optional[int] = None) -> List[Dict]:
        with self.lock:
            return self._window(self._acc(tid).tx, after, limit)
