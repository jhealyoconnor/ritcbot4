"""The compliance gate: the most important tests in the repo."""
from bot.gate import OrderRequest, check
from bot.tracker import OrderTracker

from helpers import ctx


def req(ticker="CRZY", action="SELL", qty=10000, type_="LIMIT", price=10.00):
    return OrderRequest(ticker, action, qty, type_, price if type_ == "LIMIT" else None, "test")


def test_allows_legitimate_unwind_of_long():
    ok, why = check(req(), ctx({"CRZY": 50000, "TAME": 0}))
    assert ok, why


def test_allows_legitimate_unwind_of_short_market_order():
    ok, why = check(req("TAME", "BUY", 10000, "MARKET"), ctx({"CRZY": 0, "TAME": -30000}))
    assert ok, why


def test_blocks_without_tender_inventory():
    ok, why = check(req(), ctx({"CRZY": 50000, "TAME": 0}, inventory={"CRZY": 0}))
    assert not ok and "tender inventory" in why


def test_blocks_when_flat():
    ok, why = check(req(), ctx({"CRZY": 0, "TAME": 0}))
    assert not ok and "no position" in why


def test_blocks_wrong_direction_long():
    ok, why = check(req(action="BUY"), ctx({"CRZY": 50000, "TAME": 0}))
    assert not ok and "increase" in why


def test_blocks_wrong_direction_short():
    ok, why = check(req("TAME", "SELL", 5000, price=25.0), ctx({"CRZY": 0, "TAME": -20000}))
    assert not ok and "increase" in why


def test_blocks_order_larger_than_position():
    ok, why = check(req(qty=20000), ctx({"CRZY": 15000, "TAME": 0}))
    assert not ok and "cross zero" in why


def test_blocks_crossing_zero_through_resting_orders():
    c = ctx({"CRZY": 50000, "TAME": 0}, committed={("CRZY", "SELL"): 30000})
    assert check(req(qty=20000), c)[0]
    ok, why = check(req(qty=20001), c)
    assert not ok and "cross zero" in why


def test_blocks_crossing_zero_through_resting_orders_plus_partial_fill():
    """Snapshot says +50k. A resting 30k sell has partly filled (10k) AFTER the snapshot was taken:
    the gate must still count the full 30k, so at most 20k more may be sent."""
    tr = OrderTracker()
    o = tr.new("CRZY", "SELL", 30000, "LIMIT", 10.0, "test", (), 1, sent=1)
    tr.on_post_ok(o, {"order_id": 7, "status": "OPEN", "quantity_filled": 0}, recv=2)
    pos_sent = 3                       # position request sent here (shows +50,000)
    tr.on_order({"order_id": 7, "status": "OPEN", "quantity_filled": 10000}, recv=4)   # fill seen after
    committed = tr.committed("CRZY", "SELL", pos_sent)
    assert committed == 30000
    c = ctx({"CRZY": 50000, "TAME": 0}, committed={("CRZY", "SELL"): committed})
    assert check(req(qty=20000), c)[0]
    assert not check(req(qty=20001), c)[0]
    # next loop: position snapshot (sent at 5) shows +40,000 and the 10k fill is reflected
    committed2 = tr.committed("CRZY", "SELL", 5)
    assert committed2 == 20000
    c2 = ctx({"CRZY": 40000, "TAME": 0}, committed={("CRZY", "SELL"): committed2})
    assert check(req(qty=20000), c2)[0]
    assert not check(req(qty=20001), c2)[0]


def test_unconfirmed_order_counts_in_full():
    tr = OrderTracker()
    tr.new("CRZY", "SELL", 25000, "MARKET", None, "flatten", (), 1, sent=10)   # no response yet
    assert tr.committed("CRZY", "SELL", pos_sent=11) == 25000
    assert tr.committed("CRZY", "SELL", pos_sent=5) == 25000


def test_blocks_over_max_order_size():
    assert not check(req(qty=25001), ctx({"CRZY": 80000, "TAME": 0}))[0]
    ok, why = check(req("TAME", "SELL", 10001, price=25.0), ctx({"CRZY": 0, "TAME": 80000}))
    assert not ok and "max order" in why
    assert check(req("TAME", "SELL", 10000, price=25.0), ctx({"CRZY": 0, "TAME": 80000}))[0]


def test_blocks_any_order_in_stock_with_pending_tender():
    c = ctx({"CRZY": 50000, "TAME": -20000}, pending={"CRZY"})
    ok, why = check(req(), c)
    assert not ok and "FROZEN" in why
    ok, why = check(req(type_="MARKET"), c)
    assert not ok and "FROZEN" in why
    assert check(req("TAME", "BUY", 5000, price=25.0), c)[0]       # other stock is not frozen


def test_blocks_when_case_not_active():
    assert not check(req(), ctx({"CRZY": 50000, "TAME": 0}, status="PAUSED"))[0]


def test_blocks_off_grid_price_and_bad_quantity():
    assert not check(req(price=10.005), ctx({"CRZY": 50000, "TAME": 0}))[0]
    assert not check(req(qty=0), ctx({"CRZY": 50000, "TAME": 0}))[0]
    assert not check(OrderRequest("CRZY", "SELL", 100.5, "LIMIT", 10.0), ctx({"CRZY": 50000, "TAME": 0}))[0]


def test_limit_check_with_open_orders_net_trap():
    """CRZY +120k / TAME -40k (net 80k): buying back TAME raises net. With 25k TAME buys already
    resting, another 10k would allow net 115k > 100k."""
    c = ctx({"CRZY": 120000, "TAME": -40000}, committed={("TAME", "BUY"): 25000})
    ok, why = check(req("TAME", "BUY", 10000, price=25.0), c)
    assert not ok and "net" in why
    c2 = ctx({"CRZY": 120000, "TAME": -40000}, committed={("TAME", "BUY"): 5000})
    assert check(req("TAME", "BUY", 10000, price=25.0), c2)[0]


def test_limit_check_allows_reducing_when_already_outside():
    c = ctx({"CRZY": 110000, "TAME": 0})       # somehow over net: reducing must still be allowed
    assert check(req(qty=25000), c)[0]
