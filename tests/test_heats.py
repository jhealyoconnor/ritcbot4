"""Full 300-tick heats with the real engine against the simulated market (in-process, fast).
Every run asserts: end position 0, zero compliance violations, no tender left to lapse."""
import pytest

from bot.config import load_config
from bot.engine import Engine
from sim.backtest import FakeClock, run_heat
from sim.client import SimClient
from sim.market import SimMarket, SimParams, TenderSpec

CFG = load_config()


def assert_clean(r):
    assert r.end_positions and all(v == 0 for v in r.end_positions.values()), r.end_positions
    assert r.violations == [], r.violations
    assert all(t["outcome"] in ("ACCEPTED", "DECLINED") for t in r.tenders), r.tenders


@pytest.mark.parametrize("seed", range(1, 13))
def test_random_heats_clean(seed):
    r = run_heat(seed, cfg=CFG)
    assert_clean(r)


def test_random_heats_profitable_on_average():
    pnls = [run_heat(s, cfg=CFG).pnl for s in range(20, 30)]
    assert sum(pnls) / len(pnls) > 0


def test_tender_arrives_mid_unwind_same_stock_freeze_applies():
    tenders = [TenderSpec(20, "CRZY", "BUY", 80000, 0.25), TenderSpec(26, "CRZY", "BUY", 30000, 0.25),
               TenderSpec(120, "TAME", "SELL", 40000, 0.15), TenderSpec(125, "TAME", "SELL", 30000, 0.02),
               TenderSpec(200, "CRZY", "SELL", 50000, 0.20)]
    r = run_heat(3, cfg=CFG, tenders=tenders)
    assert_clean(r)
    assert r.accepted >= 2


def test_opposite_tender_nets_inventory():
    """Long ~90k CRZY; one tick later an institution wants to buy 50k at the mid (zero premium).
    Alone that tender is worthless, but it replaces our worst 50k of unwind, so it is accepted."""
    tenders = [TenderSpec(20, "CRZY", "BUY", 90000, 0.12), TenderSpec(21, "CRZY", "SELL", 50000, 0.0)]
    r = run_heat(4, cfg=CFG, tenders=tenders)
    assert_clean(r)
    outcomes = {t["action"]: t["outcome"] for t in r.tenders}
    assert outcomes == {"BUY": "ACCEPTED", "SELL": "ACCEPTED"}
    assert any(l["netted"] == 50000 for l in r.summary["lots"])


def test_competitors_draining_the_book():
    params = SimParams(competitors=6, competitor_accept_prob=1.0)
    for seed in (5, 6, 7):
        assert_clean(run_heat(seed, cfg=CFG, params=params))


def test_large_late_tender():
    tenders = [TenderSpec(40, "TAME", "BUY", 60000, 0.15), TenderSpec(262, "CRZY", "BUY", 100000, 0.30),
               TenderSpec(268, "TAME", "SELL", 80000, 0.25)]
    r = run_heat(8, cfg=CFG, tenders=tenders)
    assert_clean(r)


def test_tender_in_last_seconds_declined_explicitly():
    tenders = [TenderSpec(288, "CRZY", "BUY", 50000, 0.02)]
    r = run_heat(9, cfg=CFG, tenders=tenders)
    assert_clean(r)
    assert r.tenders[0]["outcome"] == "DECLINED"


def test_restart_with_open_position_unwinds_it():
    r = run_heat(10, cfg=CFG, initial_positions={"CRZY": 30000, "TAME": -12000}, tenders=[])
    assert_clean(r)


def test_server_variants_spec_style_keys_no_ticker_institution_action():
    params = SimParams(book_keys="bid", tender_ticker_field=False, tender_action_perspective="institution")
    base = run_heat(11, cfg=CFG)
    variant = run_heat(11, cfg=CFG, params=params)
    assert_clean(variant)
    assert [t["outcome"] for t in variant.tenders] == [t["outcome"] for t in base.tenders]


def test_fee_on_tender_measured_from_transactions():
    tenders = [TenderSpec(20, "CRZY", "BUY", 50000, 0.25)]
    r = run_heat(12, cfg=CFG, tenders=tenders)
    assert "yes" in r.summary["fee_on_tender_observed"]
    r2 = run_heat(12, cfg=CFG, tenders=tenders, params=SimParams(tender_fee=False))
    assert r2.summary["fee_on_tender_observed"].startswith("no")


def test_dry_run_sends_nothing():
    m = SimMarket(13)
    clock = FakeClock()
    eng = Engine(SimClient(m), CFG, mode="dry", clock=clock, parallel=False)
    eng.startup()
    while m.status == "ACTIVE":
        for _ in range(3):
            clock.t += 0.34
            eng.run_once()
        m.step()
    assert all(o.trader != m.bot for o in m.orders.values())
    assert all(v == 0 for v in m.accounts[m.bot].positions.values())
    assert eng.orders_sent == 0
    finals = {r.final for r in eng.decisions.values()}
    assert finals <= {"WOULD_ACCEPT", "WOULD_DECLINE", "LAPSED", "GONE"}


def test_kill_switch_flattens_then_exits():
    m = SimMarket(14, tenders=[TenderSpec(10, "CRZY", "BUY", 80000, 0.25)])
    clock = FakeClock()
    eng = Engine(SimClient(m), CFG, mode="sim", clock=clock, parallel=False)
    eng.startup()
    while m.tick < 12:
        eng.run_once()
        m.step()
    eng.run_once()
    assert m.accounts[m.bot].positions["CRZY"] != 0
    eng.request_kill("flatten")
    for _ in range(200):
        eng.run_once()
        if eng.exit_requested:
            break
        m.step()
    assert eng.exit_requested
    assert m.accounts[m.bot].positions["CRZY"] == 0
    assert [v for v in m.violations if v["kind"] != "lapsed"] == []
