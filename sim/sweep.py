"""Parameter sweep: rank settings by average P&L per heat (penalties counted, spread shown).

    python -m sim.sweep --heats 40 --grid tender.risk_lambda=0.1,0.3,0.6 --grid unwind.passive=true,false
    python -m sim.sweep --replay "logs/*-live" --grid unwind.sweep_floor=model,breakeven

Simulated heats use the live-calibrated market in virtual time (latency and rate limit included),
with the same seeds for every setting so differences are paired. With --replay, each setting is
run over the logged heats instead.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import os
import statistics
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Tuple


def _one(args) -> Tuple[Dict, List[float], int, int]:
    combo, seeds, replays, competitors, teammates = args
    from sim.compare import run_replays, run_sim
    from sim.vtime import load_variant
    var = load_variant("sweep", None, combo)
    per_v = float(var.cfg().risk.penalty_per_violation)
    res = run_replays(var, replays, 0.24, 0.2) if replays else run_sim(var, seeds, competitors, teammates, 0.24, 0.2)
    viol = sum(len(r.violations) for r in res)
    not_flat = sum(1 for r in res if any(v != 0 for v in (r.end_positions or {"x": 1}).values()))
    return combo, [r.penalised(per_v) for r in res], viol, not_flat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.sweep")
    ap.add_argument("--grid", action="append", default=[], help="key=v1,v2,...")
    ap.add_argument("--heats", type=int, default=30)
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--competitors", type=int, default=1)
    ap.add_argument("--teammates", type=int, default=0)
    ap.add_argument("--replay", nargs="*", default=None, help="log directories (globs ok)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    keys, values = [], []
    for g in args.grid:
        k, _, vs = g.partition("=")
        keys.append(k)
        values.append(vs.split(","))
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*values)] if keys else [{}]
    replays = None
    if args.replay is not None:
        replays = sorted({d for pat in args.replay for d in glob.glob(pat)
                          if os.path.isdir(d) and os.path.exists(os.path.join(d, "ticks.jsonl"))
                          and os.path.getsize(os.path.join(d, "ticks.jsonl"))})
    seeds = list(range(args.seed, args.seed + args.heats))
    jobs = [(c, seeds, replays, args.competitors, args.teammates) for c in combos]
    if args.workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(_one, jobs))
    else:
        results = [_one(j) for j in jobs]
    rows = []
    for combo, pnls, viol, not_flat in results:
        rows.append((statistics.mean(pnls), statistics.pstdev(pnls) if len(pnls) > 1 else 0.0, min(pnls), viol,
                     not_flat, combo))
    rows.sort(key=lambda r: -r[0])
    print(f"{'rank':>4} {'mean P&L':>10} {'std':>9} {'worst':>10} {'viol':>5} {'open':>5}  setting")
    for i, (mean, sd, worst, viol, nf, combo) in enumerate(rows, 1):
        print(f"{i:>4} {mean:>10,.0f} {sd:>9,.0f} {worst:>10,.0f} {viol:>5} {nf:>5}  {combo or '(config.toml defaults)'}")
    n = len(replays) if replays else len(seeds)
    print(f"\n{n} heats per setting ({'replayed logs' if replays else 'simulated, live-like market'}); a violation "
          f"costs risk.penalty_per_violation. Trust a difference only if it exceeds ~2 x std / sqrt({n}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
