"""Mock RIT DMA REST server over SimMarket: Basic auth, artificial latency, 429 rate limiting,
random dropped connections, real-time ticks.

    python -m sim.mock_server --port 9999 --tick-seconds 1.0 --latency 0.15
then in another terminal:
    python -m bot --host localhost --port 9999 --user BOT   (password: pass)
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .market import SimError, SimMarket, SimParams


class MockConfig:
    def __init__(self, user="BOT", password="pass", latency=0.0, jitter=0.0, rate_limit=0.0, drop_prob=0.0,
                 tick_seconds=1.0, seed=0, drop_after_prob=0.0) -> None:
        self.user, self.password = user, password
        self.latency, self.jitter = latency, jitter
        self.rate_limit = rate_limit          # requests/second allowed (0 = unlimited)
        self.drop_prob = drop_prob            # drop before the request is processed
        self.drop_after_prob = drop_after_prob  # process the request, then drop the response
        self.tick_seconds = tick_seconds
        self.rng = random.Random(seed)


class _Bucket:
    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.tokens = rate
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> float:
        """0 if allowed, else seconds to wait."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            return (1 - self.tokens) / self.rate


def make_handler(market: SimMarket, cfg: MockConfig, stats: dict):
    bucket = _Bucket(cfg.rate_limit) if cfg.rate_limit else None
    expected = "Basic " + base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True     # avoid 40 ms delayed-ACK stalls on keep-alive connections

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body, headers=None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            stats["requests"] = stats.get("requests", 0) + 1
            if cfg.latency or cfg.jitter:
                time.sleep(max(0.0, cfg.latency + cfg.rng.uniform(-cfg.jitter, cfg.jitter)))
            if self.headers.get("Authorization") != expected:
                return self._send(401, {"code": "UNAUTHORIZED"})
            if bucket is not None:
                wait = bucket.take()
                if wait > 0:
                    stats["429"] = stats.get("429", 0) + 1
                    return self._send(429, {"code": "RATE_LIMIT_EXCEEDED", "wait": round(wait, 3)},
                                      {"Retry-After": f"{wait:.3f}"})
            if cfg.drop_prob and cfg.rng.random() < cfg.drop_prob:
                stats["dropped"] = stats.get("dropped", 0) + 1
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            parts = [p for p in u.path.split("/") if p]
            if not parts or parts[0] != "v1":
                return self._send(404, {"code": "NOT_FOUND"})
            parts = parts[1:]
            tid = cfg.user
            m = market
            try:
                if method == "GET" and parts == ["case"]:
                    body = m.api_case()
                elif method == "GET" and parts == ["trader"]:
                    body = m.api_trader(tid)
                elif method == "GET" and parts == ["limits"]:
                    body = m.api_limits(tid)
                elif method == "GET" and parts == ["securities"]:
                    body = m.api_securities(tid)
                elif method == "GET" and parts == ["securities", "book"]:
                    body = m.api_book(q["ticker"], int(float(q.get("limit", 20))))
                elif method == "GET" and parts == ["securities", "tas"]:
                    body = m.api_tas(q["ticker"], q.get("after"), int(float(q.get("limit", 50))))
                elif method == "GET" and parts == ["orders"]:
                    body = m.api_orders(tid, q.get("status", "OPEN"))
                elif method == "POST" and parts == ["orders"]:
                    body = m.api_post_order(tid, q.get("ticker"), q.get("type"), q.get("quantity"), q.get("action"),
                                            q.get("price"))
                elif method == "GET" and len(parts) == 2 and parts[0] == "orders":
                    body = m.api_order(tid, int(parts[1]))
                elif method == "DELETE" and len(parts) == 2 and parts[0] == "orders":
                    body = m.api_cancel(tid, int(parts[1]))
                elif method == "POST" and parts == ["commands", "cancel"]:
                    body = m.api_cancel_bulk(tid, q.get("all"), q.get("ticker"), q.get("ids"))
                elif method == "GET" and parts == ["tenders"]:
                    body = m.api_tenders(tid)
                elif method == "POST" and len(parts) == 2 and parts[0] == "tenders":
                    body = m.api_accept(tid, int(parts[1]), q.get("price"))
                elif method == "DELETE" and len(parts) == 2 and parts[0] == "tenders":
                    body = m.api_decline(tid, int(parts[1]))
                elif method == "GET" and parts == ["transactions"]:
                    body = m.api_transactions(tid, q.get("after"), int(float(q.get("limit", 50))))
                else:
                    return self._send(404, {"code": "NOT_FOUND", "message": self.path})
            except SimError as exc:
                return self._send(exc.status, {"code": "ERROR", "message": exc.message})
            except (KeyError, ValueError) as exc:
                return self._send(400, {"code": "BAD_REQUEST", "message": str(exc)})
            if cfg.drop_after_prob and cfg.rng.random() < cfg.drop_after_prob:
                stats["dropped_after"] = stats.get("dropped_after", 0) + 1
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            return self._send(200, body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


class MockServer:
    """Runs the HTTP server and a ticking thread. start()/stop() for tests."""

    def __init__(self, market: SimMarket, cfg: MockConfig, port: int = 0, start_paused: bool = False) -> None:
        self.market = market
        self.cfg = cfg
        self.stats: dict = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(market, cfg, self.stats))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._stop = threading.Event()
        self.paused = start_paused
        self.threads = []

    def start(self) -> "MockServer":
        t1 = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t2 = threading.Thread(target=self._ticker, daemon=True)
        self.threads = [t1, t2]
        t1.start()
        t2.start()
        return self

    def _ticker(self) -> None:
        nxt = time.monotonic() + self.cfg.tick_seconds
        while not self._stop.is_set() and self.market.status == "ACTIVE":
            time.sleep(max(0.0, nxt - time.monotonic()))
            nxt += self.cfg.tick_seconds
            if not self.paused:
                self.market.step()

    def stop(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.mock_server")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tick-seconds", type=float, default=1.0)
    ap.add_argument("--latency", type=float, default=0.15)
    ap.add_argument("--jitter", type=float, default=0.05)
    ap.add_argument("--rate-limit", type=float, default=25.0)
    ap.add_argument("--drop-prob", type=float, default=0.0)
    ap.add_argument("--drop-after-prob", type=float, default=0.0)
    ap.add_argument("--user", default="BOT")
    ap.add_argument("--password", default="pass")
    ap.add_argument("--book-keys", default="bids", choices=["bids", "bid"])
    args = ap.parse_args(argv)
    params = SimParams(book_keys=args.book_keys)
    market = SimMarket(args.seed, params, bot_id=args.user)
    cfg = MockConfig(args.user, args.password, args.latency, args.jitter, args.rate_limit, args.drop_prob,
                     args.tick_seconds, args.seed, args.drop_after_prob)
    srv = MockServer(market, cfg, args.port).start()
    print(f"Mock RIT DMA server on http://localhost:{srv.port}/v1 (user {args.user} / {args.password}); "
          f"{params.ticks} ticks x {args.tick_seconds}s. Ctrl+C to stop.")
    try:
        while market.status == "ACTIVE":
            time.sleep(1)
        print(f"Heat over. Bot P&L {market.bot_pnl():,.0f}; end positions {market.end_positions}; "
              f"violations {market.violations}")
        time.sleep(5)
    except KeyboardInterrupt:
        pass
    srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
