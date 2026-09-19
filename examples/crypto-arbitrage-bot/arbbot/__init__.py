"""arbbot: an honest crypto mispricing scanner and paper-trading bot.

It watches top-of-book quotes for dozens of markets on Binance, Coinbase and
Kraken, looks for cross-exchange spreads, triangular cycles and quote
anomalies, prices every opportunity net of taker fees, and paper-trades the
ones that survive. Live trading is opt-in, gated, and limited to Binance.
"""

__version__ = "0.1.0"
