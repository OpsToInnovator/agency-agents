"""Coinbase Exchange: `ticker` channel on the public feed.

Every ticker message (sent on each trade) carries best_bid/best_ask and sizes:
{"type":"ticker","product_id":"BTC-USD","price":"81005.76","best_bid":"81005.75",
 "best_bid_size":"0.1","best_ask":"81005.76","best_ask_size":"0.2","time":"2026-09-19T05:03:20.696365Z",...}
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import COINBASE, Quote
from .base import Feed, loads

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _iso_ts(s: str | None) -> float | None:
    """Parse an RFC 3339 timestamp; tolerates 9 fractional digits and a Z suffix."""
    if not s:
        return None
    try:
        s = _FRACTION.sub(r"\1", s)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


class CoinbaseFeed(Feed):
    venue = COINBASE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_seq: dict[str, int] = {}
        self.out_of_order = 0

    async def _session(self, sink) -> None:
        self._last_seq.clear()  # sequence numbers are per connection
        await super()._session(sink)

    def subscribe_messages(self) -> list[str]:
        return [json.dumps({"type": "subscribe", "product_ids": list(self.markets), "channels": ["ticker"]})]

    def parse(self, raw: str, recv_ts: float) -> list[Quote]:
        msg = loads(raw)
        if not isinstance(msg, dict):
            return []
        mtype = msg.get("type")
        if mtype == "error":
            raise ValueError(f"coinbase error: {msg.get('message')} {msg.get('reason', '')}")
        if mtype != "ticker":
            return []
        market = self.markets.get(msg.get("product_id", ""))
        if market is None or "best_bid" not in msg:
            return []
        seq = msg.get("sequence")
        if isinstance(seq, int):
            if seq <= self._last_seq.get(market.symbol, -1):
                self.out_of_order += 1
                return []
            self._last_seq[market.symbol] = seq
        return [
            Quote(
                venue=COINBASE,
                symbol=market.symbol,
                base=market.base,
                quote=market.quote,
                bid=float(msg["best_bid"]),
                bid_qty=float(msg.get("best_bid_size", 0) or 0),
                ask=float(msg["best_ask"]),
                ask_qty=float(msg.get("best_ask_size", 0) or 0),
                recv_ts=recv_ts,
                exch_ts=_iso_ts(msg.get("time")),
            )
        ]

