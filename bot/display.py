"""One-screen live display sized for a VS Code terminal panel (~100 columns, ~28 rows)."""
from __future__ import annotations

import math
import os
import sys
from typing import List


def _f(x, fmt="{:,.0f}", none="-"):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return none
    return fmt.format(x)


def render(engine, port, mode: str) -> str:
    e = engine
    s = e.snap
    end = e.end_tick()
    uc = e.cfg.unwind
    lines: List[str] = []
    stats = getattr(e.client, "stats", None)
    rps = stats.per_second() if stats else 0
    lat = (stats.latency_ewma or 0) * 1000 if stats else 0
    flags = []
    if e.kill:
        flags.append(f"KILL:{e.kill.upper()}")
    if e.unwind_only:
        flags.append("UNWIND-ONLY")
    if e.degraded:
        flags.append("DEGRADED")
    lines.append(f"RITCx LT bot [{mode.upper()}] port {port} | tick {s.tick}/{end} {s.status} | "
                 f"{rps:.1f} req/s {lat:.0f} ms | {' '.join(flags)}")
    pos = e.positions()
    net = sum(pos.values())
    gross = sum(abs(v) for v in pos.values())
    to_flat = end - int(uc.market_ticks_before_end) - s.tick
    lines.append(f"net {net:+,}/{s.net_limit:,}  gross {gross:,}/{s.gross_limit:,}  |  "
                 f"P&L ${_f(e.heat_pnl())} (ledger ${_f(e.ledger_pnl())})  |  ticks to market-order deadline {to_flat}")
    lines.append("-" * 100)
    lines.append(f"{'STOCK':5} {'POS':>9} {'BID':>7} {'ASK':>7} {'PHASE':9} {'LIVE':>4} {'BREAKEVEN':>9} "
                 f"{'SIGMA':>6} {'REFILL':>7} {'COMP':>5}  NOTE")
    for t in e.tickers:
        b = s.books.get(t)
        h = e.history[t]
        frozen = "FROZEN" if t in e.frozen else e.phase.get(t, "-")
        be = e.ledger.breakeven(t)
        lines.append(f"{t:5} {pos[t]:>+9,} {_f(b.best_bid if b else None, '{:.2f}'):>7} "
                     f"{_f(b.best_ask if b else None, '{:.2f}'):>7} {frozen:9} {len(e.tracker.live(t)):>4} "
                     f"{_f(be, '{:.3f}'):>9} {h.sigma():>6.3f} {h.refill():>7,.0f} {e.competitors(t):>5.2f}  "
                     f"{e.unwind_notes.get(t, '')[:30]}")
    lines.append("-" * 100)
    lines.append("LEDGER (accepted tenders)")
    lots = [l for l in e.ledger.lots][-5:]
    if not lots:
        lines.append("  (none)")
    for l in lots:
        done = 1 - (abs(l.remaining) / abs(l.signed_qty)) if l.signed_qty else 1
        bar = "#" * int(done * 20)
        lines.append(f"  {l.lot_id[:18]:18} {l.ticker} {l.signed_qty:+,} @ {l.price:.2f}  left {l.remaining:+,}  "
                     f"[{bar:20}] {done * 100:3.0f}%  P&L ${_f(l.pnl(e.mark(l.ticker)))}")
    lines.append("-" * 100)
    lines.append("TENDERS")
    shown = 0
    for tid, rec in sorted(e.decisions.items(), key=lambda kv: -kv[0]):
        if shown >= 5:
            break
        t = rec.tender
        ev = rec.last_ev
        left = t.expires - s.tick
        evs = f"EV ${_f(ev.ev)} ({_f(ev.ev_per_share, '{:.3f}')}/sh)" if ev else "EV -"
        state = rec.final or rec.decision or "NEW"
        live = f"{left:>3}s left" if not rec.final else "        "
        lines.append(f"  #{tid} {t.ticker} {t.action:4} {t.quantity:>7,} @ {t.price:<7} {live} {state:14} {evs}")
        if ev and not rec.final:
            lines.append(f"      {ev.reason[:92]}")
        if not t.valid:
            lines.append(f"      INVALID: {t.issue[:88]}")
        shown += 1
    if shown == 0:
        lines.append("  (none yet)")
    lines.append("-" * 100)
    lines.append(f"orders sent {e.orders_sent} | blocked {sum(e.blocked.values())} {dict(e.blocked) if e.blocked else ''}")
    for w in list(e.warnings)[-4:]:
        lines.append(f"! {w[:98]}")
    return "\n".join(lines)


def show(text: str) -> None:
    if os.name == "nt":
        os.system("cls")
        sys.stdout.write(text + "\n")
    else:
        sys.stdout.write("\x1b[H\x1b[2J" + text + "\n")
    sys.stdout.flush()
