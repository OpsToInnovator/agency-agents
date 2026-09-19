"""Common WebSocket feed machinery: connect, subscribe, parse, reconnect."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Awaitable, Callable, Iterable

import websockets

from ..models import Market, Quote

log = logging.getLogger(__name__)

QuoteSink = Callable[[Quote], None]
RawSink = Callable[[str, str, float], None]  # (venue, raw_text, recv_ts)
DisconnectHook = Callable[[str], None]  # (venue)


class ReconnectRequested(Exception):
    """Raised by a parser when the venue asks us to reconnect (e.g. Binance serverShutdown)."""


class Feed:
    """Base class. Subclasses implement `url()`, `subscribe_messages()` and
    `parse(raw, recv_ts)`; everything about connection lifetime lives here."""

    venue: str = ""
    # Reconnect proactively before the venue's own connection limit hits.
    max_connection_s: float | None = None
    # Gap between subscribe frames (Binance allows 5 client messages per second).
    subscribe_interval_s: float = 0.0
    # A planned exit must not sit in the close handshake while the peer keeps
    # streaming (the paused reader never sees the close frame): 10 s by default.
    close_timeout_s: float = 1.0

    def __init__(
        self,
        markets: Iterable[Market],
        ws_url: str,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
        raw_sink: RawSink | None = None,
        idle_timeout_s: float = 60.0,
        on_disconnect: DisconnectHook | None = None,
    ):
        self.markets: dict[str, Market] = {m.symbol: m for m in markets if m.venue == self.venue}
        self.ws_url = ws_url
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.raw_sink = raw_sink
        self.idle_timeout_s = idle_timeout_s
        self.on_disconnect = on_disconnect
        self.messages = 0
        self.quotes = 0
        self.parse_errors = 0
        self.venue_errors = 0  # the venue said no (bad symbol, bad subscription): not a parse problem
        self.unknown_symbols = 0  # quotes for markets this feed was not asked about
        self.reconnects = 0
        self.connected = False
        self._stop = asyncio.Event()

    # -- to implement -----------------------------------------------------
    def url(self) -> str:
        return self.ws_url

    def subscribe_messages(self) -> list[str]:
        return []

    def parse(self, raw: str, recv_ts: float) -> list[Quote]:
        raise NotImplementedError

    def venue_error(self, message: str, symbol: str | None = None) -> None:
        """The venue rejected something (unknown product, bad subscription). Logged
        loudly, counted apart from parse errors, and the market is dropped so it does
        not sit in the book as a permanently stale entry."""
        self.venue_errors += 1
        if symbol and symbol in self.markets:
            del self.markets[symbol]
            log.error("%s: venue rejected %s (%s); dropped from this feed", self.venue, symbol, message)
        else:
            log.error("%s: venue error: %s", self.venue, message)

    # -- lifetime ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def _session_ended(self) -> None:
        """Fire the disconnect hook the moment a session ends, before any backoff."""
        if self.connected and self.on_disconnect is not None and not self._stop.is_set():
            self.on_disconnect(self.venue)
        self.connected = False

    async def run(self, sink: QuoteSink) -> None:
        """Keep a connection alive until stop() is called, feeding quotes to `sink`."""
        if not self.markets:
            log.info("%s: no markets to subscribe, feed idle", self.venue)
            await self._stop.wait()
            return
        floor = max(0.1, self.reconnect_min_s)
        backoff = floor
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session(sink)
                self._session_ended()
                backoff = floor  # clean session: reset backoff
            except asyncio.CancelledError:
                self._session_ended()
                raise
            except Exception as exc:  # network errors, protocol errors, parse crashes
                self._session_ended()
                if self._stop.is_set():
                    break
                lived = time.monotonic() - started
                if lived > 30:
                    backoff = floor
                log.warning("%s: connection ended after %.0fs (%s: %s); reconnecting in %.1fs",
                            self.venue, lived, type(exc).__name__, exc, backoff)
                self.reconnects += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff * (0.8 + 0.4 * random.random()))
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(max(self.reconnect_max_s, floor), backoff * 2)
            finally:
                self.connected = False

    async def _session(self, sink: QuoteSink) -> None:
        url = self.url()
        log.info("%s: connecting to %s (%d markets)", self.venue, url.split("?")[0], len(self.markets))
        async with websockets.connect(url, open_timeout=20, ping_interval=20, ping_timeout=20, max_size=2**22,
                                      close_timeout=self.close_timeout_s) as ws:
            self.connected = True
            for i, msg in enumerate(self.subscribe_messages()):
                if i and self.subscribe_interval_s:
                    await asyncio.sleep(self.subscribe_interval_s)
                await ws.send(msg)
            opened = time.monotonic()
            stop_task = asyncio.ensure_future(self._stop.wait())
            try:
                while not self._stop.is_set():
                    if self.max_connection_s and time.monotonic() - opened > self.max_connection_s:
                        log.info("%s: planned reconnect after %.0fs", self.venue, self.max_connection_s)
                        return
                    recv_task = asyncio.ensure_future(ws.recv())
                    done, _ = await asyncio.wait({recv_task, stop_task}, timeout=self.idle_timeout_s,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv_task not in done:
                        recv_task.cancel()
                        if stop_task in done:
                            return
                        raise TimeoutError(f"no message for {self.idle_timeout_s:.0f}s")
                    raw = recv_task.result()
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    recv_ts = time.time()
                    self.messages += 1
                    if self.raw_sink is not None:
                        self.raw_sink(self.venue, raw, recv_ts)
                    for q in self._safe_parse(raw, recv_ts):
                        self.quotes += 1
                        sink(q)
            finally:
                stop_task.cancel()

    def _safe_parse(self, raw: str, recv_ts: float) -> list[Quote]:
        try:
            return self.parse(raw, recv_ts)
        except ReconnectRequested:
            raise
        except Exception as exc:  # a malformed message must not kill the feed
            self.parse_errors += 1
            if self.parse_errors <= 5 or self.parse_errors % 1000 == 0:
                log.warning("%s: parse error #%d (%s: %s) on %.120s", self.venue, self.parse_errors,
                            type(exc).__name__, exc, raw)
            return []


def loads(raw: str) -> dict | list:
    return json.loads(raw)
