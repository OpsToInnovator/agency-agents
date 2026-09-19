"""Kraken WebSocket v2: `ticker` channel with `event_trigger: "bbo"` so we get a
message whenever the best bid/offer changes, not only on trades.

{"channel":"ticker","type":"update","data":[{"symbol":"BTC/USD","bid":81006.2,"bid_qty":0.28,
 "ask":81006.3,"ask_qty":0.0016,"last":81006.2,...}]}
Heartbeats arrive as {"channel":"heartbeat"} roughly once a second.
"""
from __future__ import annotations

import json
import logging

from ..models import KRAKEN, Quote
from .base import Feed, loads
from .coinbase import _iso_ts

log = logging.getLogger(__name__)


class KrakenFeed(Feed):
    venue = KRAKEN

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("idle_timeout_s", 10.0)  # heartbeat arrives every 1.00s
        super().__init__(*args, **kwargs)
        self.system_status = "unknown"

    def subscribe_messages(self) -> list[str]:
        return [
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": list(self.markets), "event_trigger": "bbo", "snapshot": True},
                }
            )
        ]

    def parse(self, raw: str, recv_ts: float) -> list[Quote]:
        msg = loads(raw)
        if not isinstance(msg, dict):
            return []
        if msg.get("method") == "subscribe" and msg.get("success") is False:
            self.venue_error(f"subscribe failed: {msg.get('error')}", msg.get("symbol"))
            return []
        channel = msg.get("channel")
        if channel == "status":
            for d in msg.get("data", []):
                status = d.get("system", "unknown")
                if status != self.system_status:
                    (log.info if status == "online" else log.warning)("kraken system status: %s", status)
                    self.system_status = status
            return []
        if channel != "ticker":
            return []
        out: list[Quote] = []
        for d in msg.get("data", []):
            market = self.markets.get(d.get("symbol", ""))
            if market is None:
                self.unknown_symbols += 1
                continue
            if "bid" not in d or "ask" not in d:
                continue
            out.append(
                Quote(
                    venue=KRAKEN,
                    symbol=market.symbol,
                    base=market.base,
                    quote=market.quote,
                    bid=float(d["bid"]),
                    bid_qty=float(d.get("bid_qty", 0) or 0),
                    ask=float(d["ask"]),
                    ask_qty=float(d.get("ask_qty", 0) or 0),
                    recv_ts=recv_ts,
                    exch_ts=_iso_ts(d.get("timestamp")),
                )
            )
        return out
