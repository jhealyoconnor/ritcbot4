"""Per-heat logs under logs/<timestamp>/: events.jsonl, ticks.jsonl, orders.csv, decisions.csv, summary.*"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import Any, Dict, Optional


def _default(o: Any):
    if hasattr(o, "__dict__"):
        return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
    return str(o)


class HeatLogger:
    ORDER_FIELDS = ["wall", "tick", "event", "local_id", "order_id", "ticker", "action", "type", "qty", "price",
                    "purpose", "tender_ids", "status", "filled", "vwap", "reason", "latency_ms"]
    DECISION_FIELDS = ["wall", "tick", "tender_id", "ticker", "action", "quantity", "price", "expires", "decision",
                       "ev", "ev_per_share", "threshold", "reason", "ref", "later_px", "imm_qty", "later_qty",
                       "dump_qty", "sigma", "refill", "competitors", "pos_before", "pos_after"]

    def __init__(self, root: str, tag: str = "", enabled: bool = True) -> None:
        self.enabled = enabled
        self.dir: Optional[str] = None
        if not enabled:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(root, stamp + (f"-{tag}" if tag else ""))
        n = 1
        while os.path.exists(self.dir):
            n += 1
            self.dir = os.path.join(root, f"{stamp}-{n}" + (f"-{tag}" if tag else ""))
        os.makedirs(self.dir, exist_ok=True)
        self._events = open(os.path.join(self.dir, "events.jsonl"), "a", buffering=1)
        self._ticks = open(os.path.join(self.dir, "ticks.jsonl"), "a", buffering=1)
        self._orders_fh = open(os.path.join(self.dir, "orders.csv"), "a", newline="", buffering=1)
        self._orders = csv.DictWriter(self._orders_fh, self.ORDER_FIELDS, extrasaction="ignore")
        self._orders.writeheader()
        self._dec_fh = open(os.path.join(self.dir, "decisions.csv"), "a", newline="", buffering=1)
        self._dec = csv.DictWriter(self._dec_fh, self.DECISION_FIELDS, extrasaction="ignore")
        self._dec.writeheader()

    def event(self, kind: str, tick: Optional[int] = None, **data: Any) -> None:
        if not self.enabled:
            return
        rec = {"wall": round(time.time(), 3), "tick": tick, "event": kind}
        rec.update(data)
        self._events.write(json.dumps(rec, default=_default) + "\n")

    def tick(self, record: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        record.setdefault("wall", round(time.time(), 3))
        self._ticks.write(json.dumps(record, default=_default) + "\n")

    def order(self, **row: Any) -> None:
        if not self.enabled:
            return
        row.setdefault("wall", round(time.time(), 3))
        self._orders.writerow(row)

    def decision(self, **row: Any) -> None:
        if not self.enabled:
            return
        row.setdefault("wall", round(time.time(), 3))
        self._dec.writerow(row)

    def write_summary(self, summary: Dict[str, Any], text: str) -> None:
        if not self.enabled:
            return
        with open(os.path.join(self.dir, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2, default=_default)
        with open(os.path.join(self.dir, "summary.txt"), "w") as fh:
            fh.write(text)

    def close(self) -> None:
        if not self.enabled:
            return
        for fh in (self._events, self._ticks, self._orders_fh, self._dec_fh):
            try:
                fh.close()
            except Exception:
                pass
        self.enabled = False
