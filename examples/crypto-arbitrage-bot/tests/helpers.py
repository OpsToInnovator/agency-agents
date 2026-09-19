"""Small builders shared by the tests."""
from __future__ import annotations

from arbbot.models import BINANCE, COINBASE, KRAKEN, Market, Quote


def market(venue: str, symbol: str, base: str, quote: str, **kw) -> Market:
    return Market(venue, symbol, base, quote, **kw)


BTC_BINANCE = market(BINANCE, "BTCUSDT", "BTC", "USDT", tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=5.0)
ETH_BINANCE = market(BINANCE, "ETHUSDT", "ETH", "USDT", tick_size=0.01, step_size=0.0001, min_qty=0.0001, min_notional=5.0)
ETHBTC_BINANCE = market(BINANCE, "ETHBTC", "ETH", "BTC", tick_size=0.00001, step_size=0.0001, min_qty=0.0001, min_notional=0.0001)
BTC_COINBASE = market(COINBASE, "BTC-USD", "BTC", "USD")
BTC_KRAKEN = market(KRAKEN, "BTC/USD", "BTC", "USD")
USDT_COINBASE = market(COINBASE, "USDT-USD", "USDT", "USD")


def quote(m: Market, bid: float, ask: float, bid_qty: float = 1.0, ask_qty: float = 1.0, ts: float = 1000.0) -> Quote:
    return Quote(m.venue, m.symbol, m.base, m.quote, bid, bid_qty, ask, ask_qty, ts)
