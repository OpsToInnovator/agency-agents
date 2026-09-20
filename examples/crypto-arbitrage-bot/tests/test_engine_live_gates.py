"""Engine behaviour around a remote (live) executor: nothing reaches a venue after
stop(), and opportunities the executor cannot run never consume risk budget."""
import asyncio

import pytest

from arbbot.config import load_config
from arbbot.engine import Clock, Engine
from arbbot.execution import RiskManager
from arbbot.models import BINANCE, Leg, Opportunity, TradeRecord
from arbbot.quotes import QuoteBook
from tests.helpers import BTC_BINANCE, quote


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeRemoteExecutor:
    remote = True

    def __init__(self):
        self.executed = []

    @staticmethod
    def can_execute(opp):
        return all(l.venue == BINANCE for l in opp.legs)

    async def execute(self, opp, now, min_edge_bps=0.0):
        self.executed.append(opp)
        return TradeRecord(opp, [], "test", "", 0.0, now)


class OneShotDetector:
    name = "fake"

    def __init__(self, opps):
        self.opps = list(opps)

    def on_quote(self, q, book, now):
        out, self.opps = self.opps, []
        return out


class Recorder:
    def __init__(self):
        self.rejected = []
        self.trades = []

    def on_opportunity(self, opp, actionable): pass
    def on_anomaly(self, opp): pass
    def on_pending(self, rec): pass
    def on_rejected(self, opp, reason): self.rejected.append((opp.kind, reason))
    def on_trade(self, rec): self.trades.append(rec)
    def periodic(self, now): pass
    def final(self, now): pass
    def close(self): pass


def _opp(venue, kind="triangular"):
    leg = Leg(venue, "BTCUSDT" if venue == BINANCE else "BTC-USD", "buy", "BTC", "USDT", 100.0, 0.5, 0.001)
    return Opportunity(kind, 1000.0, [leg], 25.0, 5.0, 50.0, 0.25, "x", quote_ages_ms=[1.0])


def _engine(opps, cfg_overrides=None):
    cfg = load_config(None, cfg_overrides or {})
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    risk = RiskManager(cfg.risk, cfg.detection)
    ex = FakeRemoteExecutor()
    rep = Recorder()
    clock = Clock()
    clock.set(1000.0)  # fixture-style timestamps: keep detection latency deterministic
    eng = Engine(cfg, book, [], [OneShotDetector(opps)], risk, ex, rep, clock)
    return eng, ex, rep, risk


def test_unexecutable_opportunities_do_not_consume_budget():
    eng, ex, rep, risk = _engine([_opp("coinbase", "cross_exchange"), _opp("coinbase", "cross_exchange"), _opp(BINANCE)],
                                 {"risk": {"max_trades_per_minute": 2}})

    async def scenario():
        await eng.handle(quote(BTC_BINANCE, 100, 101, ts=1000.0))
        assert eng._inflight is not None
        await eng._inflight

    run(scenario())
    assert [r for r in rep.rejected if r[1] == "not executable by this executor"] == [("cross_exchange", "not executable by this executor")] * 2
    assert not any(r[1] == "trade rate limit" for r in rep.rejected)
    assert [o.kind for o in ex.executed] == ["triangular"]
    assert [t.status for t in rep.trades] == ["test"]


def test_implausible_edges_are_anomalies_not_opportunities():
    good = _opp(BINANCE)
    absurd = _opp(BINANCE)
    absurd.net_edge_bps = 480.0
    eng, ex, rep, risk = _engine([absurd, good])

    async def scenario():
        await eng.handle(quote(BTC_BINANCE, 100, 101, ts=1000.0))
        await eng._inflight

    run(scenario())
    assert eng.stats.gross_by_kind == {"triangular": 1} and eng.stats.actionable_by_kind == {"triangular": 1}
    assert eng.stats.anomalies == {"implausible_edge": 1}
    assert [o.net_edge_bps for o in ex.executed] == [5.0]


def test_nothing_is_sent_after_stop():
    eng, ex, rep, risk = _engine([_opp(BINANCE)])
    eng.stop()
    run(eng.handle(quote(BTC_BINANCE, 100, 101, ts=1000.0)))
    assert ex.executed == [] and eng._inflight is None
    assert eng.book.get(BINANCE, "BTCUSDT") is not None  # the book still updates


class StakedExecutor(FakeRemoteExecutor):
    """A live-style executor: no marked equity, only realized PnL and a configured stake."""
    capital_usd = 500.0

    def __init__(self, loss_per_fill):
        super().__init__()
        self.realized_pnl_usd = 0.0
        self.loss = loss_per_fill

    async def execute(self, opp, now, min_edge_bps=0.0):
        self.executed.append(opp)
        self.realized_pnl_usd -= self.loss
        return TradeRecord(opp, [], "filled", "", -self.loss, now)


class PerQuoteDetector:
    name = "fake"

    def on_quote(self, q, book, now):
        leg = Leg(BINANCE, "BTCUSDT", "buy", "BTC", "USDT", 100.0, 0.5, 0.001)
        return [Opportunity("triangular", now, [leg], 25.0, 5.0, 50.0, 0.25, "x", quote_ages_ms=[1.0])]


def _drive(ex, cfg_overrides):
    cfg = load_config(None, {"risk": {"max_daily_loss_usd": 1000.0, "max_drawdown_pct": 5.0, **cfg_overrides}})
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    risk = RiskManager(cfg.risk, cfg.detection)
    rep = Recorder()
    clock = Clock()
    eng = Engine(cfg, book, [], [PerQuoteDetector()], risk, ex, rep, clock)

    async def scenario():
        for i, t in enumerate((1000.0, 1003.0, 1006.0, 1009.0)):  # past the 2 s per-key cooldown each time
            clock.set(t)
            await eng.handle(quote(BTC_BINANCE, 100 + i, 101 + i, ts=t))  # an unchanged quote is skipped
            if eng._inflight is not None:
                await eng._inflight

    run(scenario())
    return rep, risk


def test_live_drawdown_cap_measures_realized_pnl_against_the_configured_stake():
    ex = StakedExecutor(13.0)
    rep, risk = _drive(ex, {})
    # the curve starts at 0; the second fill sits 26 below it, past 5% of US$500
    assert [t.status for t in rep.trades] == ["filled", "filled"]
    assert risk.halted and "drawdown cap hit" in risk.halt_reason and "5.20% of capital" in risk.halt_reason
    assert len(ex.executed) == 2 and any("drawdown cap hit" in r[1] for r in rep.rejected)  # the rest were refused


def test_live_drawdown_cap_stays_off_without_a_configured_stake():
    ex = StakedExecutor(13.0)
    ex.capital_usd = 0.0  # the default: balances are on the exchange and no stake was declared
    rep, risk = _drive(ex, {})
    assert [t.status for t in rep.trades] == ["filled"] * 4 and not risk.halted
    assert risk.peak_pnl_usd is None


class CompoundingExecutor(FakeRemoteExecutor):
    """A live-style executor with compounding on: the engine re-bases from what it reads."""
    remote = True
    compound = True
    initial_capital_usd = 1000.0
    max_cumulative_loss_pct = 10.0

    def __init__(self, balances):
        super().__init__()
        self.balances = list(balances)  # successive reads: free USDT, (free, locked), or an Exception
        self.reads = 0
        self.capital_usd = 1000.0
        self.max_notional_usd = 100.0
        self.realized_pnl_usd = 0.0

    @property
    def kill_floor_usd(self):
        return self.initial_capital_usd * (1 - self.max_cumulative_loss_pct / 100)

    async def read_usdt(self):
        self.reads += 1
        value = self.balances.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, tuple) else (value, 0.0)

    async def execute(self, opp, now, min_edge_bps=0.0):
        self.executed.append(opp)
        return TradeRecord(opp, [], "filled", "", 0.0, now)


class SizedDetector(PerQuoteDetector):
    max_notional_usd = 100.0
    notional_usd = 50.0

    def on_quote(self, q, book, now):
        opps = super().on_quote(q, book, now)
        for opp in opps:
            opp.notional_usd = self.notional_usd
        return opps


class RateLimited(Exception):
    status = 429


def _compound_engine(ex, det=None):
    cfg = load_config(None, {"risk": {"max_notional_per_trade_usd": 100.0, "max_daily_loss_usd": 10.0, "cooldown_s": 0.0},
                             "live": {"enabled": True, "capital_usd": 1000.0, "compound": True}})
    book = QuoteBook(max_age_s=2.0)
    book.register(BTC_BINANCE)
    risk = RiskManager(cfg.risk, cfg.detection)
    det = det or SizedDetector()
    rep = Recorder()
    clock = Clock()
    eng = Engine(cfg, book, [], [det], risk, ex, rep, clock)
    return eng, cfg, det, risk, rep, clock


async def _tick(eng, clock, t, i):
    clock.set(t)
    await eng.handle(quote(BTC_BINANCE, 100 + i, 101 + i, ts=t))
    if eng._inflight is not None:
        await eng._inflight


DAY0 = 1_789_776_000.0  # 2026-09-19 00:00 UTC


def test_compounding_rebases_once_per_utc_day_and_scales_every_cap():
    ex = CompoundingExecutor([1200.0, 1500.0])
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)

    async def scenario():
        await _tick(eng, clock, DAY0 + 10, 0)      # first execution of the day: re-base from 1200
        await _tick(eng, clock, DAY0 + 20, 1)      # same day: no second read
        await _tick(eng, clock, DAY0 + 86400 + 5, 2)  # next UTC day: re-base from 1500

    run(scenario())
    assert ex.reads == 2 and len(ex.executed) == 3
    assert cfg.risk.max_notional_per_trade_usd == 150.0 and cfg.risk.max_daily_loss_usd == 15.0  # 10% and 1% of 1500
    assert ex.capital_usd == 1500.0 and ex.max_notional_usd == 150.0 and det.max_notional_usd == 150.0
    assert ex.kill_floor_usd == 900.0  # anchored to the original capital, not the re-based stake


def test_compounding_scales_down_after_a_losing_day_and_stops_at_the_floor():
    ex = CompoundingExecutor([1200.0, 900.0])  # exactly the floor: no halt, caps at 10% / 1% of 900
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)

    async def scenario():
        await _tick(eng, clock, DAY0 + 10, 0)
        await _tick(eng, clock, DAY0 + 86400 + 5, 1)

    run(scenario())
    assert not risk.halted and len(ex.executed) == 2
    assert cfg.risk.max_notional_per_trade_usd == 90.0 and cfg.risk.max_daily_loss_usd == 9.0 and ex.capital_usd == 900.0


def test_compounding_halts_sticky_below_the_anchored_kill_floor():
    ex = CompoundingExecutor([890.0])
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)

    async def scenario():
        await _tick(eng, clock, DAY0 + 10, 0)
        await _tick(eng, clock, DAY0 + 20, 1)
        await _tick(eng, clock, DAY0 + 86400 + 5, 2)  # the day roll lifts daily caps, not this

    run(scenario())
    assert ex.executed == [] and ex.reads == 1  # nothing was sent, nothing re-read
    assert risk.halted and risk.halt_sticky and "cumulative loss budget spent" in risk.halt_reason
    assert [t.status for t in rep.trades] == ["rejected"] and cfg.risk.max_notional_per_trade_usd == 100.0  # caps untouched


def test_compounding_counts_usdt_locked_in_open_orders_as_stake():
    ex = CompoundingExecutor([(850.0, 100.0)])  # free alone is under the floor; free + locked is not
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)
    run(_tick(eng, clock, DAY0 + 10, 0))
    assert not risk.halted and len(ex.executed) == 1
    assert ex.capital_usd == 950.0 and cfg.risk.max_notional_per_trade_usd == 95.0


def test_compounding_skips_the_cycle_when_the_balance_read_fails():
    ex = CompoundingExecutor([RuntimeError("boom"), 1300.0])
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)

    async def scenario():
        await _tick(eng, clock, DAY0 + 10, 0)  # read fails: nothing sent, caps untouched, day not marked
        await _tick(eng, clock, DAY0 + 20, 1)  # same day: the read is retried and the trade goes

    run(scenario())
    assert ex.reads == 2 and len(ex.executed) == 1 and not risk.halted
    assert [t.status for t in rep.trades] == ["rejected", "filled"] and "could not read the USDT balance" in rep.trades[0].reason
    assert cfg.risk.max_notional_per_trade_usd == 130.0 and ex.capital_usd == 1300.0


def test_compounding_rate_limit_on_the_balance_read_halts_like_an_order_would():
    ex = CompoundingExecutor([RateLimited("429")])
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)
    run(_tick(eng, clock, DAY0 + 10, 0))
    assert ex.executed == [] and risk.halted and risk.halt_sticky and "binance rate limit (429)" in risk.halt_reason


def test_compounding_cancel_during_the_balance_read_is_not_an_order_in_flight():
    ex = CompoundingExecutor([asyncio.CancelledError()])
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)

    async def scenario():
        clock.set(DAY0 + 10)
        await eng.handle(quote(BTC_BINANCE, 100, 101, ts=DAY0 + 10))
        with pytest.raises(asyncio.CancelledError):
            await eng._inflight

    run(scenario())
    assert ex.executed == [] and not risk.halted  # nothing reached the venue, so nothing to reconcile


def test_compounding_rejects_the_first_opportunity_sized_at_the_previous_caps():
    det = SizedDetector()
    det.notional_usd = 95.0  # passes the 100 cap it was sized under, not the 90 the re-base sets
    ex = CompoundingExecutor([900.0])
    eng, cfg, _, risk, rep, clock = _compound_engine(ex, det)

    async def scenario():
        await _tick(eng, clock, DAY0 + 10, 0)   # re-based to 900 mid-flight: this one is over the new cap
        det.notional_usd = 90.0
        await _tick(eng, clock, DAY0 + 20, 1)   # sized at the new cap: goes

    run(scenario())
    assert [t.status for t in rep.trades] == ["rejected", "filled"] and "sized at the previous stake" in rep.trades[0].reason
    assert len(ex.executed) == 1 and not risk.halted and det.max_notional_usd == 90.0


def test_compounding_off_never_reads_the_balance():
    ex = CompoundingExecutor([1200.0])
    ex.compound = False
    eng, cfg, det, risk, rep, clock = _compound_engine(ex)
    run(_tick(eng, clock, 1_789_776_010.0, 0))
    assert ex.reads == 0 and cfg.risk.max_notional_per_trade_usd == 100.0
