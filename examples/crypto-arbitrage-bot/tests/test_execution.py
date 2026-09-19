import asyncio
import os

import pytest

from arbbot.config import DetectionConfig, LiveConfig, PaperConfig, RiskConfig
from arbbot.detectors import CrossExchangeDetector, TriangularDetector
from arbbot.execution import BinanceLiveExecutor, PaperExecutor, RiskManager
from arbbot.execution.live import LiveDisabled, sign_query
from arbbot.fees import FeeSchedule
from arbbot.models import BINANCE, COINBASE, KRAKEN, Leg, Opportunity, TradeRecord
from arbbot.quotes import QuoteBook
from tests.helpers import BTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, ETH_BINANCE, ETHBTC_BINANCE, quote

FEES = FeeSchedule({"binance": 10.0, "coinbase": 60.0, "kraken": 40.0})


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def book_with_cross_opp():
    book = QuoteBook(max_age_s=2.0)
    for m in (BTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, ETH_BINANCE, ETHBTC_BINANCE):
        book.register(m)
    book.update(quote(BTC_BINANCE, 99999, 100000, ask_qty=1, ts=1000.0))
    kr = quote(BTC_KRAKEN, 100600, 100601, bid_qty=1, ts=1000.0)
    book.update(kr)
    det = CrossExchangeDetector(FEES, DetectionConfig(stable_haircut_bps=0.0), 1000.0)
    (opp,) = det.on_quote(kr, book, 1000.0)
    return book, opp


def test_paper_cross_exchange_fill_matches_expected_profit():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, rel=1e-9)
    assert ex.balance(BINANCE, "USDT") == pytest.approx(5000 - 0.01 * 100000 * 1.001)
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.01)
    assert ex.balance(KRAKEN, "USD") == pytest.approx(5000 + 0.01 * 100600 * 0.996)
    # kraken BTC inventory was funded lazily at the mark and then sold down by 0.01
    assert ex.balance(KRAKEN, "BTC") == pytest.approx(2000 / book.usd_price("BTC", 1000.0) - 0.01)
    assert ex.contributions_usd == pytest.approx(5000 * 2 + 2000)
    equity, unmarked = ex.equity_usd(1000.0)
    assert unmarked == []
    assert equity == pytest.approx(ex.contributions_usd + rec.realized_pnl_usd, rel=1e-6)


def test_paper_slippage_reduces_pnl():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, slippage_bps=2.0, starting_base_inventory_usd=2000),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.realized_pnl_usd < opp.expected_profit_usd
    assert rec.realized_pnl_usd == pytest.approx(0.01 * (100600 * 0.9998 * 0.996 - 100000 * 1.0002 * 1.001))


def test_paper_scales_down_when_short_of_balance():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=500, starting_base_inventory_usd=2000, slippage_bps=0.0),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "partial"
    assert ex.balance(BINANCE, "USDT") == pytest.approx(0.0, abs=1e-6)
    assert rec.fills[0].qty == pytest.approx(500 / (100000 * 1.001))
    assert rec.fills[1].qty == pytest.approx(rec.fills[0].qty)


def test_paper_rejects_when_nothing_to_sell():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_base_inventory_usd=0.0), FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "balance" in rec.reason
    assert ex.rejected == 1 and ex.trades == 0


def test_paper_triangular_round_trip_pnl():
    book = QuoteBook(max_age_s=2.0)
    markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
    for m in markets:
        book.register(m)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
    book.update(quote(ETH_BINANCE, 4000, 4001, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, ts=1000.0)
    book.update(ethbtc)
    det = TriangularDetector(markets, FEES, DetectionConfig(), 1000.0)
    (opp,) = det.on_quote(ethbtc, book, 1000.0)
    ex = PaperExecutor(PaperConfig(slippage_bps=0.0), FEES, book, [BINANCE])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.0, abs=1e-12)
    assert ex.balance(BINANCE, "ETH") == pytest.approx(0.0, abs=1e-12)
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, abs=1e-6)


def _opp(net=5.0, notional=50.0, ages=(10.0,), latency=1.0, kind="cross_exchange", legs=True):
    legs_ = [Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100.0, 0.5, 0.001)] if legs else []
    return Opportunity(kind, 1000.0, legs_, net + 20, net, notional, notional * net / 1e4, "t",
                       detect_latency_ms=latency, quote_ages_ms=list(ages))


def test_risk_manager_checks(tmp_path):
    rcfg = RiskConfig(max_notional_per_trade_usd=100, max_daily_loss_usd=10, max_trades_per_minute=2, cooldown_s=5,
                      max_detect_latency_ms=50, kill_switch_file=str(tmp_path / "STOP"))
    det = DetectionConfig(min_net_edge_bps=1.0, max_quote_age_ms=2000, max_plausible_net_edge_bps=200)
    rm = RiskManager(rcfg, det)
    assert rm.check(_opp(), 1000.0) == (True, "")
    assert rm.check(_opp(net=0.5), 1000.0)[1] == "below min net edge"
    assert rm.check(_opp(net=500), 1000.0)[1] == "implausible edge (bad data?)"
    assert rm.check(_opp(ages=(3000,)), 1000.0)[1] == "stale quote"
    assert rm.check(_opp(latency=80), 1000.0)[1] == "slow detection"
    assert rm.check(_opp(notional=150), 1000.0)[1] == "over max notional"
    assert rm.check(_opp(legs=False), 1000.0)[1] == "report-only"
    # cooldown and rate limit
    rec = TradeRecord(_opp(), [], "filled", "", -4.0, 1000.0)
    rm.on_trade(rec, 1000.0)
    assert rm.check(_opp(), 1001.0)[1] == "cooldown"
    other = _opp(); other.legs[0].symbol = "ETHUSDT"
    assert rm.check(other, 1001.0) == (True, "")
    rm.on_trade(TradeRecord(other, [], "filled", "", -4.0, 1001.0), 1001.0)
    third = _opp(); third.legs[0].symbol = "SOLUSDT"
    assert rm.check(third, 1002.0)[1] == "trade rate limit"
    # daily loss cap halts
    rm.on_trade(TradeRecord(third, [], "filled", "", -3.0, 1002.0), 1002.0)
    assert rm.daily_realized_usd == pytest.approx(-11.0)
    assert "daily loss cap" in rm.check(_opp(), 1100.0)[1]
    assert rm.halted
    # kill switch beats everything
    rm2 = RiskManager(rcfg, det)
    (tmp_path / "STOP").write_text("")
    assert "kill switch" in rm2.check(_opp(), 1000.0)[1]
    assert rm2.rejections["kill switch file '%s' present" % rcfg.kill_switch_file] == 1


def test_risk_daily_loss_resets_on_new_utc_day():
    rm = RiskManager(RiskConfig(max_daily_loss_usd=10), DetectionConfig())
    rm.check(_opp(), 1000.0)
    rm.on_trade(TradeRecord(_opp(), [], "filled", "", -20.0, 1000.0), 1000.0)
    assert rm.daily_realized_usd == -20.0
    assert rm.check(_opp(), 1000.0 + 86400)[0] is True
    assert rm.daily_realized_usd == 0.0


def test_sign_query_matches_official_vector():
    params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1,
              "price": 0.1, "recvWindow": 5000, "timestamp": 1499827319559}
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    signed = sign_query(params, secret)
    assert signed.endswith("&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_live_executor_refuses_without_config_or_keys(monkeypatch):
    book = QuoteBook()
    with pytest.raises(LiveDisabled):
        BinanceLiveExecutor(LiveConfig(enabled=False), "https://x", FEES, book, real_orders=False)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    with pytest.raises(LiveDisabled):
        BinanceLiveExecutor(LiveConfig(enabled=True), "https://x", FEES, book, real_orders=False)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data, headers))
        status, payload = self.responses.pop(0)
        return FakeResponse(status, payload)

    async def close(self):
        pass


def test_live_executor_test_endpoint_by_default(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
    for m in markets:
        book.register(m)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
    book.update(quote(ETH_BINANCE, 4000, 4001, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, ts=1000.0)
    book.update(ethbtc)
    (opp,) = TriangularDetector(markets, FEES, DetectionConfig(), 1000.0).on_quote(ethbtc, book, 1000.0)
    session = FakeSession([(200, {}), (200, {}), (200, {})])
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, real_orders=True), "https://api.example", FEES, book,
                             real_orders=False, session=session)
    assert ex.endpoint == "/api/v3/order/test"
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "test" and rec.realized_pnl_usd == 0.0
    assert len(session.calls) == 3
    url, body, headers = session.calls[0]
    assert url.endswith("/api/v3/order/test")
    assert headers["X-MBX-APIKEY"] == "k"
    assert "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.00999" in body
    assert "&signature=" in body and "recvWindow=5000" in body


def test_live_executor_real_orders_need_both_switches(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, real_orders=False), "https://x", FEES, book, real_orders=True)
    assert ex.real_orders is False and ex.endpoint == "/api/v3/order/test"
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, real_orders=True), "https://x", FEES, book, real_orders=True)
    assert ex.real_orders is True and ex.endpoint == "/api/v3/order"


def test_live_executor_rejects_cross_venue_and_filter_failures(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = book_with_cross_opp()
    ex = BinanceLiveExecutor(LiveConfig(enabled=True), "https://x", FEES, book, real_orders=False, session=FakeSession([]))
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "Binance-only" in rec.reason
    tiny = Opportunity("triangular", 1000.0, [Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100000.0, 0.00001, 0.001)],
                       5, 5, 1.0, 0.0, "tiny")
    rec = run(ex.execute(tiny, 1000.0))
    assert rec.status == "rejected" and "min_notional" in rec.reason
