import asyncio
import json

import pytest

from arbbot.config import DetectionConfig, LiveConfig, PaperConfig, RiskConfig
from arbbot.detectors import CrossExchangeDetector, TriangularDetector
from arbbot.execution import BinanceLiveExecutor, PaperExecutor, RiskManager
from arbbot.execution.live import AmbiguousOrderState, LiveDisabled, sign_query
from arbbot.fees import FeeSchedule
from arbbot.models import BINANCE, COINBASE, KRAKEN, Fill, Leg, Market, Opportunity, TradeRecord
from arbbot.quotes import QuoteBook
from tests.helpers import BTC_BINANCE, BTC_COINBASE, BTC_KRAKEN, ETH_BINANCE, ETHBTC_BINANCE, quote

FEES = FeeSchedule({"binance": 10.0, "coinbase": 60.0, "kraken": 40.0})
INSTANT = dict(fill_model="instant")


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


def triangle_book_and_opp():
    book = QuoteBook(max_age_s=2.0)
    markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
    for m in markets:
        book.register(m)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
    book.update(quote(ETH_BINANCE, 4000, 4001, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, ts=1000.0)
    book.update(ethbtc)
    (opp,) = TriangularDetector(markets, FEES, DetectionConfig(), 1000.0).on_quote(ethbtc, book, 1000.0)
    return book, opp


# ---------------------------------------------------------------- instant model

def test_paper_instant_cross_exchange_fill_matches_expected_profit():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0, **INSTANT),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, rel=1e-9)
    assert rec.promised_pnl_usd == pytest.approx(opp.expected_profit_usd)
    assert ex.balance(BINANCE, "USDT") == pytest.approx(5000 - 0.01 * 100000 * 1.001)
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.01)
    assert ex.balance(KRAKEN, "USD") == pytest.approx(5000 + 0.01 * 100600 * 0.996)
    assert ex.balance(KRAKEN, "BTC") == pytest.approx(2000 / book.usd_price("BTC", 1000.0) - 0.01)
    assert ex.contributions_value_usd(1000.0) == pytest.approx(5000 * 2 + 2000)
    equity, unmarked = ex.equity_usd(1000.0)
    assert unmarked == []
    assert equity == pytest.approx(ex.contributions_value_usd(1000.0) + rec.realized_pnl_usd, rel=1e-6)
    assert ex.latency_tax_usd == pytest.approx(0.0, abs=1e-9)


def test_paper_instant_slippage_reduces_pnl():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, slippage_bps=2.0, starting_base_inventory_usd=2000, **INSTANT),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.realized_pnl_usd < opp.expected_profit_usd
    assert rec.realized_pnl_usd == pytest.approx(0.01 * (100600 * 0.9998 * 0.996 - 100000 * 1.0002 * 1.001))


def test_paper_instant_scales_down_when_short_of_balance():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=500, starting_base_inventory_usd=2000, slippage_bps=0.0, **INSTANT),
                       FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "partial"
    assert ex.balance(BINANCE, "USDT") == pytest.approx(0.0, abs=1e-6)
    assert rec.fills[0].qty == pytest.approx(500 / (100000 * 1.001))
    assert rec.fills[1].qty == pytest.approx(rec.fills[0].qty)


def test_paper_instant_rejects_when_nothing_to_sell():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_base_inventory_usd=0.0, **INSTANT), FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "balance" in rec.reason
    assert ex.rejected == 1 and ex.trades == 0


def test_paper_instant_triangle_with_slippage_sizes_legs_from_carry():
    # default slippage (2 bps): leg 2 can only spend what leg 1 delivered; the plan must not be rejected
    book, opp = triangle_book_and_opp()
    ex = PaperExecutor(PaperConfig(slippage_bps=2.0, **INSTANT), FEES, book, [BINANCE])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled", rec.reason
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.0, abs=1e-12)
    assert ex.balance(BINANCE, "ETH") == pytest.approx(0.0, abs=1e-12)
    assert rec.realized_pnl_usd < opp.expected_profit_usd  # slippage on three legs


def test_paper_buy_sell_sell_triangle_funds_nothing():
    # USDT -> ETH -> BTC -> USDT: leg 2 sells ETH (bought), leg 3 sells BTC (produced by leg 2): self-funding
    book = QuoteBook(max_age_s=2.0)
    for m in (BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE):
        book.register(m)
    book.update(quote(BTC_BINANCE, 100000, 100001, ts=1000.0))
    book.update(quote(ETH_BINANCE, 3999, 4000, ts=1000.0))
    ethbtc = quote(ETHBTC_BINANCE, 0.0404, 0.0405, ts=1000.0)
    book.update(ethbtc)
    (opp,) = TriangularDetector([BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE], FEES, DetectionConfig(), 1000.0).on_quote(ethbtc, book, 1000.0)
    assert [l.side for l in opp.legs] == ["buy", "sell", "sell"]
    ex = PaperExecutor(PaperConfig(slippage_bps=0.0, **INSTANT), FEES, book, [BINANCE])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert ex.contributions_value_usd(1000.0) == pytest.approx(1000.0)  # no phantom BTC inventory
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.0, abs=1e-12)


def test_paper_equity_matches_contributions_when_usdt_is_not_a_dollar():
    from tests.helpers import USDT_COINBASE
    book = QuoteBook(max_age_s=2.0)
    for m in (BTC_BINANCE, USDT_COINBASE):
        book.register(m)
    book.update(quote(USDT_COINBASE, 0.9995, 0.9997, ts=1000.0))
    ex = PaperExecutor(PaperConfig(), FEES, book, [BINANCE, COINBASE])
    equity, _ = ex.equity_usd(1000.0)
    assert equity == pytest.approx(1000 * 0.9996 + 1000)
    assert ex.contributions_value_usd(1000.0) == pytest.approx(equity)  # no trade, no PnL


def test_paper_instant_triangular_round_trip_pnl():
    book, opp = triangle_book_and_opp()
    ex = PaperExecutor(PaperConfig(slippage_bps=0.0, **INSTANT), FEES, book, [BINANCE])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.0, abs=1e-12)
    assert ex.balance(BINANCE, "ETH") == pytest.approx(0.0, abs=1e-12)  # no phantom inventory for a triangle
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, abs=1e-6)


# ---------------------------------------------------------------- arrival model

def test_paper_arrival_fills_after_rtt_when_book_unchanged():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0,
                                   assumed_rtt_ms=150), FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "pending" and len(ex.pending) == 1
    assert ex.settle(1000.1) == []  # not there yet
    done = ex.settle(1000.2)
    assert len(done) == 1 and not ex.pending
    rec = done[0]
    assert rec.status == "filled"
    assert rec.latency_ms == pytest.approx(200.0)
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, rel=1e-9)
    assert rec.promised_pnl_usd == pytest.approx(opp.expected_profit_usd)
    assert ex.latency_tax_usd == pytest.approx(0.0, abs=1e-9)
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.01)


def test_paper_arrival_misses_leg_when_touch_moves_away():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0,
                                   assumed_rtt_ms=100), FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))
    # Kraken bid drops below our limit before the order gets there; Binance ask improves
    book.update(quote(BTC_KRAKEN, 100500, 100501, bid_qty=1, ts=1000.05))
    book.update(quote(BTC_BINANCE, 99990, 99995, ask_qty=1, ts=1000.05))
    (rec,) = ex.settle(1000.2)
    assert rec.status == "partial"
    assert ex.missed_legs == 1 and "bid moved" in rec.reason
    assert [f.side for f in rec.fills] == ["buy"]
    assert rec.fills[0].price == pytest.approx(99995)  # IOC limit at 100000 fills at the better ask
    # we now hold 0.01 BTC we did not sell: realized is marked at mid, not promised
    assert rec.realized_pnl_usd != pytest.approx(opp.expected_profit_usd)
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.01)


def test_paper_arrival_partial_size_from_displayed_depth():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0,
                                   assumed_rtt_ms=100, fill_fraction=0.5), FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))
    book.update(quote(BTC_BINANCE, 99999, 100000, ask_qty=0.004, ts=1000.05))  # depth shrank
    (rec,) = ex.settle(1000.2)
    assert rec.status == "filled"
    assert rec.fills[0].qty == pytest.approx(0.002)  # 0.004 displayed * 0.5
    assert rec.fills[1].qty == pytest.approx(0.01)  # other venue still shows 1.0: planned size fills; legs are now unbalanced
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.002)


def test_paper_arrival_triangle_is_sequential():
    book, opp = triangle_book_and_opp()
    ex = PaperExecutor(PaperConfig(slippage_bps=0.0, assumed_rtt_ms=100), FEES, book, [BINANCE])
    run(ex.execute(opp, 1000.0))
    assert ex.settle(1000.1) == []  # leg 1 done, leg 2 now in flight
    assert ex.pending[0].resolved == [True, False, False]
    assert ex.settle(1000.2) == []
    (rec,) = ex.settle(1000.3)
    assert rec.status == "filled"
    assert rec.latency_ms == pytest.approx(300.0)
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.0, abs=1e-12)
    assert ex.balance(BINANCE, "ETH") == pytest.approx(0.0, abs=1e-12)
    assert rec.realized_pnl_usd == pytest.approx(opp.expected_profit_usd, abs=1e-6)


def test_paper_arrival_triangle_aborts_after_missed_leg():
    book, opp = triangle_book_and_opp()
    ex = PaperExecutor(PaperConfig(slippage_bps=0.0, assumed_rtt_ms=100), FEES, book, [BINANCE])
    run(ex.execute(opp, 1000.0))
    ex.settle(1000.1)  # leg 1 (buy BTC) fills
    book.update(quote(ETHBTC_BINANCE, 0.0398, 0.0399, ts=1000.15))  # leg 2 ask moved away
    (rec,) = ex.settle(1000.2)
    assert rec.status == "partial"
    assert "leg2 ETHBTC: ask moved" in rec.reason and "leg3" in rec.reason
    assert ex.missed_legs == 1
    assert ex.balance(BINANCE, "BTC") == pytest.approx(rec.fills[0].qty)  # stuck holding BTC


def test_paper_arrival_rejects_unfundable_plans_up_front():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_base_inventory_usd=0.0), FEES, book, [BINANCE, KRAKEN])
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "no BTC on kraken" in rec.reason
    assert not ex.pending and ex.rejected == 1 and ex.missed_legs == 0


def test_paper_inventory_is_funded_once_and_tops_up_bought_holdings():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, slippage_bps=0.0, **INSTANT),
                       FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))  # buys 0.01 BTC on binance, sells 0.01 from kraken stock
    ex.balances[KRAKEN]["BTC"] = 0.0  # sold out later
    ex._ensure_inventory(KRAKEN, "BTC", 1000.0)
    assert ex.balance(KRAKEN, "BTC") == 0.0  # funded once: stays sold out
    ex._ensure_inventory(BINANCE, "BTC", 1000.0)  # binance only ever bought BTC: gets its stock on top
    assert ex.balance(BINANCE, "BTC") == pytest.approx(0.01 + 2000 / book.usd_price("BTC", 1000.0))
    assert (BINANCE, "BTC") in ex.contributed


def test_paper_equity_reports_sold_out_contributed_asset_as_unmarked_when_unpriceable():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_base_inventory_usd=2000, slippage_bps=0.0, **INSTANT), FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))
    ex.balances[KRAKEN]["BTC"] = 0.0
    book.invalidate_venue(BINANCE)
    book.invalidate_venue(KRAKEN)  # no BTC mark anywhere now
    equity, unmarked = ex.equity_usd(1000.0)
    assert "kraken:BTC" in unmarked  # so the engine skips the drawdown update instead of seeing phantom PnL


def test_paper_arrival_displayed_size_is_shared_between_plans():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=50000, starting_base_inventory_usd=20000, slippage_bps=0.0,
                                   assumed_rtt_ms=100), FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))
    run(ex.execute(opp, 1000.01))
    book.update(quote(BTC_BINANCE, 99999, 100000, ask_qty=0.015, ts=1000.05))  # only 0.015 displayed
    book.update(quote(BTC_KRAKEN, 100600, 100601, bid_qty=1, ts=1000.05))
    recs = ex.settle(1000.2)
    buys = [f.qty for r in recs for f in r.fills if f.side == "buy"]
    assert sorted(buys) == pytest.approx([0.005, 0.01])  # second plan gets what is left, not the full display


def test_paper_arrival_final_settles_everything():
    book, opp = book_with_cross_opp()
    ex = PaperExecutor(PaperConfig(starting_quote_per_venue_usd=5000, starting_base_inventory_usd=2000, assumed_rtt_ms=5000),
                       FEES, book, [BINANCE, KRAKEN])
    run(ex.execute(opp, 1000.0))
    assert ex.settle(1001.0) == []
    (rec,) = ex.settle(1001.0, final=True)
    assert rec.status == "filled" and not ex.pending


# ---------------------------------------------------------------- risk manager

def _opp(net=10.0, notional=90.0, ages=(10.0,), latency=1.0, kind="cross_exchange", legs=True):
    legs_ = [Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100.0, 0.5, 0.001)] if legs else []
    return Opportunity(kind, 1000.0, legs_, net + 20, net, notional, notional * net / 1e4, "t",
                       detect_latency_ms=latency, quote_ages_ms=list(ages))


def test_risk_manager_checks(tmp_path):
    rcfg = RiskConfig(max_notional_per_trade_usd=1000, max_daily_loss_usd=10, max_trades_per_minute=2, cooldown_s=5,
                      max_detect_latency_ms=50, kill_switch_file=str(tmp_path / "STOP"), min_profit_usd=0.05)
    det = DetectionConfig(min_net_edge_bps=1.0, max_quote_age_ms=2000, max_plausible_net_edge_bps=200)
    rm = RiskManager(rcfg, det)
    assert rm.check(_opp(), 1000.0) == (True, "")
    assert rm.check(_opp(net=0.5), 1000.0)[1] == "below min net edge"
    assert rm.check(_opp(net=500), 1000.0)[1] == "implausible edge (bad data?)"
    assert rm.check(_opp(ages=(6000,)), 1000.0)[1] == "stale quote"  # beyond even the 5 s Binance rule
    assert rm.check(_opp(latency=80), 1000.0)[1] == "slow detection"
    assert rm.check(_opp(notional=1500), 1000.0)[1] == "over max notional"
    assert rm.check(_opp(notional=20), 1000.0)[1] == "below min profit"  # 20 * 10 bps = 2 cents
    assert rm.check(_opp(legs=False), 1000.0)[1] == "report-only"
    # cooldown and rate limit
    rm.on_submitted(_opp(), 1000.0)
    rm.on_settled(TradeRecord(_opp(), [], "filled", "", -4.0, 1000.0), 1000.0)
    assert rm.check(_opp(), 1001.0)[1] == "cooldown"
    other = _opp(); other.legs[0].symbol = "ETHUSDT"
    assert rm.check(other, 1001.0) == (True, "")
    rm.on_submitted(other, 1001.0)
    rm.on_settled(TradeRecord(other, [], "filled", "", -4.0, 1001.0), 1001.0)
    third = _opp(); third.legs[0].symbol = "SOLUSDT"
    assert rm.check(third, 1002.0)[1] == "trade rate limit"
    # daily loss cap halts (and pending/missed records do not count)
    rm.on_settled(TradeRecord(third, [], "missed", "", -100.0, 1002.0), 1002.0)
    assert rm.daily_realized_usd == pytest.approx(-8.0)
    rm.on_settled(TradeRecord(third, [], "partial", "", -3.0, 1002.0), 1002.0)
    assert rm.daily_realized_usd == pytest.approx(-11.0)
    assert "daily loss cap" in rm.check(_opp(), 1100.0)[1]
    assert rm.halted and not rm.halt_sticky
    # kill switch beats everything, also per leg
    rm2 = RiskManager(rcfg, det)
    (tmp_path / "STOP").write_text("")
    assert "kill switch" in rm2.check(_opp(), 1000.0)[1]
    assert "kill switch" in rm2.allow_leg(1000.0)[1]
    assert rm2.rejections["kill switch file '%s' present" % rcfg.kill_switch_file] == 1


def test_risk_daily_loss_resets_on_new_utc_day_but_sticky_halt_does_not():
    rm = RiskManager(RiskConfig(max_daily_loss_usd=10), DetectionConfig())
    rm.check(_opp(), 1000.0)
    rm.on_settled(TradeRecord(_opp(), [], "filled", "", -20.0, 1000.0), 1000.0)
    assert rm.daily_realized_usd == -20.0
    assert "daily loss cap" in rm.check(_opp(), 1001.0)[1]
    assert rm.check(_opp(), 1000.0 + 86400)[0] is True
    assert rm.daily_realized_usd == 0.0
    rm.halt("ambiguous order", sticky=True)
    assert rm.check(_opp(), 1000.0 + 2 * 86400)[1] == "ambiguous order"


def test_risk_drawdown_from_peak_halts(tmp_path):
    state = tmp_path / "risk_state.json"
    rm = RiskManager(RiskConfig(max_drawdown_pct=5.0, max_daily_loss_usd=1e9), DetectionConfig(), state_file=state, now=1000.0)
    rec = TradeRecord(_opp(), [], "filled", "", 0.0, 1000.0)
    rm.on_settled(rec, 1000.0, pnl_usd=0.0, capital_usd=1000.0)
    rm.on_settled(rec, 1001.0, pnl_usd=40.0, capital_usd=1000.0)  # new peak
    rm.on_settled(rec, 1002.0, pnl_usd=0.0, capital_usd=1000.0)  # 40 USD = 4% of capital below peak: fine
    assert not rm.halted and rm.peak_pnl_usd == 40.0
    rm.on_settled(rec, 1003.0, pnl_usd=-11.0, capital_usd=1000.0)  # 51 USD = 5.1%
    assert rm.halted and "drawdown" in rm.halt_reason and not rm.halt_sticky
    assert "peak" not in json.loads(state.read_text())  # the curve is per session, not persisted
    rm2 = RiskManager(RiskConfig(max_drawdown_pct=5.0), DetectionConfig(), state_file=state, now=1000.0 + 86400)
    assert rm2.peak_pnl_usd is None and not rm2.halted
    rm3 = RiskManager(RiskConfig(max_drawdown_pct=0.0), DetectionConfig())
    rm3.on_settled(rec, 1000.0, pnl_usd=0.0, capital_usd=1000.0)
    rm3.on_settled(rec, 1001.0, pnl_usd=-500.0, capital_usd=1000.0)
    assert not rm3.halted  # disabled


def test_risk_staleness_is_per_venue():
    det = DetectionConfig(max_quote_age_ms=2000, max_quote_age_ms_by_venue={"binance": 5000.0})
    rm = RiskManager(RiskConfig(max_notional_per_trade_usd=1000), det)
    opp = _opp(ages=(3000.0,))  # a 3 s old Binance quote is still valid under the 5 s venue rule
    assert rm.check(opp, 1000.0) == (True, "")
    opp.legs[0].venue = COINBASE
    assert rm.check(opp, 1000.0)[1] == "stale quote"


def test_risk_state_survives_restart(tmp_path):
    state = tmp_path / "risk_state.json"
    rm = RiskManager(RiskConfig(max_daily_loss_usd=10), DetectionConfig(), state_file=state, now=1000.0)
    rm.check(_opp(), 1000.0)
    rm.on_settled(TradeRecord(_opp(), [], "filled", "", -12.0, 1000.0), 1000.0)
    assert "daily loss cap" in rm.check(_opp(), 1001.0)[1]
    data = json.loads(state.read_text())
    assert data["halted"] is True and data["daily_realized_usd"] == -12.0
    # same UTC day after a restart: still halted, loss remembered
    rm2 = RiskManager(RiskConfig(max_daily_loss_usd=10), DetectionConfig(), state_file=state, now=1500.0)
    assert rm2.daily_realized_usd == -12.0 and rm2.halted
    # next day: fresh
    rm3 = RiskManager(RiskConfig(max_daily_loss_usd=10), DetectionConfig(), state_file=state, now=1000.0 + 86400)
    assert rm3.daily_realized_usd == 0.0 and not rm3.halted
    # sticky halts survive days
    rm3.halt("reconcile me", sticky=True)
    rm4 = RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0 + 5 * 86400)
    assert rm4.halted and rm4.halt_reason == "reconcile me"


# ---------------------------------------------------------------- live executor

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
        self.headers = {"x-mbx-used-weight-1m": "7"}

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Scripted responses: a list of (matcher, status, payload) where matcher is a
    substring of the URL; an Exception payload is raised instead."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _ctx(self, method, url, data):
        self.calls.append((method, url, data))
        for i, (match, status, payload) in enumerate(self.script):
            if match in url:
                self.script.pop(i)
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(status, payload)
        pytest.fail(f"unscripted {method} {url}")  # escapes `except Exception` in the code under test

    def get(self, url, headers=None, timeout=None):
        return self._ctx("GET", url, None)

    def post(self, url, data=None, headers=None, timeout=None):
        return self._ctx("POST", url, data)

    def delete(self, url, headers=None, timeout=None):
        return self._ctx("DELETE", url, None)

    async def close(self):
        pass


TIME = ("/api/v3/time", 200, {"serverTime": 1789795661273})
PING = ("/api/v3/ping", 200, {})


def _live(book, session, real=False, cfg_real=False, risk=None, tmp_path=None, **cfg_kw):
    return BinanceLiveExecutor(LiveConfig(enabled=True, real_orders=cfg_real, **cfg_kw), "https://api.example", FEES,
                               book, real_orders=real, session=session, risk=risk,
                               intent_log=(tmp_path / "intents.jsonl") if tmp_path else None)


def test_live_executor_test_endpoint_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    session = FakeSession([TIME, ("/api/v3/order/test", 200, {}), ("/api/v3/order/test", 200, {}), ("/api/v3/order/test", 200, {})])
    ex = _live(book, session, real=False, cfg_real=True, tmp_path=tmp_path)
    assert ex.endpoint == "/api/v3/order/test"
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "test" and rec.realized_pnl_usd == 0.0
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 3 and all(c[1].endswith("/api/v3/order/test") for c in posts)
    body = posts[0][2]
    assert "symbol=BTCUSDT&side=BUY&type=LIMIT&timeInForce=IOC&price=100000&quantity=0.00999&newClientOrderId=arb" in body
    assert "&signature=" in body and "recvWindow=5000" in body and "timestamp=" in body
    assert ex.rest.used_weight_1m == 7
    intents = [json.loads(l) for l in (tmp_path / "intents.jsonl").read_text().splitlines()]
    assert [i["kind"] for i in intents] == ["intent", "response"] * 3
    assert intents[0]["params"]["newClientOrderId"].startswith("arb") and len(intents[0]["params"]["newClientOrderId"]) <= 36


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
    ex = _live(book, FakeSession([TIME]))
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "Binance-only" in rec.reason
    book.update(quote(BTC_BINANCE, 99999, 100000.0, ts=1000.0))
    tiny = Opportunity("triangular", 1000.0, [Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100000.0, 0.00001, 0.001)],
                       5, 5, 1.0, 0.0, "tiny")
    rec = run(ex.execute(tiny, 1000.0, min_edge_bps=-1e9))
    assert rec.status == "rejected" and "min_notional" in rec.reason


def test_live_executor_rechecks_kill_switch_and_edge_before_each_leg(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(kill_switch_file=str(tmp_path / "STOP")), DetectionConfig())
    ex = _live(book, FakeSession([TIME, ("/api/v3/order/test", 200, {})]), risk=risk)
    # edge decays after leg 1: ETHBTC ask jumps
    orig = ex._send

    async def send_then_move(params, client_id):
        payload = await orig(params, client_id)
        book.update(quote(ETHBTC_BINANCE, 0.0398, 0.0399, ts=1000.1))
        return payload

    ex._send = send_then_move
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "leg 2" in rec.reason and "moved" in rec.reason  # 76 bps: a dislocation
    # kill switch engaged before leg 1
    (tmp_path / "STOP").write_text("")
    ex2 = _live(book, FakeSession([TIME]), risk=risk)
    book, opp = triangle_book_and_opp()
    ex2.book = book
    rec = run(ex2.execute(opp, 1000.0))
    assert rec.status == "rejected" and "kill switch" in rec.reason


def test_live_executor_ambiguous_send_recovers_by_client_id_or_halts(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(), DetectionConfig())
    # 1) real orders: POST times out, lookup finds the order -> cycle continues
    session = FakeSession([TIME, ("/api/v3/order", 200, asyncio.TimeoutError()),
                           ("/api/v3/order?", 200, {"clientOrderId": "PLACEHOLDER"})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)

    real_ctx = session._ctx

    def ctx(method, url, data):
        if method == "GET" and "origClientOrderId=" in url:
            cid = url.split("origClientOrderId=")[1].split("&")[0]
            # Binance's query-order shape: status + executed quantities, no `fills`
            session.script = [(m, s, {"clientOrderId": cid, "status": "FILLED", "executedQty": "0.00999",
                                      "cummulativeQuoteQty": "999", "orderId": 42}
                               if m == "/api/v3/order?" else p) for (m, s, p) in session.script]
        return real_ctx(method, url, data)

    session._ctx = ctx
    session.script += [("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999",
                                                "fills": [{"commission": "0.00000999", "commissionAsset": "ETH"}], "orderId": 43}),
                       ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "0.00999",
                                                "fills": [{"commission": "0.00001", "commissionAsset": "USDT"}], "orderId": 44})]
    async def _instant(_s, *_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.fills[0].order_id == "42" and not risk.halted
    assert rec.fills[0].fee_asset == "BTC" and rec.fills[0].fee == pytest.approx(0.00999 * 0.001)  # schedule fee, received asset
    # 1b) real orders: the lookup says the order never existed -> nothing filled, nothing sent after it
    session_never = FakeSession([TIME, ("/api/v3/order", 200, asyncio.TimeoutError())]
                                + [("/api/v3/order?", 400, {"code": -2013, "msg": "Order does not exist."})] * 3)
    risk_never = RiskManager(RiskConfig(), DetectionConfig())
    ex_never = _live(book, session_never, real=True, cfg_real=True, risk=risk_never)
    rec = run(ex_never.execute(opp, 1000.0))
    assert rec.status == "rejected" and "never received" in rec.reason and rec.fills == []
    assert sum(1 for c in session_never.calls if c[0] == "POST") == 1 and not risk_never.halted
    # 1c) real orders: the IOC filled nothing (EXPIRED) -> no fabricated fill, cycle not continued
    session_zero = FakeSession([TIME, ("/api/v3/order", 200, {"status": "EXPIRED", "executedQty": "0.00000000",
                                                              "cummulativeQuoteQty": "0.00000000", "fills": [], "orderId": 45})])
    ex_zero = _live(book, session_zero, real=True, cfg_real=True, risk=RiskManager(RiskConfig(), DetectionConfig()))
    rec = run(ex_zero.execute(opp, 1000.0))
    assert rec.status == "rejected" and "filled nothing" in rec.reason and rec.fills == []
    assert sum(1 for c in session_zero.calls if c[0] == "POST") == 1
    # 2) real orders: POST times out and the lookup keeps failing -> sticky halt, no blind resend
    session2 = FakeSession([TIME, ("/api/v3/order", 200, asyncio.TimeoutError()),
                            ("/api/v3/order?", 200, ConnectionError("x")), ("/api/v3/order?", 200, ConnectionError("x")),
                            ("/api/v3/order?", 200, ConnectionError("x"))])
    risk2 = RiskManager(RiskConfig(), DetectionConfig())
    ex2 = _live(book, session2, real=True, cfg_real=True, risk=risk2)
    rec = run(ex2.execute(opp, 1000.0))
    assert rec.status == "rejected" and "ambiguous" in rec.reason.lower()
    assert risk2.halted and risk2.halt_sticky
    assert sum(1 for c in session2.calls if c[0] == "POST") == 1  # never resent


def test_live_unknown_status_answers_are_reconciled_not_dropped(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")

    async def _instant(_s, *_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    # 502 on a real POST: looked up by client id, found FILLED -> cycle continues
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(), DetectionConfig())
    session = FakeSession([TIME, ("/api/v3/order", 502, {"raw": "<html>bad gateway</html>"})])
    real_ctx = session._ctx

    def ctx(method, url, data):
        if method == "GET" and "origClientOrderId=" in url:
            cid = url.split("origClientOrderId=")[1].split("&")[0]
            return FakeResponse(200, {"clientOrderId": cid, "status": "FILLED", "executedQty": "0.00999",
                                      "cummulativeQuoteQty": "999", "orderId": 7})
        return real_ctx(method, url, data)

    session._ctx = ctx
    session.script += [("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "fills": [], "orderId": 8}),
                       ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "0.00999", "fills": [], "orderId": 9})]
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled" and rec.fills[0].order_id == "7" and not risk.halted
    assert sum(1 for c in session.calls if c[0] == "POST") == 3  # never resent
    # -1007 on leg 2 with the lookup failing: partial, sticky halt, loss of leg 1 booked
    book, opp = triangle_book_and_opp()
    risk2 = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session2 = FakeSession([TIME, ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "fills": [], "orderId": 1}),
                            ("/api/v3/order", 504, {"code": -1007, "msg": "Send status unknown; execution status unknown."}),
                            ("/api/v3/order?", 200, ConnectionError("x")), ("/api/v3/order?", 200, ConnectionError("x")),
                            ("/api/v3/order?", 200, ConnectionError("x"))])
    ex2 = _live(book, session2, real=True, cfg_real=True, risk=risk2)
    rec = run(ex2.execute(opp, 1000.0))
    assert rec.status == "partial" and risk2.halted and risk2.halt_sticky and "ambiguous" in rec.reason
    assert rec.realized_pnl_usd < 0 and len(rec.fills) == 1
    assert sum(1 for c in session2.calls if c[0] == "POST") == 2
    # an early -2013 is not proof: only the last lookup attempt may conclude the order never arrived
    book, opp = triangle_book_and_opp()
    session3 = FakeSession([TIME, ("/api/v3/order", 200, asyncio.TimeoutError()),
                            ("/api/v3/order?", 400, {"code": -2013, "msg": "Order does not exist."})])
    real3 = session3._ctx

    def ctx3(method, url, data):
        if method == "GET" and "origClientOrderId=" in url and not any(m == "/api/v3/order?" for m, _, _ in session3.script):
            cid = url.split("origClientOrderId=")[1].split("&")[0]
            return FakeResponse(200, {"clientOrderId": cid, "status": "FILLED", "executedQty": "0.00999",
                                      "cummulativeQuoteQty": "999", "orderId": 11})
        return real3(method, url, data)

    session3._ctx = ctx3
    session3.script += [("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "fills": [], "orderId": 12}),
                        ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "0.00999", "fills": [], "orderId": 13})]
    ex3 = _live(book, session3, real=True, cfg_real=True, risk=RiskManager(RiskConfig(), DetectionConfig()))
    rec = run(ex3.execute(opp, 1000.0))
    assert rec.status == "filled" and rec.fills[0].order_id == "11"  # the second lookup found it


def test_live_failure_after_a_real_fill_halts_and_books(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "fills": [], "orderId": 1})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk, tmp_path=tmp_path)
    orig = ex._journal

    def flaky_journal(entry):
        if entry.get("kind") == "response":
            raise OSError(28, "No space left on device")
        orig(entry)

    ex._journal = flaky_journal
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "fill state unknown" in rec.reason
    assert risk.halted and risk.halt_sticky and "fill state unknown" in risk.halt_reason
    assert sum(1 for c in session.calls if c[0] == "POST") == 1


def test_live_unpriceable_commission_is_booked_in_quote(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()  # no BNB market in this book
    session = FakeSession([TIME,
                           ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "orderId": 1,
                                                   "fills": [{"commission": "0.001", "commissionAsset": "BNB"}]}),
                           ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "0.00999", "orderId": 2,
                                                   "fills": [{"commission": "0.00025", "commissionAsset": "ETH"}]}),
                           ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "1000.2", "orderId": 3,
                                                   "fills": [{"commission": "1.0", "commissionAsset": "USDT"}]})])
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "filled"
    assert rec.fills[0].fees_by_asset == {"USDT": pytest.approx(999 * 0.001)}  # BNB fee booked at the schedule rate in USDT
    assert rec.fills[0].fee_in("BTC") == 0.0  # so the whole 0.00999 BTC carries to leg 2
    assert ex.unmarked_assets == {}
    assert rec.realized_pnl_usd == pytest.approx(-999 + 1000.2 - 0.999 - 1.0 - 0.00025 * book.usd_price("ETH", 1000.0), rel=1e-6)


def test_live_partial_cycle_books_its_loss_and_splits_commissions(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    # leg 1 fills with commission split across BNB and BTC; leg 2 is rejected by the venue
    session = FakeSession([TIME,
                           ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999.0", "orderId": 1,
                                                   "fills": [{"commission": "0.001", "commissionAsset": "BNB"},
                                                             {"commission": "0.000005", "commissionAsset": "BTC"}]}),
                           ("/api/v3/order", 400, {"code": -2010, "msg": "Account has insufficient balance for requested action."})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk, auto_unwind=False)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "partial" and risk.halted and risk.halt_sticky
    assert rec.unwound == "off" and rec.reason.endswith("NOT unwound: live.auto_unwind is off")
    # BNB has no market in this universe: its commission is booked at the schedule rate in USDT
    assert rec.fills[0].fees_by_asset == {"USDT": pytest.approx(999.0 * 0.001), "BTC": 0.000005}
    assert rec.fills[0].fee_in("BTC") == 0.000005  # only the BTC part reduces what we can sell on
    # realized = -999 USDT - 0.999 USDT fee + (0.00999 - 0.000005) BTC marked at mid (99999.5)
    expected = -999.0 - 0.999 + (0.00999 - 0.000005) * 99999.5
    assert rec.realized_pnl_usd == pytest.approx(expected, rel=1e-9)
    assert ex.realized_pnl_usd == pytest.approx(expected, rel=1e-9)
    risk.on_settled(rec, 1000.0)
    assert risk.daily_realized_usd == pytest.approx(expected, rel=1e-9)


def test_live_gates_whole_cycle_before_leg_one_and_only_dislocations_later(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    def thin_triangle():
        # USDT -> BTC -> ETH -> USDT with ~1.5 bps net: 100000 * 0.0396 = 3960; ETH bid 3972.6 -> +31.8 bps gross
        book = QuoteBook(max_age_s=2.0)
        markets = [BTC_BINANCE, ETH_BINANCE, ETHBTC_BINANCE]
        for m in markets:
            book.register(m)
        book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))
        book.update(quote(ETH_BINANCE, 3972.6, 3973, ts=1000.0))
        ethbtc = quote(ETHBTC_BINANCE, 0.0395, 0.0396, ts=1000.0)
        book.update(ethbtc)
        (opp,) = TriangularDetector(markets, FEES, DetectionConfig(), 1000.0).on_quote(ethbtc, book, 1000.0)
        assert 1.0 < opp.net_edge_bps < 3.0
        return book, opp

    book, opp = thin_triangle()
    # each leg drifts 0.9 bps against us: the cycle no longer clears a 1 bps minimum -> nothing sent
    book.update(quote(BTC_BINANCE, 99999, 100000 * 1.00009, ts=1000.0))
    book.update(quote(ETHBTC_BINANCE, 0.0395, 0.0396 * 1.00009, ts=1000.0))
    book.update(quote(ETH_BINANCE, 3972.6 * 0.99991, 3973, ts=1000.0))
    session = FakeSession([TIME])
    ex = _live(book, session)
    rec = run(ex.execute(opp, 1000.0, min_edge_bps=1.0))
    assert rec.status == "rejected" and "edge decayed" in rec.reason
    assert not [c for c in session.calls if c[0] == "POST"]
    # mid-cycle a 2 bps drift does not strand inventory: the cycle completes
    book, opp = thin_triangle()
    session = FakeSession([TIME, ("/api/v3/order/test", 200, {}), ("/api/v3/order/test", 200, {}), ("/api/v3/order/test", 200, {})])
    ex = _live(book, session)
    orig = ex._send

    async def send_then_drift(params, client_id):
        payload = await orig(params, client_id)
        if params["symbol"] == "BTCUSDT":
            book.update(quote(ETHBTC_BINANCE, 0.0395, 0.0396 * 1.0002, ts=1000.1))
        return payload

    ex._send = send_then_drift
    rec = run(ex.execute(opp, 1000.0, min_edge_bps=1.0))
    assert rec.status == "test" and len(rec.fills) == 3


def test_live_refuses_markets_without_filters(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    from tests.helpers import market
    bare = market(BINANCE, "BTCUSDT", "BTC", "USDT")  # static universe: no tick/step
    book = QuoteBook(max_age_s=2.0)
    book.register(bare)
    book.update(quote(bare, 99999, 100000, ts=1000.0))
    opp = Opportunity("triangular", 1000.0, [Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100000.0, 0.001, 0.001)], 5, 5, 100.0, 0.05, "x")
    session = FakeSession([TIME])
    ex = _live(book, session)
    rec = run(ex.execute(opp, 1000.0, min_edge_bps=-1e9))  # a one-leg "cycle" never clears a real edge gate
    assert rec.status == "rejected" and "no exchange filters" in rec.reason
    assert not [c for c in session.calls if c[0] == "POST"]
    pre = FakeSession([
        PING,
        ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
        ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
        ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
        ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "1000"}]}),
        ("/api/v3/order/test", 200, {}),
    ])
    problems = run(_live(book, pre).preflight(["BTCUSDT"]))
    assert problems == ["no exchange filters for BTCUSDT (use discovery, not --static)"]
    assert not pre.script  # every scripted call was made: no early reachability bail-out


def test_live_preflight_reports_a_sticky_halt(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    state = tmp_path / "risk_state.json"
    halted = RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0)
    halted.halt("reconcile me", sticky=True)
    book = QuoteBook()
    book.register(BTC_BINANCE)
    risk = RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0)
    session = FakeSession([
        PING,
        ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
        ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
        ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
        ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "1000"}]}),
        ("/api/v3/order/test", 200, {}),
    ])
    problems = run(_live(book, session, risk=risk).preflight(["BTCUSDT"]))
    assert problems == ["trading is halted: reconcile me (sticky: reconcile, then remove or edit the risk state file)"]
    assert not session.script


def test_live_preflight_reports_problems(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    session = FakeSession([
        PING,
        ("/api/v3/time", 200, {"serverTime": int(1e15)}),  # absurd skew
        ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "BREAK"}]}),
        ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": True, "enableSpotAndMarginTrading": True}),
        ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "12.5"}]}),
        ("/api/v3/order/test", 200, {}),
    ])
    ex = _live(book, session)
    problems = run(ex.preflight(["BTCUSDT"]))
    joined = " | ".join(problems)
    assert "clock skew" in joined and "BTCUSDT: status BREAK" in joined and "WITHDRAW" in joined and "free USDT 12.50" in joined
    ok_session = FakeSession([
        PING,
        ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
        ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
        ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
        ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "1000"}]}),
        ("/api/v3/order/test", 200, {}),
    ])
    book.register(BTC_BINANCE)
    assert run(_live(book, ok_session).preflight(["BTCUSDT"])) == []
    assert not ok_session.script


def test_live_preflight_refuses_a_geo_blocked_machine_before_any_signed_call(monkeypatch, tmp_path):
    """HTTP 451 from the TRADING host (the public mirror answers everywhere) is reported in
    plain words, nothing signed leaves the machine, and the local checks still run."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    book.register(BTC_BINANCE)
    session = FakeSession([("/api/v3/ping", 451, {"raw": "<html>Unavailable For Legal Reasons</html>"})])
    (tmp_path / "STOP").write_text("")
    risk = RiskManager(RiskConfig(kill_switch_file=str(tmp_path / "STOP")), DetectionConfig(), now=1000.0)
    problems = run(_live(book, session, risk=risk).preflight(["BTCUSDT"]))
    assert len(problems) == 2
    assert "HTTP 451" in problems[0] and "VPN" in problems[0] and "api.example" in problems[0]
    assert "kill switch" in problems[1]
    assert [c[0] for c in session.calls] == ["GET"] and "/api/v3/ping" in session.calls[0][1]
    assert "signature=" not in session.calls[0][1]


def test_live_preflight_names_other_reachability_failures(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    book.register(BTC_BINANCE)
    forbidden = run(_live(book, FakeSession([("/api/v3/ping", 403, {"raw": "blocked"})])).preflight(["BTCUSDT"]))
    assert forbidden == [p for p in forbidden if "HTTP 403" in p] and len(forbidden) == 1
    down = run(_live(book, FakeSession([("/api/v3/ping", 0, ConnectionError("no route to host"))])).preflight(["BTCUSDT"]))
    assert len(down) == 1 and down[0].startswith("cannot reach https://api.example") and "no route to host" in down[0]
    teapot = run(_live(book, FakeSession([("/api/v3/ping", 418, {"code": -1003, "msg": "banned"})])).preflight(["BTCUSDT"]))
    assert len(teapot) == 1 and "HTTP 418" in teapot[0] and "banned" in teapot[0]
    # a CDN maintenance page arrives as a non-JSON body: its text is shown, on one line
    html = run(_live(book, FakeSession([("/api/v3/ping", 502, {"raw": "<html>\n  Unavailable\n</html>"})])).preflight(["BTCUSDT"]))
    assert len(html) == 1 and "HTTP 502" in html[0] and "<html> Unavailable </html>" in html[0] and "\n" not in html[0]

    class EmptyBody(FakeResponse):
        async def text(self):
            return ""

    class EmptySession(FakeSession):
        def _ctx(self, method, url, data):
            self.calls.append((method, url, data))
            return EmptyBody(503, None)

    empty = run(_live(book, EmptySession([])).preflight(["BTCUSDT"]))
    assert len(empty) == 1 and "HTTP 503" in empty[0] and "(empty body)" in empty[0]


def test_live_preflight_refuses_a_200_that_is_not_binances(monkeypatch):
    """A TLS-trusted interceptor (corporate web filter, captive portal) can answer 200 itself;
    only Binance's documented ping body, exactly {}, counts as reaching the exchange."""
    from arbbot.cli import connectivity_check
    from arbbot.config import load_config

    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    book.register(BTC_BINANCE)
    for body in ({"raw": "<html>portal</html>"}, {"message": "ok"}, [], "blocked"):
        session = FakeSession([("/api/v3/ping", 200, body)])
        problems = run(_live(book, session).preflight(["BTCUSDT"]))
        assert len(problems) == 1 and "not Binance's" in problems[0], body
        assert [c[0] for c in session.calls] == ["GET"]  # no signed call left the machine
    cfg = load_config(None, {"venues": {"binance_trade_rest": "https://trade.example"}})
    rc, msg = run(connectivity_check(cfg, FakeSession([("/api/v3/ping", 200, {"raw": "<html>"})])))
    assert rc == 1 and msg.startswith("FAIL") and "not Binance's" in msg
    rc, msg = run(connectivity_check(cfg, FakeSession([("/api/v3/ping", 200, {})])))
    assert rc == 0


def test_connectivity_check_needs_no_keys_and_no_live_section(monkeypatch, capsys):
    """`arbbot preflight --connectivity` is the step-0b check: keyless, and honest about a 451."""
    from arbbot.cli import build_parser, cmd_preflight, connectivity_check
    from arbbot.config import load_config

    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    cfg = load_config(None, {"venues": {"binance_trade_rest": "https://trade.example", "binance_rest": "https://mirror.example"}})
    ok = FakeSession([("/api/v3/ping", 200, {})])
    rc, msg = run(connectivity_check(cfg, ok))
    assert rc == 0 and msg.startswith("OK") and "trade.example" in msg
    assert ok.calls[0][1].startswith("https://trade.example/api/v3/ping")  # the trading host, not the mirror
    blocked = FakeSession([("/api/v3/ping", 451, {"raw": "<html>"})])
    rc, msg = run(connectivity_check(cfg, blocked))
    assert rc == 1 and msg.startswith("FAIL") and "HTTP 451" in msg and "VPN" in msg
    # the CLI flag parses and does not demand [live] enabled or keys; a scripted session is injected
    # by patching the check the command calls, so no real socket is opened
    args = build_parser().parse_args(["preflight", "--connectivity"])
    assert args.connectivity is True

    async def fake_check(cfg, session=None):
        return 1, "FAIL https://api.binance.com answered HTTP 451 (test)"

    monkeypatch.setattr("arbbot.cli.connectivity_check", fake_check)
    assert run(cmd_preflight(args)) == 1
    assert "HTTP 451" in capsys.readouterr().err


def test_live_executor_html_error_body_is_a_rejection_not_a_crash(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()

    class HtmlResponse(FakeResponse):
        async def text(self):
            return "<html>404 Not Found</html>"

    session = FakeSession([TIME])
    real_ctx = session._ctx

    def ctx(method, url, data):
        if "/api/v3/order/test" in url:
            session.calls.append((method, url, data))
            return HtmlResponse(404, None)
        return real_ctx(method, url, data)

    session._ctx = ctx
    ex = _live(book, session)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "HTTP 404" in rec.reason


def test_live_preflight_wants_the_declared_stake_on_the_exchange(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook()
    book.register(BTC_BINANCE)

    def session(free_usdt):
        return FakeSession([
            PING,
            ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
            ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
            ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
            ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": free_usdt}]}),
            ("/api/v3/order/test", 200, {}),
        ])

    def staked(sess):
        ex = BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=500.0), "https://api.example", FEES, book,
                                 real_orders=False, session=sess, max_notional_usd=50.0)
        assert ex.capital_usd == 500.0
        return ex

    # inside the 10% kill budget the stake may be down and still re-arm: a losing day, a crash,
    # a reconciled halt or BNB held for fees must not lock the operator out
    for free in ("500", "499.92", "495", "490", "460", "450"):
        assert run(staked(session(free)).preflight(["BTCUSDT"])) == [], free
    for free in ("449.98", "440", "300"):
        short = run(staked(session(free)).preflight(["BTCUSDT"]))
        assert len(short) == 1 and "below the kill floor 450.00" in short[0] and "do not restart" in short[0], free
    # the message can never read "500.00 is below ... 500.00"
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=500.0, max_cumulative_loss_pct=0.0), "https://api.example",
                             FEES, book, real_orders=False, session=session("499.999"), max_notional_usd=50.0)
    assert run(ex.preflight(["BTCUSDT"])) == []


def test_live_pre_send_failure_after_a_fill_books_the_half_cycle(monkeypatch, tmp_path):
    """A full disk under the intent journal on leg 2 must not escape as a crash that books
    nothing: the leg-1 fill is marked, the record is partial and the halt is sticky."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999", "fills": [], "orderId": 1})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk, tmp_path=tmp_path, auto_unwind=False)
    orig = ex._journal
    intents = []

    def flaky_journal(entry):
        if entry.get("kind") == "intent":
            intents.append(entry)
            if len(intents) == 2:
                raise OSError(28, "No space left on device")
        orig(entry)

    ex._journal = flaky_journal
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "partial" and "before send" in rec.reason and len(rec.fills) == 1
    assert rec.realized_pnl_usd == pytest.approx(-1.004, abs=0.01)  # the bought BTC marked, less the scheduled fee
    assert ex.realized_pnl_usd == rec.realized_pnl_usd
    assert risk.halted and risk.halt_sticky and "cycle aborted mid-way" in risk.halt_reason
    assert sum(1 for c in session.calls if c[0] == "POST") == 1
    # the same failure on the FIRST leg sent nothing: rejected, nothing booked, no halt
    risk2 = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    ex2 = _live(book, FakeSession([TIME]), real=True, cfg_real=True, risk=risk2, tmp_path=tmp_path, auto_unwind=False)

    def dead_journal(entry):
        raise OSError(28, "No space left on device")

    ex2._journal = dead_journal
    rec2 = run(ex2.execute(opp, 1000.0))
    assert rec2.status == "rejected" and rec2.fills == [] and rec2.realized_pnl_usd == 0.0 and not risk2.halted


def test_risk_drawdown_budget_counts_the_first_loss_and_rebases_each_day():
    rm = RiskManager(RiskConfig(max_drawdown_pct=5.0, max_daily_loss_usd=1e9), DetectionConfig(), now=1000.0)
    rm._roll_day(1000.0)
    assert rm.note_pnl(-13.0, 500.0) is False and rm.peak_pnl_usd == 0.0  # the curve starts at zero
    assert rm.note_pnl(-26.0, 500.0) is True and rm.halted and not rm.halt_sticky  # 26 > 5% of 500
    # a first loss past the budget halts at once
    fresh = RiskManager(RiskConfig(max_drawdown_pct=5.0, max_daily_loss_usd=1e9), DetectionConfig())
    assert fresh.note_pnl(-30.0, 500.0) is True
    # the UTC day rolls: the halt lifts and the budget re-bases to the day's opening PnL,
    # exactly what a restart (executor PnL back to 0, peak seeded at 0) would grant
    rm._roll_day(1000.0 + 86400)
    assert not rm.halted and rm.peak_pnl_usd == -26.0
    assert rm.note_pnl(-40.0, 500.0) is False  # 14 below the day's opening PnL
    assert rm.note_pnl(-52.0, 500.0) is True  # 26 below it
    # a run-up inside the day raises the peak as before
    rm2 = RiskManager(RiskConfig(max_drawdown_pct=5.0, max_daily_loss_usd=1e9), DetectionConfig())
    assert rm2.note_pnl(40.0, 500.0) is False and rm2.peak_pnl_usd == 40.0
    assert rm2.note_pnl(16.0, 500.0) is False and rm2.note_pnl(14.0, 500.0) is True


def test_live_read_usdt_rejects_a_payload_that_is_not_an_account(monkeypatch):
    """An empty or non-JSON 200 must raise, never read as a zero balance: the compounding
    kill floor is measured on this number and a zero would halt the bot sticky."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    session = FakeSession([
        ("/api/v3/account", 200, {"balances": [{"asset": "BNB", "free": "1"}, {"asset": "USDT", "free": "850.5", "locked": "100"}]}),
        ("/api/v3/account", 200, {}),
        ("/api/v3/account", 200, {"raw": "<html>"}),
        ("/api/v3/account", 200, {"balances": [{"asset": "BNB", "free": "1"}]}),
    ])
    ex = _live(book, session)
    assert run(ex.read_usdt()) == (850.5, 100.0)
    with pytest.raises(ValueError, match="without balances"):
        run(ex.read_usdt())
    with pytest.raises(ValueError, match="without balances"):
        run(ex.read_usdt())
    assert run(ex.read_usdt()) == (0.0, 0.0)  # omitZeroBalances: no USDT row is a zero balance


def test_live_kill_floor_is_anchored_to_the_declared_capital(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=1000.0, max_cumulative_loss_pct=10.0, compound=True),
                             "https://api.example", FEES, book, real_orders=False, session=FakeSession([]))
    assert ex.compound and ex.initial_capital_usd == 1000.0 and ex.kill_floor_usd == 900.0
    ex.capital_usd = 1300.0  # what a re-base does after a winning day
    assert ex.kill_floor_usd == 900.0 and ex.initial_capital_usd == 1000.0


def test_live_preflight_counts_locked_usdt_toward_the_kill_floor(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)

    def session(free, locked):
        return FakeSession([
            PING,
            ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
            ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
            ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
            ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": free, "locked": locked}]}),
            ("/api/v3/order/test", 200, {}),
        ])

    def staked(sess):
        return BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=500.0), "https://api.example", FEES, book,
                                   real_orders=False, session=sess, max_notional_usd=50.0)

    assert run(staked(session("400", "60")).preflight(["BTCUSDT"])) == []  # 460 on the exchange: inside the budget
    problems = run(staked(session("400", "40")).preflight(["BTCUSDT"]))  # 440: the budget is spent
    assert len(problems) == 1 and "USDT 440.00 is below the kill floor 450.00" in problems[0]


def test_live_preflight_prints_the_compounding_ratios(monkeypatch, caplog):
    """live.capital_usd is the DENOMINATOR of the compounding ratios, not a multiplier of the
    caps. Preflight prints what the file actually derives, so a stake raised on its own (which
    shrinks both ratios) is visible on day 0 instead of at the next re-base."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)

    def session():
        return FakeSession([
            PING,
            ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
            ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
            ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
            ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "2000", "locked": "0"}]}),
            ("/api/v3/order/test", 200, {}),
        ])

    def preflight(capital, notional, daily):
        risk = RiskManager(RiskConfig(max_daily_loss_usd=daily), DetectionConfig(), now=1000.0)
        ex = BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=capital, compound=True), "https://api.example",
                                 FEES, book, real_orders=False, session=session(), risk=risk, max_notional_usd=notional)
        with caplog.at_level("INFO", logger="arbbot.execution.live"):
            caplog.clear()
            assert run(ex.preflight(["BTCUSDT"])) == []
        return "\n".join(r.getMessage() for r in caplog.records)

    shipped = preflight(1000.0, 100.0, 10.0)  # the shipped file: 10% per trade, 1% a day
    assert "compounding on" in shipped and "10.00% of the stake per trade" in shipped and "1.00% a day" in shipped
    assert "kill floor 900.00" in shipped

    mis_scaled = preflight(2000.0, 100.0, 10.0)  # capital_usd doubled, the caps left behind
    assert "5.00% of the stake per trade" in mis_scaled and "0.50% a day" in mis_scaled


def test_live_preflight_stays_quiet_about_ratios_without_compounding(monkeypatch, caplog):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    session = FakeSession([
        PING,
        ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
        ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
        ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
        ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "2000", "locked": "0"}]}),
        ("/api/v3/order/test", 200, {}),
    ])
    ex = BinanceLiveExecutor(LiveConfig(enabled=True, capital_usd=1000.0), "https://api.example", FEES, book,
                             real_orders=False, session=session, max_notional_usd=100.0)
    with caplog.at_level("INFO", logger="arbbot.execution.live"):
        assert run(ex.preflight(["BTCUSDT"])) == []
    assert "compounding on" not in "\n".join(r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ auto-unwind
# A cycle that fills leg 1 and then fails leaves the account holding an asset it never
# wanted. These pin what the bot does about that instead of halting and waiting for a human.

LEG1_FILL = ("/api/v3/order", 200, {"executedQty": "0.00999", "cummulativeQuoteQty": "999.0",
                                    "fills": [], "orderId": 1, "status": "FILLED"})
REJECT_2010 = ("/api/v3/order", 400, {"code": -2010, "msg": "Account has insufficient balance for requested action."})
# leg 1 above leaves deltas = {USDT: -999.0, BTC: 0.00998001} (0.00999 less the 0.1% schedule fee)
STRANDED_BTC = 0.00998001


def _unwind_fill(qty, quote_qty, status="FILLED", order_id=2):
    return ("/api/v3/order", 200, {"executedQty": qty, "cummulativeQuoteQty": quote_qty, "status": status,
                                   "orderId": order_id,
                                   "fills": [{"commission": str(round(float(quote_qty) * 0.001, 8)),
                                              "commissionAsset": "USDT"}]})


def _posts(session):
    return [c for c in session.calls if c[0] == "POST"]


def _record_sleeps(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("arbbot.execution.live.asyncio.sleep", fake_sleep)
    return slept


def test_unwind_sells_the_stranded_leg_back_to_usdt_and_keeps_trading(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, _unwind_fill("0.00998", "995.495")])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 3  # two cycle legs, then one unwind
    body = _posts(session)[2][2]
    assert body.startswith("symbol=BTCUSDT&side=SELL&type=LIMIT&timeInForce=IOC&price=99749.00&quantity=0.00998")
    assert "newClientOrderId=unw" in body
    assert rec.status == "partial" and rec.unwound == "flat" and "unwound, cycle flat" in rec.reason
    assert not risk.halted and not risk.halt_sticky  # the bot resumed its own halt and keeps trading
    assert len(rec.fills) == 2 and rec.fills[1].side == "sell"
    # -999.0 spent, 995.495 back, 0.995495 USDT commission: the true cost of the round trip
    assert rec.realized_pnl_usd == pytest.approx(-4.500495)
    assert ex.realized_pnl_usd == pytest.approx(-4.500495)
    # the 1e-8 BTC tail is below the lot step: written off at zero, never marked at mid
    assert ex.dust_assets["BTC"] == pytest.approx(1e-8, rel=1e-3)
    assert rec.opportunity.extra["unwind"]["orders"][0]["symbol"] == "BTCUSDT"
    risk.on_settled(rec, 1000.0, pnl_usd=rec.realized_pnl_usd, capital_usd=1000.0)
    assert risk.daily_realized_usd == pytest.approx(-4.500495)  # the loss still reaches the daily cap


def test_unwind_never_runs_when_the_fill_state_is_unknown(monkeypatch, tmp_path):
    """Selling an asset we may not hold, or may hold twice, is worse than halting."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")

    # (a) a 5xx the lookup cannot resolve
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL,
                           ("/api/v3/order", 504, {"code": -1007, "msg": "Timeout waiting for response"}),
                           ("/api/v3/order", 0, ConnectionError("down")),
                           ("/api/v3/order", 0, ConnectionError("down")),
                           ("/api/v3/order", 0, ConnectionError("down"))])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "skipped"
    assert risk.halted and risk.halt_sticky and "ambiguous" in risk.halt_reason
    assert "ALSO: cycle aborted mid-way" in risk.halt_reason  # the more specific halt is kept

    # (b) a post-send local failure: we never learned what the venue did
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, ("/api/v3/order", 200, {"executedQty": "0.25", "cummulativeQuoteQty": "0.0099",
                                                                    "fills": [], "orderId": 2, "status": "FILLED"})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk, tmp_path=tmp_path)
    orig = ex._journal
    ex._journal = lambda e: (_ for _ in ()).throw(OSError(28, "no space")) if e.get("kind") == "response" and e.get("client_id", "").startswith("arb") and len(_posts(session)) == 2 else orig(e)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.unwound == "skipped" and "fill state unknown" in rec.reason
    assert len(_posts(session)) == 2

    # (c) the middlebox case: an HTTP 400 whose body never came from Binance
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, ("/api/v3/order", 400, {"raw": "<html>blocked by proxy</html>"})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "skipped"
    assert "fill state unknown" in rec.reason and risk.halt_sticky


def test_unwind_stops_dead_and_never_retries_a_definitive_rejection(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")

    # (a) the venue says we do not hold it: our position model is wrong, so stop
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, REJECT_2010])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 3  # never a fourth
    assert rec.unwound == "failed" and "our position model is wrong" in risk.halt_reason
    assert risk.halted and risk.halt_sticky

    # (b) a rate limit during the unwind: the sender's own halt reason survives
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, ("/api/v3/order", 418, {"code": -1003, "msg": "banned"})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 3 and rec.unwound == "failed"
    assert "back off before re-arming" in risk.halt_reason  # resume refused: not our halt

    # (c) the CYCLE leg was rate limited: no unwind order at all
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, ("/api/v3/order", 429, {"code": -1003, "msg": "too many"})])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "skipped"
    assert "no further orders until the ban clears" in rec.reason


def test_unwind_is_refused_while_the_stop_file_exists(monkeypatch, tmp_path):
    """STOP is the one instruction that comes straight from a human and means send nothing."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    stop = tmp_path / "STOP"
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9, kill_switch_file=str(stop)), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    inner = ex._send

    async def send_then_stop(params, client_id):
        payload = await inner(params, client_id)
        stop.write_text("")  # the operator hits STOP while leg 1 is in flight
        return payload

    ex._send = send_then_stop
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 1 and rec.unwound == "skipped"
    assert "kill switch" in rec.reason and f"{STRANDED_BTC:.8f} BTC" in risk.halt_reason
    assert "BTCUSDT" in risk.halt_reason and risk.halt_sticky


def test_unwind_runs_while_the_risk_manager_is_already_halted(monkeypatch):
    """A halt stops the bot OPENING risk. Refusing to flatten because we are halted for
    holding inventory would be circular, so a reducing order still goes."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, _unwind_fill("0.00998", "995.495")])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    inner = ex._send

    async def send_then_halt(params, client_id):
        payload = await inner(params, client_id)
        if client_id.startswith("arb"):
            risk.halt("daily loss cap hit (-12.00 USD)", sticky=False)
        return payload

    ex._send = send_then_halt
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "flat"  # leg 1, then the unwind
    assert risk.halted, "a halt we did not raise must survive our unwind"
    assert "daily loss cap hit" in risk.halt_reason and "ALSO: cycle aborted mid-way" in risk.halt_reason


def test_unwind_retries_a_partial_fill_then_writes_the_tail_off_as_dust(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    slept = _record_sleeps(monkeypatch)
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010,
                           _unwind_fill("0.00500", "498.745", status="EXPIRED"),
                           _unwind_fill("0.00498", "496.71", order_id=3)])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 4  # never a fifth
    assert "quantity=0.00498" in _posts(session)[3][2]  # the remainder, recomputed from the ledger
    assert "price=99749.00" in _posts(session)[3][2]
    assert rec.unwound == "flat" and not risk.halted and len(rec.fills) == 3
    assert ex.dust_assets == {"BTC": pytest.approx(1e-8, rel=1e-3)}
    assert slept == [0.25]


def test_unwind_prices_every_attempt_against_the_touch_at_the_abort(monkeypatch):
    """The bound is anchored once. Re-deriving it from the current touch would follow a
    falling book all the way down while every single order honoured its own bound."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    slept = _record_sleeps(monkeypatch)
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    nothing = ("/api/v3/order", 200, {"executedQty": "0", "cummulativeQuoteQty": "0", "status": "EXPIRED", "fills": []})
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, nothing, nothing, nothing])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    inner = ex._send

    async def send_then_drop(params, client_id):
        payload = await inner(params, client_id)
        if client_id.startswith("unw"):
            book.update(quote(BTC_BINANCE, 99000, 99001, ts=1000.0))  # the book falls under us
        return payload

    ex._send = send_then_drop
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 5
    assert "price=99749.00" in _posts(session)[4][2]  # still the anchor, not 98752.50
    assert rec.unwound == "failed" and risk.halted and risk.halt_sticky
    assert f"{STRANDED_BTC:.8f} BTC" in risk.halt_reason and "3 attempt(s)" in risk.halt_reason
    assert slept == [0.25, 0.75]


def test_unwind_refuses_without_a_fresh_quote(monkeypatch):
    """The freshness gate is the stand-in for 'is this symbol still trading', and it only
    works because the unwind ages the book against real elapsed time."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    ex._now_plus_elapsed = lambda now, t0: now + 10.0  # the book's max_age_s is 2.0
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "failed"
    assert "no fresh sane quote" in rec.reason and risk.halt_sticky


def test_stranded_ranks_by_usd_and_ignores_fee_shorts_and_the_start_asset(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    ex = _live(book, FakeSession([]))
    only_btc = ex._stranded(opp, {"USDT": -999.0, "BNB": -0.001, "BTC": STRANDED_BTC}, 1000.0)
    assert [(a, s) for a, _q, s, _u in only_btc] == [("BTC", "BTCUSDT")]  # the fee short is not a position
    assert only_btc[0][3] == pytest.approx(STRANDED_BTC * 99999.5)
    both = ex._stranded(opp, {"USDT": -999.0, "BTC": 2e-06, "ETH": 0.25}, 1000.0)
    assert [a for a, _q, _s, _u in both] == ["ETH", "BTC"]  # biggest exposure first
    (xrp,) = ex._stranded(opp, {"XRP": 10.0}, 1000.0)
    assert xrp[2] == "" and xrp[3] is None  # unroutable and unpriceable, but never silently dropped


def test_unwind_halts_when_no_leg_sells_the_stranded_asset(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    session = FakeSession([])
    ex = _live(book, session, real=True, cfg_real=True, risk=RiskManager(RiskConfig(), DetectionConfig()))
    left = run(ex._unwind(opp, [], {"USDT": -999.0, "XRP": 10.0}, 1000.0, ex._t0))
    assert left.startswith("no leg of this cycle sells") and not _posts(session)


def test_unwind_halts_when_the_venue_rejects_a_residue_worth_more_than_dust(monkeypatch):
    """A filter rejection on something worth real money means our cached filters or our mark
    disagree with the venue. That is a broken assumption, not dust."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    inner = ex._send

    async def send_then_raise_the_floor(params, client_id):
        payload = await inner(params, client_id)
        # the venue's real minimum turns out to be far above what we cached
        book.register(Market(BINANCE, "BTCUSDT", "BTC", "USDT", tick_size=0.01, step_size=0.00001,
                             min_qty=0.00001, min_notional=10000.0))
        return payload

    ex._send = send_then_raise_the_floor
    rec = run(ex.execute(opp, 1000.0))
    assert len(_posts(session)) == 2 and rec.unwound == "failed"
    assert "our cached filters disagree with the venue" in rec.reason and risk.halt_sticky
    assert ex.dust_assets == {}  # nothing was written off


def test_unwind_halts_on_disk_before_the_first_unwind_order_leaves(monkeypatch, tmp_path):
    """A crash at any instant between 'we hold something' and 'we are flat' must come back
    to a halted process."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    state = tmp_path / "risk.json"
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig(), state_file=state, now=1000.0)
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, _unwind_fill("0.00998", "995.495")])
    ex = _live(book, session, real=True, cfg_real=True, risk=risk, tmp_path=tmp_path)
    seen = {}
    inner = ex._send

    async def capture(params, client_id):
        if client_id.startswith("unw"):
            seen.update(json.loads(state.read_text()))
        return await inner(params, client_id)

    ex._send = capture
    rec = run(ex.execute(opp, 1000.0))
    assert seen["halted"] is True and seen["halt_sticky"] is True and seen["halt_token"]
    assert rec.unwound == "flat"
    after = json.loads(state.read_text())
    assert after["halted"] is False and after["halt_token"] == ""
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "intents.jsonl").read_text().splitlines()]
    assert kinds == ["intent", "response", "intent", "error", "unwind_plan", "unwind_intent", "unwind_response",
                     "unwind_dust", "unwind_done"]
    cid = [json.loads(l) for l in (tmp_path / "intents.jsonl").read_text().splitlines()
           if json.loads(l)["kind"] == "unwind_intent"][0]["client_id"]
    assert cid.startswith("unw") and len(cid) <= 36
    # a fresh manager on the same file comes back clean, because the unwind finished
    assert not RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0).halted


def test_risk_resume_only_lifts_its_own_halt(tmp_path):
    state = tmp_path / "risk.json"
    rm = RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0)
    rm.halt("cycle aborted mid-way", sticky=True, token="unw:A")
    assert rm.resume("unw:B") is False and rm.halted
    assert rm.resume("") is False and rm.halted
    assert rm.resume("unw:A") is True and not rm.halted and not rm.halt_sticky and rm.halt_token == ""
    rm.halt("binance rate limit (418); back off before re-arming", sticky=True)  # no token
    assert rm.resume("unw:A") is False and rm.halted and rm.halt_sticky
    reloaded = RiskManager(RiskConfig(), DetectionConfig(), state_file=state, now=1000.0)
    assert reloaded.halted and reloaded.halt_token == ""  # a halt from a dead process is never resumable


def test_position_known_after_and_send_blocked_by():
    from arbbot.execution.live import BinanceHTTPError, OrderNeverArrived, position_known_after, send_blocked_by
    assert position_known_after(None) is True
    assert position_known_after(OrderNeverArrived("never arrived")) is True
    assert position_known_after(AmbiguousOrderState("unknown")) is False
    assert position_known_after(BinanceHTTPError(400, {"code": -2010, "msg": "no balance"})) is True
    assert position_known_after(BinanceHTTPError(400, {"raw": "<html>blocked</html>"})) is False
    assert position_known_after(BinanceHTTPError(404, None)) is False
    assert position_known_after(BinanceHTTPError(502, {"code": -1007, "msg": "timeout"})) is False
    assert position_known_after(asyncio.TimeoutError()) is False
    assert position_known_after(asyncio.CancelledError()) is False
    assert send_blocked_by(BinanceHTTPError(418, {"code": -1003})) is not None
    assert send_blocked_by(BinanceHTTPError(429, {"code": -1003})) is not None
    assert send_blocked_by(BinanceHTTPError(400, {"code": -2010})) is None
    assert send_blocked_by(None) is None


def test_reconcile_requires_a_terminal_order_status(monkeypatch):
    """A lookup can race the matching engine. A NEW snapshot is not an outcome, and reading
    it as one writes off an order the venue is about to fill."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    working = ("/api/v3/order", 200, {"clientOrderId": None, "status": "NEW", "executedQty": "0",
                                      "cummulativeQuoteQty": "0"})
    session = FakeSession([TIME, ("/api/v3/order", 0, asyncio.TimeoutError()), working, working, working])

    def same_id(url):
        return session

    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    # the lookup echoes whatever client id was asked for
    orig_get = session.get

    def echoing_get(url, headers=None, timeout=None):
        cid = url.split("origClientOrderId=")[1].split("&")[0] if "origClientOrderId=" in url else None
        for entry in session.script:
            if "/api/v3/order" in entry[0] and isinstance(entry[2], dict) and entry[2].get("status") == "NEW":
                entry[2]["clientOrderId"] = cid
                break
        return orig_get(url, headers=headers, timeout=timeout)

    session.script = [list(e) for e in session.script]
    session.get = echoing_get
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "rejected" and "ambiguous" in rec.reason.lower()
    assert risk.halted and risk.halt_sticky and len(_posts(session)) == 1


def test_unwind_circuit_breaker_halts_after_repeated_unwinds(monkeypatch):
    """A successful unwind resumes trading, so the next cycle can strand again. Breaking this
    often means the edge model or the venue is misbehaving."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    ex = _live(book, FakeSession([]), real=True, cfg_real=True, risk=risk)
    for i in range(4):
        book, opp = triangle_book_and_opp()
        ex.book = book
        ex.rest._session_obj = None
        session = FakeSession([TIME, LEG1_FILL, REJECT_2010, _unwind_fill("0.00998", "995.495")])
        ex.rest.session = session
        rec = run(ex.execute(opp, 1000.0))
        assert rec.unwound == "flat", i
        if i < 3:
            assert not risk.halted, i
    assert risk.halted and risk.halt_sticky and "were unwound in the last hour" in risk.halt_reason


def test_unwind_does_not_resume_when_an_asset_cannot_be_priced(monkeypatch):
    """Resuming on a realized number we know is incomplete would hide the loss."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, opp = triangle_book_and_opp()
    risk = RiskManager(RiskConfig(max_daily_loss_usd=1e9), DetectionConfig())
    unwind_with_bnb_fee = ("/api/v3/order", 200, {"executedQty": "0.00998", "cummulativeQuoteQty": "995.495",
                                                  "status": "FILLED", "orderId": 2,
                                                  "fills": [{"commission": "0.001", "commissionAsset": "BNB"}]})
    session = FakeSession([TIME, LEG1_FILL, REJECT_2010, unwind_with_bnb_fee])
    book.register(Market(BINANCE, "BNBUSDT", "BNB", "USDT", tick_size=0.01, step_size=0.001, min_qty=0.001,
                         min_notional=5.0))
    book.update(quote(Market(BINANCE, "BNBUSDT", "BNB", "USDT"), 600.0, 600.1, ts=1000.0))
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    inner_apply = ex._apply_fill

    def apply_then_lose_bnb(deltas, leg, fill):
        carry = inner_apply(deltas, leg, fill)
        if leg.symbol == "BTCUSDT" and leg.side == "sell":
            book.invalidate_venue(BINANCE)  # the feed drops before we can mark the ledger
            book.update(quote(BTC_BINANCE, 99999, 100000, ts=1000.0))  # BTC comes back, BNB does not
        return carry

    ex._apply_fill = apply_then_lose_bnb
    rec = run(ex.execute(opp, 1000.0))
    assert rec.unwound == "failed" and "could not be priced" in rec.reason
    assert risk.halted and risk.halt_sticky and "BNB" in ex.unmarked_assets


def test_unwind_extends_the_shutdown_wait(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, _opp = triangle_book_and_opp()
    on = _live(book, FakeSession([]), real=True, cfg_real=True)
    off = _live(book, FakeSession([]), real=True, cfg_real=True, auto_unwind=False)
    assert on.worst_case_s > off.worst_case_s  # the engine must wait out an unwind in flight
    test_mode_on = _live(book, FakeSession([]), real=False, cfg_real=False)
    test_mode_off = _live(book, FakeSession([]), real=False, cfg_real=False, auto_unwind=False)
    assert test_mode_on.worst_case_s == test_mode_off.worst_case_s  # inert without real orders


def test_preflight_refuses_auto_unwind_on_an_account_holding_foreign_coins(monkeypatch):
    """The unwind cannot tell a venue rejection from a sale of coins the operator owns."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    now = __import__("time").time()
    book = QuoteBook(max_age_s=1e9)
    book.register(BTC_BINANCE)
    book.update(quote(BTC_BINANCE, 99999, 100000, ts=now))

    def session(balances):
        return FakeSession([
            PING,
            ("/api/v3/time", 200, {"serverTime": int(__import__("time").time() * 1000)}),
            ("/api/v3/exchangeInfo", 200, {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
            ("/sapi/v1/account/apiRestrictions", 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}),
            ("/api/v3/account", 200, {"balances": balances}),
            ("/api/v3/order", 200, {"executedQty": "0", "status": "EXPIRED"}),
        ])

    held = [{"asset": "USDT", "free": "1000", "locked": "0"}, {"asset": "BTC", "free": "0.5", "locked": "0"}]
    s1 = session(held)
    problems = run(_live(book, s1, real=True, cfg_real=True).preflight(["BTCUSDT"]))
    assert len(problems) == 1 and "auto_unwind" in problems[0] and "BTC" in problems[0]
    assert len([c for c in s1.calls if "/api/v3/account" in c[1]]) == 1  # no extra round trip
    assert run(_live(book, session(held), real=True, cfg_real=True, auto_unwind=False).preflight(["BTCUSDT"])) == []
    crumb = [{"asset": "USDT", "free": "1000", "locked": "0"}, {"asset": "BTC", "free": "0.00001", "locked": "0"}]
    assert run(_live(book, session(crumb), real=True, cfg_real=True).preflight(["BTCUSDT"])) == []


def test_apply_fill_books_a_cycle_leg_and_an_unwind_identically(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    book, _opp = triangle_book_and_opp()
    ex = _live(book, FakeSession([]))
    buy_leg = Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100000.0, 0.01, 0.001)
    buy = Fill(BINANCE, "BTCUSDT", "buy", 100000.0, 0.01, 0.00001, "BTC", 1000.0, "x",
               fees_by_asset={"BTC": 0.000005, "BNB": 0.001})
    deltas = {}
    carry = ex._apply_fill(deltas, buy_leg, buy)
    assert deltas["USDT"] == pytest.approx(-1000.0) and deltas["BTC"] == pytest.approx(0.01 - 0.000005)
    assert deltas["BNB"] == pytest.approx(-0.001) and carry == pytest.approx(0.01 - 0.000005)
    sell_leg = Leg(BINANCE, "BTCUSDT", "sell", "BTC", "USDT", 100000.0, 0.01, 0.001)
    sell = Fill(BINANCE, "BTCUSDT", "sell", 100000.0, 0.01, 1.0, "USDT", 1000.0, "y", fees_by_asset={"USDT": 1.0})
    d2 = {}
    carry2 = ex._apply_fill(d2, sell_leg, sell)
    assert d2["BTC"] == pytest.approx(-0.01) and d2["USDT"] == pytest.approx(1000.0 - 1.0)
    assert carry2 == pytest.approx(1000.0 - 1.0)
