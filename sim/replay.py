"""Replay a logged live heat: the books and tenders the bot actually saw are played back tick by
tick, in virtual time (so a slow loop is slow), and a bot version trades against them.

The recorded books already contain the impact of the trades the bot really made, so replaying
on them directly would charge that impact twice (v1's replay gave less than half the live P&L).
This version:
  * ADDS BACK the liquidity the live bot really took (its aggressive fills, at their prices),
    fading at `decay` per tick as the real book refilled;
  * REMOVES what the replayed bot takes, fading at the same rate;
  * fills resting orders when the recorded book crosses them, when the recorded touch moves
    through their price between two snapshots (the market traded through that level), and from a
    random flow of market orders (`flow_rate` per tick) when they sit at or inside the touch.
With the settings used live, main's code reproduces the live results (see `python -m sim.compare`).

    python -m sim.replay logs/20261120-101500-live [--set unwind.passive=false]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Any, Dict, List, Optional

from .market import SimMarket, SimParams, SOrder, TenderSpec, TickerParams
from .vtime import VTResult, load_variant, run_vt_heat


def _jsonl(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_log(path: str) -> Dict[str, Any]:
    books: Dict[int, Dict[str, Dict]] = {}
    for rec in _jsonl(os.path.join(path, "ticks.jsonl")):
        if rec.get("tick") is not None and "books" in rec:
            cur = books.setdefault(int(rec["tick"]), {})
            for tk, b in rec["books"].items():
                cur.setdefault(tk, b)
    tenders: List[Dict] = []
    seen = set()
    for rec in _jsonl(os.path.join(path, "events.jsonl")):
        if rec.get("event") != "tender_new":
            continue
        raw, parsed = rec.get("tender") or {}, rec.get("parsed") or {}
        tid = raw.get("tender_id")
        if tid in seen or not parsed.get("valid", True):
            continue
        seen.add(tid)
        tenders.append({"tid": tid, "tick": int(raw.get("tick") or rec.get("tick") or 0),
                        "seen_tick": int(rec.get("tick") or 0), "expires": int(raw.get("expires") or 0),
                        "ticker": parsed.get("ticker"), "action": parsed.get("our_action"),
                        "qty": int(parsed.get("qty") or 0), "price": float(parsed.get("price") or 0),
                        "caption": raw.get("caption", "")})
    real_fills: List[Dict] = []      # aggressive fills of the live bot: liquidity to add back
    opath = os.path.join(path, "orders.csv")
    if os.path.exists(opath):
        with open(opath, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    filled = float(r.get("filled") or 0)
                    if r.get("event") != "SENT" or filled <= 0 or not r.get("vwap"):
                        continue
                    real_fills.append({"tick": int(r["tick"]), "ticker": r["ticker"],
                                       "side": "bids" if r["action"] == "SELL" else "asks",
                                       "price": round(float(r["vwap"]), 2), "qty": filled})
                except (ValueError, KeyError):
                    continue
    ticks = sorted(books)
    return {"books": books, "tenders": tenders, "real_fills": real_fills,
            "first_tick": ticks[0] if ticks else 0, "last_tick": ticks[-1] if ticks else 0}


class ReplayMarket(SimMarket):
    def __init__(self, path: str, decay: float = 0.85, flow_rate: float = 5.0, flow_min: int = 500,
                 flow_max: int = 6000, touch_share: float = 0.5, through_frac: float = 1.0, seed: int = 0,
                 ticks: int = 300) -> None:
        self.log = load_log(path)
        books = self.log["books"]
        tickers = sorted({tk for b in books.values() for tk in b}) or ["CRZY", "TAME"]
        caps = {"CRZY": 25000, "TAME": 10000}
        params = SimParams(ticks=ticks, competitors=0, n_tenders=0, tender_fee=False, tender_mode="live",
                           tx_default_limit=20)
        params.tickers = {tk: TickerParams(10.0, 0.0, caps.get(tk, 10000), model="replay") for tk in tickers}
        super().__init__(seed, params, tenders=[])
        self.decay, self.flow_rate, self.flow_min, self.flow_max = decay, flow_rate, flow_min, flow_max
        self.touch_share = touch_share
        self.through_frac = through_frac
        self.prev_touch: Dict[str, tuple] = {}
        self.book_ticks = sorted(books)
        self.taken: List[Dict] = []          # what the replayed bot took: [{tick, ticker, side, qty}]
        self.current: Dict[str, Dict] = {}
        self.current_tick: Dict[str, int] = {}
        self.tick = max(0, self.log["first_tick"] - 1)
        self._bi = 0
        self.step()                          # show the first recorded book before the bot connects

    # ------------------------------------------------------------- the book
    def _compose(self, tk: str) -> None:
        for o in list(self.bids[tk]) + list(self.asks[tk]):
            if o.trader == "ANON":
                self._remove(o, "CANCELLED")
        rec = self.current.get(tk)
        if not rec:
            return
        snap_tick = self.current_tick.get(tk, self.tick)
        sides = {"bids": {}, "asks": {}}
        for side in ("bids", "asks"):
            for price, qty in rec.get(side) or []:
                sides[side][round(float(price), 2)] = sides[side].get(round(float(price), 2), 0.0) + float(qty)
        best_bid = max(sides["bids"]) if sides["bids"] else None
        best_ask = min(sides["asks"]) if sides["asks"] else None
        for f in self.log["real_fills"]:
            if f["ticker"] != tk or f["tick"] >= snap_tick:      # snapshot predates the fill: nothing to restore
                continue
            w = self.decay ** (self.tick - f["tick"])
            if w < 0.03:
                continue
            price = f["price"]
            if f["side"] == "bids" and best_ask is not None and price >= best_ask:
                price = round(best_ask - 0.01, 2)
            if f["side"] == "asks" and best_bid is not None and price <= best_bid:
                price = round(best_bid + 0.01, 2)
            sides[f["side"]][price] = sides[f["side"]].get(price, 0.0) + f["qty"] * w
        for side, desc in (("bids", True), ("asks", False)):
            out = sum(t["qty"] * self.decay ** (self.tick - t["tick"]) for t in self.taken
                      if t["ticker"] == tk and t["side"] == side and t["tick"] < self.tick)
            levels = sorted(sides[side].items(), key=lambda kv: -kv[0] if desc else kv[0])
            action = "BUY" if side == "bids" else "SELL"
            for price, qty in levels:
                cut = min(qty, out)
                out -= cut
                q = int(qty - cut)
                if q > 0:
                    self._match(SOrder(next(self.ids), "ANON", tk, action, "LIMIT", q, price, self.tick))
        # trade-through: the recorded touch moved past a resting order of ours since the last tick,
        # so the market traded at that level (live, such orders did fill)
        prev = self.prev_touch.get(tk)
        if prev is not None and self.through_frac > 0:
            pb, pa = prev
            if best_ask is not None and pa is not None:
                for o in list(self.asks[tk]):
                    if o.trader != "ANON" and pa <= o.price + 1e-9 < best_ask:
                        self._passive_fill(o, int((o.qty - o.filled) * self.through_frac))
            if best_bid is not None and pb is not None:
                for o in list(self.bids[tk]):
                    if o.trader != "ANON" and pb >= o.price - 1e-9 > best_bid:
                        self._passive_fill(o, int((o.qty - o.filled) * self.through_frac))
        self.prev_touch[tk] = (best_bid, best_ask)
        b, a = self.bids[tk], self.asks[tk]
        if b and a:
            self.F[tk] = (b[0].price + a[0].price) / 2.0
            self.last[tk] = self.F[tk]

    def _passive_fill(self, o: SOrder, q: int) -> None:
        if q <= 0:
            return
        self._fill(o, q, o.price)
        self.tas[o.ticker].append({"id": next(self.tas_ids), "period": self.period, "tick": self.tick,
                                   "price": o.price, "quantity": q})
        if o.filled >= o.qty:
            self._remove(o, "TRANSACTED")

    def _flow(self, tk: str) -> None:
        """Random market orders that reach the bot's resting orders at or inside the recorded touch."""
        for _ in range(self._poisson(self.flow_rate)):
            size = self.rng.randint(self.flow_min, self.flow_max)
            hit = self.rng.choice(["bids", "asks"])
            book = self.bids[tk] if hit == "bids" else self.asks[tk]
            anon_best = next((o.price for o in book if o.trader == "ANON"), None)
            for o in list(book):
                if size <= 0 or o.trader == "ANON":
                    break
                share = 1.0
                if anon_best is not None and abs(o.price - anon_best) < 1e-9:
                    share = self.touch_share
                q = int(min(size * share, o.qty - o.filled))
                if q <= 0:
                    continue
                self._passive_fill(o, q)
                size -= q

    # ---------------------------------------------------------------- ticks
    def step(self) -> None:
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            while self._bi < len(self.book_ticks) and self.book_ticks[self._bi] <= self.tick:
                bt = self.book_ticks[self._bi]
                for tk, b in self.log["books"][bt].items():
                    self.current[tk] = b
                    self.current_tick[tk] = bt
                self._bi += 1
            for tk in self.tickers:
                self._compose(tk)
                self._flow(tk)
            bot = self.accounts[self.bot]
            for tr in self.log["tenders"]:
                due = tr["tick"] == self.tick or (tr["tick"] < self.tick and self.tick == self.log["first_tick"]
                                                  and tr["expires"] > self.tick)
                if not due or any(s.tid == tr["tid"] for s in self.tender_specs):
                    continue
                spec = TenderSpec(tr["tick"], tr["ticker"], tr["action"], tr["qty"], 0.0, tid=tr["tid"],
                                  price=tr["price"], expires=tr["expires"] or self.tick + 30, caption=tr["caption"])
                self.tender_specs.append(spec)
                bot.pending[spec.tid] = spec
                self.tender_log.append({"tid": spec.tid, "tick": spec.tick, "ticker": spec.ticker,
                                        "action": spec.action, "qty": spec.qty, "price": spec.price,
                                        "discount": None, "outcome": "PENDING"})
            for tid, spec in list(bot.pending.items()):
                if self.tick >= spec.expires:
                    del bot.pending[tid]
                    self._tlog(tid, "LAPSED")
                    self._violate("lapsed", tender_id=tid)
            if self.tick >= self.p.ticks:
                self._end()

    def api_post_order(self, tid, ticker, type_, qty, action, price=None):
        with self.lock:
            out = super().api_post_order(tid, ticker, type_, qty, action, price)
            took = float(out.get("quantity_filled") or 0)
            if took > 0 and tid == self.bot:
                self.taken.append({"tick": self.tick, "ticker": ticker,
                                   "side": "bids" if str(action).upper() == "SELL" else "asks", "qty": took})
            return out


def replay(path: str, variant=None, overrides: Optional[Dict] = None, latency: float = 0.24, spacing: float = 0.2,
           **market_kw) -> VTResult:
    variant = variant or load_variant("v2", None, overrides)
    return run_vt_heat(ReplayMarket(path, **market_kw), variant, latency=latency, spacing=spacing, seed=path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.replay")
    ap.add_argument("logdirs", nargs="+")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--bot", help="path to another checkout whose bot/ should be replayed (default: this one)")
    args = ap.parse_args(argv)
    ov = dict(item.split("=", 1) for item in args.set)
    var = load_variant("bot", args.bot, ov)
    total = 0.0
    for d in args.logdirs:
        r = replay(d, var)
        total += r.pnl
        print(f"{os.path.basename(os.path.normpath(d))}: P&L {r.pnl:,.0f}, accepted {r.accepted}, "
              f"end {r.end_positions}, violations {len(r.violations)}{' ERROR ' + r.me.error if r.me.error else ''}")
        for t in r.tenders:
            print(f"   #{t['tid']} {t['ticker']} {t['action']} {t['qty']:,} @ {t['price']}: {t['outcome']}")
    print(f"total {total:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
