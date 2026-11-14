"""End-to-end over real HTTP: the live engine (parallel reads, rate limiter, retries) against the
mock RIT server with latency, 429 rate limiting and dropped connections, including connections
dropped AFTER the server executed an order. Time is compressed (0.1 s ticks) so a 300-tick heat
takes ~30 s; the real-time version with 150 ms latency is marked slow (pytest -m slow)."""
import time

import pytest

from bot.api import RitClient
from bot.config import load_config
from bot.engine import Engine
from sim.market import SimMarket, SimParams, TenderSpec
from sim.mock_server import MockConfig, MockServer


def run_live_heat(seed, tick_seconds=0.1, latency=0.004, jitter=0.002, rate_limit=300.0, drop_prob=0.0,
                  drop_after_prob=0.0, tenders=None, overrides=None):
    market = SimMarket(seed, SimParams(), tenders, bot_id="BOT")
    mcfg = MockConfig("BOT", "pass", latency, jitter, rate_limit, drop_prob, tick_seconds, seed, drop_after_prob)
    srv = MockServer(market, mcfg, 0).start()
    ov = {"requests.loop_period": 0.0, "requests.initial_rate": 300.0, "requests.max_rate": 600.0,
          "requests.min_rate": 20.0, "connection.backoff_base": 0.01, "connection.read_timeout": 1.0,
          "risk.stale_data_seconds": 1.0, "risk.tick_seconds_prior": tick_seconds,
          "risk.tick_window_margin": 0.15 * tick_seconds,
          # time is compressed 10x here, so a burst of orders spans a whole tick; the tick-boundary
          # guard (optional in live use) keeps this plumbing test deterministic
          "risk.tick_boundary_guard": True}
    ov.update(overrides or {})
    cfg = load_config(overrides=ov)
    client = RitClient(f"http://127.0.0.1:{srv.port}/v1", "BOT", "pass", cfg.connection, cfg.requests)
    eng = Engine(client, cfg, mode="live")
    try:
        eng.startup()
        deadline = time.monotonic() + tick_seconds * 300 * 3 + 30
        while time.monotonic() < deadline:
            st = eng.run_once()
            if st == "STOPPED" and market.status == "STOPPED" and not eng.heat_started:
                break
            time.sleep(max(0.0, cfg.requests.loop_period))
    finally:
        srv.stop()
    return market, eng, srv


def assert_clean(market):
    assert market.end_positions is not None, "heat did not finish"
    assert all(v == 0 for v in market.end_positions.values()), market.end_positions
    assert market.violations == [], market.violations


def test_http_heat_with_latency_and_rate_limit():
    market, eng, srv = run_live_heat(21)
    assert_clean(market)
    assert eng.summaries and eng.summaries[-1]["end_positions"] == {"CRZY": 0, "TAME": 0}
    assert srv.stats.get("requests", 0) > 1000


def test_http_heat_with_429s_and_dropped_connections():
    tenders = [TenderSpec(15, "CRZY", "BUY", 80000, 0.25), TenderSpec(60, "TAME", "SELL", 50000, 0.18),
               TenderSpec(64, "TAME", "SELL", 20000, 0.18), TenderSpec(150, "CRZY", "SELL", 70000, 0.22),
               TenderSpec(255, "CRZY", "BUY", 60000, 0.25)]
    market, eng, srv = run_live_heat(22, rate_limit=120.0, drop_prob=0.02, drop_after_prob=0.02, tenders=tenders)
    assert_clean(market)
    assert srv.stats.get("429", 0) > 0
    assert srv.stats.get("dropped", 0) + srv.stats.get("dropped_after", 0) > 0
    assert sum(1 for t in market.tender_log if t["outcome"] == "ACCEPTED") >= 2


@pytest.mark.slow
def test_realtime_heat_150ms_latency():
    market, eng, srv = run_live_heat(23, tick_seconds=1.0, latency=0.15, jitter=0.05, rate_limit=25.0,
                                     overrides={"requests.loop_period": 0.25, "requests.initial_rate": 20.0,
                                                "requests.max_rate": 40.0, "requests.min_rate": 3.0,
                                                "connection.read_timeout": 3.0, "risk.stale_data_seconds": 3.0,
                                                "risk.tick_window_margin": 0.08})
    assert_clean(market)
