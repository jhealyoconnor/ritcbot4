"""Ledger of inventory that came from accepted tenders: the ONLY inventory the bot may trade.

Each accepted tender is a lot. Unwind fills are attributed to open lots first-in-first-out.
A tender opposite to existing inventory nets against it (the old lot is closed at the new
tender's price). Positions found at startup (or that appear unexplained) become RECOVERED lots so
they are unwound rather than left open at the end of the heat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class Lot:
    lot_id: str
    ticker: str
    signed_qty: int
    price: float
    tick: int
    kind: str                     # TENDER or RECOVERED
    remaining: int
    tender_fee: float = 0.0
    fill_qty: float = 0.0         # shares of this lot closed (by unwind fills or netting)
    fill_cash: float = 0.0        # signed cash from those closes (+ for sells)
    fill_fees: float = 0.0
    netted_qty: float = 0.0
    expected_ev: Optional[float] = None
    unwind_start_tick: int = 0

    @property
    def open(self) -> bool:
        return self.remaining != 0

    def tender_cash(self) -> float:
        return -self.signed_qty * self.price - self.tender_fee

    def pnl(self, mark: Optional[float]) -> float:
        total = self.tender_cash() + self.fill_cash - self.fill_fees
        if self.remaining and mark is not None:
            total += self.remaining * mark
        return total

    def avg_close_price(self) -> Optional[float]:
        return abs(self.fill_cash) / self.fill_qty if self.fill_qty else None


class Ledger:
    def __init__(self, fee_per_share: float, fee_on_tender: bool) -> None:
        self.fee = fee_per_share
        self.fee_on_tender = fee_on_tender
        self.lots: List[Lot] = []
        self.unattributed: List[Dict] = []
        self._mismatch_loops: Dict[str, int] = {}

    # ----------------------------------------------------------- tenders
    def accept_tender(self, tender_id, ticker: str, signed_qty: int, price: float, tick: int,
                      expected_ev: Optional[float] = None) -> Lot:
        fee_t = abs(signed_qty) * self.fee if self.fee_on_tender else 0.0
        lot = Lot(str(tender_id), ticker, int(signed_qty), float(price), tick, "TENDER", int(signed_qty),
                  tender_fee=fee_t, expected_ev=expected_ev, unwind_start_tick=tick)
        # net against opposite open lots, oldest first
        for old in self.open_lots(ticker):
            if lot.remaining == 0:
                break
            if _sign(old.remaining) == _sign(lot.remaining):
                continue
            m = min(abs(old.remaining), abs(lot.remaining))
            s_old = _sign(old.remaining)
            old.remaining -= s_old * m
            old.fill_qty += m
            old.fill_cash += s_old * m * price          # old long closed by selling to the client at `price`
            old.netted_qty += m
            lot.remaining += s_old * m
            lot.fill_qty += m
            lot.fill_cash -= s_old * m * price
            lot.netted_qty += m
        for other in self.open_lots(ticker):
            other.unwind_start_tick = tick
        self.lots.append(lot)
        return lot

    def recover(self, ticker: str, signed_qty: int, mark: float, tick: int, reason: str = "RECOVERED") -> Lot:
        lot = Lot(f"{reason}-{ticker}-{tick}", ticker, int(signed_qty), float(mark), tick, "RECOVERED",
                  int(signed_qty), unwind_start_tick=tick)
        self.lots.append(lot)
        return lot

    # ------------------------------------------------------------- fills
    def on_fill(self, ticker: str, signed_qty: float, cash: float, fee: float) -> None:
        """signed_qty: + for a buy fill, - for a sell fill; cash: price*qty (positive)."""
        remaining = abs(signed_qty)
        if remaining <= 0:
            return
        px = cash / remaining
        for lot in self.open_lots(ticker):
            if remaining <= 1e-9:
                break
            if _sign(lot.remaining) != -_sign(signed_qty):
                continue
            m = min(abs(lot.remaining), remaining)
            lot.remaining -= _sign(lot.remaining) * int(round(m))
            lot.fill_qty += m
            lot.fill_cash += (-_sign(signed_qty)) * m * px
            lot.fill_fees += fee * (m / abs(signed_qty))
            remaining -= m
        if remaining > 1e-9:
            self.unattributed.append({"ticker": ticker, "qty": _sign(signed_qty) * remaining, "price": px})

    # --------------------------------------------------------- reconcile
    def expected_position(self, ticker: str) -> int:
        return sum(l.remaining for l in self.lots if l.ticker == ticker)

    def reconcile(self, ticker: str, server_pos: int, unknown_fill_capacity: float, mark: Optional[float],
                  tick: int, grace_loops: int = 3, explained_grace: int = 40) -> Optional[str]:
        """Align lots with the server position. Call only with a position snapshot requested after
        every accept was confirmed. unknown_fill_capacity = shares of our orders whose final fill is
        not known yet (a gap up to this size is just fills we haven't been told about)."""
        exp = self.expected_position(ticker)
        if exp == server_pos:
            self._mismatch_loops[ticker] = 0
            return None
        n = self._mismatch_loops.get(ticker, 0) + 1
        self._mismatch_loops[ticker] = n
        shrinking = exp != 0 and (server_pos == 0 or _sign(server_pos) == _sign(exp)) and abs(server_pos) < abs(exp)
        if shrinking and abs(exp) - abs(server_pos) <= unknown_fill_capacity + 1e-6 and n < explained_grace:
            return None                      # fills in flight, not yet attributed
        if n < grace_loops:
            return None
        self._mismatch_loops[ticker] = 0
        if shrinking:
            diff = server_pos - exp          # e.g. expected +100, server +60 -> a sell of 40 we never saw
            self.on_fill(ticker, diff, abs(diff) * (mark or 0.0), abs(diff) * self.fee)
            return f"{ticker}: closed {abs(diff)} unseen shares at mark to match server position {server_pos}"
        if exp != 0 and _sign(exp) != _sign(server_pos):
            self.on_fill(ticker, -exp, abs(exp) * (mark or 0.0), abs(exp) * self.fee)
            add = server_pos
        else:
            add = server_pos - exp
        self.recover(ticker, add, mark or 0.0, tick, "UNEXPLAINED")
        return f"{ticker}: server position {server_pos} not explained by tenders; added {add} to unwind"

    # ------------------------------------------------------------ queries
    def open_lots(self, ticker: str) -> List[Lot]:
        return [l for l in self.lots if l.ticker == ticker and l.open]

    def has_inventory(self, ticker: str, sign: int) -> bool:
        return any(_sign(l.remaining) == sign for l in self.open_lots(ticker))

    def breakeven(self, ticker: str) -> Optional[float]:
        """Price at which unwinding the open tender lots nets zero after both fees."""
        lots = [l for l in self.open_lots(ticker) if l.kind == "TENDER"]
        if not lots:
            return None
        sign = _sign(lots[0].remaining)
        lots = [l for l in lots if _sign(l.remaining) == sign]
        qty = sum(abs(l.remaining) for l in lots)
        avg = sum(abs(l.remaining) * l.price for l in lots) / qty
        fee_t = self.fee if self.fee_on_tender else 0.0
        return avg + sign * (fee_t + self.fee)

    def unwind_start(self, ticker: str) -> Optional[int]:
        lots = self.open_lots(ticker)
        return max(l.unwind_start_tick for l in lots) if lots else None

    def tender_ids(self, ticker: str) -> List[str]:
        return [l.lot_id for l in self.open_lots(ticker)]

    def total_pnl(self, marks: Dict[str, Optional[float]]) -> float:
        return sum(l.pnl(marks.get(l.ticker)) for l in self.lots)

    def fees_paid(self) -> float:
        return sum(l.tender_fee + l.fill_fees for l in self.lots)
