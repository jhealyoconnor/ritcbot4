"""API client failure modes against a scripted local HTTP server: 429, 401, dropped connections."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bot.api import ApiError, AuthError, RitClient, TransportError, UncertainError
from bot.config import load_config

CFG = load_config(overrides={"connection.backoff_base": 0.01, "connection.max_get_retries": 3})


class Script:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []


def serve(script):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _do(self):
            script.calls.append((self.command, self.path, self.headers.get("Authorization")))
            step = script.steps.pop(0) if script.steps else ("200", {"ok": True})
            kind, body = step
            if kind == "drop":
                self.close_connection = True
                self.connection.shutdown(2)
                return
            code = int(kind)
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if code == 429:
                self.send_header("Retry-After", "0.05")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def make():
    servers = []

    def _make(steps):
        script = Script(steps)
        srv = serve(script)
        servers.append(srv)
        client = RitClient(f"http://127.0.0.1:{srv.server_address[1]}/v1", "trader", "pw", CFG.connection, CFG.requests)
        return client, script

    yield _make
    for s in servers:
        s.shutdown()
        s.server_close()


def test_basic_auth_header_and_ok(make):
    c, s = make([("200", {"tick": 5, "status": "ACTIVE"})])
    r = c.case()
    assert r.data["tick"] == 5 and r.recv > r.sent
    assert s.calls[0][2].startswith("Basic ")


def test_429_waits_and_retries_and_slows_down(make):
    c, s = make([("429", {"code": "RATE_LIMIT_EXCEEDED", "wait": 0.05}), ("200", {"tick": 1})])
    rate0 = c.limiter.rate
    r = c.case()
    assert r.data == {"tick": 1} and len(s.calls) == 2
    assert c.limiter.rate < rate0 and c.stats.rate_limited == 1


def test_401_raises_auth_error(make):
    c, _ = make([("401", {"code": "UNAUTHORIZED"})])
    with pytest.raises(AuthError, match="username/password/port"):
        c.case()


def test_dropped_connection_get_retries(make):
    c, s = make([("drop", None), ("drop", None), ("200", {"tick": 9})])
    assert c.case().data == {"tick": 9} and len(s.calls) == 3


def test_dropped_connection_get_gives_up(make):
    c, _ = make([("drop", None)] * 5)
    with pytest.raises(TransportError):
        c.case()


def test_order_send_never_retried_blindly(make):
    c, s = make([("drop", None), ("200", {"order_id": 1})])
    with pytest.raises(UncertainError):
        c.post_order("CRZY", "LIMIT", 100, "SELL", 9.9)
    assert len(s.calls) == 1


def test_tender_accept_never_retried_blindly_and_sends_price(make):
    c, s = make([("drop", None)])
    with pytest.raises(UncertainError):
        c.accept_tender(7, 9.75)
    assert s.calls[0][0] == "POST" and "/tenders/7" in s.calls[0][1] and "price=9.75" in s.calls[0][1]


def test_decline_is_delete(make):
    c, s = make([("200", {"success": True})])
    c.decline_tender(7)
    assert s.calls[0][0] == "DELETE" and s.calls[0][1].endswith("/tenders/7")


def test_http_error_raises_api_error(make):
    c, _ = make([("400", {"code": "BAD", "message": "quantity exceeds max"})])
    with pytest.raises(ApiError) as ei:
        c.post_order("CRZY", "MARKET", 30000, "SELL")
    assert ei.value.status == 400
