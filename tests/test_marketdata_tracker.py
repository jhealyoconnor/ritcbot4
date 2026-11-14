"""Parsing (book keys, own orders, tender captions) and the order tracker's accounting."""
from bot.marketdata import parse_book, parse_caption, parse_tender
from bot.tracker import OrderTracker

CAP_BUY = ("An institution would like to SELL 75,000 shares of CRZY to you at a price of $9.75. "
           "Would you like to BUY the shares from them?")
CAP_SELL = ("An institution would like to BUY 40,000 shares of TAME from you at a price of $25.31. "
            "Would you like to SELL the shares to them?")


def entry(price, qty, trader="ANON", filled=0):
    return {"price": price, "quantity": qty, "quantity_filled": filled, "trader_id": trader}


def test_book_accepts_both_key_styles_and_excludes_own():
    raw = {"bids": [entry(9.98, 8000), entry(9.98, 1000, "ME"), entry(9.95, 25000, filled=5000)],
           "asks": [entry(10.0, 5000), entry(10.01, 2000, "ME")]}
    b = parse_book(raw, "CRZY", "ME")
    assert b.keys == "bids/asks"
    assert b.bids == [(9.98, 8000), (9.95, 20000)]
    assert b.asks == [(10.0, 5000)]
    assert b.own_bids == [(9.98, 1000)]
    b2 = parse_book({"bid": raw["bids"], "ask": raw["asks"]}, "CRZY", "ME")
    assert b2.keys == "bid/ask" and b2.bids == b.bids


def test_anon_only():
    raw = {"bids": [entry(9.98, 8000), entry(9.97, 3000, "trader7")], "asks": []}
    assert parse_book(raw, "CRZY", "ME", anon_only=True).bids == [(9.98, 8000)]
    assert parse_book(raw, "CRZY", "ME", anon_only=False).bids == [(9.98, 8000), (9.97, 3000)]


def test_caption_parse():
    c = parse_caption(CAP_BUY)
    assert c["our_action"] == "BUY" and c["quantity"] == 75000 and c["ticker"] == "CRZY" and c["price"] == 9.75
    c = parse_caption(CAP_SELL)
    assert c["our_action"] == "SELL" and c["ticker"] == "TAME" and c["price"] == 25.31


def _raw(**kw):
    base = {"tender_id": 5, "tick": 10, "expires": 40, "caption": CAP_BUY, "quantity": 75000, "action": "BUY",
            "is_fixed_bid": True, "price": 9.75, "ticker": "CRZY"}
    base.update(kw)
    return base


def test_tender_trader_perspective():
    t = parse_tender(_raw(), ["CRZY", "TAME"])
    assert t.valid and t.action == "BUY" and t.signed_qty == 75000 and t.field_perspective == "trader"


def test_tender_institution_perspective_uses_caption():
    t = parse_tender(_raw(action="SELL"), ["CRZY", "TAME"])
    assert t.valid and t.action == "BUY" and t.field_perspective == "institution"


def test_tender_without_ticker_field():
    raw = _raw()
    del raw["ticker"]
    t = parse_tender(raw, ["CRZY", "TAME"])
    assert t.valid and t.ticker == "CRZY"


def test_tender_mismatch_is_invalid():
    assert not parse_tender(_raw(quantity=70000), ["CRZY", "TAME"]).valid
    assert not parse_tender(_raw(price=9.80), ["CRZY", "TAME"]).valid
    assert not parse_tender(_raw(ticker="TAME"), ["CRZY", "TAME"]).valid
    assert not parse_tender(_raw(is_fixed_bid=False), ["CRZY", "TAME"]).valid


def test_tender_no_caption_falls_back():
    t = parse_tender(_raw(caption=""), ["CRZY", "TAME"], "trader")
    assert t.valid and t.action == "BUY"
    t = parse_tender(_raw(caption=""), ["CRZY", "TAME"], "institution")
    assert t.valid and t.action == "SELL"


def test_tracker_vanished_order_is_terminal():
    tr = OrderTracker()
    o = tr.new("CRZY", "SELL", 10000, "LIMIT", 9.9, "x", ("101",), 1, sent=1)
    tr.on_post_ok(o, {"order_id": 9, "status": "OPEN", "quantity_filled": 0}, recv=2)
    tr.on_open_list([], sent=3, recv=4)
    assert o.status == "VANISHED" and not o.final_known
    assert tr.committed("CRZY", "SELL", pos_sent=5) == 0
    assert tr.committed("CRZY", "SELL", pos_sent=4) == 10000      # snapshot older than the observation
    assert tr.needs_final_fill() == [o]
    fills = tr.on_order({"order_id": 9, "status": "TRANSACTED", "quantity_filled": 10000, "vwap": 9.91}, recv=6)
    assert fills and fills[0][1] == 10000 and abs(fills[0][2] - 99100) < 1e-6
    assert tr.needs_final_fill() == []


def test_tracker_uncertain_send_adopted_or_vanished():
    tr = OrderTracker()
    o = tr.new("CRZY", "SELL", 5000, "LIMIT", 9.9, "x", (), 1, sent=1)
    tr.on_post_uncertain(o, recv=2)
    assert tr.committed("CRZY", "SELL", 3) == 5000
    tr.on_open_list([{"order_id": 44, "ticker": "CRZY", "action": "SELL", "quantity": 5000,
                      "quantity_filled": 1000, "status": "OPEN"}], sent=3, recv=4)
    assert o.order_id == 44 and o.filled == 1000
    o2 = tr.new("CRZY", "SELL", 3000, "MARKET", None, "x", (), 1, sent=5)
    tr.on_post_uncertain(o2, recv=6)
    tr.on_open_list([{"order_id": 44, "ticker": "CRZY", "action": "SELL", "quantity": 5000,
                      "quantity_filled": 1000, "status": "OPEN"}], sent=7, recv=8)
    assert o2.status == "VANISHED"
    assert tr.committed("CRZY", "SELL", 9) == 4000


def test_tracker_foreign_orders_count():
    tr = OrderTracker()
    tr.on_open_list([{"order_id": 1, "ticker": "TAME", "action": "BUY", "quantity": 3000, "quantity_filled": 500}],
                    sent=1, recv=2)
    assert tr.committed("TAME", "BUY", 3) == 2500 and tr.has_live("TAME")
