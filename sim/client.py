"""In-process client with the same interface as bot.api.RitClient, backed by a SimMarket."""
from __future__ import annotations

from typing import Optional

from bot.api import ApiError, AuthError, RequestStats, Result, Sequencer

from .market import SimError, SimMarket


class SimClient:
    def __init__(self, market: SimMarket, trader_id: Optional[str] = None) -> None:
        self.m = market
        self.tid = trader_id or market.bot
        self.seq = Sequencer()
        self.stats = RequestStats()

    def _call(self, fn, *args, **kw) -> Result:
        sent = self.seq.next()
        try:
            data = fn(*args, **kw)
        except SimError as exc:
            self.stats.record(0.0, False)
            if exc.status == 401:
                raise AuthError(exc.message)
            raise ApiError(exc.status, exc.message)
        self.stats.record(0.0, True)
        return Result(data, sent, self.seq.next(), 0.0)

    def case(self):
        return self._call(self.m.api_case)

    def trader(self):
        return self._call(self.m.api_trader, self.tid)

    def limits(self):
        return self._call(self.m.api_limits, self.tid)

    def securities(self):
        return self._call(self.m.api_securities, self.tid)

    def book(self, ticker, limit=30):
        return self._call(self.m.api_book, ticker, limit)

    def tas(self, ticker, after=None):
        return self._call(self.m.api_tas, ticker, after)

    def orders(self, status="OPEN"):
        return self._call(self.m.api_orders, self.tid, status)

    def order(self, oid):
        return self._call(self.m.api_order, self.tid, oid)

    def post_order(self, ticker, type_, quantity, action, price=None):
        return self._call(self.m.api_post_order, self.tid, ticker, type_, quantity, action, price)

    def cancel_order(self, oid):
        return self._call(self.m.api_cancel, self.tid, oid)

    def cancel_ticker(self, ticker):
        return self._call(self.m.api_cancel_bulk, self.tid, ticker=ticker)

    def cancel_all(self):
        return self._call(self.m.api_cancel_bulk, self.tid, all_=1)

    def cancel_ids(self, ids):
        return self._call(self.m.api_cancel_bulk, self.tid, ids=",".join(str(i) for i in ids))

    def tenders(self):
        return self._call(self.m.api_tenders, self.tid)

    def accept_tender(self, tender_id, price):
        return self._call(self.m.api_accept, self.tid, tender_id, price)

    def decline_tender(self, tender_id):
        return self._call(self.m.api_decline, self.tid, tender_id)

    def transactions(self, after=None):
        return self._call(self.m.api_transactions, self.tid, after)
