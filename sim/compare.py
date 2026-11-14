"""Compare bot versions on identical markets: simulated heats (live-like market, virtual time,
competitors, teammates running the same bot) and replays of logged heats.

    python -m sim.compare --variant main=../ritcbot4-main,fees.on_tender=false --variant v2=. \\
        --heats 60 --competitors 0,1,2 --teammates 2 --replay logs/*-live

Each variant is NAME=PATH[,key=value...]: PATH is a checkout whose bot/ package is used ('.' =
this one), followed by config overrides. Every variant sees the same seeds, so differences are
paired. Violations (penalties) and non-zero end positions are counted and must be 0.
"""
from __future__ import annotations

import argparse
import glob
import os
import statistics
from typing import Dict, List

from .market import SimMarket, live_params
from .replay import ReplayMarket
from .vtime import VTResult, Variant, load_variant, run_vt_heat


def parse_variant(spec: str) -> Variant:
    name, _, rest = spec.partition("=")
    parts = rest.split(",") if rest else ["."]
    path = parts[0] or "."
    ov = dict(p.split("=", 1) for p in parts[1:] if p)
    return load_variant(name, None if path == "." else path, ov)


def stats(results: List[VTResult]) -> Dict:
    pn = [r.pnl for r in results]
    return {"n": len(pn), "mean": statistics.mean(pn), "std": statistics.pstdev(pn) if len(pn) > 1 else 0.0,
            "worst": min(pn), "best": max(pn),
            "viol": sum(len(r.violations) for r in results),
            "not_flat": sum(1 for r in results if any(v != 0 for v in (r.end_positions or {"x": 1}).values())),
            "errors": sum(1 for r in results if r.me.error),
            "accepted": statistics.mean(r.accepted for r in results),
            "loops": statistics.mean(r.me.loops for r in results)}


def run_sim(var: Variant, seeds, competitors: int, teammates: int, latency: float, spacing: float,
            mate_var: Variant = None) -> List[VTResult]:
    out = []
    for s in seeds:
        m = SimMarket(s, live_params(competitors=competitors, teammates=teammates))
        out.append(run_vt_heat(m, var, [mate_var or var] * teammates, latency, spacing, seed=s))
    return out


def run_replays(var: Variant, dirs: List[str], latency: float, spacing: float, repeats: int = 3) -> List[VTResult]:
    out = []
    for d in dirs:
        runs = [run_vt_heat(ReplayMarket(d, seed=k), var, None, latency, spacing, seed=d) for k in range(repeats)]
        best = runs[0]
        best.me.pnl = statistics.mean(r.pnl for r in runs)      # average over the random passive flow
        best.violations = [v for r in runs for v in r.violations]
        out.append(best)
    return out


def fmt(name: str, st: Dict) -> str:
    return (f"  {name:<14} mean {st['mean']:>9,.0f}  std {st['std']:>8,.0f}  worst {st['worst']:>9,.0f}  "
            f"best {st['best']:>9,.0f}  penalties {st['viol']}  not flat {st['not_flat']}  "
            f"errors {st['errors']}  accepted/heat {st['accepted']:.2f}  loops/heat {st['loops']:.0f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.compare")
    ap.add_argument("--variant", action="append", required=True, help="NAME=PATH[,key=value...]")
    ap.add_argument("--heats", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--competitors", default="0,1,2")
    ap.add_argument("--teammates", type=int, default=2)
    ap.add_argument("--replay", nargs="*", default=[])
    ap.add_argument("--latency", type=float, default=0.24)
    ap.add_argument("--spacing", type=float, default=0.2, help="minimum seconds between requests (rate limit)")
    args = ap.parse_args(argv)
    variants = [parse_variant(v) for v in args.variant]
    seeds = list(range(args.seed, args.seed + args.heats))
    table: Dict[str, Dict[str, Dict]] = {}
    if args.heats > 0:
        for c in [int(x) for x in args.competitors.split(",") if x != ""]:
            key = f"sim, {c} competitor(s), {args.heats} heats"
            print(key)
            for v in variants:
                table.setdefault(key, {})[v.name] = stats(run_sim(v, seeds, c, 0, args.latency, args.spacing))
                print(fmt(v.name, table[key][v.name]), flush=True)
        if args.teammates > 0:
            key = f"sim, {args.teammates} teammates on the same bot + 1 competitor, {args.heats} heats"
            print(key)
            for v in variants:
                table.setdefault(key, {})[v.name] = stats(run_sim(v, seeds, 1, args.teammates, args.latency, args.spacing))
                print(fmt(v.name, table[key][v.name]), flush=True)
    dirs = sorted({d for pat in args.replay for d in glob.glob(pat) if os.path.isdir(d)
                   and os.path.exists(os.path.join(d, "ticks.jsonl")) and os.path.getsize(os.path.join(d, "ticks.jsonl"))})
    if dirs:
        key = f"replay of {len(dirs)} logged heat(s)"
        print(key)
        for v in variants:
            res = run_replays(v, dirs, args.latency, args.spacing)
            table.setdefault(key, {})[v.name] = stats(res)
            print(fmt(v.name, table[key][v.name]) + "  per heat: " + ", ".join(f"{r.pnl:,.0f}" for r in res), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
