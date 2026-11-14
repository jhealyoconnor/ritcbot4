"""Per-tender P&L from the server's own transaction log (GET /transactions).

The log is the ground truth: every TENDER, ORDER fill and COMMISSION row with its cash flow.
Rows carry no order or tender id, so fills are matched to tenders first-in-first-out per stock:
an accepted tender opens a lot; later fills in that stock on the other side close the oldest open
lot. A tender on the opposite side of an open lot closes it (netting) and only the excess opens a
new lot. Commission rows follow the row they belong to. When every lot is closed, the lots' P&L
adds up exactly to the change in the cash balance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class TxLot:
    row_id: int
    tick: int
    ticker: str
    signed_qty: float              # + bought from the client (long), - sold to the client (short)
    price: float
    remaining: float
    cash: float = 0.0              # tender cash + cash from closing trades
    fees: float = 0.0              # commissions (positive number)
    closed_qty: float = 0.0
    close_cash: float = 0.0        # |cash| received/paid on closing trades
    netted_qty: float = 0.0
    first_close_tick: Optional[int] = None
    last_close_tick: Optional[int] = None
    tender_id: Optional[Any] = None

    @property
    def open(self) -> bool:
        return abs(self.remaining) > 1e-9

    @property
    def pnl(self) -> float:
        """Realised cash P&L (complete once the lot is closed)."""
        return self.cash - self.fees

    def pnl_marked(self, mark: Optional[float]) -> float:
        return self.pnl + (self.remaining * mark if mark is not None and self.open else 0.0)

    @property
    def avg_close(self) -> Optional[float]:
        return self.close_cash / self.closed_qty if self.closed_qty > 1e-9 else None

    @property
    def edge_per_share(self) -> Optional[float]:
        """Average unwind price vs the tender price, in the profitable direction, before fees."""
        a = self.avg_close
        if a is None:
            return None
        return (a - self.price) * _sign(self.signed_qty)


@dataclass
class TxReport:
    lots: List[TxLot] = field(default_factory=list)
    unattributed_qty: float = 0.0        # fills with no tender lot to close (should always be 0)
    unattributed_cash: float = 0.0
    other_cash: float = 0.0              # fines and anything that is not a tender/order/commission
    fines: float = 0.0
    rows: int = 0
    first_id: Optional[int] = None
    last_id: Optional[int] = None
    gaps: int = 0                        # missing row ids inside the range (0 = complete)
    last_balance: Optional[float] = None
    tender_fee_rows: int = 0             # commission rows that directly follow a TENDER row

    @property
    def complete(self) -> bool:
        return self.rows > 0 and self.gaps == 0 and self.first_id == 1

    @property
    def total_pnl(self) -> float:
        return sum(l.pnl for l in self.lots) + self.unattributed_cash + self.other_cash

    @property
    def total_fees(self) -> float:
        return sum(l.fees for l in self.lots)

    def flat(self) -> bool:
        return not any(l.open for l in self.lots)


def clean_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """De-duplicate by id and sort oldest first (the server returns newest first, with overlaps)."""
    by_id: Dict[int, Dict[str, Any]] = {}
    for r in rows or []:
        if r.get("id") is not None:
            by_id[int(r["id"])] = r
    return [by_id[i] for i in sorted(by_id)]


def analyse(rows: Iterable[Dict[str, Any]]) -> TxReport:
    rows = clean_rows(rows)
    rep = TxReport(rows=len(rows))
    if not rows:
        return rep
    rep.first_id, rep.last_id = int(rows[0]["id"]), int(rows[-1]["id"])
    rep.gaps = (rep.last_id - rep.first_id + 1) - len(rows)
    if rows[-1].get("balance") is not None:
        rep.last_balance = float(rows[-1]["balance"])
    open_lots: Dict[str, List[TxLot]] = {}
    last_alloc: List = []                 # [(lot or None, weight)] of the previous TENDER/ORDER row
    last_kind = ""
    for r in rows:
        kind = str(r.get("type") or "").upper()
        tk = r.get("ticker")
        qty = float(r.get("quantity") or 0.0)
        cash = float(r.get("cash_flow") or 0.0)
        tick = int(r.get("tick") or 0)
        if kind in ("TENDER", "ORDER"):
            lots = open_lots.setdefault(tk, [])
            left = abs(qty)
            alloc = []
            for lot in lots:
                if left <= 1e-9:
                    break
                if not lot.open or _sign(lot.remaining) == _sign(qty):
                    continue
                m = min(abs(lot.remaining), left)
                w = m / abs(qty)
                lot.remaining += _sign(qty) * m
                lot.cash += cash * w
                lot.closed_qty += m
                lot.close_cash += abs(cash) * w
                if kind == "TENDER":
                    lot.netted_qty += m
                lot.first_close_tick = tick if lot.first_close_tick is None else lot.first_close_tick
                lot.last_close_tick = tick
                alloc.append((lot, w))
                left -= m
            if left > 1e-9:
                w = left / abs(qty)
                if kind == "TENDER":
                    lot = TxLot(int(r["id"]), tick, tk, _sign(qty) * left, float(r.get("price") or 0.0),
                                _sign(qty) * left, cash=cash * w)
                    lots.append(lot)
                    rep.lots.append(lot)
                    alloc.append((lot, w))
                else:
                    rep.unattributed_qty += left
                    rep.unattributed_cash += cash * w
                    alloc.append((None, w))
            last_alloc, last_kind = alloc, kind
        elif kind == "COMMISSION":
            if last_kind == "TENDER":
                rep.tender_fee_rows += 1
            if not last_alloc:
                rep.other_cash += cash
            for lot, w in last_alloc:
                if lot is None:
                    rep.unattributed_cash += cash * w
                else:
                    lot.fees += -cash * w
            last_alloc, last_kind = [], kind
        else:
            rep.other_cash += cash
            if "FINE" in kind:
                rep.fines += -cash
            last_alloc, last_kind = [], kind
    return rep


def match_tenders(rep: TxReport, tenders: Iterable[Dict[str, Any]]) -> None:
    """Attach tender ids: tenders = [{'id', 'ticker', 'signed_qty', 'price'}] in acceptance order."""
    pool = list(tenders)
    for lot in rep.lots:
        full = lot.signed_qty
        for t in pool:
            same_px = abs(float(t["price"]) - lot.price) < 0.005
            if t["ticker"] == lot.ticker and same_px and _sign(t["signed_qty"]) == _sign(full) \
                    and abs(t["signed_qty"]) + 1e-6 >= abs(full):
                lot.tender_id = t["id"]
                pool.remove(t)
                break
