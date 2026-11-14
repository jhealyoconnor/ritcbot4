"""Order tracking with conservative ("pessimistic") accounting.

Rule (proved in tests): an order counts as possibly-unreflected in a position snapshot by
    qty - filled_seen
where filled_seen comes from the latest observation of that order RECEIVED BEFORE the position
request was SENT. An order seen terminal (TRANSACTED/CANCELLED/vanished) before the snapshot was
requested counts 0. Anything not yet seen counts in full. This can over-count (briefly blocking a
valid order) but can never under-count, so the bot can never sell more than it holds.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TERMINAL = {"TRANSACTED", "CANCELLED", "VANISHED", "REJECTED"}


@dataclass
class Obs:
    recv: int
    filled: float
    status: str


@dataclass
class TrackedOrder:
    local_id: int
    ticker: str
    action: str
    qty: int
    type: str
    price: Optional[float]
    purpose: str
    tender_ids: Tuple[str, ...]
    created_tick: int
    sent: int
    order_id: Optional[int] = None
    uncertain: bool = False
    post_done: Optional[int] = None        # seq when the POST attempt finished (success or not)
    obs: List[Obs] = field(default_factory=list)
    status: str = "PENDING"
    filled: float = 0.0
    vwap: Optional[float] = None
    final_known: bool = False              # final fill quantity is known
    cancel_requested_tick: Optional[int] = None
    attributed_qty: float = 0.0            # fills already pushed to the ledger
    attributed_cash: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled)

    def unreflected(self, pos_sent: int) -> float:
        if self.status == "REJECTED":
            return 0.0
        best: Optional[Obs] = None
        for o in self.obs:
            if o.recv < pos_sent:
                best = o
        if best is None:
            return float(self.qty)
        if best.status in TERMINAL:
            return 0.0
        return max(0.0, self.qty - best.filled)


class OrderTracker:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.orders: Dict[int, TrackedOrder] = {}
        self.by_oid: Dict[int, int] = {}
        self.foreign: List[Dict[str, Any]] = []      # open orders on the server we did not send
        self.list_sent = 0
        self.list_recv = 0

    # -------------------------------------------------------------- sending
    def new(self, ticker: str, action: str, qty: int, type_: str, price: Optional[float], purpose: str,
            tender_ids, tick: int, sent: int) -> TrackedOrder:
        to = TrackedOrder(next(self._ids), ticker, action, int(qty), type_, price, purpose,
                          tuple(tender_ids or ()), tick, sent)
        self.orders[to.local_id] = to
        return to

    def on_post_ok(self, to: TrackedOrder, data: Dict[str, Any], recv: int) -> List[Tuple[TrackedOrder, float, float]]:
        to.post_done = recv
        oid = data.get("order_id") if isinstance(data, dict) else None
        if oid is not None:
            to.order_id = int(oid)
            self.by_oid[to.order_id] = to.local_id
        return self._observe(to, data or {}, recv)

    def on_post_rejected(self, to: TrackedOrder, recv: int) -> None:
        to.post_done = recv
        to.status = "REJECTED"
        to.final_known = True
        to.obs.append(Obs(recv, 0.0, "REJECTED"))

    def on_post_uncertain(self, to: TrackedOrder, recv: int) -> None:
        to.post_done = recv
        to.uncertain = True

    # ---------------------------------------------------------- observation
    def _observe(self, to: TrackedOrder, data: Dict[str, Any], recv: int) -> List[Tuple[TrackedOrder, float, float]]:
        status = str(data.get("status") or "OPEN").upper()
        filled = float(data.get("quantity_filled") or 0.0)
        vwap = data.get("vwap")
        if filled < to.filled:            # never go backwards
            filled = to.filled
        to.filled = filled
        if vwap is not None:
            to.vwap = float(vwap)
        if status in ("TRANSACTED", "CANCELLED"):
            to.status = status
            to.final_known = True
        elif not to.terminal:
            to.status = "OPEN"
        to.obs.append(Obs(recv, filled, to.status))
        return self._fill_delta(to)

    def _fill_delta(self, to: TrackedOrder) -> List[Tuple[TrackedOrder, float, float]]:
        """Returns [(order, delta_qty, delta_cash)] for fills not yet pushed to the ledger."""
        dq = to.filled - to.attributed_qty
        if dq <= 1e-9:
            return []
        px = to.vwap if to.vwap is not None else (to.price or 0.0)
        total_cash = to.filled * px
        dc = total_cash - to.attributed_cash
        to.attributed_qty = to.filled
        to.attributed_cash = total_cash
        return [(to, dq, dc)]

    def on_order(self, data: Dict[str, Any], recv: int) -> List[Tuple[TrackedOrder, float, float]]:
        """GET /orders/{id} response."""
        oid = data.get("order_id")
        if oid is None or int(oid) not in self.by_oid:
            return []
        return self._observe(self.orders[self.by_oid[int(oid)]], data, recv)

    def on_cancelled(self, order_ids, recv: int) -> None:
        for oid in order_ids or []:
            lid = self.by_oid.get(int(oid))
            if lid is None:
                continue
            to = self.orders[lid]
            if not to.terminal:
                to.status = "CANCELLED"
                to.obs.append(Obs(recv, to.filled, "CANCELLED"))
                # final fill may still be higher than we know -> fetch later
                to.final_known = False

    def on_open_list(self, orders: List[Dict[str, Any]], sent: int, recv: int) -> List[Tuple[TrackedOrder, float, float]]:
        fills: List[Tuple[TrackedOrder, float, float]] = []
        seen = set()
        foreign: List[Dict[str, Any]] = []
        for o in orders or []:
            oid = o.get("order_id")
            if oid is None:
                continue
            oid = int(oid)
            if oid in self.by_oid:
                seen.add(oid)
                fills += self._observe(self.orders[self.by_oid[oid]], o, recv)
                continue
            adopted = self._adopt(o, sent)
            if adopted is not None:
                seen.add(oid)
                fills += self._observe(adopted, o, recv)
            else:
                foreign.append(o)
        # our orders that should have been listed but were not are terminal (filled or cancelled)
        for to in self.orders.values():
            if to.terminal:
                continue
            if to.order_id is not None and to.order_id not in seen and to.post_done is not None and to.post_done < sent:
                to.status = "VANISHED"
                to.obs.append(Obs(recv, to.filled, "VANISHED"))
            elif to.order_id is None and to.uncertain and to.post_done is not None and to.post_done < sent:
                # uncertain send that is not resting: it either never existed or already executed.
                # Either way nothing more can fill; positions read after this reflect it.
                to.status = "VANISHED"
                to.final_known = True
                to.obs.append(Obs(recv, to.filled, "VANISHED"))
        self.foreign = foreign
        self.list_sent, self.list_recv = sent, recv
        return fills

    def _adopt(self, o: Dict[str, Any], list_sent: int) -> Optional[TrackedOrder]:
        for to in self.orders.values():
            if (to.order_id is None and to.uncertain and not to.terminal and to.post_done is not None
                    and to.post_done < list_sent and to.ticker == o.get("ticker")
                    and to.action == str(o.get("action")).upper() and int(to.qty) == int(float(o.get("quantity", 0)))):
                to.order_id = int(o["order_id"])
                self.by_oid[to.order_id] = to.local_id
                to.uncertain = False
                return to
        return None

    # ------------------------------------------------------------- queries
    def committed(self, ticker: str, action: str, pos_sent: int) -> float:
        total = 0.0
        for to in self.orders.values():
            if to.ticker == ticker and to.action == action:
                total += to.unreflected(pos_sent)
        for o in self.foreign:
            if o.get("ticker") == ticker and str(o.get("action")).upper() == action:
                total += max(0.0, float(o.get("quantity", 0)) - float(o.get("quantity_filled", 0) or 0))
        return total

    def live(self, ticker: Optional[str] = None) -> List[TrackedOrder]:
        return [to for to in self.orders.values() if not to.terminal and (ticker is None or to.ticker == ticker)]

    def foreign_in(self, ticker: str) -> List[Dict[str, Any]]:
        return [o for o in self.foreign if o.get("ticker") == ticker]

    def has_live(self, ticker: str) -> bool:
        return bool(self.live(ticker)) or bool(self.foreign_in(ticker))

    def needs_final_fill(self) -> List[TrackedOrder]:
        return [to for to in self.orders.values()
                if to.terminal and not to.final_known and to.order_id is not None and to.status != "REJECTED"]
