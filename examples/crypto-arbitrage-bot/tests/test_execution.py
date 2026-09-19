import asyncio
import json

import pytest

from arbbot.config import DetectionConfig, LiveConfig, PaperConfig, RiskConfig
from arbbot.detectors import CrossExchangeDetector, TriangularDetector
from arbbot.execution import BinanceLiveExecutor, PaperExecutor, RiskManager
from arbbot.execution.live import AmbiguousOrderState, LiveDisabled, sign_query
from arbbot.fees import FeeSchedule
from arbbot.models import BINANCE, COINBASE, KRAKEN, Leg, Opportunity, TradeRecord
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


def _live(book, session, real=False, cfg_real=False, risk=None, tmp_path=None):
    return BinanceLiveExecutor(LiveConfig(enabled=True, real_orders=cfg_real), "https://api.example", FEES, book,
                               real_orders=real, session=session, risk=risk,
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
    ex = _live(book, session, real=True, cfg_real=True, risk=risk)
    rec = run(ex.execute(opp, 1000.0))
    assert rec.status == "partial" and risk.halted and risk.halt_sticky
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

    short = run(staked(session("300")).preflight(["BTCUSDT"]))
    assert len(short) == 1 and "below live.capital_usd (500.00)" in short[0]
    assert run(staked(session("500")).preflight(["BTCUSDT"])) == []
