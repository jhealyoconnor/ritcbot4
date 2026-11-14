"""Fast in-process heats: the real Engine against SimMarket (no HTTP), several loops per tick.

    python -m sim.backtest --heats 20 [--set tender.risk_lambda=0.5] [--seed 1]
"""
from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from bot.config import load_config
from bot.engine import Engine
from bot.logs import HeatLogger

from .client import SimClient
from .market import SimMarket, SimParams, TenderSpec


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class HeatResult:
    seed: int
    pnl: float
    violations: List[Dict]
    end_positions: Dict[str, int]
    tenders: List[Dict]
    accepted: int
    fees: float
    summary: Dict = field(default_factory=dict)

    def penalised(self, per_violation: float) -> float:
        return self.pnl - per_violation * len(self.violations)


def run_heat(seed: int = 0, overrides: Optional[Dict] = None, tenders: Optional[List[TenderSpec]] = None,
             params: Optional[SimParams] = None, loops_per_tick: int = 3, cfg=None,
             initial_positions: Optional[Dict[str, int]] = None, market: Optional[SimMarket] = None,
             log_dir: Optional[str] = None) -> HeatResult:
    cfg = cfg or load_config(overrides=overrides)
    logger = HeatLogger(log_dir, tag=f"sim{seed}") if log_dir else None
    m = market or SimMarket(seed, params, tenders, initial_bot_positions=initial_positions)
    client = SimClient(m)
    clock = FakeClock()
    eng = Engine(client, cfg, mode="sim", clock=clock, parallel=False, logger=logger)
    eng.startup()
    m.start_nlv = m.nlv(m.bot)
    while m.status == "ACTIVE":
        for _ in range(loops_per_tick):
            clock.t += 1.0 / loops_per_tick
            eng.run_once()
        m.step()
    eng.run_once()   # sees STOPPED -> heat summary
    if logger is not None:
        logger.close()
    acc = m.accounts[m.bot]
    return HeatResult(seed, m.bot_pnl(), list(m.violations), dict(m.end_positions or {}), list(m.tender_log),
                      sum(1 for t in m.tender_log if t["outcome"] == "ACCEPTED"), acc.fees,
                      eng.summaries[-1] if eng.summaries else {})


def run_many(seeds, overrides=None, params=None, loops_per_tick=3) -> List[HeatResult]:
    cfg = load_config(overrides=overrides)
    return [run_heat(s, params=params, loops_per_tick=loops_per_tick, cfg=cfg) for s in seeds]


def describe(results: List[HeatResult], per_violation: float = 100000.0) -> Dict:
    pnls = [r.pnl for r in results]
    pen = [r.penalised(per_violation) for r in results]
    return {"heats": len(results), "mean_pnl": statistics.mean(pnls),
            "std_pnl": statistics.pstdev(pnls) if len(pnls) > 1 else 0.0,
            "mean_penalised": statistics.mean(pen), "min_pnl": min(pnls), "max_pnl": max(pnls),
            "violations": sum(len(r.violations) for r in results),
            "accepted_per_heat": statistics.mean(r.accepted for r in results)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.backtest")
    ap.add_argument("--heats", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    overrides = {}
    for item in args.set:
        k, _, v = item.partition("=")
        overrides[k] = v
    res = run_many(range(args.seed, args.seed + args.heats), overrides)
    for r in res:
        print(f"seed {r.seed:3d}: P&L {r.pnl:>10,.0f}  accepted {r.accepted}  fees {r.fees:>8,.0f}  "
              f"end {r.end_positions}  violations {len(r.violations)}")
        if args.verbose:
            for t in r.tenders:
                print(f"     {t}")
            for v in r.violations:
                print(f"     VIOLATION {v}")
    d = describe(res)
    print(f"\nmean P&L {d['mean_pnl']:,.0f} (std {d['std_pnl']:,.0f}, min {d['min_pnl']:,.0f}, "
          f"max {d['max_pnl']:,.0f}); violations {d['violations']}; accepted/heat {d['accepted_per_heat']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
