"""Per-tender P&L rebuilt from the server's transaction rows."""
import pytest

from bot.txpnl import analyse, match_tenders


def row(i, kind, ticker, qty, price, cash, tick=1):
    return {"id": i, "type": kind, "ticker": ticker, "quantity": qty, "price": price, "cash_flow": cash, "tick": tick}


def test_single_tender_nets_exactly():
    rows = [row(1, "TENDER", "CRZY", 50000, 9.90, -495000.0),
            row(2, "ORDER", "CRZY", -30000, 9.98, 299400.0), row(3, "COMMISSION", "CRZY", 30000, 9.98, -600.0),
            row(4, "ORDER", "CRZY", -20000, 9.93, 198600.0), row(5, "COMMISSION", "CRZY", 20000, 9.93, -400.0)]
    rep = analyse(reversed(rows + rows[:2]))            # newest first, with duplicates, like the server
    assert rep.complete and rep.flat() and len(rep.lots) == 1
    lot = rep.lots[0]
    assert lot.pnl == pytest.approx(-495000 + 299400 + 198600 - 1000)
    assert lot.fees == pytest.approx(1000) and lot.avg_close == pytest.approx(9.96)
    assert lot.edge_per_share == pytest.approx(0.06)
    assert rep.total_pnl == pytest.approx(lot.pnl) and rep.tender_fee_rows == 0 and rep.unattributed_qty == 0


def test_two_stocks_fifo_and_tender_fee_row():
    rows = [row(1, "TENDER", "TAME", -40000, 25.20, 1008000.0), row(2, "COMMISSION", "TAME", 40000, 25.2, -800.0),
            row(3, "TENDER", "CRZY", 20000, 10.0, -200000.0),
            row(4, "ORDER", "TAME", 40000, 25.10, -1004000.0), row(5, "COMMISSION", "TAME", 40000, 25.1, -800.0),
            row(6, "ORDER", "CRZY", -20000, 10.05, 201000.0), row(7, "COMMISSION", "CRZY", 20000, 10.05, -400.0)]
    rep = analyse(rows)
    assert rep.tender_fee_rows == 1
    tame, crzy = rep.lots
    assert tame.pnl == pytest.approx(1008000 - 1004000 - 1600) and crzy.pnl == pytest.approx(1000 - 400)
    match_tenders(rep, [{"id": 7, "ticker": "TAME", "signed_qty": -40000, "price": 25.2},
                        {"id": 9, "ticker": "CRZY", "signed_qty": 20000, "price": 10.0}])
    assert (tame.tender_id, crzy.tender_id) == (7, 9)


def test_opposite_tender_nets_against_open_lot():
    rows = [row(1, "TENDER", "CRZY", 60000, 10.00, -600000.0),
            row(2, "TENDER", "CRZY", -40000, 10.10, 404000.0),
            row(3, "ORDER", "CRZY", -20000, 10.02, 200400.0), row(4, "COMMISSION", "CRZY", 20000, 10.02, -400.0)]
    rep = analyse(rows)
    assert len(rep.lots) == 1 and rep.flat()
    assert rep.lots[0].netted_qty == 40000 and rep.lots[0].pnl == pytest.approx(4000 + 400 - 400)


def test_gaps_and_unattributed_fills_are_reported():
    rows = [row(5, "ORDER", "CRZY", -1000, 10.0, 10000.0), row(6, "COMMISSION", "CRZY", 1000, 10.0, -20.0),
            row(9, "CASE_FINE", None, 0, 0, -500.0)]
    rep = analyse(rows)
    assert not rep.complete and rep.gaps == 2 and rep.unattributed_qty == 1000 and rep.fines == 500
