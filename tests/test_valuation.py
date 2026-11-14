"""Tender valuation: the brief's worked examples, book walk, fees, limits, netting, late tenders."""
import pytest

from bot.config import load_config
from bot.marketdata import Tender, round_price
from bot.valuation import LiqParams, decide, evaluate_tender, ref_price, walk

from helpers import BRIEF_ASKS, BRIEF_BIDS

CFG = load_config()


def params(**kw):
    tc = CFG.tender
    base = dict(fee=CFG.fees.per_share, tick=0.01, ref_depth=tc.ref_depth.CRZY, sigma=tc.vol_prior.CRZY,
                refill=tc.refill_prior.CRZY, competitors=tc.competitors, risk_lambda=tc.risk_lambda,
                slippage_ticks=tc.slippage_ticks, dump_penalty=tc.dump_penalty, ticks_available=200)
    base.update(kw)
    return LiqParams(**base)


def tender(price, qty=50000, action="BUY", ticker="CRZY"):
    return Tender(1, ticker, action, qty, price, 30, 0, "", True)


def run(t, positions=None, fee_tender=None, p=None, bids=BRIEF_BIDS, asks=BRIEF_ASKS):
    positions = positions or {"CRZY": 0, "TAME": 0}
    ft = CFG.fees.per_share if fee_tender is None else fee_tender
    ev = evaluate_tender(t, positions, bids, asks, p or params(), ft, 100000, 250000, True)
    return decide(ev, 0, t.expires, CFG.tender, False, False)


@pytest.mark.parametrize("fee_tender", [0.02, 0.0])
def test_brief_accept_example_990(fee_tender):
    ev = run(tender(9.90), fee_tender=fee_tender)
    assert ev.decision == "ACCEPT", (ev.ev, ev.reason, ev.detail)
    assert ev.ev > 0
    # only the 9.98 and 9.95 levels (33,000 shares) clear break-even and are taken immediately
    assert ev.detail["imm_qty"] == 33000


@pytest.mark.parametrize("fee_tender", [0.02, 0.0])
def test_brief_decline_example_997(fee_tender):
    ev = run(tender(9.97), fee_tender=fee_tender)
    assert ev.decision == "DECLINE", (ev.ev, ev.reason)
    assert ev.ev < 0


def test_book_walk_and_vwap_with_fees():
    got, cash = walk(BRIEF_BIDS, 40000)
    assert got == 40000 and cash == pytest.approx(8000 * 9.98 + 25000 * 9.95 + 7000 * 9.90)
    assert ref_price(BRIEF_BIDS, 20000) == pytest.approx((8000 * 9.98 + 12000 * 9.95) / 20000)
    # dumping all 50k into today's book with 2c on both legs loses $310 (brief example, fee-aware)
    got, cash = walk(BRIEF_BIDS, 50000)
    assert cash - 50000 * 9.90 - 50000 * 0.04 == pytest.approx(-310)


def test_sell_tender_symmetric():
    asks = [(10.00, 8000), (10.03, 25000), (10.08, 7000), (10.10, 10000), (10.14, 15000)]
    bids = [(9.98, 5000)]
    good = run(tender(10.08, action="SELL"), bids=bids, asks=asks)
    bad = run(tender(10.01, action="SELL"), bids=bids, asks=asks)
    assert good.decision == "ACCEPT" and bad.decision == "DECLINE"


def test_netting_tender_is_worth_more():
    """Holding +50k CRZY, an institution wants to BUY 40k from us: accepting removes 40k of our
    worst unwind shares, so the tender is worth far more than the same tender when flat."""
    t = tender(9.99, qty=40000, action="SELL")
    flat = run(t)
    held = run(t, positions={"CRZY": 50000, "TAME": 0})
    assert held.ev > flat.ev + 1000
    assert held.decision == "ACCEPT"


def test_declines_when_limits_would_break():
    ev = run(tender(9.50, qty=60000), positions={"CRZY": 50000, "TAME": 0})
    assert ev.decision == "DECLINE" and not ev.feasible
    ev = run(tender(9.50, qty=60000, ticker="TAME"), positions={"CRZY": 50000, "TAME": 0})
    assert ev.decision == "DECLINE" and "net" in ev.reason


def test_per_stock_cap():
    # net limit fine thanks to the TAME short, but CRZY would be 120k on its own
    ev = run(tender(9.50, qty=70000), positions={"CRZY": 50000, "TAME": -40000})
    assert ev.decision == "DECLINE" and "per-stock" in ev.reason


def test_late_tender_is_worth_less():
    early = run(tender(9.85, qty=90000))
    late = run(tender(9.85, qty=90000), p=params(ticks_available=3))
    assert late.ev < early.ev
    assert late.detail["dump_qty"] > 0


def test_competitors_reduce_value():
    alone = run(tender(9.88, qty=80000))
    crowded = run(tender(9.88, qty=80000), p=params(competitors=3))
    assert crowded.ev < alone.ev


def test_invalid_tender_declined():
    t = tender(9.90)
    t.valid, t.issue = False, "caption mismatch"
    ev = run(t)
    assert ev.decision == "DECLINE" and "invalid" in ev.reason


def test_tick_rounding():
    assert round_price(9.9237, 0.01, "up") == 9.93
    assert round_price(9.9237, 0.01, "down") == 9.92
    assert round_price(9.92, 0.01, "up") == 9.92
    assert round_price(10.005, 0.01, "nearest") in (10.0, 10.01)
    assert round_price(25.1349999, 0.01, "down") == 25.13
