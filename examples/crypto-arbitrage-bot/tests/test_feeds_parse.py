import json

import pytest

from arbbot.feeds import BinanceFeed, CoinbaseFeed, KrakenFeed
from arbbot.feeds.base import ReconnectRequested
from arbbot.feeds.coinbase import _iso_ts
from tests.helpers import BTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, ETH_BINANCE


def test_binance_combined_payload():
    feed = BinanceFeed([BTC_BINANCE, ETH_BINANCE], "wss://x/stream")
    assert feed.url() == "wss://x/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
    raw = json.dumps({"stream": "btcusdt@bookTicker", "data": {"u": 5, "s": "BTCUSDT", "b": "81024.00000000",
                                                                 "B": "5.48", "a": "81024.01000000", "A": "0.37"}})
    (q,) = feed.parse(raw, 123.0)
    assert (q.venue, q.symbol, q.base, q.quote) == ("binance", "BTCUSDT", "BTC", "USDT")
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty, q.recv_ts) == (81024.0, 5.48, 81024.01, 0.37, 123.0)
    assert q.exch_ts is None


def test_binance_ignores_unknown_symbol_and_out_of_order():
    feed = BinanceFeed([BTC_BINANCE], "wss://x/stream")
    unknown = json.dumps({"stream": "solusdt@bookTicker", "data": {"u": 1, "s": "SOLUSDT", "b": "1", "B": "1", "a": "2", "A": "1"}})
    assert feed.parse(unknown, 0.0) == []
    first = json.dumps({"data": {"u": 10, "s": "BTCUSDT", "b": "1", "B": "1", "a": "2", "A": "1"}})
    stale = json.dumps({"data": {"u": 9, "s": "BTCUSDT", "b": "1", "B": "1", "a": "2", "A": "1"}})
    assert len(feed.parse(first, 0.0)) == 1
    assert feed.parse(stale, 0.0) == []
    assert feed.out_of_order == 1


def test_binance_server_shutdown_requests_reconnect():
    feed = BinanceFeed([BTC_BINANCE], "wss://x/stream")
    with pytest.raises(ReconnectRequested):
        feed.parse(json.dumps({"stream": "!serverShutdown", "data": {"e": "serverShutdown", "E": 1}}), 0.0)
    with pytest.raises(ReconnectRequested):
        feed._safe_parse(json.dumps({"e": "serverShutdown", "E": 1}), 0.0)


def test_binance_malformed_message_is_counted_not_raised():
    feed = BinanceFeed([BTC_BINANCE], "wss://x/stream")
    assert feed._safe_parse("{not json", 0.0) == []
    assert feed._safe_parse(json.dumps({"data": {"s": "BTCUSDT", "b": "x", "B": "1", "a": "2", "A": "1"}}), 0.0) == []
    assert feed.parse_errors == 2


def test_coinbase_ticker():
    feed = CoinbaseFeed([BTC_COINBASE], "wss://x")
    sub = json.loads(feed.subscribe_messages()[0])
    assert sub == {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}
    raw = json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": "81005.76", "best_bid": "81005.75",
                      "best_bid_size": "0.1", "best_ask": "81005.76", "best_ask_size": "0.2",
                      "time": "2026-09-19T05:03:20.696365Z"})
    (q,) = feed.parse(raw, 1.0)
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty) == (81005.75, 0.1, 81005.76, 0.2)
    assert q.exch_ts == pytest.approx(_iso_ts("2026-09-19T05:03:20.696365Z"))
    assert feed.parse(json.dumps({"type": "subscriptions", "channels": []}), 1.0) == []
    assert feed.parse(json.dumps({"type": "heartbeat", "product_id": "BTC-USD"}), 1.0) == []


def test_coinbase_error_attribution_matches_whole_product_ids():
    from tests.helpers import market
    ar = market("coinbase", "AR-USD", "AR", "USD")
    hbar = market("coinbase", "HBAR-USD", "HBAR", "USD")
    feed = CoinbaseFeed([ar, hbar], "wss://x")
    feed.parse(json.dumps({"type": "error", "message": "Failed to subscribe", "reason": "HBAR-USD is not a valid product"}), 1.0)
    assert "AR-USD" in feed.markets and "HBAR-USD" not in feed.markets


def test_binance_error_ack_real_shape_is_a_venue_error():
    feed = BinanceFeed([BTC_BINANCE], "wss://x/stream")
    assert feed.parse(json.dumps({"error": {"code": 2, "msg": "Invalid request: unknown property"}, "id": 1}), 0.0) == []
    assert feed.venue_errors == 1


def test_coinbase_drops_out_of_order_sequence():
    feed = CoinbaseFeed([BTC_COINBASE], "wss://x")
    base = {"type": "ticker", "product_id": "BTC-USD", "best_bid": "1", "best_bid_size": "1", "best_ask": "2", "best_ask_size": "1"}
    assert len(feed.parse(json.dumps({**base, "sequence": 10}), 1.0)) == 1
    assert feed.parse(json.dumps({**base, "sequence": 9}), 1.0) == []
    assert feed.parse(json.dumps({**base, "sequence": 10}), 1.0) == []
    assert len(feed.parse(json.dumps({**base, "sequence": 11}), 1.0)) == 1
    assert feed.out_of_order == 2


def test_coinbase_error_is_a_venue_error_and_drops_the_product():
    from tests.helpers import market
    nope = market("coinbase", "NOPE-USD", "NOPE", "USD")
    feed = CoinbaseFeed([BTC_COINBASE, nope], "wss://x")
    out = feed.parse(json.dumps({"type": "error", "message": "Failed to subscribe", "reason": "NOPE-USD is not a valid product"}), 1.0)
    assert out == [] and feed.venue_errors == 1 and feed.parse_errors == 0
    assert "NOPE-USD" not in feed.markets and "BTC-USD" in feed.markets
    feed.parse(json.dumps({"type": "subscriptions", "channels": [{"name": "ticker", "product_ids": []}]}), 1.0)
    assert feed.venue_errors == 2  # subscribed to nothing


def test_iso_ts_accepts_nanoseconds_and_z():
    assert _iso_ts("2026-09-19T05:05:28.024848219Z") == pytest.approx(_iso_ts("2026-09-19T05:05:28.024848+00:00"))
    assert _iso_ts("nonsense") is None
    assert _iso_ts(None) is None


def test_kraken_ticker_bbo_subscription_and_parse():
    feed = KrakenFeed([BTC_KRAKEN], "wss://x")
    sub = json.loads(feed.subscribe_messages()[0])
    assert sub["params"]["event_trigger"] == "bbo"
    assert sub["params"]["symbol"] == ["BTC/USD"]
    assert feed.idle_timeout_s == 10.0
    raw = json.dumps({"channel": "ticker", "type": "update", "data": [
        {"symbol": "BTC/USD", "bid": 81006.2, "bid_qty": 0.28, "ask": 81006.3, "ask_qty": 0.0016,
         "last": 81006.2, "timestamp": "2026-09-19T05:04:16.593391Z", "trades": 1}]})
    (q,) = feed.parse(raw, 2.0)
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty) == (81006.2, 0.28, 81006.3, 0.0016)
    assert q.exch_ts is not None
    assert feed.parse(json.dumps({"channel": "heartbeat"}), 2.0) == []
    assert feed.parse(json.dumps({"channel": "status", "type": "update", "data": [{"system": "maintenance"}]}), 2.0) == []
    assert feed.system_status == "maintenance"


def test_kraken_subscribe_failure_drops_the_symbol():
    from tests.helpers import market
    xbt = market("kraken", "XBT/USD", "BTC", "USD")
    feed = KrakenFeed([BTC_KRAKEN, xbt], "wss://x")
    out = feed.parse(json.dumps({"method": "subscribe", "success": False, "symbol": "XBT/USD",
                                 "error": "Currency pair not supported XBT/USD"}), 0.0)
    assert out == [] and feed.venue_errors == 1 and feed.parse_errors == 0
    assert "XBT/USD" not in feed.markets and "BTC/USD" in feed.markets


def test_binance_large_universe_subscribes_with_frames():
    from arbbot.feeds.binance import MAX_STREAMS_PER_CONNECTION, SUBSCRIBE_BATCH, URL_STREAM_LIMIT
    from tests.helpers import market
    many = [market("binance", f"X{i}USDT", f"X{i}", "USDT") for i in range(URL_STREAM_LIMIT + 50)]
    feed = BinanceFeed(many, "wss://x/stream")
    assert feed.url() == "wss://x/stream"
    frames = [json.loads(m) for m in feed.subscribe_messages()]
    assert sum(len(f["params"]) for f in frames) == URL_STREAM_LIMIT + 50
    assert all(len(f["params"]) <= SUBSCRIBE_BATCH and f["method"] == "SUBSCRIBE" for f in frames)
    assert feed.subscribe_interval_s >= 0.2  # 5 client messages per second at Binance
    assert feed.parse(json.dumps({"result": None, "id": 1}), 0.0) == []
    feed.parse(json.dumps({"result": None, "id": 2, "error": {"code": 2, "msg": "Invalid request"}}), 0.0)
    assert feed.venue_errors == 1
    too_many = [market("binance", f"Y{i}USDT", f"Y{i}", "USDT") for i in range(MAX_STREAMS_PER_CONNECTION + 5)]
    assert len(BinanceFeed(too_many, "wss://x/stream").markets) == MAX_STREAMS_PER_CONNECTION
