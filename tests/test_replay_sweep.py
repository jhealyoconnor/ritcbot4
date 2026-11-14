"""Log -> replay round trip and the sweep CLI."""
import glob
import os

from bot.config import load_config
from sim.backtest import run_heat
from sim.replay import load_log, replay
from sim.sweep import main as sweep_main


def test_logged_heat_replays_clean(tmp_path):
    r = run_heat(31, cfg=load_config(), log_dir=str(tmp_path))
    assert r.violations == []
    (d,) = glob.glob(os.path.join(str(tmp_path), "*"))
    for f in ("events.jsonl", "ticks.jsonl", "orders.csv", "decisions.csv", "summary.json", "summary.txt"):
        assert os.path.exists(os.path.join(d, f)), f
    books, tenders, last = load_log(d)
    assert last >= 299 and len(tenders) == 5 and books
    rr = replay(d)
    assert all(v == 0 for v in rr.end_positions.values()), rr.end_positions
    assert rr.violations == [], rr.violations
    assert all(t["outcome"] in ("ACCEPTED", "DECLINED") for t in rr.tenders)


def test_sweep_runs(capsys):
    assert sweep_main(["--heats", "2", "--workers", "1", "--grid", "tender.risk_lambda=0.1,0.6"]) == 0
    out = capsys.readouterr().out
    assert "rank" in out and "risk_lambda" in out
