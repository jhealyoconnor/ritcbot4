"""Command line: python -m bot [--check | --dry-run] [--port N] [--host H] ...

Port: --port, else RIT_PORT in .env / environment, else you are asked at startup.
Credentials: RIT_USER / RIT_PASS in .env (never in the repo), else asked at startup.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import time

from .api import ApiError, AuthError, RitClient, TransportError
from .config import REPO_ROOT, load_config, load_dotenv
from .display import render, show
from .engine import Engine
from .logs import HeatLogger
from .marketdata import parse_book, parse_tender


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bot", description="RITCx Liability Trading bot (DMA REST API)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="log in, print case status and raw samples, exit")
    mode.add_argument("--dry-run", action="store_true", help="live data, log what it WOULD do, send nothing")
    p.add_argument("--port", type=int, help="RIT DMA port (else RIT_PORT, else prompt)")
    p.add_argument("--host", help="RIT host (else RIT_HOST, else config)")
    p.add_argument("--user", help="trader id (else RIT_USER, else prompt)")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a config value")
    p.add_argument("--no-display", action="store_true", help="plain log lines instead of the live screen")
    p.add_argument("--once", action="store_true", help="exit after one heat ends")
    p.add_argument("--log-dir", help="log directory (default from config)")
    return p


def resolve_connection(args, cfg):
    load_dotenv()
    host = args.host or os.environ.get("RIT_HOST") or cfg.connection.host
    port = args.port or os.environ.get("RIT_PORT")
    if not port:
        port = input("RIT DMA port: ").strip()
    port = int(port)
    user = args.user or os.environ.get("RIT_USER") or input("Trader ID: ").strip()
    password = os.environ.get("RIT_PASS") or getpass.getpass("Password: ")
    return host, port, user, password


def run_check(client: RitClient, host: str, port: int, cfg) -> int:
    print(f"Connecting to http://{host}:{port}/v1 ...")
    try:
        case = client.case().data
        trader = client.trader().data
    except AuthError as exc:
        print(f"LOGIN FAILED: {exc}")
        return 1
    except (ApiError, TransportError) as exc:
        print(f"CONNECTION FAILED: {exc}\nCheck host/port and your internet connection.")
        return 1
    print(f"OK  port {port} | trader {trader.get('trader_id')} | NLV {trader.get('nlv')} | fines {trader.get('total_fines')}")
    print(f"CASE {case.get('name')} | status {case.get('status')} | tick {case.get('tick')} / "
          f"{case.get('ticks_per_period')} | period {case.get('period')}/{case.get('total_periods')} | "
          f"enforce limits {case.get('is_enforce_trading_limits')}")
    try:
        lims = client.limits().data
        print(f"LIMITS {json.dumps(lims)}")
        secs = client.securities().data
        for s in secs:
            print(f"  {s.get('ticker')}: pos {s.get('position')} bid {s.get('bid')} ask {s.get('ask')} "
                  f"max_trade_size {s.get('max_trade_size')} trading_fee {s.get('trading_fee')} "
                  f"rebate {s.get('limit_order_rebate')} decimals {s.get('quoted_decimals')} "
                  f"api_orders/s {s.get('api_orders_per_second')} exec_delay_ms {s.get('execution_delay_ms')}")
        for t in cfg.case.tickers:
            raw = client.book(t, 5).data
            b = parse_book(raw, t, trader.get("trader_id"))
            print(f"BOOK {t}: JSON keys {sorted(raw.keys()) if isinstance(raw, dict) else type(raw)} -> parsed as "
                  f"'{b.keys}', top bid {b.bids[:2]} top ask {b.asks[:2]}")
            first = (raw.get("bids") or raw.get("bid") or [None])[0] if isinstance(raw, dict) else None
            if first:
                print(f"  sample book entry: {json.dumps(first)}")
        tenders = client.tenders().data
        print(f"TENDERS pending: {len(tenders or [])}")
        for t in tenders or []:
            pt = parse_tender(t, list(cfg.case.tickers), str(cfg.tender.action_fallback))
            print(f"  raw: {json.dumps(t)}")
            print(f"  parsed: our action {pt.action} {pt.quantity} {pt.ticker} @ {pt.price}, action field = "
                  f"{pt.field_perspective}, valid {pt.valid} {pt.issue}")
        print(f"Open orders: {len(client.orders('OPEN').data or [])}")
    except (ApiError, TransportError) as exc:
        print(f"read failed: {exc}")
        return 1
    print(f"Average latency {client.stats.avg_latency() * 1000:.0f} ms over {client.stats.count} requests. CHECK OK.")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, set_args=args.set)
    host, port, user, password = resolve_connection(args, cfg)
    base = f"http://{host}:{port}/v1"
    client = RitClient(base, user, password, cfg.connection, cfg.requests)
    if args.check:
        return run_check(client, host, port, cfg)

    mode = "dry" if args.dry_run else "live"
    logger = HeatLogger(args.log_dir or os.path.join(REPO_ROOT, cfg.logging.dir), tag=mode)
    engine = Engine(client, cfg, mode=mode, logger=logger, stop_dir=os.getcwd())
    use_display = cfg.display.enabled and not args.no_display

    presses = {"n": 0}

    def on_sigint(signum, frame):
        presses["n"] += 1
        engine.request_kill("flatten" if presses["n"] == 1 else "now")
        if presses["n"] >= 3:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_sigint)

    def heat_end(summary, text):
        print("\n" + text)
        print(f"Logs: {logger.dir}")

    engine.on_heat_end = heat_end
    print(f"{mode.upper()} mode on {base} as {user}. Logs: {logger.dir}")
    print("Ctrl+C once = flatten safely then exit; twice = cancel all orders and exit now. "
          "Or create a file named STOP / STOP_NOW in this folder.")
    try:
        engine.startup()
    except AuthError as exc:
        print(f"LOGIN FAILED: {exc}")
        return 1
    last_draw = 0.0
    last_status = None
    try:
        while True:
            t0 = time.monotonic()
            try:
                status = engine.run_once()
            except AuthError as exc:
                print(f"\nAUTH FAILED: {exc}")
                return 1
            if engine.exit_requested:
                if not engine.dry and engine.kill == "now":
                    engine._cancel_all("exit")
                break
            if status != "ACTIVE":
                if status != last_status:
                    print(f"Case is {status}. Waiting for it to be ACTIVE (port {port})...")
                last_status = status
                if args.once and status == "STOPPED" and engine.summaries:
                    break
                time.sleep(0.5)
                continue
            last_status = status
            now = time.monotonic()
            if use_display and now - last_draw >= float(cfg.display.refresh_seconds):
                show(render(engine, port, mode))
                last_draw = now
            elif not use_display and engine.loop % 8 == 0:
                print(f"tick {engine.snap.tick} pos {engine.positions()} frozen {sorted(engine.frozen)} "
                      f"pnl {engine.heat_pnl()} warn {list(engine.warnings)[-1:] }")
            time.sleep(max(0.0, float(cfg.requests.loop_period) - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        if not engine.dry:
            engine._cancel_all("keyboard interrupt")
    if engine.heat_started:
        summary, text = engine.summary("exit")
        logger.write_summary(summary, text)
        print("\n" + text)
        if any(v != 0 for v in engine.positions().values()):
            print("WARNING: positions are NOT flat. Flatten them in the RIT client now.")
    print(f"Logs: {logger.dir}")
    logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
