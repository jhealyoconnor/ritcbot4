"""RIT DMA REST API client.

* Basic auth, one keep-alive requests.Session shared by a few worker threads.
* Timeouts on every call.
* 429: wait Retry-After (or the body's "wait") and retry; the adaptive limiter slows down.
* Dropped connections: reads, cancels and declines retry with backoff. Order sends and tender
  accepts are NEVER retried blindly (they may have gone through): they raise UncertainError so
  the caller can reconcile against the server first.
* 401: AuthError ("check username/password/port").
* Every request gets a sequence number when sent and when its answer is received. The order
  tracker uses these to know which fills a position snapshot can already contain.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests
from requests.adapters import HTTPAdapter


class AuthError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, body: str, path: str = ""):
        super().__init__(f"HTTP {status} on {path}: {body[:300]}")
        self.status = status
        self.body = body
        self.path = path


class TransportError(Exception):
    """The request could not be completed after retries (connection dropped / timeout)."""


class UncertainError(Exception):
    """A non-idempotent request (order send / tender accept) may or may not have been processed."""


@dataclass
class Result:
    data: Any
    sent: int
    recv: int
    latency: float


class Sequencer:
    def __init__(self) -> None:
        self._count = itertools.count(1)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            return next(self._count)


class AdaptiveRateLimiter:
    """Token bucket whose rate is learned from 429s (multiplicative decrease, additive increase)."""

    def __init__(self, rate: float, min_rate: float, max_rate: float, increase_per_sec: float,
                 decrease_factor: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.increase_per_sec = increase_per_sec
        self.decrease_factor = decrease_factor
        self.clock = clock
        self.sleep = sleep
        self.tokens = min(rate, 4.0)
        self.last = clock()
        self.blocked_until = 0.0
        self.last_429 = 0.0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = self.clock()
                if now - self.last_429 > 1.0:
                    self.rate = min(self.max_rate, self.rate + self.increase_per_sec * (now - self.last))
                self.tokens = min(max(self.rate * 0.25, 1.0), self.tokens + (now - self.last) * self.rate)
                self.last = now
                wait = max(0.0, self.blocked_until - now)
                if wait <= 0 and self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                if wait <= 0:
                    wait = (1.0 - self.tokens) / max(self.rate, 1e-6)
            self.sleep(min(wait, 1.0))

    def on_429(self, wait: float) -> None:
        with self.lock:
            now = self.clock()
            if now - self.last_429 > 0.5:          # parallel requests rejected together count once
                self.rate = max(self.min_rate, self.rate * self.decrease_factor)
            self.blocked_until = max(self.blocked_until, now + wait)
            self.last_429 = now
            self.tokens = 0.0


class RequestStats:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.count = 0
        self.errors = 0
        self.rate_limited = 0
        self.latency_sum = 0.0
        self.latency_ewma: Optional[float] = None
        self.recent: deque = deque()

    def record(self, latency: float, ok: bool) -> None:
        with self.lock:
            now = self.clock()
            self.count += 1
            if not ok:
                self.errors += 1
            self.latency_sum += latency
            self.latency_ewma = latency if self.latency_ewma is None else 0.8 * self.latency_ewma + 0.2 * latency
            self.recent.append(now)
            while self.recent and now - self.recent[0] > 5.0:
                self.recent.popleft()

    def per_second(self) -> float:
        with self.lock:
            now = self.clock()
            while self.recent and now - self.recent[0] > 5.0:
                self.recent.popleft()
            return len(self.recent) / 5.0

    def avg_latency(self) -> float:
        return self.latency_sum / self.count if self.count else 0.0


class RitClient:
    def __init__(self, base_url: str, user: str, password: str, conn_cfg: Any, req_cfg: Any,
                 seq: Optional[Sequencer] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (user, password)
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(4, int(conn_cfg.workers) * 2), max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.timeout = (float(conn_cfg.connect_timeout), float(conn_cfg.read_timeout))
        self.max_retries = int(conn_cfg.max_get_retries)
        self.max_429 = int(conn_cfg.max_429_retries)
        self.backoff = float(conn_cfg.backoff_base)
        self.seq = seq or Sequencer()
        self.clock = clock
        self.sleep = sleep
        self.limiter = AdaptiveRateLimiter(req_cfg.initial_rate, req_cfg.min_rate, req_cfg.max_rate,
                                           req_cfg.increase_per_sec, req_cfg.decrease_factor, clock, sleep)
        self.stats = RequestStats(clock)

    # ------------------------------------------------------------------ core
    def call(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
             idempotent: bool = True) -> Result:
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempts = 0
        limited = 0
        while True:
            self.limiter.acquire()
            sent = self.seq.next()
            t0 = self.clock()
            try:
                resp = self.session.request(method, url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.stats.record(self.clock() - t0, False)
                if not idempotent:
                    raise UncertainError(f"{method} {path}: {exc}") from exc
                attempts += 1
                if attempts > self.max_retries:
                    raise TransportError(f"{method} {path}: {exc}") from exc
                self.sleep(self.backoff * (2 ** (attempts - 1)))
                continue
            latency = self.clock() - t0
            recv = self.seq.next()
            if resp.status_code == 429:
                self.stats.record(latency, False)
                self.stats.rate_limited += 1
                wait = min(_retry_after(resp), 1.0)
                self.limiter.on_429(wait)
                limited += 1
                if limited > self.max_429:
                    raise ApiError(429, resp.text, path)
                self.sleep(wait)
                continue
            if resp.status_code == 401:
                self.stats.record(latency, False)
                raise AuthError("HTTP 401 Unauthorized: check username/password/port")
            if resp.status_code >= 500 and idempotent and attempts < self.max_retries:
                self.stats.record(latency, False)
                attempts += 1
                self.sleep(self.backoff * (2 ** (attempts - 1)))
                continue
            if not resp.ok:
                self.stats.record(latency, False)
                raise ApiError(resp.status_code, resp.text, path)
            self.stats.record(latency, True)
            try:
                data = resp.json()
            except ValueError:
                data = None
            return Result(data, sent, recv, latency)

    # ------------------------------------------------------------- endpoints
    def case(self) -> Result:
        return self.call("GET", "case")

    def trader(self) -> Result:
        return self.call("GET", "trader")

    def limits(self) -> Result:
        return self.call("GET", "limits")

    def securities(self) -> Result:
        return self.call("GET", "securities")

    def book(self, ticker: str, limit: int = 30) -> Result:
        return self.call("GET", "securities/book", {"ticker": ticker, "limit": limit})

    def tas(self, ticker: str, after: Optional[int] = None, limit: Optional[int] = None) -> Result:
        params: Dict[str, Any] = {"ticker": ticker, "limit": limit if limit is not None else (1000 if after is not None else 50)}
        if after is not None:
            params["after"] = after
        return self.call("GET", "securities/tas", params)

    def orders(self, status: str = "OPEN") -> Result:
        return self.call("GET", "orders", {"status": status})

    def order(self, order_id: int) -> Result:
        return self.call("GET", f"orders/{order_id}")

    def post_order(self, ticker: str, type_: str, quantity: int, action: str,
                   price: Optional[float] = None) -> Result:
        params: Dict[str, Any] = {"ticker": ticker, "type": type_, "quantity": int(quantity), "action": action}
        if type_ == "LIMIT":
            params["price"] = price
        return self.call("POST", "orders", params, idempotent=False)

    def cancel_order(self, order_id: int) -> Result:
        return self.call("DELETE", f"orders/{order_id}")

    def cancel_ticker(self, ticker: str) -> Result:
        return self.call("POST", "commands/cancel", {"ticker": ticker})

    def cancel_all(self) -> Result:
        return self.call("POST", "commands/cancel", {"all": 1})

    def cancel_ids(self, ids) -> Result:
        return self.call("POST", "commands/cancel", {"ids": ",".join(str(i) for i in ids)})

    def tenders(self) -> Result:
        return self.call("GET", "tenders")

    def accept_tender(self, tender_id: int, price: float) -> Result:
        return self.call("POST", f"tenders/{tender_id}", {"price": price}, idempotent=False)

    def decline_tender(self, tender_id: int) -> Result:
        return self.call("DELETE", f"tenders/{tender_id}")

    def transactions(self, after: Optional[int] = None, limit: int = 1000) -> Result:
        # live server: rows with id >= after, but only the latest `limit` (default 20), so ask for plenty
        params: Dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        return self.call("GET", "transactions", params)


def _retry_after(resp: requests.Response) -> float:
    try:
        header = resp.headers.get("Retry-After")
        if header:
            return max(0.05, float(header))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.05, float(resp.json()["wait"]))
    except (ValueError, KeyError, TypeError):
        return 1.0
