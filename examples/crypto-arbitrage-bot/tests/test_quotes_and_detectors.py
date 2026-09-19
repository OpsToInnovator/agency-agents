import pytest

from arbbot.config import DetectionConfig
from arbbot.detectors import AnomalyDetector, CrossExchangeDetector, TriangularDetector
from arbbot.detectors.triangular import build_cycles
from arbbot.fees import FeeSchedule
from arbbot.models import BINANCE, COINBASE, KRAKEN, Market, Quote
from arbbot.quotes import CHANGE_NEW, CHANGE_NONE, CHANGE_PRICE, CHANGE_SIZE, QuoteBook
from tests.helpers import (BTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, ETH_BINANCE, ETHBTC_BINANCE, USDT_COINBASE,
                           market, quote)

FEES = FeeSchedule({"binance": 10.0, "coinbase": 60.0, "kraken": 40.0})


def make_book(max_age=2.0, **kw) -> QuoteBook:
    book = QuoteBook(max_age_s=max_age, **kw)
    for m in (BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, USDT_COINBASE):
        book.register(m)
    return book


def test_quote_sanity_and_staleness():
    book = make_book(max_age=2.0, max_age_by_venue={"binance": 5.0})
    q = quote(BTC_BINANCE, 100, 101, ts=1000.0)
    assert q.is_sane and q.mid == 100.5 and q.spread_bps == pytest.approx(99.5, rel=1e-3)
    assert book.is_fresh(q, 1004.9) and not book.is_fresh(q, 1005.1)
    cb = quote(BTC_COINBASE, 100, 101, ts=1000.0)
    assert book.is_fresh(cb, 1001.9) and not book.is_fresh(cb, 1002.1)
    assert not quote(BTC_COINBASE, 101, 100).is_sane


def test_update_reports_what_changed():
    book = make_book()
    assert book.update(quote(BTC_BINANCE, 100, 101, 1, 1)) == CHANGE_NEW
    assert book.update(quote(BTC_BINANCE, 100, 101, 1, 1, ts=1001)) == CHANGE_NONE
    assert book.update(quote(BTC_BINANCE, 100, 101, 2, 1, ts=1002)) == CHANGE_SIZE
    assert book.update(quote(BTC_BINANCE, 100, 102, 2, 1, ts=1003)) == CHANGE_PRICE
    assert book.get(BINANCE, "BTCUSDT").recv_ts == 1003


def test_stable_rate_goes_stale_and_is_dropped_on_disconnect():
    book = make_book()
    book.update(quote(USDT_COINBASE, 0.9895, 0.9905, ts=1000.0))  # a thin, trade-triggered print
    assert book.usd_rate("USDT", 1000.0) == pytest.approx(0.99)
    assert book.usd_rate("USDT", 1000.0 + 599) == pytest.approx(0.99)
    assert book.usd_rate("USDT", 1000.0 + 601) == 1.0  # too old to trust: back to par
    book.update(quote(USDT_COINBASE, 0.9995, 0.9997, ts=2000.0))
    assert book.usd_rate("USDT", 2000.0) == pytest.approx(0.9996)
    book.invalidate_venue(COINBASE)
    assert book.usd_rate("USDT", 2000.0) == 1.0


def test_stable_rate_outside_band_is_ignored():
    book = make_book()
    book.update(quote(USDT_COINBASE, 0.9995, 0.9997, ts=1000.0))
    assert book.usd_rate("USDT") == pytest.approx(0.9996)
    book.update(quote(USDT_COINBASE, 0.50, 0.51, ts=1001.0))  # fat finger / bad print
    assert book.usd_rate("USDT") == pytest.approx(0.9996)
    assert book.stable_rate_rejections == 1


def test_usd_rate_and_marks_from_stable_market():
    book = make_book()
    assert book.usd_rate("USD") == 1.0 and book.usd_rate("USDT") == 1.0 and book.usd_rate("BTC") is None
    book.update(quote(USDT_COINBASE, 0.9995, 0.9997, ts=1000.0))
    assert book.usd_rate("USDT") == pytest.approx(0.9996)
    book.update(quote(BTC_BINANCE, 100000, 100002, ts=1000.0))
    book.update(quote(BTC_COINBASE, 99990, 99992, ts=1000.0))
    # median of [100001 * 0.9996, 99991]
    assert book.usd_price("BTC", 1000.5) == pytest.approx((100001 * 0.9996 + 99991) / 2)
    book.update(quote(ETHBTC_BINANCE, 0.0323, 0.0324, ts=1000.0))
    assert book.usd_price("ETH", 1000.5) == pytest.approx(0.03235 * book.usd_price("BTC", 1000.5))
    assert book.usd_price("XYZ", 1000.5) is None


def test_invalidate_venue_and_quarantine():
    book = make_book()
    book.update(quote(BTC_BINANCE, 100, 101))
    book.update(quote(BTC_COINBASE, 100, 101))
    assert len(book.usd_quotes_for_base("BTC", 1000.5)) == 2
    assert book.invalidate_venue(COINBASE) == 1
    assert [q.venue for q in book.usd_quotes_for_base("BTC", 1000.5)] == [BINANCE]
    book.update(quote(BTC_KRAKEN, 100, 101))
    assert book.quarantine("BTC", "test") is True
    assert book.quarantine("BTC", "again") is False
    assert [q.venue for q in book.usd_quotes_for_base("BTC", 1000.5)] == [BINANCE]


def test_cross_exchange_net_edge_and_sizing():
    book = make_book()
    cfg = DetectionConfig(stable_haircut_bps=0.0)
    det = CrossExchangeDetector(FEES, cfg, max_notional_usd=1000.0)
    book.update(quote(BTC_BINANCE, 99999, 100000, ask_qty=0.05, ts=1000.0))
    kr = quote(BTC_KRAKEN, 100600, 100601, bid_qty=0.02, ts=1000.0)
    book.update(kr)
    opps = det.on_quote(kr, book, 1000.0)
    # only buy-binance/sell-kraken is gross positive
    assert len(opps) == 1
    opp = opps[0]
    assert [l.side for l in opp.legs] == ["buy", "sell"]
    assert opp.legs[0].venue == BINANCE and opp.legs[1].venue == KRAKEN
    assert opp.gross_edge_bps == pytest.approx(60.0)
    # net = 100600*(1-0.004) - 100000*(1+0.001) = 100197.6 - 100100 = 97.6 -> 9.76 bps
    assert opp.net_edge_bps == pytest.approx(9.76)
    assert opp.legs[0].qty == pytest.approx(min(0.05, 0.02, 1000 / 100000))
    assert opp.expected_profit_usd == pytest.approx(0.01 * 97.6)
    assert opp.notional_usd == pytest.approx(1000.0)


def test_cross_exchange_haircut_and_usdt_conversion():
    book = make_book()
    cfg = DetectionConfig(stable_haircut_bps=5.0)
    det = CrossExchangeDetector(FEES, cfg, max_notional_usd=100.0)
    book.update(quote(USDT_COINBASE, 0.9990, 0.9990, ts=1000.0))
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
    cb = quote(BTC_COINBASE, 100000, 100001, ts=1000.0)
    book.update(cb)
    opps = det.on_quote(cb, book, 1000.0)
    # binance ask in USD = 100000 * 0.999 = 99900; coinbase bid 100000 -> gross 10.01 bps
    assert len(opps) == 1
    opp = opps[0]
    assert opp.gross_edge_bps == pytest.approx((100000 / 99900 - 1) * 1e4)
    assert opp.extra["haircut_bps"] == 5.0
    raw_net = (100000 * 0.994 - 99900 * 1.001) / 99900 * 1e4
    assert opp.net_edge_bps == pytest.approx(raw_net - 5.0)  # decision edge includes the haircut
    assert opp.extra["fee_net_edge_bps"] == pytest.approx(raw_net)
    assert opp.expected_profit_usd == pytest.approx(opp.notional_usd * raw_net / 1e4)  # the haircut is not a cost


def test_cross_exchange_needs_two_fresh_venues():
    book = make_book()
    det = CrossExchangeDetector(FEES, DetectionConfig(), 100.0)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
    old = quote(BTC_COINBASE, 100600, 100601, ts=990.0)  # 10s old
    book.update(old)
    assert det.on_quote(quote(BTC_BINANCE, 99999, 100000, ts=1000.0), book, 1000.0) == []


def test_identity_mismatch_quarantines_asset():
    book = make_book()
    det = CrossExchangeDetector(FEES, DetectionConfig(identity_mismatch_bps=2000.0), 100.0)
    one_b = market(BINANCE, "ONEUSDT", "ONE", "USDT")
    one_k = market(KRAKEN, "ONE/USD", "ONE", "USD")
    book.register(one_b)
    book.register(one_k)
    book.update(quote(one_b, 0.0024, 0.0025, ts=1000.0))
    k = quote(one_k, 0.1415, 0.1416, ts=1000.0)
    book.update(k)
    out = det.on_quote(k, book, 1000.0)
    assert len(out) == 1 and out[0].kind == "anomaly" and out[0].extra["subtype"] == "identity_mismatch"
    assert "ONE" in book.quarantined
    assert det.on_quote(k, book, 1000.0) == []  # quarantined: nothing more, ever
    assert book.usd_price("ONE", 1000.0) == pytest.approx(0.00245)  # anchor venue only


def test_triangular_cycles_and_edge():
    markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
    cycles = build_cycles(markets, ["USDT"])
    assert {c.path for c in cycles} == {"USDT -> BTC -> ETH -> USDT", "USDT -> ETH -> BTC -> USDT"}
    book = make_book()
    det = TriangularDetector(markets, FEES, DetectionConfig(), max_notional_usd=1000.0)
    # BTC 100000, ETH 4000, ETHBTC 0.04 exactly: gross break-even; make ETHBTC cheap for a gross edge
    book.update(quote(BTC_BINANCE, 99999, 100000, bid_qty=1, ask_qty=1, ts=1000.0))
    book.update(quote(ETH_BINANCE, 4000, 4001, bid_qty=1, ask_qty=1, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, bid_qty=0.5, ask_qty=0.5, ts=1000.0)
    book.update(ethbtc)
    opps = det.on_quote(ethbtc, book, 1000.0)
    assert len(opps) == 1
    opp = opps[0]
    assert opp.extra["start"] == "USDT"
    # USDT -> BTC (buy @100000) -> ETH (buy ETHBTC @0.0396) -> USDT (sell ETH @4000)
    gross = (1 / 100000) * (1 / 0.0396) * 4000
    assert opp.gross_edge_bps == pytest.approx((gross - 1) * 1e4)
    net = (1 / 100000 / 1.001) * (1 / 0.0396 / 1.001) * (4000 * 0.999)
    assert opp.net_edge_bps == pytest.approx((net - 1) * 1e4)
    assert [l.side for l in opp.legs] == ["buy", "buy", "sell"]
    # size: capped by ETHBTC ask_qty 0.5 ETH -> 0.5*0.0396 BTC -> *100000 = 1980 USDT > max notional 1000
    assert opp.notional_usd == pytest.approx(1000.0)
    assert opp.legs[0].qty == pytest.approx(1000 / (100000 * 1.001))
    assert opp.legs[2].qty == pytest.approx(opp.legs[1].qty)
    assert opp.expected_profit_usd == pytest.approx(1000 * (net - 1))


def test_triangular_skips_stale_leg():
    markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
    book = make_book()
    det = TriangularDetector(markets, FEES, DetectionConfig(), 100.0)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=900.0))  # stale
    book.update(quote(ETH_BINANCE, 4000, 4001, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, ts=1000.0)
    book.update(ethbtc)
    assert det.on_quote(ethbtc, book, 1000.0) == []


def test_anomaly_detector_flags_jump_cross_venue_and_crossed_book():
    book = make_book()
    cfg = DetectionConfig(anomaly_threshold_bps=50.0, anomaly_cooldown_s=1.0, anomaly_ewma_halflife_s=10.0)
    det = AnomalyDetector(cfg)
    q0 = quote(BTC_BINANCE, 100000, 100001, ts=1000.0)
    book.update(q0)
    assert det.on_quote(q0, book, 1000.0) == []
    jump = quote(BTC_BINANCE, 101000, 101001, ts=1000.1)  # +100 bps in 100ms
    book.update(jump)
    flags = det.on_quote(jump, book, 1000.1)
    assert [f.extra["subtype"] for f in flags] == ["jump"]
    assert det.on_quote(jump, book, 1000.2) == []  # cooldown
    cb = quote(BTC_COINBASE, 100000, 100001, ts=1000.2)
    book.update(cb)
    flags = det.on_quote(cb, book, 1000.2)
    assert [f.extra["subtype"] for f in flags] == ["venue_disagreement"]  # one peer: cannot say who is wrong
    assert flags[0].extra["deviation_bps"] == pytest.approx(-99.0, abs=0.1)
    kr = quote(BTC_KRAKEN, 101000, 101001, ts=1000.3)
    book.update(kr)
    det.on_quote(kr, book, 1000.3)
    cb2 = quote(BTC_COINBASE, 100000, 100001, ts=1001.5)
    book.update(cb2)
    flags = det.on_quote(cb2, book, 1001.5)
    assert [f.extra["subtype"] for f in flags] == ["price_error"]  # two peers agree Coinbase is the outlier
    crossed = Quote(BINANCE, "BTCUSDT", "BTC", "USDT", 100002, 1, 100001, 1, 1002.0)
    flags = det.on_quote(crossed, book, 1002.0)
    assert [f.extra["subtype"] for f in flags] == ["crossed_book"]
    assert all(not f.executable for f in flags)
