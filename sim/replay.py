"""Replay a logged live (or dry-run) heat: the books and tenders the bot actually saw are played back
tick by tick and the current strategy trades against them.

Fill model (deliberately simple and conservative):
  * aggressive orders fill against the recorded book minus what we already took ("depletion"),
    which refills at `refill` shares/tick per side;
  * resting orders fill only when the recorded book later trades through their price;
  * other traders' behaviour is whatever is in the recorded book.

    python -m sim.replay logs/20261120-101500-live [--set unwind.passive=false]
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional

from bot.config import load_config

from .backtest import HeatResult, run_heat
from .market import SimMarket, SimParams, SOrder, TenderSpec


def load_log(path: str):
    books: Dict[int, Dict[str, Dict]] = {}
    tenders: List[Dict] = []
    last_tick = 0
    with open(os.path.join(path, "ticks.jsonl")) as fh:
        for line in fh:
            rec = json.loads(line)
            t = rec.get("tick")
            if t is None:
                continue
            last_tick = max(last_tick, int(t))
            if "books" in rec:
                cur = books.setdefault(int(t), {})
                for tk, b in rec["books"].items():
                    cur.setdefault(tk, b)
    seen = set()
    ev_path = os.path.join(path, "events.jsonl")
    if os.path.exists(ev_path):
        with open(ev_path) as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("event") != "tender_new":
                    continue
                raw, parsed = rec.get("tender") or {}, rec.get("parsed") or {}
                tid = raw.get("tender_id")
                if tid in seen or not parsed.get("valid", True):
                    continue
                seen.add(tid)
                tenders.append({"tid": tid, "tick": int(raw.get("tick") or rec.get("tick") or 0),
                                "expires": int(raw.get("expires") or 0), "ticker": parsed.get("ticker"),
                                "action": parsed.get("our_action"), "qty": int(parsed.get("qty") or 0),
                                "price": float(parsed.get("price") or 0), "caption": raw.get("caption", "")})
    return books, tenders, last_tick


class ReplayMarket(SimMarket):
    def __init__(self, path: str, refill: Optional[Dict[str, float]] = None, ticks: Optional[int] = None) -> None:
        self.rec_books, self.rec_tenders, last = load_log(path)
        tickers = sorted({tk for b in self.rec_books.values() for tk in b}) or ["CRZY", "TAME"]
        params = SimParams(ticks=ticks or max(last, 1), competitors=0, n_tenders=0)
        params.tickers = {tk: params.tickers.get(tk) or list(params.tickers.values())[0] for tk in tickers}
        super().__init__(0, params, tenders=[])
        self.refill = refill or {"CRZY": 3000.0, "TAME": 1500.0}
        self.depletion = {tk: {"bid": 0.0, "ask": 0.0} for tk in self.tickers}
        self.current: Dict[str, Dict] = {}
        self._load_tick(0)

    # recorded book -> ANON orders (minus our depletion); crossing our resting orders fills them
    def _load_tick(self, tick: int) -> None:
        for t in sorted(self.rec_books):
            if t <= tick:
                self.current.update(self.rec_books[t])
        for tk in self.tickers:
            for o in list(self.bids[tk]) + list(self.asks[tk]):
                if o.trader == "ANON":
                    self._remove(o, "CANCELLED")
            b = self.current.get(tk)
            if not b:
                continue
            for side, key in (("BUY", "bids"), ("SELL", "asks")):
                skip = self.depletion[tk]["bid" if side == "BUY" else "ask"]
                for price, qty in b.get(key) or []:
                    take = min(qty, skip)
                    skip -= take
                    q = int(qty - take)
                    if q > 0:
                        self._match(SOrder(next(self.ids), "ANON", tk, side, "LIMIT", q, float(price), self.tick))
            bids, asks = b.get("bids") or [], b.get("asks") or []
            if bids and asks:
                self.F[tk] = (bids[0][0] + asks[0][0]) / 2
                self.last[tk] = self.F[tk]

    def step(self) -> None:
        with self.lock:
            if self.status != "ACTIVE":
                return
            self.tick += 1
            for tk in self.tickers:
                for side in ("bid", "ask"):
                    self.depletion[tk][side] = max(0.0, self.depletion[tk][side] - self.refill.get(tk, 2000.0))
            self._load_tick(self.tick)
            for tr in self.rec_tenders:
                if tr["tick"] == self.tick and tr["tid"] not in {s.tid for s in self.tender_specs}:
                    spec = TenderSpec(tr["tick"], tr["ticker"], tr["action"], tr["qty"], 0.0, tid=tr["tid"],
                                      price=tr["price"], expires=tr["expires"] or self.tick + 30, caption=tr["caption"])
                    if not spec.caption:
                        inst = "SELL" if spec.action == "BUY" else "BUY"
                        spec.caption = (f"An institution would like to {inst} {spec.qty:,} shares of {spec.ticker} "
                                        f"at a price of ${spec.price:.2f}. Would you like to {spec.action} the shares?")
                    self.tender_specs.append(spec)
                    self.accounts[self.bot].pending[spec.tid] = spec
                    self.tender_log.append({"tid": spec.tid, "tick": spec.tick, "ticker": spec.ticker,
                                            "action": spec.action, "qty": spec.qty, "price": spec.price,
                                            "discount": None, "outcome": "PENDING"})
            bot = self.accounts[self.bot]
            for tid, spec in list(bot.pending.items()):
                if self.tick >= spec.expires:
                    del bot.pending[tid]
                    self._tlog(tid, "LAPSED")
                    self._violate("lapsed", tender_id=tid)
            if self.tick >= self.p.ticks:
                self._end()

    def api_post_order(self, tid, ticker, type_, qty, action, price=None):
        with self.lock:
            before = sum(f["qty"] for f in self.bot_fills)
            out = super().api_post_order(tid, ticker, type_, qty, action, price)
            took = sum(f["qty"] for f in self.bot_fills) - before
            if took:
                self.depletion[ticker]["bid" if str(action).upper() == "SELL" else "ask"] += took
            return out


def replay(path: str, overrides: Optional[Dict] = None, refill: Optional[Dict[str, float]] = None) -> HeatResult:
    cfg = load_config(overrides=overrides)
    m = ReplayMarket(path, refill)
    return run_heat(cfg=cfg, market=m)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.replay")
    ap.add_argument("logdirs", nargs="+")
    ap.add_argument("--set", action="append", default=[])
    args = ap.parse_args(argv)
    ov = dict(item.split("=", 1) for item in args.set)
    for d in args.logdirs:
        r = replay(d, ov)
        print(f"{d}: P&L {r.pnl:,.0f}, accepted {r.accepted}, end {r.end_positions}, violations {len(r.violations)}")
        for t in r.tenders:
            print(f"   {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
