"""In-process client with the same interface as bot.api.RitClient, backed by a SimMarket.

Two modes:
  * instant (sched=None): calls cost no time. Used by the fast unit/scenario tests.
  * virtual time (sched given): every request is SENT no sooner than `spacing` seconds after the
    previous one (the server's rate limit) and RETURNS `latency` seconds later. Requests issued
    through batch() are sent in parallel (staggered by `spacing`), so a batch of N costs
    (N-1)*spacing + latency; requests issued one by one cost `latency` each. The market advances
    tick by tick as virtual time passes, so a slow loop really is slow in the simulation.

`api` is the bot.api module of the engine under test, so the same client can drive the engine on
this branch or an older one loaded from another checkout (they have different exception classes).
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from .market import SimError, SimMarket


class SimClient:
    def __init__(self, market: SimMarket, trader_id: Optional[str] = None, api: Any = None,
                 sched: Any = None, latency: float = 0.0, spacing: float = 0.0) -> None:
        if api is None:
            import bot.api as api  # noqa: PLC0415
        self.api = api
        self.m = market
        self.tid = trader_id or market.bot
        self.seq = api.Sequencer()
        self.stats = api.RequestStats(self.now)
        self.sched = sched
        self.latency = latency
        self.spacing = spacing
        self.t = 0.0                      # this actor's virtual time
        self.last_send = -1e9
        self._batch_t0: Optional[float] = None
        self._batch_end = 0.0
        self._batch_results: List[Any] = []
        self.requests = 0

    def now(self) -> float:
        return self.t

    # ------------------------------------------------------------------ core
    def _call(self, fn: Callable, *args, **kw):
        self.requests += 1
        sent = self.seq.next()
        if self.sched is None:
            t_sent = t_recv = self.t
            thunk = lambda: fn(*args, **kw)  # noqa: E731
            run = thunk
        else:
            base = self._batch_t0 if self._batch_t0 is not None else self.t
            t_sent = max(base, self.last_send + self.spacing)
            t_recv = t_sent + self.latency
            self.last_send = t_sent
            run = lambda: self.sched.serve(self.tid, t_sent + self.latency / 2.0, lambda: fn(*args, **kw))  # noqa: E731
        try:
            data = run()
        except SimError as exc:
            self._after(t_recv)
            self.stats.record(self.latency, False)
            if exc.status == 401:
                raise self.api.AuthError(exc.message)
            raise self.api.ApiError(exc.status, exc.message)
        self._after(t_recv)
        self.stats.record(self.latency, True)
        res = self.api.Result(data, sent, 0, self.latency)
        res.t_sent, res.t_recv = t_sent, t_recv
        if self._batch_t0 is not None:
            self._batch_results.append(res)       # recv numbers are assigned when the batch completes
        else:
            res.recv = self.seq.next()
        return res

    def _after(self, t_recv: float) -> None:
        if self.sched is None:
            return
        if self._batch_t0 is not None:
            self._batch_end = max(self._batch_end, t_recv)
        else:
            self.t = max(self.t, t_recv)

    def batch(self, calls: List[Tuple[str, Callable[[], Any]]]) -> dict:
        """Run calls 'in parallel': all are sent before any response is processed."""
        out = {}
        self._batch_t0, self._batch_end, self._batch_results = self.t, self.t, []
        try:
            for name, fn in calls:
                try:
                    out[name] = fn()
                except self.api.AuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    out[name] = exc
        finally:
            for res in self._batch_results:
                res.recv = self.seq.next()
            self.t = max(self.t, self._batch_end)
            self._batch_t0, self._batch_results = None, []
        return out

    # ------------------------------------------------------------- endpoints
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

    def tas(self, ticker, after=None, limit=None):
        return self._call(self.m.api_tas, ticker, after, limit)

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

    def transactions(self, after=None, limit=None):
        return self._call(self.m.api_transactions, self.tid, after, limit)
