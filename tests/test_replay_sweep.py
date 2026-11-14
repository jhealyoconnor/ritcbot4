"""Timing-accurate simulator, log -> replay round trip, and the sweep / compare tools."""
import glob
import os

from bot.config import load_config
from sim.backtest import run_heat
from sim.compare import main as compare_main
from sim.market import SimMarket, live_params
from sim.replay import ReplayMarket, load_log, replay
from sim.sweep import main as sweep_main
from sim.vtime import load_variant, run_vt_heat


def test_virtual_time_heat_is_clean_and_slow_loops_are_slow():
    var = load_variant("v2", None, {})
    m = SimMarket(5, live_params(competitors=1))
    r = run_vt_heat(m, var, latency=0.24, spacing=0.2)
    assert r.me.error is None, r.me.error
    assert r.end_positions and all(v == 0 for v in r.end_positions.values())
    assert r.violations == []
    assert all(t["outcome"] in ("ACCEPTED", "DECLINED") for t in r.tenders)
    # 300 ticks of 1 s at ~0.24 s per request: far fewer loops than the instant simulator's 900
    assert 100 < r.me.loops < 1300


def test_teammates_share_one_market_deterministically():
    var = load_variant("v2", None, {})
    runs = []
    for _ in range(2):
        m = SimMarket(7, live_params(competitors=1, teammates=2))
        r = run_vt_heat(m, var, [var, var])
        assert r.me.error is None and all(x.error is None for x in r.mates), (r.me.error, [x.error for x in r.mates])
        assert r.violations == [] and all(v == 0 for v in r.end_positions.values())
        assert all(v == 0 for mate in r.mates for v in mate.positions.values())
        runs.append((round(r.pnl, 2), [round(x.pnl, 2) for x in r.mates]))
    assert runs[0] == runs[1]


def test_logged_heat_replays_clean(tmp_path):
    r = run_heat(31, cfg=load_config(), log_dir=str(tmp_path))
    assert r.violations == []
    (d,) = glob.glob(os.path.join(str(tmp_path), "*"))
    for f in ("events.jsonl", "ticks.jsonl", "orders.csv", "decisions.csv", "summary.json", "summary.txt"):
        assert os.path.exists(os.path.join(d, f)), f
    log = load_log(d)
    assert log["last_tick"] >= 299 and len(log["tenders"]) == 5 and log["books"]
    rr = replay(d)
    assert rr.me.error is None, rr.me.error
    assert all(v == 0 for v in rr.end_positions.values()), rr.end_positions
    assert rr.violations == [], rr.violations
    assert all(t["outcome"] in ("ACCEPTED", "DECLINED") for t in rr.tenders)


def test_replay_adds_back_liquidity_the_live_bot_took(tmp_path):
    d = tmp_path / "heat"
    d.mkdir()
    book0 = {"CRZY": {"bids": [[10.00, 5000], [9.95, 5000]], "asks": [[10.05, 5000]]}}
    book2 = {"CRZY": {"bids": [[9.95, 5000]], "asks": [[10.05, 5000]]}}          # 10.00 bid gone: we hit it
    (d / "ticks.jsonl").write_text("\n".join([
        '{"tick": 10, "books": %s}' % str(book0).replace("'", '"'),
        '{"tick": 12, "books": %s}' % str(book2).replace("'", '"')]) + "\n")
    (d / "events.jsonl").write_text("")
    (d / "orders.csv").write_text("wall,tick,event,ticker,action,qty,filled,vwap\n1,10,SENT,CRZY,SELL,5000,5000,10.0\n")
    m = ReplayMarket(str(d), decay=0.9, flow_rate=0.0)
    while m.tick < 12:
        m.step()
    bids = {o.price: o.qty for o in m.bids["CRZY"]}
    assert bids.get(9.95) == 5000 and 3500 <= bids.get(10.0, 0) <= 4500     # 5000 * 0.9^2 restored


def test_sweep_and_compare_run(capsys):
    assert sweep_main(["--heats", "2", "--workers", "1", "--grid", "tender.risk_lambda=0.1,0.6"]) == 0
    assert "rank" in capsys.readouterr().out
    assert compare_main(["--variant", "v2=.", "--heats", "2", "--competitors", "1", "--teammates", "0"]) == 0
    assert "mean" in capsys.readouterr().out
