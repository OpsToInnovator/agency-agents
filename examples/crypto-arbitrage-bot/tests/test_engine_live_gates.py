"""Engine behaviour around a remote (live) executor: nothing reaches a venue after
stop(), and opportunities the executor cannot run never consume risk budget."""
import asyncio

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


def test_nothing_is_sent_after_stop():
    eng, ex, rep, risk = _engine([_opp(BINANCE)])
    eng.stop()
    run(eng.handle(quote(BTC_BINANCE, 100, 101, ts=1000.0)))
    assert ex.executed == [] and eng._inflight is None
    assert eng.book.get(BINANCE, "BTCUSDT") is not None  # the book still updates
