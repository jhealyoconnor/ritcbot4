"""Unwind planner: sweep, schedule, passive, stale cancels, escalation, end-of-heat flatten."""
from bot.config import load_config
from bot.tracker import TrackedOrder
from bot.unwind import ESCALATE, FLATTEN, NORMAL, UnwindContext, phase_for, plan

from helpers import BRIEF_ASKS, BRIEF_BIDS

CFG = load_config(overrides={"unwind.min_edge": 0.0, "unwind.sweep_floor": "breakeven"})   # v1 sweep rules
CFG2 = load_config()


def uctx(**kw):
    base = dict(ticker="CRZY", tick=50, end_tick=300, pos=50000, committed=0.0, live=[], foreign=[],
                bids=list(BRIEF_BIDS), asks=list(BRIEF_ASKS), tick_size=0.01, max_order=25000, breakeven=9.94,
                sigma=0.04, sigma_prior=0.04, competitors=0.0, start_tick=50, last_schedule_tick=-1,
                ref_mid=9.99, tender_ids=("101",), cfg=CFG.unwind)
    base.update(kw)
    return UnwindContext(**base)


def order(local_id, action="SELL", qty=10000, price=9.95, purpose="schedule", tick=50, filled=0, oid=None):
    o = TrackedOrder(local_id, "CRZY", action, qty, "LIMIT", price, purpose, ("101",), tick, 1,
                     order_id=oid or local_id)
    o.status, o.filled = "OPEN", filled
    return o


def test_phases():
    assert phase_for(100, 300, CFG.unwind) == NORMAL
    assert phase_for(275, 300, CFG.unwind) == ESCALATE
    assert phase_for(290, 300, CFG.unwind) == FLATTEN


def test_sweep_takes_profitable_depth_at_breakeven_limit():
    p = plan(uctx())
    sweeps = [r for r in p.places if r.purpose == "sweep"]
    assert sum(r.qty for r in sweeps) == 33000          # 8k @ 9.98 + 25k @ 9.95 clear 9.94
    assert all(r.price == 9.94 and r.action == "SELL" and r.qty <= 25000 for r in sweeps)
    total = sum(r.qty for r in p.places)
    assert total <= 50000


def test_never_places_more_than_position_minus_committed():
    p = plan(uctx(committed=45000.0))
    assert sum(r.qty for r in p.places) <= 5000


def test_schedule_once_per_tick_and_bounded_by_depth():
    c = uctx(breakeven=None, last_schedule_tick=-1)
    p = plan(c)
    sched = [r for r in p.places if r.purpose == "schedule"]
    assert len(sched) == 1 and p.scheduled
    assert sched[0].qty <= 25000 and sched[0].price <= 9.98
    p2 = plan(uctx(breakeven=None, last_schedule_tick=50))
    assert not [r for r in p2.places if r.purpose == "schedule"]


def test_short_position_buys():
    p = plan(uctx(pos=-30000, breakeven=10.05, ref_mid=10.0))
    assert p.places and all(r.action == "BUY" for r in p.places)
    sweeps = [r for r in p.places if r.purpose == "sweep"]
    assert sum(r.qty for r in sweeps) == 8000 and sweeps[0].price == 10.05   # 5k@10.00 + 3k@10.03


def test_wrong_direction_and_stale_orders_cancelled():
    wrong = order(1, action="BUY", price=9.90)
    stale = order(2, tick=40)
    fresh = order(3, tick=50)
    p = plan(uctx(live=[wrong, stale, fresh], committed=20000.0))
    assert wrong in p.cancels and stale in p.cancels and fresh not in p.cancels


def test_flat_cancels_everything_and_places_nothing():
    o = order(1)
    p = plan(uctx(pos=0, live=[o], foreign=[{"order_id": 77, "ticker": "CRZY"}]))
    assert p.cancels == [o] and p.cancel_foreign == [77] and not p.places


def test_flatten_uses_market_orders_capped_at_exact_remaining():
    p = plan(uctx(tick=291, pos=37000, committed=0.0))
    assert p.phase == FLATTEN
    assert [r.type for r in p.places] == ["MARKET", "MARKET"]
    assert [r.qty for r in p.places] == [25000, 12000]
    p = plan(uctx(tick=295, pos=-7000, committed=0.0, ticker="TAME", max_order=10000))
    assert [(r.action, r.qty, r.type) for r in p.places] == [("BUY", 7000, "MARKET")]


def test_flatten_cancels_limits_and_respects_committed():
    lim = order(1, qty=10000)
    p = plan(uctx(tick=292, pos=30000, committed=10000.0, live=[lim]))
    assert lim in p.cancels
    assert sum(r.qty for r in p.places) == 20000


def test_escalation_prices_further_through_and_no_passive():
    p = plan(uctx(tick=280, breakeven=None, start_tick=200))
    assert p.phase == ESCALATE
    sched = [r for r in p.places if r.purpose == "schedule"]
    assert sched and sched[0].price == 9.93                     # best 9.98 - 5 ticks
    assert not [r for r in p.places if r.purpose == "passive"]


def test_passive_inside_spread():
    p = plan(uctx(breakeven=None, bids=[(9.95, 500)], asks=[(10.00, 5000)]))
    pas = [r for r in p.places if r.purpose == "passive"]
    assert pas and pas[0].price == 9.99 and pas[0].action == "SELL"


def test_model_floor_stops_the_sweep_at_the_working_price():
    """Break-even 9.80, but the model expects 9.96 by working the shares: only the 9.98 level is swept."""
    p = plan(uctx(cfg=CFG2.unwind, breakeven=9.80, work_px=9.957))
    sweeps = [r for r in p.places if r.purpose == "sweep"]
    assert sum(r.qty for r in sweeps) == 8000 and sweeps[0].price == 9.96
    assert sum(r.qty for r in p.places) <= 50000 and any(r.purpose == "passive" for r in p.places)


def test_model_floor_never_goes_below_breakeven_plus_min_edge_and_is_off_in_the_last_30_ticks():
    p = plan(uctx(cfg=CFG2.unwind, breakeven=9.94, work_px=9.80))
    assert all(r.price == 9.98 for r in p.places if r.purpose == "sweep")          # 9.94 + 0.04
    late = plan(uctx(cfg=CFG2.unwind, breakeven=9.80, work_px=9.957, tick=275, start_tick=272))
    assert sum(r.qty for r in late.places if r.purpose == "sweep") == 50000        # floor back at 9.84


def test_model_floor_for_a_short():
    p = plan(uctx(cfg=CFG2.unwind, pos=-30000, breakeven=10.30, work_px=10.035, ref_mid=10.0))
    sweeps = [r for r in p.places if r.purpose == "sweep"]
    assert sum(r.qty for r in sweeps) == 8000 and sweeps[0].price == 10.03
