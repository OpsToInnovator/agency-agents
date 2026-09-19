"""Replay a recorded JSONL feed through the real venue parsers.

Fixture rows look like {"t": 1789794253.18, "venue": "coinbase", "raw": "<ws text>"}.
Time is taken from the fixture, so staleness and latency logic behave as they
did live; `speed` scales the wall-clock pacing (0 = as fast as possible).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, Iterable

from ..models import Market, Quote
from .base import Feed
from .binance import BinanceFeed
from .coinbase import CoinbaseFeed
from .kraken import KrakenFeed

log = logging.getLogger(__name__)

_PARSERS = {BinanceFeed.venue: BinanceFeed, CoinbaseFeed.venue: CoinbaseFeed, KrakenFeed.venue: KrakenFeed}


def parser_for(venue: str, markets: Iterable[Market]) -> Feed:
    cls = _PARSERS[venue]
    return cls(markets, ws_url="")


class ReplayFeed:
    """Not a `Feed` subclass: it drives all venues from one file."""

    def __init__(self, path: str | Path, markets: Iterable[Market], speed: float = 0.0,
                 clock_setter: Callable[[float], None] | None = None):
        self.path = Path(path)
        markets = list(markets)
        self.parsers = {v: parser_for(v, markets) for v in _PARSERS}
        self.speed = speed
        self.clock_setter = clock_setter
        self.messages = 0
        self.quotes = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def rows(self):
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    async def run(self, sink: Callable[[Quote], None]) -> None:
        prev_t: float | None = None
        for row in self.rows():
            if self._stop.is_set():
                break
            t = float(row["t"])
            if self.speed and prev_t is not None and t > prev_t:
                await asyncio.sleep((t - prev_t) / self.speed)
            prev_t = t
            if self.first_ts is None:
                self.first_ts = t
            self.last_ts = t
            if self.clock_setter:
                self.clock_setter(t)
            parser = self.parsers.get(row["venue"])
            if parser is None:
                continue
            self.messages += 1
            try:
                quotes = parser._safe_parse(row["raw"], t)
            except Exception as exc:  # a recorded serverShutdown just means "reconnect happened"
                log.info("replay: %s", exc)
                continue
            for q in quotes:
                self.quotes += 1
                sink(q)
                # Yield after every quote so the consumer sees each one in
                # order: replays must be deterministic, never coalesced.
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        log.info("replay finished: %d messages, %d quotes", self.messages, self.quotes)
