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
        "risk": {"cooldown_s": 0.0, "max_trades_per_minute": 10000, "min_profit_usd": 0.0},
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
    settled = sum(stats.trades_by_status.get(k, 0) for k in ("filled", "partial", "missed"))
    assert settled > 0 and stats.pending == settled  # every sent order settled by the end of the tape
    ex = engine.executor
    assert not ex.pending
    equity, _ = ex.equity_usd(clock.now())
    # accounting identity holds whatever the fills did
    assert equity == pytest.approx(ex.contributions_value_usd(clock.now()) + ex.realized_pnl_usd, rel=1e-4)
    # the arrival model is never more generous than the promise on a moving tape
    assert ex.realized_pnl_usd <= ex.promised_pnl_usd + 1e-6


def test_replay_arrival_fills_never_beat_their_limits():
    """Every arrival-model fill is an IOC at the detection price: buys never pay
    more than the limit, sells never receive less, and misses are recorded."""
    cfg = load_config(None, {
        "venues": {"taker_fee_bps": {"binance": 0.0, "coinbase": 0.0, "kraken": 0.0}},
        "detection": {"min_net_edge_bps": 0.5, "stable_haircut_bps": 0.0},
        "risk": {"cooldown_s": 0.0, "max_trades_per_minute": 10000, "min_profit_usd": 0.0},
        "paper": {"slippage_bps": 0.0, "fill_model": "arrival", "assumed_rtt_ms": 300},
        "report": {"write_jsonl": False, "interval_s": 3600},
    })
    markets = static_universe(cfg)
    clock = Clock()
    feed = ReplayFeed(FIXTURE, markets, speed=0.0, clock_setter=clock.set)
    clock.set(float(json.loads(FIXTURE.read_text().splitlines()[0])["t"]))
    engine = make_engine(cfg, markets, [feed], clock)
    records = []
    original = engine.reporter.on_trade
    engine.reporter.on_trade = lambda rec: (records.append(rec), original(rec))
    run(engine.run())
    assert records and all(r.status in ("filled", "partial", "missed") for r in records)
    limits = {}
    for r in records:
        for leg in r.opportunity.legs:
            limits[(leg.venue, leg.symbol, leg.side, r.opportunity.ts)] = leg.price
        for f in r.fills:
            limit = limits[(f.venue, f.symbol, f.side, r.opportunity.ts)]
            if f.side == "buy":
                assert f.price <= limit * (1 + 1e-12)
            else:
                assert f.price >= limit * (1 - 1e-12)
        if r.ts < engine.stats.last_quote_ts:  # settled at end of tape: orders still in flight settle early
            assert r.latency_ms >= 300 - 1e-6
    assert engine.executor.missed_legs == sum(len([m for m in r.reason.split(";") if m.strip()]) for r in records if r.status != "filled")


def test_cli_replay_writes_jsonl(tmp_path, capsys):
    parser = build_parser()
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("[risk]\nmin_profit_usd = -1e9\ncooldown_s = 0.0\nmax_trades_per_minute = 100000\n")
    args = parser.parse_args(["replay", str(FIXTURE), "--log-dir", str(tmp_path / "logs"), "--min-edge", "-1000",
                              "--config", str(cfg_file)])
    rc = run(cmd_replay(args))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["quotes"] > 300
    runs = list(Path(tmp_path / "logs").iterdir())
    assert len(runs) == 1
    opp_file = runs[0] / "opportunities.jsonl"
    assert opp_file.exists()
    first = json.loads(opp_file.read_text().splitlines()[0])
    assert first["actionable"] is True and "legs" in first
    trades = runs[0] / "trades.jsonl"
    assert trades.exists()  # min edge -1000 bps: everything gross-positive gets paper traded


def test_replay_uses_the_tape_header_universe(tmp_path, capsys):
    from arbbot.feeds.replay import read_tape_header, tape_header
    from arbbot.models import Market

    universe = [Market("binance", "BTCUSDT", "BTC", "USDT"), Market("kraken", "BTC/USD", "BTC", "USD")]
    rows = FIXTURE.read_text().splitlines()
    tape = tmp_path / "with_header.jsonl"
    tape.write_text(tape_header(universe) + "\n" + "\n".join(rows) + "\n")
    assert [m.key for m in read_tape_header(tape)] == [("binance", "BTCUSDT"), ("kraken", "BTC/USD")]
    assert read_tape_header(FIXTURE) is None
    args = build_parser().parse_args(["replay", str(tape), "--no-jsonl"])
    rc = run(cmd_replay(args))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["malformed_rows"] == 0
    assert out["unknown_symbol_rows"] > 100  # every other market on the tape is not in this universe
    assert 0 < out["quotes"] < 100
    args = build_parser().parse_args(["replay", str(tmp_path / "nope.jsonl"), "--no-jsonl"])
    assert run(cmd_replay(args)) == 2


def test_replay_skips_malformed_rows_and_reports_them(tmp_path, capsys):
    rows = FIXTURE.read_text().splitlines()
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(rows[:50] + ['{"t": 1789794253.9, "venue": "binance", "raw": "{\\"stream\\": '] + rows[50:80] + ["not json at all"]) + "\n")
    args = build_parser().parse_args(["replay", str(bad), "--no-jsonl"])
    rc = run(cmd_replay(args))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["malformed_rows"] == 2 and out["quotes"] > 0 and out["feed_errors"] == {}
    assert out["unknown_symbol_rows"] == 0


def test_sweep_script_runs_a_small_grid(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("sweep", Path(__file__).resolve().parents[1] / "scripts" / "sweep.py")
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    rc = sweep.main([str(FIXTURE), "--fee-scale", "1", "0", "--haircut", "0", "--slippage", "0", "--json"])
    assert rc == 0
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert len(rows) == 2
    full_fee, no_fee = rows
    assert full_fee["net_positive"] == 0 and full_fee["filled"] == 0
    assert no_fee["net_positive"] > 0  # "profit" appears exactly when the fees stop being real


def test_cli_refuses_live_without_config(tmp_path, capsys):
    from arbbot.cli import cmd_scan

    args = build_parser().parse_args(["scan", "--static", "--live", "--duration", "0.01", "--no-jsonl", "--venues", "binance"])
    rc = run(cmd_scan(args))
    assert rc == 2
    assert "refusing --live" in capsys.readouterr().err
    args = build_parser().parse_args(["scan", "--static", "--i-know-this-sends-real-orders", "--duration", "0.01", "--no-jsonl"])
    rc = run(cmd_scan(args))
    assert rc == 2
