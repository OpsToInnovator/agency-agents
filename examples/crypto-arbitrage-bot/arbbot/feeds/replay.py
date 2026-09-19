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


def tape_header(markets: Iterable[Market]) -> str:
    """First line of a recording: the universe it was made with."""
    return json.dumps({"universe": [
        {"venue": m.venue, "symbol": m.symbol, "base": m.base, "quote": m.quote, "tick_size": m.tick_size,
         "step_size": m.step_size, "min_qty": m.min_qty, "min_notional": m.min_notional} for m in markets
    ]}, separators=(",", ":"))


def read_tape_header(path: str | Path) -> list[Market] | None:
    """Markets recorded in the tape's header line, or None for a headerless tape."""
    first = ""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                first = line.strip()
                break
    if not first:
        return None
    try:
        row = json.loads(first)
    except ValueError:
        return None
    if not isinstance(row, dict) or "universe" not in row:
        return None
    return [Market(m["venue"], m["symbol"], m["base"], m["quote"], m.get("tick_size"), m.get("step_size"),
                   m.get("min_qty"), m.get("min_notional")) for m in row["universe"]]


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
        self.bad_rows = 0  # truncated/malformed lines (a recording cut mid-write)
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def unknown_symbols(self) -> int:
        """Rows for markets the replay universe does not know (tape recorded with a different universe)."""
        return sum(p.unknown_symbols for p in self.parsers.values())

    def first_row_ts(self) -> float | None:
        """Timestamp of the first data row, without counting or logging bad rows."""
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and "universe" in row:
                        continue
                    return float(row["t"])
                except (ValueError, KeyError, TypeError):
                    continue
        return None

    def rows(self):
        seen_data = False
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if not seen_data and isinstance(row, dict) and "universe" in row:
                        continue  # header (first non-blank line)
                    seen_data = True
                    float(row["t"])
                    row["venue"], row["raw"]
                except (ValueError, KeyError, TypeError) as exc:
                    self.bad_rows += 1
                    if self.bad_rows <= 3:
                        log.warning("replay: skipping malformed row %d of %s (%s)", lineno, self.path.name, type(exc).__name__)
                    continue
                yield row

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
        log.info("replay finished: %d messages, %d quotes, %d malformed rows skipped, %d rows for unknown markets",
                 self.messages, self.quotes, self.bad_rows, self.unknown_symbols)
