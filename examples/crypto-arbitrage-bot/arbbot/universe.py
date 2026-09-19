"""Which markets to watch, and how each venue spells them.

`discover()` asks the venues (top-N Binance USDT markets by 24h quote volume,
plus the cross pairs that make triangles, plus the same assets' USD markets on
Coinbase and Kraken). `static_universe()` is the offline fallback: a snapshot
of that same discovery taken on 2026-09-19.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Config
from .filters import parse_binance_filters
from .models import BINANCE, COINBASE, KRAKEN, USD_FAMILY, Market

log = logging.getLogger(__name__)

USER_AGENT = "arbbot/0.1 (+https://github.com/msitarzewski/agency-agents)"

# Kraken's REST layer still uses legacy codes; its v2 WebSocket uses the modern ones.
KRAKEN_ASSET_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}

BINANCE_QUOTE_SUFFIXES = ("USDT", "FDUSD", "USDC", "BTC", "ETH", "BNB")

# Snapshot of `discover()` output on 2026-09-19: top Binance USDT markets by
# volume that also trade against USD on Coinbase and/or Kraken.
STATIC_BASES = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "TRX",
    "XLM", "BCH", "UNI", "NEAR", "APT", "ARB", "OP", "FIL", "AAVE", "INJ", "SUI", "TAO",
    "PEPE", "HBAR", "POL", "ETHFI", "PENDLE", "ENA", "WLD", "STRK", "ONDO", "TIA", "ZRO",
    "FET", "RAY", "CAKE", "PENGU", "MORPHO", "ZEC", "DASH", "PAXG", "TRUMP", "SKY", "COTI",
    "ASTER", "XPL", "PUMP", "PROVE", "REZ", "ZAMA", "SAGA", "ONE", "AR", "LSK", "SYN", "GENIUS",
]
STATIC_BINANCE_CROSSES = [
    "ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC", "ADABTC", "LTCBTC", "LINKBTC", "DOTBTC", "AVAXBTC",
    "DOGEBTC", "BCHBTC", "UNIBTC", "NEARBTC", "TRXBTC", "FILBTC", "AAVEBTC",
    "BNBETH", "SOLETH", "LINKETH", "XRPETH", "ADAETH", "LTCETH", "SOLBNB",
    "USDCUSDT",
]
# Assets from STATIC_BASES with no USD market on Coinbase Exchange (checked 2026-09-19).
STATIC_COINBASE_ONLY_MISSING = {"TRX", "SAGA", "ONE", "AR", "LSK", "SYN", "GENIUS"}


def split_binance_symbol(symbol: str) -> tuple[str, str] | None:
    for q in BINANCE_QUOTE_SUFFIXES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return None


def kraken_canonical(code: str) -> str:
    return KRAKEN_ASSET_ALIASES.get(code, code)


def _binance_markets(symbols: list[str], info: dict[str, dict] | None = None) -> list[Market]:
    out: list[Market] = []
    for s in symbols:
        s = s.upper()
        if info and s in info:
            si = info[s]
            f = parse_binance_filters(si)
            out.append(Market(BINANCE, s, si["baseAsset"], si["quoteAsset"], **f))
            continue
        split = split_binance_symbol(s)
        if split is None:
            log.warning("cannot split Binance symbol %s into base/quote; skipping", s)
            continue
        out.append(Market(BINANCE, s, split[0], split[1]))
    return out


def static_universe(cfg: Config) -> list[Market]:
    venues = cfg.enabled_venues()
    symbols = [s.upper() for s in cfg.universe.binance_symbols] or (
        [f"{b}USDT" for b in STATIC_BASES[: cfg.universe.top_n]] + STATIC_BINANCE_CROSSES
    )
    markets: list[Market] = []
    if BINANCE in venues:
        markets += _binance_markets(symbols)
    if cfg.universe.binance_symbols or BINANCE in venues:
        # follow the Binance list (possibly empty when it holds only crosses)
        bases = sorted({m.base for m in _binance_markets(symbols) if m.quote in USD_FAMILY and m.base not in USD_FAMILY})
    else:
        bases = STATIC_BASES[: cfg.universe.top_n]
    if COINBASE in venues:
        markets += [Market(COINBASE, f"{b}-USD", b, "USD") for b in bases if b not in STATIC_COINBASE_ONLY_MISSING]
        if cfg.universe.track_stable_rates:
            markets.append(Market(COINBASE, "USDT-USD", "USDT", "USD"))
    if KRAKEN in venues:
        markets += [Market(KRAKEN, f"{b}/USD", b, "USD") for b in bases]
        if cfg.universe.track_stable_rates:
            markets.append(Market(KRAKEN, "USDT/USD", "USDT", "USD"))
            markets.append(Market(KRAKEN, "USDC/USD", "USDC", "USD"))
    return _dedupe(markets)


def _dedupe(markets: list[Market]) -> list[Market]:
    seen: set[tuple[str, str]] = set()
    out = []
    for m in markets:
        if m.key in seen:
            continue
        seen.add(m.key)
        out.append(m)
    return out


async def _get_json(session: Any, url: str, **kw: Any) -> Any:
    async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, **kw) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def discover(cfg: Config, session: Any | None = None) -> list[Market]:
    """Ask the venues for their market lists; fall back to the static snapshot
    for any venue that cannot be reached."""
    import aiohttp  # local import so the offline code path needs no aiohttp

    venues = cfg.enabled_venues()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        markets: list[Market] = []
        binance_info: dict[str, dict] = {}
        if BINANCE in venues:
            try:
                info, tickers = await asyncio.gather(
                    _get_json(session, f"{cfg.venues.binance_rest}/api/v3/exchangeInfo"),
                    _get_json(session, f"{cfg.venues.binance_rest}/api/v3/ticker/24hr"),
                )
                binance_info = {
                    s["symbol"]: s
                    for s in info["symbols"]
                    if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
                }
                symbols = [s.upper() for s in cfg.universe.binance_symbols]
                if not symbols:
                    vol = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in tickers}
                    usdt = [s for s, si in binance_info.items() if si["quoteAsset"] == "USDT" and si["baseAsset"] not in USD_FAMILY]
                    usdt.sort(key=lambda s: vol.get(s, 0.0), reverse=True)
                    symbols = usdt[: cfg.universe.top_n]
                    bases = {binance_info[s]["baseAsset"] for s in symbols}
                    for b in sorted(bases):
                        for q in cfg.universe.triangle_quotes:
                            cross = f"{b}{q}"
                            if cross in binance_info and b != q:
                                symbols.append(cross)
                    # quote-vs-quote pairs close triangles like USDT->BTC->ETH->USDT
                    for q in cfg.universe.triangle_quotes:
                        if f"{q}USDT" in binance_info and f"{q}USDT" not in symbols:
                            symbols.append(f"{q}USDT")
                        for q2 in cfg.universe.triangle_quotes:
                            if q != q2 and f"{q}{q2}" in binance_info and f"{q}{q2}" not in symbols:
                                symbols.append(f"{q}{q2}")
                missing = [s for s in symbols if s not in binance_info]
                if missing:
                    log.warning("Binance: %d requested symbols are not trading: %s", len(missing), ", ".join(missing[:10]))
                markets += _binance_markets([s for s in symbols if s in binance_info], binance_info)
            except Exception as exc:
                log.warning("Binance discovery failed (%s: %s); using static list", type(exc).__name__, exc)
                markets += [m for m in static_universe(cfg) if m.venue == BINANCE]
        if BINANCE in venues:
            bases = sorted({m.base for m in markets if m.quote in USD_FAMILY and m.base not in USD_FAMILY})
        elif cfg.universe.binance_symbols:
            requested = _binance_markets([s.upper() for s in cfg.universe.binance_symbols])
            bases = sorted({m.base for m in requested if m.quote in USD_FAMILY and m.base not in USD_FAMILY})
        else:
            bases = STATIC_BASES[: cfg.universe.top_n]

        if COINBASE in venues:
            try:
                products = await _get_json(session, f"{cfg.venues.coinbase_rest}/products")
                online = {
                    p["id"]: p for p in products
                    if p.get("status") == "online" and p.get("quote_currency") == "USD" and not p.get("trading_disabled")
                }
                for b in bases:
                    pid = f"{b}-USD"
                    if pid in online:
                        p = online[pid]
                        markets.append(Market(COINBASE, pid, b, "USD",
                                              tick_size=_f(p.get("quote_increment")), step_size=_f(p.get("base_increment")),
                                              min_notional=_f(p.get("min_market_funds"))))
                if cfg.universe.track_stable_rates and "USDT-USD" in online:
                    markets.append(Market(COINBASE, "USDT-USD", "USDT", "USD"))
            except Exception as exc:
                log.warning("Coinbase discovery failed (%s: %s); using static list", type(exc).__name__, exc)
                markets += [m for m in static_universe(cfg) if m.venue == COINBASE]

        if KRAKEN in venues:
            try:
                pairs = (await _get_json(session, f"{cfg.venues.kraken_rest}/0/public/AssetPairs",
                                         params={"assetVersion": "1"}))["result"]
                by_ws: dict[str, dict] = {}
                for key, p in pairs.items():
                    if p.get("status", "online") != "online":
                        continue
                    if "/" in key:  # assetVersion=1 honoured: key is the WS v2 symbol
                        by_ws[key] = p
                        continue
                    ws = p.get("wsname")  # legacy naming fallback (XBT/USD, XDG/USD)
                    if not ws:
                        continue
                    b, q = ws.split("/")
                    by_ws[f"{kraken_canonical(b)}/{kraken_canonical(q)}"] = p
                for b in bases:
                    sym = f"{b}/USD"
                    if sym in by_ws:
                        p = by_ws[sym]
                        tick = _f(p.get("tick_size"))
                        if tick is None and "pair_decimals" in p:
                            tick = 10 ** -int(p["pair_decimals"])
                        markets.append(Market(KRAKEN, sym, b, "USD",
                                              tick_size=tick,
                                              step_size=10 ** -int(p["lot_decimals"]) if "lot_decimals" in p else None,
                                              min_qty=_f(p.get("ordermin")), min_notional=_f(p.get("costmin"))))
                if cfg.universe.track_stable_rates:
                    for stable in ("USDT", "USDC"):
                        if f"{stable}/USD" in by_ws:
                            markets.append(Market(KRAKEN, f"{stable}/USD", stable, "USD"))
            except Exception as exc:
                log.warning("Kraken discovery failed (%s: %s); using static list", type(exc).__name__, exc)
                markets += [m for m in static_universe(cfg) if m.venue == KRAKEN]
        return _dedupe(markets)
    finally:
        if own_session:
            await session.close()


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def summarize(markets: list[Market]) -> str:
    by_venue: dict[str, int] = {}
    for m in markets:
        by_venue[m.venue] = by_venue.get(m.venue, 0) + 1
    bases = {m.base for m in markets if m.quote in USD_FAMILY}
    return ", ".join(f"{v}={n}" for v, n in sorted(by_venue.items())) + f"; {len(bases)} assets"
