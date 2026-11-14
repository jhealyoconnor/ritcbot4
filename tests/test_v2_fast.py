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
