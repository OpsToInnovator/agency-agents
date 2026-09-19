import asyncio
import json
from pathlib import Path

import pytest

from arbbot.cli import build_parser, cmd_replay, make_engine
from arbbot.config import load_config
from arbbot.engine import Clock
from arbbot.feeds import ReplayFeed
from arbbot.universe import static_universe
from tests.conftest import FIXTURES

FIXTURE = FIXTURES / "feed_fixture.jsonl"


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_fixture_replays_deterministically(tmp_path):
    cfg = load_config(None, {"report": {"write_jsonl": False, "interval_s": 3600}})
    markets = static_universe(cfg)
    results = []
    for _ in range(2):
        clock = Clock()
        feed = ReplayFeed(FIXTURE, markets, speed=0.0, clock_setter=clock.set)
        clock.set(float(json.loads(FIXTURE.read_text().splitlines()[0])["t"]))
        engine = make_engine(cfg, markets, [feed], clock)
        stats = run(engine.run())
        results.append((stats.quotes, dict(stats.gross_by_kind), dict(stats.actionable_by_kind),
                        {k: round(v[0], 6) for k, v in stats.best_net.items()}))
    assert results[0] == results[1]
    quotes, gross, actionable, best_net = results[0]
    assert quotes > 300  # every quote in the fixture is processed (nothing coalesced)
    assert gross["cross_exchange"] > 100 and gross["triangular"] > 0
    # the honest result: gross-positive spreads everywhere, nothing survives fees
    assert actionable == {}
    assert best_net["cross_exchange"] < 0 and best_net["triangular"] < 0
    assert engine.stats.coalesced == 0


def test_replay_with_zero_fees_trades_on_paper():
    # With fees set to zero and a tiny threshold, the same tape produces paper trades,
    # which proves the execution path end to end and shows where "profit" comes from.
    cfg = load_config(None, {
        "venues": {"taker_fee_bps": {"binance": 0.0, "coinbase": 0.0, "kraken": 0.0}},
        "detection": {"min_net_edge_bps": 0.5, "stable_haircut_bps": 0.0},
        "risk": {"cooldown_s": 0.0, "max_trades_per_minute": 10000},
        "paper": {"slippage_bps": 0.0},
        "report": {"write_jsonl": False, "interval_s": 3600},
    })
    markets = static_universe(cfg)
    clock = Clock()
    feed = ReplayFeed(FIXTURE, markets, speed=0.0, clock_setter=clock.set)
    clock.set(float(json.loads(FIXTURE.read_text().splitlines()[0])["t"]))
    engine = make_engine(cfg, markets, [feed], clock)
    stats = run(engine.run())
    assert stats.actionable_by_kind["cross_exchange"] > 0
    assert stats.trades_by_status.get("filled", 0) + stats.trades_by_status.get("partial", 0) > 0
    ex = engine.executor
    assert ex.realized_pnl_usd > 0
    equity, _ = ex.equity_usd(clock.now())
    assert equity == pytest.approx(ex.contributions_usd + ex.realized_pnl_usd, rel=1e-4)


def test_cli_replay_writes_jsonl(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(["replay", str(FIXTURE), "--log-dir", str(tmp_path), "--min-edge", "-1000"])
    rc = run(cmd_replay(args))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["quotes"] > 300
    runs = list(Path(tmp_path).iterdir())
    assert len(runs) == 1
    opp_file = runs[0] / "opportunities.jsonl"
    assert opp_file.exists()
    first = json.loads(opp_file.read_text().splitlines()[0])
    assert first["actionable"] is True and "legs" in first
    trades = runs[0] / "trades.jsonl"
    assert trades.exists()  # min edge -1000 bps: everything gross-positive gets paper traded


def test_cli_refuses_live_without_config(tmp_path, capsys):
    from arbbot.cli import cmd_scan

    args = build_parser().parse_args(["scan", "--static", "--live", "--duration", "0.01", "--no-jsonl", "--venues", "binance"])
    rc = run(cmd_scan(args))
    assert rc == 2
    assert "refusing --live" in capsys.readouterr().err
    args = build_parser().parse_args(["scan", "--static", "--i-know-this-sends-real-orders", "--duration", "0.01", "--no-jsonl"])
    rc = run(cmd_scan(args))
    assert rc == 2
