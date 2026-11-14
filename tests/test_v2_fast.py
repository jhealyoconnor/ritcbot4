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
