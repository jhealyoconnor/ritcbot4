"""Timing-accurate heats in virtual time.

Every engine ("actor") runs its real loop; each API request takes `latency` seconds and requests
are spaced at least `spacing` seconds apart (server rate limit). The market moves one tick per
`tick_len` seconds of virtual time. Several actors (you plus teammates running a bot) share one
market: a scheduler serves requests strictly in time order, so the run is deterministic.

Engines can come from this checkout or from another one (e.g. a `main` worktree) so that two
versions of the bot are compared on identical markets:

    v = load_variant("main", "/path/to/main-worktree", {"fees.on_tender": "false"})
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .client import SimClient
from .market import SimMarket

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Scheduler:
    """Serves requests from all actors in order of their (virtual) service time."""

    def __init__(self, market: SimMarket, tick_len: float = 1.0) -> None:
        self.m = market
        self.tick_len = tick_len
        self.cv = threading.Condition()
        self.pending: Dict[str, float] = {}
        self.active: set = set()
        self.t0_tick = market.tick

    def register(self, actor: str) -> None:
        with self.cv:
            self.active.add(actor)

    def done(self, actor: str) -> None:
        with self.cv:
            self.active.discard(actor)
            self.pending.pop(actor, None)
            self.cv.notify_all()

    def _advance(self, t: float) -> None:
        while self.m.status == "ACTIVE" and (self.m.tick - self.t0_tick + 1) * self.tick_len <= t:
            self.m.step()

    def serve(self, actor: str, t_service: float, fn: Callable[[], Any]) -> Any:
        with self.cv:
            self.pending[actor] = t_service
            self.cv.notify_all()
            while True:
                if len(self.pending) >= len(self.active):
                    nxt = min(self.pending.items(), key=lambda kv: (kv[1], kv[0]))[0]
                    if nxt == actor:
                        break
                self.cv.wait()
            try:
                self._advance(t_service)
                return fn()
            finally:
                del self.pending[actor]
                self.cv.notify_all()


@dataclass
class Variant:
    name: str
    engine_mod: Any
    api_mod: Any
    config_mod: Any
    overrides: Dict[str, Any] = field(default_factory=dict)
    _cfg: Any = None

    def cfg(self):
        if self._cfg is None:
            self._cfg = self.config_mod.load_config(overrides=dict(self.overrides))
        return self._cfg


_loaded: Dict[str, Any] = {}


def load_variant(name: str, path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Variant:
    """path=None: the bot package of this checkout. Otherwise the `bot` package found under `path`."""
    if path is None or os.path.abspath(path) == REPO:
        pkg = "bot"
    else:
        pkg = "bot_" + "".join(c if c.isalnum() else "_" for c in os.path.abspath(path))
        if pkg not in _loaded:
            init = os.path.join(path, "bot", "__init__.py")
            spec = importlib.util.spec_from_file_location(pkg, init, submodule_search_locations=[os.path.dirname(init)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg] = mod
            spec.loader.exec_module(mod)
            _loaded[pkg] = mod
    return Variant(name, importlib.import_module(pkg + ".engine"), importlib.import_module(pkg + ".api"),
                   importlib.import_module(pkg + ".config"), dict(overrides or {}))


@dataclass
class ActorResult:
    trader: str
    variant: str
    pnl: float
    positions: Dict[str, int]
    requests: int
    loops: int
    summary: Dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class VTResult:
    seed: Any
    me: ActorResult
    mates: List[ActorResult]
    violations: List[Dict]
    end_positions: Dict[str, int]
    tenders: List[Dict]
    accepted: int

    @property
    def pnl(self) -> float:
        return self.me.pnl

    def penalised(self, per_violation: float) -> float:
        return self.pnl - per_violation * len(self.violations)


def _drive(engine, client: SimClient, market: SimMarket, loop_period: float, sched: Scheduler, out: Dict) -> None:
    loops = 0
    try:
        engine.startup()
        while market.status == "ACTIVE":
            t0 = client.t
            engine.run_once()
            loops += 1
            client.t = max(client.t, t0 + loop_period)
            if getattr(engine, "exit_requested", False):
                break
        engine.run_once()
    except Exception as exc:  # noqa: BLE001
        import traceback
        out["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
    finally:
        out["loops"] = loops
        sched.done(client.tid)


def run_vt_heat(market: SimMarket, me: Variant, mates: Optional[List[Variant]] = None, latency: float = 0.24,
                spacing: float = 0.2, tick_len: float = 1.0, seed: Any = None, logger: Any = None) -> VTResult:
    """One heat. `me` trades the refereed account (market.bot); `mates` trade MATE1.. accounts."""
    mates = mates or []
    sched = Scheduler(market, tick_len)
    actors = []
    ids = [market.bot] + [f"MATE{i + 1}" for i in range(len(mates))]
    for tid, var in zip(ids, [me] + mates):
        if tid not in market.accounts:
            raise ValueError(f"market has no account {tid} (set SimParams.teammates)")
        client = SimClient(market, tid, var.api_mod, sched, latency, spacing)
        cfg = var.cfg()
        kw = {"mode": "sim", "clock": client.now, "parallel": False}
        if logger is not None and tid == market.bot:
            kw["logger"] = logger
        engine = var.engine_mod.Engine(client, cfg, **kw)
        sched.register(tid)
        actors.append((tid, var, client, engine, float(cfg.requests.loop_period), {}))
    market.start_nlv = market.nlv(market.bot)
    start = {tid: market.nlv(tid) for tid in ids}
    if len(actors) == 1:
        tid, var, client, engine, period, out = actors[0]
        _drive(engine, client, market, period, sched, out)
    else:
        threads = [threading.Thread(target=_drive, args=(engine, client, market, period, sched, out), daemon=True)
                   for tid, var, client, engine, period, out in actors]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    res = []
    for tid, var, client, engine, period, out in actors:
        summ = engine.summaries[-1] if getattr(engine, "summaries", None) else {}
        res.append(ActorResult(tid, var.name, market.nlv(tid) - start[tid], dict(market.accounts[tid].positions),
                               client.requests, out.get("loops", 0), summ, out.get("error")))
    return VTResult(seed, res[0], res[1:], list(market.violations), dict(market.end_positions or {}),
                    list(market.tender_log), sum(1 for t in market.tender_log if t["outcome"] == "ACCEPTED"))
