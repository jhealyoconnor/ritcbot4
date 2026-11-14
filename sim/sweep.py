"""Parameter sweep: rank settings by average P&L per heat (penalties counted, variance shown).

    python -m sim.sweep --heats 20 --grid tender.risk_lambda=0.1,0.3,0.6 --grid unwind.horizon_ticks.CRZY=8,15,25
    python -m sim.sweep --replay logs/2026*-live --grid unwind.passive=true,false

With --replay, each setting is run over the logged heats (books and tenders you actually saw);
otherwise over --heats simulated heats (same seeds for every setting, so differences are paired).
"""
from __future__ import annotations

import argparse
import glob
import itertools
import statistics
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Tuple

from bot.config import load_config


def _one(args) -> Tuple[Dict, List[float], int]:
    combo, seeds, replays = args
    from sim.backtest import run_heat
    from sim.replay import replay
    cfg = load_config(overrides=combo)
    per_v = float(cfg.risk.penalty_per_violation)
    pnls, viol = [], 0
    if replays:
        for d in replays:
            r = replay(d, combo)
            pnls.append(r.penalised(per_v))
            viol += len(r.violations)
    else:
        for s in seeds:
            r = run_heat(s, cfg=cfg)
            pnls.append(r.penalised(per_v))
            viol += len(r.violations)
    return combo, pnls, viol


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.sweep")
    ap.add_argument("--grid", action="append", default=[], help="key=v1,v2,...")
    ap.add_argument("--heats", type=int, default=12)
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--replay", nargs="*", default=None, help="log directories (globs ok)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    keys, values = [], []
    for g in args.grid:
        k, _, vs = g.partition("=")
        keys.append(k)
        values.append(vs.split(","))
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*values)] if keys else [{}]
    replays = sorted({d for pat in (args.replay or []) for d in glob.glob(pat)}) if args.replay is not None else None
    seeds = list(range(args.seed, args.seed + args.heats))
    jobs = [(c, seeds, replays) for c in combos]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_one, jobs))
    rows = []
    for combo, pnls, viol in results:
        mean = statistics.mean(pnls)
        sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
        rows.append((mean, sd, min(pnls), viol, combo))
    rows.sort(key=lambda r: -r[0])
    print(f"{'rank':>4} {'mean P&L':>10} {'std':>9} {'worst':>10} {'viol':>5}  setting")
    for i, (mean, sd, worst, viol, combo) in enumerate(rows, 1):
        print(f"{i:>4} {mean:>10,.0f} {sd:>9,.0f} {worst:>10,.0f} {viol:>5}  {combo or '(config.toml defaults)'}")
    n = len(replays) if replays else len(seeds)
    print(f"\n{n} heats per setting ({'replayed logs' if replays else 'simulated'}); violations are charged "
          f"risk.penalty_per_violation each. Prefer settings that win by more than ~1 std / sqrt({n}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
