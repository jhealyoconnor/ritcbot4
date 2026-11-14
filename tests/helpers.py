from bot.gate import GateContext


def ctx(positions, committed=None, inventory=None, pending=(), status="ACTIVE", net=100000, gross=250000):
    committed = committed or {}
    inventory = inventory if inventory is not None else {t: (1 if p > 0 else -1 if p < 0 else 0)
                                                         for t, p in positions.items()}
    return GateContext(
        status=status, positions=dict(positions),
        committed=lambda t, a: committed.get((t, a), 0),
        has_inventory=lambda t, s: inventory.get(t, 0) == s and s != 0,
        pending_tickers=set(pending), max_order={"CRZY": 25000, "TAME": 10000},
        tick_size={"CRZY": 0.01, "TAME": 0.01}, net_limit=net, gross_limit=gross, tickers=("CRZY", "TAME"))


BRIEF_BIDS = [(9.98, 8000), (9.95, 25000), (9.90, 7000), (9.88, 10000), (9.84, 15000)]
BRIEF_ASKS = [(10.00, 5000), (10.03, 3000), (10.07, 2000), (10.42, 18000), (10.45, 25000)]
