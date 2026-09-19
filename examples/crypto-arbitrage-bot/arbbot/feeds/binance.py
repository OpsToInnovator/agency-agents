"""Binance spot: combined `<symbol>@bookTicker` streams.

Payload (wrapped by the combined-stream endpoint):
{"stream":"btcusdt@bookTicker","data":{"u":123,"s":"BTCUSDT","b":"81024.00","B":"5.48","a":"81024.01","A":"0.37"}}
`u` is the order-book update id; bookTicker carries no timestamp.
"""
from __future__ import annotations

import json
import logging

from ..models import BINANCE, Quote
from .base import Feed, ReconnectRequested, loads

log = logging.getLogger(__name__)

MAX_STREAMS_PER_CONNECTION = 1024
# The mirror answers HTTP 414 once the URL passes ~16 KB (~800 streams); beyond
# this many streams we connect bare and SUBSCRIBE in batches instead.
URL_STREAM_LIMIT = 300
SUBSCRIBE_BATCH = 200


class BinanceFeed(Feed):
    venue = BINANCE
    # Binance closes streams after 24h; reconnect ourselves a little earlier.
    max_connection_s = 23 * 3600
    subscribe_interval_s = 0.3  # 5 client messages per second allowed

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_u: dict[str, int] = {}
        self.out_of_order = 0
        if len(self.markets) > MAX_STREAMS_PER_CONNECTION:
            dropped = list(self.markets)[MAX_STREAMS_PER_CONNECTION:]
            log.warning("binance: one connection carries at most %d streams; dropping %d markets: %s%s",
                        MAX_STREAMS_PER_CONNECTION, len(dropped), ", ".join(dropped[:8]), "..." if len(dropped) > 8 else "")
            for s in dropped:
                del self.markets[s]

    async def _session(self, sink) -> None:
        self._last_u.clear()  # update ids are per connection
        await super()._session(sink)

    def _streams(self) -> list[str]:
        return [f"{s.lower()}@bookTicker" for s in self.markets]

    def url(self) -> str:
        streams = self._streams()
        if len(streams) > URL_STREAM_LIMIT:
            return self.ws_url  # subscribe with frames instead (see subscribe_messages)
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}streams={'/'.join(streams)}"

    def subscribe_messages(self) -> list[str]:
        streams = self._streams()
        if len(streams) <= URL_STREAM_LIMIT:
            return []
        return [json.dumps({"method": "SUBSCRIBE", "params": streams[i:i + SUBSCRIBE_BATCH], "id": i // SUBSCRIBE_BATCH + 1})
                for i in range(0, len(streams), SUBSCRIBE_BATCH)]

    def parse(self, raw: str, recv_ts: float) -> list[Quote]:
        msg = loads(raw)
        if not isinstance(msg, dict):
            return []
        if "result" in msg and "id" in msg:  # SUBSCRIBE ack: {"result": null, "id": 1}
            if msg.get("error"):
                self.venue_error(f"subscribe failed: {msg['error']}")
            return []
        data = msg.get("data", msg)  # tolerate the un-wrapped single-stream form
        if not isinstance(data, dict):
            return []
        if data.get("e") == "serverShutdown" or msg.get("stream") == "!serverShutdown":
            raise ReconnectRequested("binance serverShutdown")
        symbol = data.get("s")
        if not symbol or "b" not in data:
            return []
        market = self.markets.get(symbol)
        if market is None:
            return []
        u = data.get("u")
        if isinstance(u, int):
            if u <= self._last_u.get(symbol, -1):
                self.out_of_order += 1
                return []
            self._last_u[symbol] = u
        return [
            Quote(
                venue=BINANCE,
                symbol=symbol,
                base=market.base,
                quote=market.quote,
                bid=float(data["b"]),
                bid_qty=float(data["B"]),
                ask=float(data["a"]),
                ask_qty=float(data["A"]),
                recv_ts=recv_ts,
            )
        ]
