"""Main loop: read the server, decide tenders, unwind through the compliance gate, log.

One call to run_once() is one loop (~0.25 s). Order of work each loop:
  1. parallel reads: case (tick/status), tenders, securities (positions), open orders
  2. books for stocks with a position or pending tender (others less often)
  3. fills -> ledger, history, reconcile ledger with server positions
  4. kill switch / risk checks
  5. tenders: freeze + cancel resting orders in the stock, evaluate, ACCEPT / WAIT / DECLINE
  6. unwind every non-frozen stock with a position (every order through gate.check)
  7. low-priority reads (limits, NLV, transactions, time & sales, final fills), logging
"""
from __future__ import annotations

import math
import os
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import gate as gate_mod
from . import unwind as unwind_mod
from .api import ApiError, AuthError, TransportError, UncertainError
from .gate import GateContext, OrderRequest, position_ranges
from .history import TickerHistory
from .ledger import Ledger
from .logs import HeatLogger
from .marketdata import (Book, SecurityInfo, Tender, parse_book, parse_securities, parse_tender,
                         tick_size_from_decimals)
from .tracker import OrderTracker, TrackedOrder
from .valuation import Evaluation, LiqParams, decide, evaluate_tender


@dataclass
class Snapshot:
    tick: int = 0
    period: int = 0
    status: str = "UNKNOWN"
    ticks_per_period: int = 300
    total_periods: int = 1
    enforce_limits: Optional[bool] = None
    tenders: List[Tender] = field(default_factory=list)
    sec: Dict[str, SecurityInfo] = field(default_factory=dict)
    pos_sent: int = 0
    books: Dict[str, Book] = field(default_factory=dict)
    net_limit: int = 100000
    gross_limit: int = 250000
    nlv: Optional[float] = None
    total_fines: Optional[float] = None
    t_ok: float = 0.0


@dataclass
class TenderRecord:
    tender: Tender
    first_seen_tick: int
    first_seen_wall: float
    final: str = ""                 # ACCEPTED / DECLINED / WOULD_ACCEPT / WOULD_DECLINE / LAPSED / GONE / ACCEPT_FAILED
    decision: str = ""
    last_ev: Optional[Evaluation] = None
    accept_ev: Optional[float] = None
    attempts: int = 0
    accept_uncertain: bool = False
    pos_before: int = 0
    decided_tick: Optional[int] = None
    decided_latency: Optional[float] = None


class Engine:
    def __init__(self, client, cfg, mode: str = "live", logger: Optional[HeatLogger] = None,
                 clock: Callable[[], float] = time.monotonic, parallel: bool = True,
                 stop_dir: Optional[str] = None) -> None:
        self.client = client
        self.cfg = cfg
        self.mode = mode
        self.dry = mode == "dry"
        self.log = logger or HeatLogger("", enabled=False)
        self.clock = clock
        self.tickers: List[str] = list(cfg.case.tickers)
        self.pool = ThreadPoolExecutor(max_workers=int(cfg.connection.workers)) if parallel else None
        self.stop_dir = stop_dir
        self.own_id: Optional[str] = None
        self.static: Dict[str, SecurityInfo] = {}
        self.kill: Optional[str] = None          # None / 'flatten' / 'now'
        self.exit_requested = False
        self.err_count = 0
        self.loop = 0
        self.warnings: deque = deque(maxlen=6)
        self.heat_no = 0
        self.summaries: List[Dict] = []
        self.on_heat_end: Optional[Callable[[Dict, str], None]] = None
        self._reset_heat()

    # ================================================================ setup
    def _reset_heat(self) -> None:
        fee = float(self.cfg.fees.per_share)
        self.tracker = OrderTracker()
        self.ledger = Ledger(fee, bool(self.cfg.fees.on_tender))
        tc = self.cfg.tender
        self.history = {t: TickerHistory(t, float(getattr(tc.vol_prior, t, 0.03)),
                                         float(getattr(tc.refill_prior, t, 2000))) for t in self.tickers}
        self.decisions: Dict[int, TenderRecord] = {}
        self.snap = Snapshot(net_limit=int(self.cfg.case.net_limit), gross_limit=int(self.cfg.case.gross_limit))
        self.frozen: Set[str] = set()
        self.accept_pending: Dict[str, Dict] = {}
        self.last_schedule_tick: Dict[str, int] = {t: -1 for t in self.tickers}
        self.blocked: Counter = Counter()
        self.blocked_samples: List[Dict] = []
        self.orders_sent = 0
        self.max_net = 0
        self.max_gross = 0
        self.heat_started = False
        self.start_nlv: Optional[float] = None
        self.tas_after: Dict[str, Optional[int]] = {t: None for t in self.tickers}
        self.tx_after: Optional[int] = 0          # after=0 returns the full log, then incremental
        self.transactions: List[Dict] = []
        self.unwind_only = False
        self.degraded = False
        self.last_logged_tick = -1
        self.cancel_sent: Dict[int, int] = {}       # order_id -> tick a cancel was sent
        self.competitor_results: List[Dict] = []
        self.unwind_notes: Dict[str, str] = {}
        self.phase: Dict[str, str] = {}
        self.req_start = self.client.stats.count if hasattr(self.client, "stats") else 0
        self.t_heat_start = self.clock()
        self.perspectives: Counter = Counter()

    def warn(self, msg: str) -> None:
        self.warnings.append(f"t{self.snap.tick}: {msg}")
        self.log.event("warning", self.snap.tick, message=msg)

    def _par(self, calls: List[Tuple[str, Callable[[], Any]]]) -> Dict[str, Any]:
        """Run calls in parallel; returns {name: Result or Exception}."""
        out: Dict[str, Any] = {}
        if self.pool is None or len(calls) == 1:
            for name, fn in calls:
                try:
                    out[name] = fn()
                except AuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    out[name] = exc
            return out
        futs = {name: self.pool.submit(fn) for name, fn in calls}
        for name, fut in futs.items():
            try:
                out[name] = fut.result()
            except AuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                out[name] = exc
        return out

    def startup(self) -> None:
        """Learn who we are and the static case parameters; cancel stray orders."""
        r = self._par([("trader", self.client.trader), ("case", self.client.case),
                       ("sec", self.client.securities), ("limits", self.client.limits)])
        for k, v in r.items():
            if isinstance(v, Exception):
                self.warn(f"startup {k}: {v}")
        if not isinstance(r.get("trader"), Exception):
            self.own_id = (r["trader"].data or {}).get("trader_id")
            self.snap.nlv = (r["trader"].data or {}).get("nlv")
        if not isinstance(r.get("case"), Exception):
            self._apply_case(r["case"].data)
        if not isinstance(r.get("sec"), Exception):
            self._apply_securities(r["sec"])
        if not isinstance(r.get("limits"), Exception):
            self._apply_limits(r["limits"].data)
        if not self.dry:
            self._cancel_all("startup: cancel stray orders")
        self.log.event("startup", self.snap.tick, own_id=self.own_id, mode=self.mode,
                       static={t: vars(s) for t, s in self.static.items()},
                       net_limit=self.snap.net_limit, gross_limit=self.snap.gross_limit)

    # =============================================================== helpers
    def end_tick(self) -> int:
        e = int(self.cfg.case.heat_end_tick)
        return e if e > 0 else int(self.snap.ticks_per_period or self.cfg.case.default_ticks_per_period)

    def fee(self, ticker: str) -> float:
        server = self.static.get(ticker).trading_fee if ticker in self.static else None
        return max(float(self.cfg.fees.per_share), float(server or 0.0))

    def fee_tender(self, ticker: str) -> float:
        return self.fee(ticker) if self.cfg.fees.on_tender else 0.0

    def tick_size(self, ticker: str) -> float:
        s = self.static.get(ticker)
        return tick_size_from_decimals(s.quoted_decimals if s else None)

    def max_order(self, ticker: str) -> int:
        cfg_max = int(getattr(self.cfg.case.max_order, ticker, 10000))
        s = self.static.get(ticker)
        if s and s.max_trade_size:
            return min(cfg_max, int(s.max_trade_size))
        return cfg_max

    def pos(self, ticker: str) -> int:
        s = self.snap.sec.get(ticker)
        return s.position if s else 0

    def positions(self) -> Dict[str, int]:
        return {t: self.pos(t) for t in self.tickers}

    def mark(self, ticker: str) -> Optional[float]:
        b = self.snap.books.get(ticker)
        if b and b.mid is not None:
            return b.mid
        s = self.snap.sec.get(ticker)
        if s:
            if s.bid and s.ask:
                return (s.bid + s.ask) / 2
            return s.last
        return None

    def competitors(self, ticker: str) -> float:
        base = float(self.cfg.tender.competitors)
        est = self.history[ticker].competitor_est if self.cfg.tender.auto_competitors else None
        return max(base, est or 0.0)

    def gate_context(self) -> GateContext:
        s = self.snap
        pending = set(self.frozen)
        return GateContext(
            status=s.status, positions=self.positions(),
            committed=lambda t, a: self.tracker.committed(t, a, s.pos_sent),
            has_inventory=self.ledger.has_inventory, pending_tickers=pending,
            max_order={t: self.max_order(t) for t in self.tickers},
            tick_size={t: self.tick_size(t) for t in self.tickers},
            net_limit=s.net_limit, gross_limit=s.gross_limit, tickers=tuple(self.tickers))

    # ============================================================ apply reads
    def _apply_case(self, data: Dict) -> None:
        if not data:
            return
        s = self.snap
        s.tick = int(data.get("tick") or 0)
        s.period = int(data.get("period") or 0)
        s.status = str(data.get("status") or "UNKNOWN").upper()
        s.ticks_per_period = int(data.get("ticks_per_period") or self.cfg.case.default_ticks_per_period)
        s.total_periods = int(data.get("total_periods") or 1)
        if "is_enforce_trading_limits" in data:
            s.enforce_limits = bool(data.get("is_enforce_trading_limits"))

    def _apply_securities(self, res) -> None:
        sec = parse_securities(res.data)
        for t, info in sec.items():
            if t in self.tickers:
                st = self.static.get(t)
                if st is None or info.max_trade_size or info.trading_fee is not None:
                    self.static[t] = info
        self.snap.sec = {t: sec.get(t, SecurityInfo(t)) for t in self.tickers}
        self.snap.pos_sent = res.sent

    def _apply_limits(self, data) -> None:
        net, gross = int(self.cfg.case.net_limit), int(self.cfg.case.gross_limit)
        for lim in data or []:
            if lim.get("net_limit"):
                net = min(net, int(float(lim["net_limit"])))
            if lim.get("gross_limit"):
                gross = min(gross, int(float(lim["gross_limit"])))
        self.snap.net_limit, self.snap.gross_limit = net, gross

    def _apply_orders(self, res) -> None:
        for to, dq, dc in self.tracker.on_open_list(res.data or [], res.sent, res.recv):
            self._fill(to, dq, dc)

    def _apply_book(self, ticker: str, res) -> None:
        book = parse_book(res.data, ticker, self.own_id, bool(self.cfg.tender.anon_only))
        if self.own_id is None:
            book = self._strip_own(book)
        book.recv, book.tick = res.recv, self.snap.tick
        self.snap.books[ticker] = book

    def _strip_own(self, book: Book) -> Book:
        """Fallback when our trader id is unknown: remove our resting limit quantities."""
        for to in self.tracker.live(book.ticker):
            if to.type != "LIMIT" or to.price is None:
                continue
            levels = book.bids if to.action == "BUY" else book.asks
            for i, (p, q) in enumerate(levels):
                if abs(p - to.price) < 1e-9:
                    levels[i] = (p, max(0, q - int(to.remaining)))
            levels[:] = [lv for lv in levels if lv[1] > 0]
        return book

    def _fill(self, to: TrackedOrder, dq: float, dc: float) -> None:
        signed = dq if to.action == "BUY" else -dq
        fee = dq * self.fee(to.ticker)
        self.ledger.on_fill(to.ticker, signed, dc, fee)
        self.history[to.ticker].on_own_fill(self.snap.tick, dq, to.action)
        self.log.event("fill", self.snap.tick, ticker=to.ticker, action=to.action, qty=dq,
                       price=round(dc / dq, 4) if dq else None, order_id=to.order_id, purpose=to.purpose,
                       tender_ids=list(to.tender_ids))

    # =================================================================== loop
    def run_once(self) -> str:
        self.loop += 1
        try:
            r = self._par([("case", self.client.case), ("tenders", self.client.tenders),
                           ("sec", self.client.securities), ("orders", lambda: self.client.orders("OPEN"))])
        except AuthError:
            raise
        failed = [k for k, v in r.items() if isinstance(v, Exception)]
        if "case" in failed:
            self._error(f"read case failed: {r['case']}")
            return "ERROR"
        prev_tick, prev_status, prev_period = self.snap.tick, self.snap.status, self.snap.period
        self._apply_case(r["case"].data)
        s = self.snap
        if s.status != "ACTIVE":
            if s.status == "STOPPED" and self.heat_started:
                self._end_heat("case STOPPED")
            return s.status
        if self.heat_started and (s.tick + 5 < prev_tick or (prev_period and s.period != prev_period)):
            self._end_heat("new heat detected")
        if failed:
            self._error(f"read failed: {', '.join(failed)}: {[str(r[k])[:80] for k in failed]}")
            if "sec" in failed or "orders" in failed:
                return "ERROR"
        else:
            self.err_count = 0
        self._apply_securities(r["sec"])
        self._apply_orders(r["orders"])
        if not isinstance(r.get("tenders"), Exception):
            self.snap.tenders = [parse_tender(t, self.tickers, str(self.cfg.tender.action_fallback))
                                 for t in (r["tenders"].data or [])]
        s.t_ok = self.clock()
        self.degraded = self.err_count >= int(self.cfg.risk.max_consecutive_errors)
        if not self.heat_started:
            self._start_heat()

        self._read_books()
        self._update_history_and_ledger()
        self._check_kill_and_risk()
        if self.exit_requested:
            return "EXIT"
        self._handle_tenders()
        self._unwind_all()
        self._aux()
        self._log_tick()
        if self.kill == "flatten" and all(self.pos(t) == 0 for t in self.tickers) and not self.tracker.live():
            self.warn("flatten complete: all positions flat")
            self.exit_requested = True
        return "ACTIVE"

    def _error(self, msg: str) -> None:
        self.err_count += 1
        self.warn(msg)
        if self.err_count >= int(self.cfg.risk.max_consecutive_errors):
            self.degraded = True

    def _start_heat(self) -> None:
        self.heat_started = True
        self.heat_no += 1
        self.t_heat_start = self.clock()
        self.req_start = self.client.stats.count if hasattr(self.client, "stats") else 0
        try:
            tr = self.client.trader().data or {}
            self.start_nlv = tr.get("nlv")
            self.snap.nlv = self.start_nlv
            self.own_id = self.own_id or tr.get("trader_id")
        except (ApiError, TransportError) as exc:
            self.warn(f"trader read failed: {exc}")
        for t in self.tickers:
            p = self.pos(t)
            if p != 0 and not self.ledger.open_lots(t):
                self.ledger.recover(t, p, self.mark(t) or 0.0, self.snap.tick)
                self.warn(f"{t}: open position {p:+,} at start treated as tender inventory to unwind")
        self.log.event("heat_start", self.snap.tick, heat=self.heat_no, positions=self.positions(),
                       nlv=self.start_nlv, ticks_per_period=self.snap.ticks_per_period)

    def _read_books(self, only: Optional[Set[str]] = None) -> None:
        pending_t = {t.ticker for t in self.snap.tenders}
        need = []
        for t in self.tickers:
            if only is not None and t not in only:
                continue
            if (only is not None or self.pos(t) != 0 or t in pending_t or t not in self.snap.books
                    or self.loop % int(self.cfg.requests.idle_book_every) == 0 or self.tracker.live(t)):
                need.append(t)
        if not need:
            return
        lv = int(self.cfg.requests.book_levels)
        r = self._par([(t, (lambda t=t: self.client.book(t, lv))) for t in need])
        for t, res in r.items():
            if isinstance(res, Exception):
                self.warn(f"book {t}: {res}")
            else:
                self._apply_book(t, res)

    def _update_history_and_ledger(self) -> None:
        s = self.snap
        for t in self.tickers:
            b = s.books.get(t)
            if b is not None and b.tick == s.tick:
                self.history[t].on_book(s.tick, b.bids, b.asks, self.tick_size(t))
                for res in self.history[t].update_watches(s.tick, b.bids, b.asks):
                    self.competitor_results.append(res)
                    self.log.event("competitor_check", s.tick, ticker=t, **res)
            ap = self.accept_pending.get(t)
            if ap is not None:
                if s.pos_sent > ap["recv"] and (self.pos(t) != ap["pos_before"] or ap["loops"] <= 0):
                    del self.accept_pending[t]
                else:
                    ap["loops"] -= 1
                    continue
            cap = sum(max(0.0, o.qty - o.attributed_qty) for o in self.tracker.orders.values()
                      if o.ticker == t and o.status != "REJECTED" and (not o.terminal or not o.final_known))
            cap += sum(float(o.get("quantity", 0)) for o in self.tracker.foreign_in(t))
            msg = self.ledger.reconcile(t, self.pos(t), cap, self.mark(t), s.tick)
            if msg:
                self.warn(msg)
        net = abs(sum(self.positions().values()))
        gross = sum(abs(v) for v in self.positions().values())
        self.max_net, self.max_gross = max(self.max_net, net), max(self.max_gross, gross)

    # ================================================================== risk
    def request_kill(self, mode: str) -> None:
        if mode == "now" or self.kill == "flatten" and mode == "flatten-again":
            self.kill = "now"
        elif self.kill is None:
            self.kill = "flatten"
            self.unwind_only = True
            self.warn("KILL SWITCH: flattening positions, then exiting (again = cancel all and exit now)")

    def _check_kill_and_risk(self) -> None:
        d = self.stop_dir
        if d is not None:
            if os.path.exists(os.path.join(d, "STOP_NOW")):
                self.kill = "now"
            elif os.path.exists(os.path.join(d, "STOP")) and self.kill is None:
                self.request_kill("flatten")
        if self.kill == "now":
            if not self.dry:
                self._cancel_all("kill switch: immediate")
            self.exit_requested = True
            return
        pnl = self.heat_pnl()
        if pnl is not None and pnl < -float(self.cfg.risk.max_loss) and not self.unwind_only:
            self.unwind_only = True
            self.warn(f"MAX LOSS hit (P&L {pnl:,.0f}): unwind-only, declining new tenders")
        if self.clock() - self.snap.t_ok > float(self.cfg.risk.stale_data_seconds):
            self.degraded = True

    def heat_pnl(self) -> Optional[float]:
        if self.snap.nlv is not None and self.start_nlv is not None:
            return float(self.snap.nlv) - float(self.start_nlv)
        marks = {t: self.mark(t) for t in self.tickers}
        return self.ledger.total_pnl(marks)

    def ledger_pnl(self) -> float:
        return self.ledger.total_pnl({t: self.mark(t) for t in self.tickers})

    # =============================================================== tenders
    def _liq_params(self, ticker: str) -> LiqParams:
        tc = self.cfg.tender
        h = self.history[ticker]
        ticks_avail = max(0, self.end_tick() - int(self.cfg.unwind.market_ticks_before_end) - self.snap.tick)
        return LiqParams(fee=self.fee(ticker), tick=self.tick_size(ticker),
                         ref_depth=int(getattr(tc.ref_depth, ticker, 10000)), sigma=h.sigma(), refill=h.refill(),
                         competitors=self.competitors(ticker), risk_lambda=float(tc.risk_lambda),
                         slippage_ticks=float(tc.slippage_ticks), dump_penalty=float(tc.dump_penalty),
                         ticks_available=ticks_avail)

    def evaluate(self, t: Tender) -> Evaluation:
        b = self.snap.books.get(t.ticker) or Book(t.ticker)
        ctx = self.gate_context()
        ranges = position_ranges(ctx)
        return evaluate_tender(t, self.positions(), b.bids, b.asks, self._liq_params(t.ticker),
                               self.fee_tender(t.ticker), self.snap.net_limit, self.snap.gross_limit,
                               bool(self.cfg.case.per_stock_cap), ranges)

    def _handle_tenders(self) -> None:
        s = self.snap
        listed = {t.tender_id: t for t in s.tenders}
        for t in s.tenders:
            if t.tender_id not in self.decisions:
                self.decisions[t.tender_id] = TenderRecord(t, s.tick, self.clock())
                self.perspectives[t.field_perspective] += 1
                self.log.event("tender_new", s.tick, tender=t.raw, parsed={
                    "ticker": t.ticker, "our_action": t.action, "qty": t.quantity, "price": t.price,
                    "expires": t.expires, "valid": t.valid, "issue": t.issue,
                    "action_field_perspective": t.field_perspective})
                b = s.books.get(t.ticker)
                if b is not None:
                    self.history[t.ticker].watch_tender(t.tender_id, s.tick, t.quantity, t.unwind_action, b.bids, b.asks)
                if len(self.perspectives) > 1 and "unknown" not in self.perspectives:
                    self.warn(f"tender 'action' field meaning is inconsistent: {dict(self.perspectives)}")
        self._resolve_unlisted(listed)
        open_recs = [rec for tid, rec in self.decisions.items() if tid in listed and not rec.final]
        self.frozen = {rec.tender.ticker for rec in open_recs if rec.tender.ticker in self.tickers}
        # strict freeze: cancel resting orders in frozen stocks before deciding
        refresh: Set[str] = set()
        for tk in sorted(self.frozen):
            if self.tracker.has_live(tk):
                if not self.dry:
                    self._cancel_ticker(tk, "freeze: tender pending")
                refresh.add(tk)
        if refresh and not self.dry:
            self._refresh(refresh)
        for rec in sorted(open_recs, key=lambda r: (r.tender.tick, r.tender.tender_id)):
            t = listed[rec.tender.tender_id]
            ev = self.evaluate(t)
            decide(ev, s.tick, t.expires, self.cfg.tender, self.unwind_only or self.kill is not None, self.degraded)
            changed = rec.decision != ev.decision
            rec.decision, rec.last_ev = ev.decision, ev
            if changed or ev.decision == "WAIT":
                self._log_decision(t, ev)
            if ev.decision == "ACCEPT":
                if self.tracker.has_live(t.ticker) and not self.dry:
                    rec.decision = "ACCEPT-WAIT-CANCEL"
                    continue
                self._accept(t, ev, rec)
            elif ev.decision == "DECLINE":
                self._decline(t, ev, rec)
        self.frozen = {rec.tender.ticker for tid, rec in self.decisions.items()
                       if tid in listed and not rec.final and rec.tender.ticker in self.tickers}

    def _log_decision(self, t: Tender, ev: Evaluation) -> None:
        d = ev.detail
        self.log.decision(tick=self.snap.tick, tender_id=t.tender_id, ticker=t.ticker, action=t.action,
                          quantity=t.quantity, price=t.price, expires=t.expires, decision=ev.decision,
                          ev=round(ev.ev, 2) if not math.isinf(ev.ev) else None,
                          ev_per_share=round(ev.ev_per_share, 4) if not math.isinf(ev.ev_per_share) else None,
                          threshold=round(ev.threshold, 2), reason=ev.reason, **{k: d.get(k) for k in (
                              "ref", "later_px", "imm_qty", "later_qty", "dump_qty", "sigma", "refill",
                              "competitors", "pos_before", "pos_after")})
        self.log.event("decision", self.snap.tick, tender_id=t.tender_id, decision=ev.decision, ev=ev.ev,
                       threshold=ev.threshold, reason=ev.reason, detail=ev.detail)

    def _resolve_unlisted(self, listed: Dict[int, Tender]) -> None:
        for tid, rec in self.decisions.items():
            if rec.final or tid in listed:
                continue
            t = rec.tender
            if rec.accept_uncertain:
                moved = self.pos(t.ticker) - rec.pos_before
                if abs(moved - t.signed_qty) <= abs(t.signed_qty) * 0.05 + 1:
                    self._book_accept(t, rec, recv=self.snap.pos_sent)
                    self.warn(f"tender {tid}: uncertain accept confirmed by position change")
                    continue
                rec.final = "GONE"
                self.warn(f"tender {tid}: uncertain accept not reflected in position; treating as not accepted")
                continue
            rec.final = "LAPSED" if self.snap.tick >= t.expires else "GONE"
            if not self.dry:
                self.warn(f"tender {tid} disappeared undecided ({rec.final})")
            self.log.event("tender_gone", self.snap.tick, tender_id=tid, final=rec.final)

    def _accept(self, t: Tender, ev: Evaluation, rec: TenderRecord) -> None:
        if self.dry:
            rec.final = "WOULD_ACCEPT"
            rec.accept_ev = ev.ev
            self.log.event("dry_accept", self.snap.tick, tender_id=t.tender_id, ev=ev.ev,
                           sweep_now=ev.detail.get("imm_qty"), detail=ev.detail)
            return
        rec.pos_before = self.pos(t.ticker)
        try:
            res = self.client.accept_tender(t.tender_id, t.price)
        except UncertainError as exc:
            rec.accept_uncertain = True
            self.warn(f"accept {t.tender_id} uncertain ({exc}); will confirm from positions")
            return
        except ApiError as exc:
            rec.attempts += 1
            self.warn(f"accept {t.tender_id} rejected: {exc}")
            if rec.attempts >= 3 or exc.status in (400, 404, 409, 410):
                rec.final = "ACCEPT_FAILED"
            return
        except TransportError as exc:
            rec.attempts += 1
            self.warn(f"accept {t.tender_id} failed: {exc}")
            return
        data = res.data if isinstance(res.data, dict) else {}
        if data.get("success") is False:
            rec.attempts += 1
            self.warn(f"accept {t.tender_id} returned success=false: {data}")
            if rec.attempts >= 3:
                rec.final = "ACCEPT_FAILED"
            return
        rec.accept_ev = ev.ev
        self._book_accept(t, rec, res.recv)
        self._refresh({t.ticker}, orders=False)

    def _book_accept(self, t: Tender, rec: TenderRecord, recv: int) -> None:
        rec.final = "ACCEPTED"
        rec.decided_tick = self.snap.tick
        rec.decided_latency = self.clock() - rec.first_seen_wall
        self.ledger.accept_tender(t.tender_id, t.ticker, t.signed_qty, t.price, self.snap.tick, rec.accept_ev)
        self.accept_pending[t.ticker] = {"recv": recv, "pos_before": rec.pos_before, "loops": 12}
        self.last_schedule_tick[t.ticker] = -1
        self.frozen.discard(t.ticker)
        self.log.event("accepted", self.snap.tick, tender_id=t.tender_id, ticker=t.ticker, action=t.action,
                       qty=t.quantity, price=t.price, ev=rec.accept_ev, seconds_to_decide=round(rec.decided_latency, 3))

    def _decline(self, t: Tender, ev: Evaluation, rec: TenderRecord) -> None:
        rec.decided_tick = self.snap.tick
        if self.dry:
            rec.final = "WOULD_DECLINE"
            self.log.event("dry_decline", self.snap.tick, tender_id=t.tender_id, reason=ev.reason)
            return
        try:
            self.client.decline_tender(t.tender_id)
            rec.final = "DECLINED"
            self.log.event("declined", self.snap.tick, tender_id=t.tender_id, reason=ev.reason)
        except ApiError as exc:
            rec.attempts += 1
            self.warn(f"decline {t.tender_id}: {exc}")
            if exc.status in (400, 404, 409, 410) or rec.attempts >= 3:
                rec.final = "DECLINED?"
        except TransportError as exc:
            rec.attempts += 1
            self.warn(f"decline {t.tender_id}: {exc}")

    def _refresh(self, tickers: Set[str], orders: bool = True) -> None:
        calls: List[Tuple[str, Callable]] = [("sec", self.client.securities)]
        if orders:
            calls.append(("orders", lambda: self.client.orders("OPEN")))
        lv = int(self.cfg.requests.book_levels)
        calls += [(f"book:{t}", (lambda t=t: self.client.book(t, lv))) for t in tickers]
        r = self._par(calls)
        if not isinstance(r.get("sec"), Exception):
            self._apply_securities(r["sec"])
        if orders and not isinstance(r.get("orders"), Exception):
            self._apply_orders(r["orders"])
        for t in tickers:
            res = r.get(f"book:{t}")
            if res is not None and not isinstance(res, Exception):
                self._apply_book(t, res)

    # ================================================================ orders
    def send(self, req: OrderRequest) -> Optional[TrackedOrder]:
        ok, reason = gate_mod.check(req, self.gate_context())
        if not ok:
            self.blocked[reason.split(":")[0]] += 1
            if len(self.blocked_samples) < 200:
                self.blocked_samples.append({"tick": self.snap.tick, "req": vars(req), "reason": reason})
            self.log.order(tick=self.snap.tick, event="BLOCKED", ticker=req.ticker, action=req.action, type=req.type,
                           qty=req.qty, price=req.price, purpose=req.purpose, tender_ids="|".join(req.tender_ids),
                           reason=reason)
            return None
        if self.dry:
            self.log.order(tick=self.snap.tick, event="DRY", ticker=req.ticker, action=req.action, type=req.type,
                           qty=req.qty, price=req.price, purpose=req.purpose, tender_ids="|".join(req.tender_ids))
            return None
        seq = self.client.seq.next()
        to = self.tracker.new(req.ticker, req.action, req.qty, req.type, req.price, req.purpose, req.tender_ids,
                              self.snap.tick, seq)
        t0 = self.clock()
        self.orders_sent += 1
        try:
            res = self.client.post_order(req.ticker, req.type, req.qty, req.action, req.price)
        except UncertainError as exc:
            self.tracker.on_post_uncertain(to, self.client.seq.next())
            self.warn(f"order send uncertain ({exc}); reconciling before resending")
            self._log_order("UNCERTAIN", to, reason=str(exc)[:120])
            return to
        except (ApiError, TransportError) as exc:
            self.tracker.on_post_rejected(to, self.client.seq.next())
            self.warn(f"order rejected: {str(exc)[:120]}")
            self._log_order("REJECTED", to, reason=str(exc)[:200])
            return to
        for o, dq, dc in self.tracker.on_post_ok(to, res.data or {}, res.recv):
            self._fill(o, dq, dc)
        self._log_order("SENT", to, latency_ms=round((self.clock() - t0) * 1000))
        return to

    def _log_order(self, event: str, to: TrackedOrder, **extra) -> None:
        self.log.order(tick=self.snap.tick, event=event, local_id=to.local_id, order_id=to.order_id, ticker=to.ticker,
                       action=to.action, type=to.type, qty=to.qty, price=to.price, purpose=to.purpose,
                       tender_ids="|".join(to.tender_ids), status=to.status, filled=to.filled, vwap=to.vwap, **extra)

    def _cancel_orders(self, orders: List[TrackedOrder], foreign_ids: List[int], why: str) -> None:
        ids = []
        for o in orders:
            if o.order_id is None or o.terminal:
                continue
            last = self.cancel_sent.get(o.order_id)
            if last is not None and self.snap.tick - last < 2:
                continue
            ids.append(o.order_id)
        for oid in foreign_ids:
            last = self.cancel_sent.get(oid)
            if last is None or self.snap.tick - last >= 2:
                ids.append(oid)
        if not ids or self.dry:
            return
        for oid in ids:
            self.cancel_sent[oid] = self.snap.tick
        try:
            if len(ids) == 1:
                res = self.client.cancel_order(ids[0])
                self.tracker.on_cancelled(ids, res.recv)
            else:
                res = self.client.cancel_ids(ids)
                self.tracker.on_cancelled((res.data or {}).get("cancelled_order_ids") or [], res.recv)
            self.log.event("cancel", self.snap.tick, ids=ids, why=why)
        except (ApiError, TransportError) as exc:
            self.log.event("cancel_failed", self.snap.tick, ids=ids, why=why, error=str(exc)[:200])

    def _cancel_ticker(self, ticker: str, why: str) -> None:
        try:
            res = self.client.cancel_ticker(ticker)
            self.tracker.on_cancelled((res.data or {}).get("cancelled_order_ids") or [], res.recv)
            self.log.event("cancel_ticker", self.snap.tick, ticker=ticker, why=why, result=res.data)
        except (ApiError, TransportError) as exc:
            self.warn(f"cancel {ticker} failed: {exc}")

    def _cancel_all(self, why: str) -> None:
        try:
            res = self.client.cancel_all()
            self.tracker.on_cancelled((res.data or {}).get("cancelled_order_ids") or [], res.recv)
            self.log.event("cancel_all", self.snap.tick, why=why, result=res.data)
        except (ApiError, TransportError) as exc:
            self.warn(f"cancel all failed: {exc}")

    # ================================================================ unwind
    def _unwind_all(self) -> None:
        s = self.snap
        uc = self.cfg.unwind
        for t in self.tickers:
            if t in self.frozen:
                self.phase[t] = "FROZEN"
                continue
            p = self.pos(t)
            live = self.tracker.live(t)
            foreign = self.tracker.foreign_in(t)
            if p == 0 and not live and not foreign:
                self.phase[t] = "FLAT"
                continue
            if t in self.accept_pending and p == self.accept_pending[t]["pos_before"]:
                continue                         # accept not yet visible in positions
            b = s.books.get(t) or Book(t)
            h = self.history[t]
            start = self.ledger.unwind_start(t)
            ctx = unwind_mod.UnwindContext(
                ticker=t, tick=s.tick, end_tick=self.end_tick(), pos=p,
                committed=self.tracker.committed(t, "SELL" if p > 0 else "BUY", s.pos_sent) if p else 0.0,
                live=live, foreign=foreign, bids=b.bids, asks=b.asks, tick_size=self.tick_size(t),
                max_order=self.max_order(t), breakeven=self.ledger.breakeven(t), sigma=h.sigma(),
                sigma_prior=h.vol_prior, competitors=self.competitors(t),
                start_tick=start if start is not None else s.tick, last_schedule_tick=self.last_schedule_tick[t],
                ref_mid=h.ewma_mid, tender_ids=tuple(self.ledger.tender_ids(t)), cfg=uc,
                force_flatten=self.kill == "flatten")
            plan = unwind_mod.plan(ctx)
            self.phase[t] = plan.phase
            self.unwind_notes[t] = "; ".join(plan.notes)
            if plan.cancels or plan.cancel_foreign:
                self._cancel_orders(plan.cancels, plan.cancel_foreign, f"unwind {plan.phase}")
            for req in plan.places[: int(uc.max_places_per_loop)]:
                self.send(req)
            if plan.scheduled:
                self.last_schedule_tick[t] = s.tick

    # =================================================================== aux
    def _aux(self) -> None:
        if self.loop % int(self.cfg.requests.aux_every) != 0:
            return
        calls: List[Tuple[str, Callable]] = [("limits", self.client.limits), ("trader", self.client.trader),
                                             ("tx", lambda: self.client.transactions(self.tx_after))]
        for t in self.tickers:
            calls.append((f"tas:{t}", (lambda t=t: self.client.tas(t, self.tas_after[t]))))
        for to in self.tracker.needs_final_fill()[:4]:
            calls.append((f"order:{to.local_id}", (lambda oid=to.order_id: self.client.order(oid))))
        r = self._par(calls)
        if not isinstance(r.get("limits"), Exception):
            self._apply_limits(r["limits"].data)
        if not isinstance(r.get("trader"), Exception):
            d = r["trader"].data or {}
            self.snap.nlv = d.get("nlv")
            self.snap.total_fines = d.get("total_fines")
            if self.snap.total_fines:
                self.warn(f"server reports fines: {self.snap.total_fines}")
        if not isinstance(r.get("tx"), Exception):
            rows = r["tx"].data or []
            for row in rows:
                if row.get("id") is not None:
                    self.tx_after = max(self.tx_after or 0, int(row["id"]))
            if rows:
                self.transactions.extend(rows)
                self.log.event("transactions", self.snap.tick, rows=rows)
        for t in self.tickers:
            res = r.get(f"tas:{t}")
            if res is not None and not isinstance(res, Exception):
                rows = res.data or []
                if self.tas_after[t] is None:
                    rows = [x for x in rows]       # first read: recent history only
                for row in rows:
                    if row.get("id") is not None:
                        self.tas_after[t] = max(self.tas_after[t] or 0, int(row["id"]))
                self.history[t].on_tas(rows)
        for key, res in r.items():
            if key.startswith("order:") and not isinstance(res, Exception):
                for to, dq, dc in self.tracker.on_order(res.data or {}, res.recv):
                    self._fill(to, dq, dc)

    def fee_on_tender_observed(self) -> str:
        """Best reading of the server's own transaction log: is the fee charged on tender trades?
        Compares commission booked at each tender's tick with what that tick's order fills explain."""
        rows = self.transactions
        tender_rows = [r for r in rows if str(r.get("type")).upper() == "TENDER"]
        if not tender_rows:
            return "unknown (no tender transactions seen)"
        if not any(str(r.get("type")).upper() == "COMMISSION" for r in rows):
            return "unknown (server log shows no COMMISSION rows; compare NLV change instead)"
        verdicts = []
        for tr in tender_rows:
            tick, tk = tr.get("tick"), tr.get("ticker")
            qty = abs(float(tr.get("quantity") or 0))
            fee = self.fee(tk) if tk in self.tickers else float(self.cfg.fees.per_share)
            comm = sum(abs(float(r.get("cash_flow") or 0)) for r in rows
                       if str(r.get("type")).upper() == "COMMISSION" and r.get("tick") == tick)
            filled = sum(abs(float(r.get("quantity") or 0)) for r in rows
                         if str(r.get("type")).upper() == "ORDER" and r.get("tick") == tick)
            excess = comm - fee * filled
            verdicts.append("yes" if qty and excess >= 0.5 * fee * qty else "no")
        return ", ".join(verdicts)

    # ================================================================ logging
    def _log_tick(self) -> None:
        s = self.snap
        rec: Dict[str, Any] = {"tick": s.tick, "loop": self.loop, "status": s.status, "positions": self.positions(),
                               "frozen": sorted(self.frozen), "live_orders": len(self.tracker.live()),
                               "phase": dict(self.phase), "pnl": self.heat_pnl(), "ledger_pnl": self.ledger_pnl()}
        stats = getattr(self.client, "stats", None)
        if stats is not None:
            rec["rps"] = round(stats.per_second(), 2)
            rec["latency_ms"] = round((stats.latency_ewma or 0) * 1000, 1)
        if s.tick != self.last_logged_tick:
            n = int(self.cfg.logging.book_levels)
            rec["books"] = {t: {"bids": b.bids[:n], "asks": b.asks[:n], "own_bids": b.own_bids, "own_asks": b.own_asks}
                            for t, b in s.books.items()}
            rec["tenders"] = [{"id": t.tender_id, "ticker": t.ticker, "action": t.action, "qty": t.quantity,
                               "price": t.price, "expires": t.expires, "valid": t.valid} for t in s.tenders]
            rec["sigma"] = {t: round(self.history[t].sigma(), 5) for t in self.tickers}
            rec["refill"] = {t: round(self.history[t].refill(), 1) for t in self.tickers}
            self.last_logged_tick = s.tick
        self.log.tick(rec)

    # ============================================================== heat end
    def _end_heat(self, why: str) -> None:
        if not self.dry:
            self._cancel_all(f"heat end: {why}")
        summary, text = self.summary(why)
        self.summaries.append(summary)
        self.log.event("heat_end", self.snap.tick, summary=summary)
        self.log.write_summary(summary, text)
        if self.on_heat_end:
            self.on_heat_end(summary, text)
        heat_no = self.heat_no
        self._reset_heat()
        self.heat_no = heat_no

    def summary(self, why: str = "") -> Tuple[Dict[str, Any], str]:
        marks = {t: self.mark(t) for t in self.tickers}
        recs = list(self.decisions.values())
        lots = []
        for l in self.ledger.lots:
            avg = l.avg_close_price()
            slip = None
            if avg is not None and l.kind == "TENDER":
                slip = (avg - l.price) if l.signed_qty > 0 else (l.price - avg)
            lots.append({"lot": l.lot_id, "ticker": l.ticker, "qty": l.signed_qty, "price": l.price, "kind": l.kind,
                         "remaining": l.remaining, "pnl": round(l.pnl(marks.get(l.ticker)), 2),
                         "expected_ev": round(l.expected_ev, 2) if l.expected_ev is not None else None,
                         "avg_close": round(avg, 4) if avg else None,
                         "edge_per_share_vs_tender": round(slip, 4) if slip is not None else None,
                         "fees": round(l.tender_fee + l.fill_fees, 2), "netted": l.netted_qty})
        elapsed = max(1e-6, self.clock() - self.t_heat_start)
        stats = getattr(self.client, "stats", None)
        nreq = (stats.count - self.req_start) if stats else 0
        summary = {
            "heat": self.heat_no, "ended": why, "tick": self.snap.tick,
            "tenders_seen": len(recs),
            "accepted": sum(1 for r in recs if r.final == "ACCEPTED"),
            "declined": sum(1 for r in recs if r.final.startswith("DECLINED")),
            "would_accept": sum(1 for r in recs if r.final == "WOULD_ACCEPT"),
            "would_decline": sum(1 for r in recs if r.final == "WOULD_DECLINE"),
            "lapsed": sum(1 for r in recs if r.final in ("LAPSED", "GONE")),
            "tenders": [{"id": r.tender.tender_id, "ticker": r.tender.ticker, "action": r.tender.action,
                         "qty": r.tender.quantity, "price": r.tender.price, "final": r.final,
                         "ev": round(r.last_ev.ev, 2) if r.last_ev and not math.isinf(r.last_ev.ev) else None,
                         "reason": r.last_ev.reason if r.last_ev else "", "seen_tick": r.first_seen_tick,
                         "decided_tick": r.decided_tick} for r in recs],
            "lots": lots,
            "ledger_pnl": round(self.ledger.total_pnl(marks), 2),
            "server_pnl": round(self.heat_pnl(), 2) if self.heat_pnl() is not None else None,
            "fees_paid": round(self.ledger.fees_paid(), 2),
            "orders_sent": self.orders_sent,
            "blocked_orders": sum(self.blocked.values()), "blocked_reasons": dict(self.blocked),
            "end_positions": self.positions(),
            "max_net": self.max_net, "max_gross": self.max_gross,
            "requests_per_sec": round(nreq / elapsed, 2),
            "avg_latency_ms": round(stats.avg_latency() * 1000, 1) if stats else None,
            "rate_limited_429": stats.rate_limited if stats else None,
            "fee_on_tender_observed": self.fee_on_tender_observed(),
            "tender_action_field": dict(self.perspectives),
            "competitor_checks": self.competitor_results,
            "server_fines": self.snap.total_fines,
            "warnings": list(self.warnings),
        }
        lines = [f"===== HEAT {self.heat_no} SUMMARY ({why}) =====",
                 f"Tenders: seen {summary['tenders_seen']}, accepted {summary['accepted']}, declined "
                 f"{summary['declined']}, would-accept {summary['would_accept']}, would-decline "
                 f"{summary['would_decline']}, lapsed/gone {summary['lapsed']}"]
        for tr in summary["tenders"]:
            lines.append(f"  #{tr['id']} {tr['ticker']} {tr['action']} {tr['qty']:,} @ {tr['price']}: {tr['final']} "
                         f"(EV {tr['ev']}) {tr['reason']}")
        for l in lots:
            lines.append(f"  lot {l['lot']} {l['ticker']} {l['qty']:+,} @ {l['price']}: P&L ${l['pnl']:,.2f} "
                         f"(expected {l['expected_ev']}), avg close {l['avg_close']}, edge/share "
                         f"{l['edge_per_share_vs_tender']}, fees ${l['fees']:,.2f}, remaining {l['remaining']}")
        lines += [f"P&L: ledger ${summary['ledger_pnl']:,.2f} | server NLV change {summary['server_pnl']}",
                  f"Fees paid (est.): ${summary['fees_paid']:,.2f} | fee on tender observed: {summary['fee_on_tender_observed']}",
                  f"Orders sent {self.orders_sent}, compliance-blocked {summary['blocked_orders']} {summary['blocked_reasons']}",
                  f"END POSITIONS: {summary['end_positions']} (must be 0)",
                  f"Max net {self.max_net:,} / {self.snap.net_limit:,}, max gross {self.max_gross:,} / {self.snap.gross_limit:,}",
                  f"Requests/sec {summary['requests_per_sec']}, avg latency {summary['avg_latency_ms']} ms, 429s {summary['rate_limited_429']}",
                  f"Tender action field meaning: {summary['tender_action_field']}",
                  f"Competitor checks: {self.competitor_results}",
                  f"Server fines: {self.snap.total_fines}"]
        return summary, "\n".join(lines) + "\n"
