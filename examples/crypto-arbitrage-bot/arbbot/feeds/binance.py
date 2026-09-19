"""Binance spot: combined `<symbol>@bookTicker` streams.

Payload (wrapped by the combined-stream endpoint):
{"stream":"btcusdt@bookTicker","data":{"u":123,"s":"BTCUSDT","b":"81024.00","B":"5.48","a":"81024.01","A":"0.37"}}
`u` is the order-book update id; bookTicker carries no timestamp.
"""
from __future__ import annotations

from ..models import BINANCE, Quote
from .base import Feed, ReconnectRequested, loads

MAX_STREAMS_PER_CONNECTION = 1024


class BinanceFeed(Feed):
    venue = BINANCE
    # Binance closes streams after 24h; reconnect ourselves a little earlier.
    max_connection_s = 23 * 3600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_u: dict[str, int] = {}
        self.out_of_order = 0

    async def _session(self, sink) -> None:
        self._last_u.clear()  # update ids are per connection
        await super()._session(sink)

    def url(self) -> str:
        symbols = list(self.markets)[:MAX_STREAMS_PER_CONNECTION]
        streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}streams={streams}"

    def parse(self, raw: str, recv_ts: float) -> list[Quote]:
        msg = loads(raw)
        if not isinstance(msg, dict):
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
