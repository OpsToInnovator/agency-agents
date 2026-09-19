"""Command line: scan (live data, paper or live orders), replay, record, markets."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from typing import Any

from . import __version__
from .config import Config, ConfigError, load_config
from .detectors import AnomalyDetector, CrossExchangeDetector, TriangularDetector
from .engine import Clock, Engine
from .execution import BinanceLiveExecutor, PaperExecutor, RiskManager
from .execution.live import LiveDisabled
from .feeds import BinanceFeed, CoinbaseFeed, KrakenFeed, ReplayFeed
from .fees import FeeSchedule
from .models import BINANCE, COINBASE, KRAKEN, Market
from .quotes import QuoteBook
from .report import Reporter
from .universe import discover, static_universe, summarize

log = logging.getLogger("arbbot")

REAL_ORDERS_FLAG = "--i-know-this-sends-real-orders"


def build_components(cfg: Config, markets: list[Market], clock: Clock, live: bool = False,
                     real_orders: bool = False) -> tuple[QuoteBook, list, RiskManager, Any, FeeSchedule]:
    fees = FeeSchedule(cfg.venues.taker_fee_bps)
    book = QuoteBook(max_age_s=cfg.detection.max_quote_age_ms / 1000.0,
                     max_age_by_venue={k: v / 1000.0 for k, v in cfg.detection.max_quote_age_ms_by_venue.items()},
                     anchor_venue=cfg.detection.anchor_venue)
    for m in markets:
        book.register(m)
    detectors: list = []
    if cfg.detection.cross_exchange:
        detectors.append(CrossExchangeDetector(fees, cfg.detection, cfg.risk.max_notional_per_trade_usd))
    if cfg.detection.triangular:
        binance_markets = [m for m in markets if m.venue == BINANCE]
        if binance_markets:
            tri = TriangularDetector(binance_markets, fees, cfg.detection, cfg.risk.max_notional_per_trade_usd)
            log.info("triangular: %d cycles over %d Binance markets", len(tri.cycles), len(binance_markets))
            detectors.append(tri)
    if cfg.detection.anomaly:
        detectors.append(AnomalyDetector(cfg.detection))
    risk = RiskManager(cfg.risk, cfg.detection)
    venues = cfg.enabled_venues()
    if live:
        executor: Any = BinanceLiveExecutor(cfg.live, cfg.venues.binance_trade_rest, fees, book, real_orders=real_orders)
    else:
        executor = PaperExecutor(cfg.paper, fees, book, venues)
    return book, detectors, risk, executor, fees


def build_feeds(cfg: Config, markets: list[Market], raw_sink=None) -> list:
    v = cfg.venues
    feeds: list = []
    kw = dict(reconnect_min_s=v.reconnect_min_s, reconnect_max_s=v.reconnect_max_s, raw_sink=raw_sink)
    enabled = cfg.enabled_venues()
    if BINANCE in enabled:
        feeds.append(BinanceFeed(markets, v.binance_ws, **kw))
    if COINBASE in enabled:
        feeds.append(CoinbaseFeed(markets, v.coinbase_ws, **kw))
    if KRAKEN in enabled:
        feeds.append(KrakenFeed(markets, v.kraken_ws, **kw))
    return feeds


def make_engine(cfg: Config, markets: list[Market], feeds: list, clock: Clock, live: bool = False,
                real_orders: bool = False, run_id: str | None = None) -> Engine:
    book, detectors, risk, executor, _ = build_components(cfg, markets, clock, live, real_orders)
    reporter = Reporter(cfg.report, None, executor, risk, book, run_id=run_id,
                        min_net_edge_bps=cfg.detection.min_net_edge_bps)
    engine = Engine(cfg, book, feeds, detectors, risk, executor, reporter, clock)
    reporter.engine = engine
    for f in feeds:
        if hasattr(f, "on_disconnect"):
            f.on_disconnect = engine.on_disconnect
    return engine


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {}
    uni: dict[str, Any] = {}
    if getattr(args, "top", None):
        uni["top_n"] = args.top
    if getattr(args, "symbols", None):
        uni["binance_symbols"] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if getattr(args, "venues", None):
        uni["venues"] = [s.strip().lower() for s in args.venues.split(",") if s.strip()]
    if uni:
        o["universe"] = uni
    det: dict[str, Any] = {}
    if getattr(args, "min_edge", None) is not None:
        det["min_net_edge_bps"] = args.min_edge
    if det:
        o["detection"] = det
    rep: dict[str, Any] = {}
    if getattr(args, "no_jsonl", False):
        rep["write_jsonl"] = False
    if getattr(args, "log_dir", None):
        rep["log_dir"] = args.log_dir
    if rep:
        o["report"] = rep
    return o


async def _markets(cfg: Config, static: bool) -> list[Market]:
    if static or not cfg.universe.auto_discover:
        markets = static_universe(cfg)
    else:
        markets = await discover(cfg)
    log.info("universe: %s", summarize(markets))
    return markets


def _install_sigint(engine: Engine) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):
            pass


async def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, _overrides(args))
    markets = await _markets(cfg, args.static)
    raw_fh = open(args.record, "a", encoding="utf-8") if args.record else None

    def raw_sink(venue: str, raw: str, ts: float) -> None:
        raw_fh.write(json.dumps({"t": round(ts, 6), "venue": venue, "raw": raw}, separators=(",", ":")) + "\n")

    feeds = build_feeds(cfg, markets, raw_sink if raw_fh else None)
    live = bool(args.live)
    real = bool(getattr(args, "real_orders", False))
    if real and not live:
        print(f"{REAL_ORDERS_FLAG} requires --live", file=sys.stderr)
        return 2
    if live:
        if not cfg.live.enabled:
            print("refusing --live: set [live] enabled = true in the config file first", file=sys.stderr)
            return 2
        if real and not cfg.live.real_orders:
            print(f"refusing {REAL_ORDERS_FLAG}: set [live] real_orders = true in the config file first", file=sys.stderr)
            return 2
    try:
        engine = make_engine(cfg, markets, feeds, Clock(), live=live, real_orders=real)
    except LiveDisabled as exc:
        print(f"live mode unavailable: {exc}", file=sys.stderr)
        return 2
    mode = "LIVE/REAL ORDERS" if (live and real) else "LIVE/test-endpoint" if live else "PAPER"
    log.info("arbbot %s starting in %s mode: %d markets, %d feeds, min net edge %.2f bps, fees %s",
             __version__, mode, len(markets), len(feeds), cfg.detection.min_net_edge_bps,
             {k: f"{v:g}bps" for k, v in FeeSchedule(cfg.venues.taker_fee_bps).as_bps().items() if k in cfg.enabled_venues()})
    _install_sigint(engine)
    try:
        await engine.run(duration=args.duration)
    finally:
        if raw_fh:
            raw_fh.close()
        if hasattr(engine.executor, "close"):
            await engine.executor.close()
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, _overrides(args))
    markets = static_universe(cfg)
    clock = Clock()
    feed = ReplayFeed(args.fixture, markets, speed=args.speed, clock_setter=clock.set)
    # prime the clock with the first row so stats.started is in fixture time
    for row in feed.rows():
        clock.set(float(row["t"]))
        break
    engine = make_engine(cfg, markets, [feed], clock)
    _install_sigint(engine)
    stats = await engine.run()
    print(json.dumps({
        "quotes": stats.quotes,
        "gross_positive": dict(stats.gross_by_kind),
        "net_actionable": dict(stats.actionable_by_kind),
        "anomalies": dict(stats.anomalies),
        "trades": dict(stats.trades_by_status),
        "best_net_bps": {k: round(v[0], 3) for k, v in stats.best_net.items()},
        "realized_pnl_usd": round(getattr(engine.executor, "realized_pnl_usd", 0.0), 6),
    }, indent=2))
    return 0


async def cmd_markets(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, _overrides(args))
    markets = await _markets(cfg, args.static)
    by_base: dict[str, list[str]] = {}
    for m in markets:
        by_base.setdefault(m.base, []).append(f"{m.venue}:{m.symbol}")
    for base in sorted(by_base):
        print(f"{base:8s} {'  '.join(sorted(by_base[base]))}")
    print(f"\n{len(markets)} markets: {summarize(markets)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arbbot", description="Crypto mispricing scanner and paper-trading bot")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"arbbot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", help="TOML config file (see config.example.toml)")
        sp.add_argument("--top", type=int, help="number of Binance USDT markets to watch")
        sp.add_argument("--symbols", help="comma-separated Binance symbols instead of discovery")
        sp.add_argument("--venues", help="comma-separated subset of binance,coinbase,kraken")
        sp.add_argument("--min-edge", type=float, dest="min_edge", help="min net edge in bps to act on")
        sp.add_argument("--no-jsonl", action="store_true", dest="no_jsonl", help="do not write logs/<run>/*.jsonl")
        sp.add_argument("--log-dir", dest="log_dir")

    s = sub.add_parser("scan", help="connect to the venues and scan (paper trading by default)")
    common(s)
    s.add_argument("--duration", type=float, help="stop after N seconds (default: run until Ctrl-C)")
    s.add_argument("--static", action="store_true", help="skip REST discovery; use the built-in market list")
    s.add_argument("--record", metavar="FILE", help="also append raw feed messages to FILE (replayable)")
    s.add_argument("--live", action="store_true", help="send Binance orders to /api/v3/order/test (needs [live] enabled)")
    s.add_argument(REAL_ORDERS_FLAG, action="store_true", dest="real_orders",
                   help="with --live: send REAL orders (needs [live] real_orders = true)")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("replay", help="replay a recorded fixture through the full pipeline (offline)")
    common(r)
    r.add_argument("fixture")
    r.add_argument("--speed", type=float, default=0.0, help="playback speed multiplier (0 = as fast as possible)")
    r.set_defaults(func=cmd_replay)

    m = sub.add_parser("markets", help="show the market universe that would be scanned")
    common(m)
    m.add_argument("--static", action="store_true")
    m.set_defaults(func=cmd_markets)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        return asyncio.run(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
