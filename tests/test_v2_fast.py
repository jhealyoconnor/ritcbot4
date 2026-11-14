"""v2 quick fixes: 401 between heats, light idle loop, immediate sweep, sweep in the last 30 ticks."""
from bot.api import AuthError
from bot.config import load_config
from bot.engine import Engine
from bot.unwind import ESCALATE, plan
from sim.backtest import FakeClock
from sim.client import SimClient
from sim.market import SimMarket, TenderSpec

from test_unwind import uctx

CFG = load_config()


def test_fail_fast_defaults():
    c = CFG.connection
    assert c.max_get_retries == 0 and c.connect_timeout + c.read_timeout <= 2.3


def test_sweep_runs_in_the_last_30_ticks():
    p = plan(uctx(tick=275, start_tick=272))
    assert p.phase == ESCALATE
    assert sum(r.qty for r in p.places if r.purpose == "sweep") == 33000


def test_v2_defaults():
    assert CFG.fees.on_tender is False and CFG.unwind.min_edge == 0.04 and CFG.unwind.sweep_floor == "model"


def test_idle_loop_uses_two_requests_and_sweep_follows_accept_in_the_same_loop():
    m = SimMarket(3, tenders=[TenderSpec(20, "CRZY", "BUY", 60000, 0.25)])
    c = SimClient(m)
    eng = Engine(c, CFG, mode="sim", clock=FakeClock(), parallel=False)
    eng.startup()
    eng.run_once()
    counts = []
    for _ in range(3):
        n0 = c.requests
        eng.run_once()
        counts.append(c.requests - n0)
    assert min(counts) == 2, counts
    while m.tick < 20:
        m.step()
    eng.run_once()                                   # sees the tender, accepts, sweeps
    assert m.accounts[m.bot].positions["CRZY"] < 60000 and m.tender_log[0]["outcome"] == "ACCEPTED"
    assert m.violations == []


def test_401_between_heats_writes_summary_and_resumes():
    m = SimMarket(4, tenders=[])
    c = SimClient(m)
    eng = Engine(c, CFG, mode="sim", clock=FakeClock(), parallel=False)
    assert eng.auth_lost() is False                  # 401 before any heat: caller exits
    eng.startup()
    eng.run_once()
    seen = []
    eng.on_heat_end = lambda s, t: seen.append(s)
    assert eng.auth_lost() is True and len(seen) == 1 and "401" in seen[0]["ended"]
    assert eng.auth_lost() is True and len(seen) == 1 and not eng.heat_started
    eng.run_once()                                   # server back: next heat starts
    assert eng.heat_started and eng.heat_no == 2


def test_two_step_ctrl_c():
    from bot.cli import CtrlC
    c = CtrlC()
    assert c.press(10.0) == "warn"
    assert c.press(20.0) == "warn"            # too late: counts as a new first press
    assert c.press(22.5) == "flatten"
    assert c.press(23.0) == "now" and c.press(23.1) == "abort"


def test_summary_has_per_tender_pnl_from_server_transactions_and_survives_401():
    from sim.market import live_params
    m = SimMarket(6, live_params(competitors=0), tenders=[TenderSpec(20, "CRZY", "BUY", 60000, 0.30),
                                                           TenderSpec(120, "TAME", "SELL", 40000, 0.30)])
    c = SimClient(m)
    clock = FakeClock()
    eng = Engine(c, CFG, mode="sim", clock=clock, parallel=False)
    eng.startup()
    while m.tick < 295:
        for _ in range(2):
            clock.t += 0.5
            eng.run_once()
        m.step()
    assert all(v == 0 for v in m.accounts[m.bot].positions.values())
    seen = []
    eng.on_heat_end = lambda s, t: seen.append((s, t))
    assert eng.auth_lost() is True                       # server answers 401: heat is closed out
    s, text = seen[0]
    tx = s["server_tx"]
    assert tx["complete"] and tx["flat"] and len(tx["tenders"]) == 2
    assert {x["tender_id"] for x in tx["tenders"]} == {t["tid"] for t in m.tender_log}
    assert abs(sum(x["realised"] for x in tx["tenders"]) - m.nlv(m.bot)) < 0.5
    assert abs(s["final_pnl"] - m.nlv(m.bot)) < 0.5 and s["final_pnl_source"] == "server transactions"
    assert all(x["predicted_ev"] is not None for x in tx["tenders"]) and "FINAL P&L" in text


def test_competitor_estimate_waits_for_trade_data_and_is_capped():
    from bot.history import TickerHistory
    h = TickerHistory("CRZY", 0.04, 3000)
    h.require_tas_coverage = True
    h.watch_tender(1, 100, 50000, "SELL", [(10.0, 5000)], [(10.1, 5000)])
    assert h.update_watches(111, [(10.0, 5000)], [(10.1, 5000)]) == [] and h.competitor_est is None   # no trade data yet
    h.on_tas([{"id": 5, "tick": 99, "quantity": 100}] + [{"id": 10 + k, "tick": 100 + k, "quantity": 10000} for k in range(11)])
    h.on_tas([{"id": 20, "tick": 110, "quantity": 10000}])          # repeated row (inclusive 'after') is ignored
    (res,) = h.update_watches(113, [(10.0, 5000)], [(10.1, 5000)])
    assert res["other_volume"] == 110000 and 2.0 < h.competitor_est < 2.3
    cfg = load_config(overrides={"tender.competitor_cap": 1.0})
    eng = Engine(SimClient(SimMarket(1, tenders=[])), cfg, mode="sim", clock=FakeClock(), parallel=False)
    eng.history["CRZY"].competitor_est = 7.5
    assert eng.competitors("CRZY") == 1.0


def test_passive_jitter_improves_by_one_extra_tick_only_when_there_is_room():
    wide = dict(breakeven=None, bids=[(9.90, 500)], asks=[(10.00, 5000)])
    base = [r for r in plan(uctx(**wide)).places if r.purpose == "passive"][0].price
    jit = [r for r in plan(uctx(jitter=1, **wide)).places if r.purpose == "passive"][0].price
    assert (base, jit) == (9.99, 9.98)
    tight = dict(breakeven=None, bids=[(9.98, 500)], asks=[(10.00, 5000)])
    assert [r for r in plan(uctx(jitter=1, **tight)).places if r.purpose == "passive"][0].price == 9.99
    short = dict(pos=-20000, breakeven=None, bids=[(9.90, 500)], asks=[(10.00, 5000)], ref_mid=9.95)
    assert [r for r in plan(uctx(jitter=1, **short)).places if r.purpose == "passive"][0].price == 9.92


def test_endgame_starts_earlier_on_a_slow_link():
    eng = Engine(SimClient(SimMarket(1, tenders=[])), CFG, mode="sim", clock=FakeClock(), parallel=False)
    eng.snap.ticks_per_period = 300
    eng.loop_secs = 0.8
    assert eng.unwind_end_tick() == 300                      # fast loop: unchanged
    eng.loop_secs = 3.0
    assert eng.unwind_end_tick() == 300 - 12                 # 6 loops x 3 ticks + 4 = 22 ticks -> 12 earlier
    eng.loop_secs = 30.0
    assert eng.unwind_end_tick() == 300 - CFG.unwind.flatten_max_extra_ticks


def test_stale_tender_list_is_rechecked_before_an_order_when_enabled():
    cfg = load_config(overrides={"risk.max_tender_age": 0.2})
    m = SimMarket(3, tenders=[TenderSpec(5, "TAME", "BUY", 90000, 0.25), TenderSpec(8, "TAME", "BUY", 5000, 0.0)])
    c = SimClient(m)
    clock = FakeClock()
    eng = Engine(c, cfg, mode="sim", clock=clock, parallel=False)
    eng.startup()
    while m.tick < 7:
        clock.t += 1.0
        eng.run_once()
        m.step()
    eng.run_once()
    assert m.accounts[m.bot].positions["TAME"] > 0
    m.step()                                   # tick 8: a second TAME tender appears, unseen by the bot
    clock.t += 5.0                             # ... and its tender list is now stale
    from bot.gate import OrderRequest
    pos = m.accounts[m.bot].positions["TAME"]
    assert eng.send(OrderRequest("TAME", "SELL", min(1000, pos), "MARKET", None, "schedule")) is None
    assert "TAME" in eng.frozen and m.violations == []
