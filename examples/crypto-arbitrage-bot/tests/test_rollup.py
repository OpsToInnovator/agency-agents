"""The daily roll-up / GO-NO-GO scorecard must reproduce the risk manager's cooldown,
attribute PnL to the right asset, and never call a week GO on thin evidence."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.001


def _load():
    spec = importlib.util.spec_from_file_location("rollup", ROOT / "scripts" / "rollup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


rollup = _load()

DAY = 86400.0
T0 = 1_789_776_000.0  # 2026-09-19 00:00:00 UTC


def tri_opp(ts: float, asset: str = "UNI", notional: float = 100.0, net_bps: float = 5.0) -> dict:
    return {
        "actionable": True, "kind": "triangular", "ts": ts, "gross_edge_bps": net_bps + 30, "net_edge_bps": net_bps,
        "notional_usd": notional, "expected_profit_usd": notional * net_bps / 1e4, "detect_latency_ms": 0.1,
        "quote_ages_ms": [0.0, 1.0, 2.0],
        "description": f"binance USDT -> {asset} -> BTC -> USDT: buy {asset}USDT @ 9, sell {asset}BTC @ 0.0001, sell BTCUSDT @ 81000",
        "legs": [
            {"venue": "binance", "symbol": f"{asset}USDT", "side": "buy", "price": 9.0, "qty": 11.0, "fee_rate": FEE},
            {"venue": "binance", "symbol": f"{asset}BTC", "side": "sell", "price": 0.0001, "qty": 11.0, "fee_rate": FEE},
            {"venue": "binance", "symbol": "BTCUSDT", "side": "sell", "price": 81000.0, "qty": 0.0012, "fee_rate": FEE},
        ],
        "extra": {"start": "USDT"},
    }


def cross_opp(ts: float, asset: str = "AR") -> dict:
    return {
        "actionable": True, "kind": "cross_exchange", "ts": ts, "gross_edge_bps": 100.0, "net_edge_bps": 4.0,
        "notional_usd": 20.0, "expected_profit_usd": 0.008, "detect_latency_ms": 0.1, "quote_ages_ms": [0.0, 0.0],
        "description": f"{asset}: buy kraken {asset}/USD @ 4.29, sell binance {asset}USDT @ 4.35",
        "legs": [
            {"venue": "kraken", "symbol": f"{asset}/USD", "side": "buy", "price": 4.29, "qty": 4.6, "fee_rate": 0.008},
            {"venue": "binance", "symbol": f"{asset}USDT", "side": "sell", "price": 4.35, "qty": 4.6, "fee_rate": FEE},
        ],
        "extra": {"haircut_bps": 5.0},
    }


def trade(opp: dict, status: str, realized: float, promised: float, ts: float | None = None) -> dict:
    return {"ts": opp["ts"] + 0.15 if ts is None else ts, "status": status, "reason": "", "realized_pnl_usd": realized,
            "promised_pnl_usd": promised, "latency_ms": 150.0, "fills": [], "opportunity": opp}


def anomaly(ts: float, description: str, subtype: str) -> dict:
    return {"kind": "anomaly", "ts": ts, "gross_edge_bps": 0.0, "net_edge_bps": 0.0, "notional_usd": 0.0,
            "expected_profit_usd": 0.0, "detect_latency_ms": 0.1, "quote_ages_ms": [], "description": description,
            "legs": [], "extra": {"subtype": subtype}}


def write_run(run_dir: Path, opps: list, trades: list, anomalies: list) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("opportunities", opps), ("trades", trades), ("anomalies", anomalies)):
        with (run_dir / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")


# -- helpers -----------------------------------------------------------------
def test_asset_extraction_matches_the_detectors_description_formats():
    assert rollup.assets_of(tri_opp(T0)) == ["UNI", "BTC"]
    assert rollup.assets_of(cross_opp(T0, "AR")) == ["AR"]
    assert rollup.base_of_symbol("UNIBTC") == "UNI"
    assert rollup.base_of_symbol("ETH-USD") == "ETH"
    assert rollup.base_of_symbol("XBT/USD") == "XBT"
    assert rollup.anomaly_asset(anomaly(T0, "binance UNIBTC jump: +80.0 bps", "jump")) == "UNI"
    assert rollup.anomaly_asset(anomaly(T0, "ONE identity_mismatch: kraken ONE/USD ...", "identity_mismatch")) == "ONE"
    assert rollup.anomaly_asset(anomaly(T0, "kraken AR/USD venue_disagreement: -90.0 bps", "venue_disagreement")) == "AR"


def test_cooldown_dedup_mirrors_the_risk_manager():
    # 97 re-emissions of one opportunity inside 2 ms are ONE send; the same key 2 s later is a second
    burst = [tri_opp(T0 + i * 0.00002) for i in range(97)]
    later = [tri_opp(T0 + 2.0), tri_opp(T0 + 3.9), tri_opp(T0 + 4.0)]
    other = [cross_opp(T0 + 0.001)]
    sent = rollup.sendable(burst + later + other, cooldown_s=2.0)
    assert [round(o["ts"] - T0, 3) for o in sent if o["kind"] == "triangular"] == [0.0, 2.0, 4.0]
    assert sum(1 for o in sent if o["kind"] == "cross_exchange") == 1


def test_max_drawdown_is_peak_to_trough_of_the_cumulative_curve():
    assert rollup.max_drawdown([1.0, 1.0, -0.5, -1.0, 2.0]) == pytest.approx(1.5)
    assert rollup.max_drawdown([-1.0, -1.0]) == pytest.approx(2.0)
    assert rollup.max_drawdown([]) == 0.0
    assert rollup.max_drawdown([0.5, 0.5]) == 0.0


# -- roll-up ------------------------------------------------------------------
def test_rollup_groups_by_utc_day_and_kind_and_attributes_flagged_pnl():
    o1 = tri_opp(T0 + 100, "UNI")
    o2 = tri_opp(T0 + 200, "ARB")
    o3 = tri_opp(T0 + DAY + 50, "UNI")
    c1 = cross_opp(T0 + 300, "AR")
    trades = [trade(o1, "filled", 0.03, 0.05), trade(o2, "missed", 0.0, 0.04), trade(o3, "partial", -0.01, 0.02),
              trade(c1, "filled", 0.008, 0.008), trade(o2, "rejected", 0.0, 0.0)]
    # ARB flagged in the same UTC hour as o2; UNI quarantined (ticker collision) at any time
    anomalies = [anomaly(T0 + 150, "binance ARBUSDT venue_disagreement: +60.0 bps", "venue_disagreement"),
                 anomaly(T0 + 5000, "UNI identity_mismatch: kraken UNI/USD vs binance UNIUSDT", "identity_mismatch")]
    table = rollup.rollup([o1, o2, o3, c1], trades, anomalies, cooldown_s=2.0)
    d0, d1 = "2026-09-19", "2026-09-20"
    tri0 = table[(d0, "triangular")]
    assert tri0.observations == 2 and tri0.sendable == 2
    assert dict(tri0.by_status) == {"filled": 1, "missed": 1, "rejected": 1}
    assert tri0.settled == 2 and tri0.fill_rate == 0.5 and tri0.one_legged_rate == 0.0
    assert tri0.realized == pytest.approx(0.03) and tri0.promised == pytest.approx(0.09)
    assert tri0.latency_tax == pytest.approx(0.06)
    assert tri0.notional_settled == 200.0 and tri0.realized_bps == pytest.approx(1.5)
    assert tri0.pnl_by_asset == {"UNI": pytest.approx(0.03), "ARB": 0.0}
    assert tri0.flagged_pnl == 0.0  # ARB trade was flagged but missed (realized 0)
    assert tri0.quarantined_pnl == pytest.approx(0.03)  # UNI is quarantined for the whole run
    tri1 = table[(d1, "triangular")]
    assert tri1.settled == 1 and tri1.one_legged_rate == 1.0 and tri1.realized == pytest.approx(-0.01)
    cross0 = table[(d0, "cross_exchange")]
    assert cross0.realized == pytest.approx(0.008) and cross0.pnl_by_asset == {"AR": pytest.approx(0.008)}


def test_anomaly_rows_in_opportunities_file_are_not_counted_as_observations():
    rows = [tri_opp(T0), anomaly(T0 + 1, "binance UNIUSDT jump: +70.0 bps", "jump")]
    table = rollup.rollup(rows, [], [], cooldown_s=2.0)
    assert list(table) == [("2026-09-19", "triangular")]
    assert table[("2026-09-19", "triangular")].observations == 1


# -- scorecard -----------------------------------------------------------------
def _week(per_day_realized: list[float], trades_per_day: int = 25, asset_cycle=("UNI", "ARB", "SOL", "LINK")):
    """A synthetic 7-day triangular history with evenly spread, all-filled trades."""
    opps, trades = [], []
    for day, total in enumerate(per_day_realized):
        each = total / trades_per_day
        for i in range(trades_per_day):
            o = tri_opp(T0 + day * DAY + 60 * i, asset_cycle[i % len(asset_cycle)])
            opps.append(o)
            trades.append(trade(o, "filled", each, abs(each) * 1.5 if each else 0.01))
    return opps, trades


def test_scorecard_passes_a_strong_week_and_reports_manual_g10(tmp_path):
    opps, trades = _week([3.0, 2.5, 2.8, 3.1, 2.2, 2.9, 3.0])
    write_run(tmp_path / "logs" / "run1", opps, trades, [])
    scan_log = tmp_path / "scan.log"
    scan_log.write_text("10:00:10 INFO    arbbot.report: [    10s] quotes=100 (binance=100) 10/s unchanged=0 markets=100 stale=5 | "
                        "gross>0: triangular=50 | net>=1bps: 0 | anomalies=0 | trades=0\n"
                        "10:00:20 INFO    arbbot.report:   disconnects: binance=3\n", encoding="utf-8")
    rep = rollup.build_report(tmp_path / "logs", days=7, cooldown_s=2.0, capital_usd=1000.0, include_today=False,
                              scan_log=scan_log, rtt_log=None, today="2026-09-27")
    results = {c["code"]: c["result"] for c in rep["scorecard"]}
    assert results == {f"G{i}": "PASS" for i in range(1, 10)} | {"G10": "NOT MEASURED"}
    assert rep["verdict"].startswith("GO")
    assert rep["window"] == [f"2026-09-{d}" for d in range(19, 26)]


def test_scorecard_is_no_go_on_a_single_failure_and_never_on_thin_evidence(tmp_path):
    # profitable but too few trades: G1 fails and the verdict is NO-GO
    opps, trades = _week([3.0] * 7, trades_per_day=5)
    write_run(tmp_path / "logs" / "run1", opps, trades, [])
    rep = rollup.build_report(tmp_path / "logs", 7, 2.0, 1000.0, False, None, None, today="2026-09-27")
    results = {c["code"]: c["result"] for c in rep["scorecard"]}
    assert results["G1"] == "FAIL" and rep["verdict"] == "NO-GO"
    # profitable in total but three losing days and one asset carrying all the PnL: G2 and G7 fail
    opps, trades = _week([10.0, -1.0, -1.0, -1.0, 2.0, 2.0, 2.0], asset_cycle=("UNI",))
    write_run(tmp_path / "logs2" / "run1", opps, trades, [])
    rep = rollup.build_report(tmp_path / "logs2", 7, 2.0, 1000.0, False, None, None, today="2026-09-27")
    results = {c["code"]: c["result"] for c in rep["scorecard"]}
    assert results["G2"] == "FAIL" and results["G7"] == "FAIL" and rep["verdict"] == "NO-GO"


def test_scorecard_without_scan_log_is_incomplete_not_go(tmp_path):
    opps, trades = _week([3.0, 2.5, 2.8, 3.1, 2.2, 2.9, 3.0])
    write_run(tmp_path / "logs" / "run1", opps, trades, [])
    rep = rollup.build_report(tmp_path / "logs", 7, 2.0, 1000.0, False, None, None, today="2026-09-27")
    results = {c["code"]: c["result"] for c in rep["scorecard"]}
    assert results["G8"] == "NOT MEASURED" and results["G9"] == "NOT MEASURED"
    assert rep["verdict"].startswith("INCOMPLETE")


def test_incomplete_week_and_todays_partial_day_are_not_scored(tmp_path):
    opps, trades = _week([3.0] * 3)
    write_run(tmp_path / "logs" / "run1", opps, trades, [])
    rep = rollup.build_report(tmp_path / "logs", 7, 2.0, 1000.0, False, None, None, today="2026-09-21")
    assert rep["window"] == ["2026-09-19", "2026-09-20"]  # the 21st is today and still running
    assert rep["verdict"].startswith("INCOMPLETE: 2 of 7")


def test_scan_log_parser_sums_restart_segments_and_counts_halts(tmp_path):
    log = tmp_path / "scan.log"
    log.write_text(
        "10:00:10 INFO    arbbot.report: [    10s] quotes=10 (binance=10) 1/s unchanged=0 markets=100 stale=10 | gross>0: cross_exchange=5 triangular=2 | net>=1bps: 0 | anomalies=0 | x\n"
        "10:00:20 INFO    arbbot.report: [    20s] quotes=20 (binance=20) 1/s unchanged=0 markets=100 stale=30 | gross>0: cross_exchange=9 triangular=4 | net>=1bps: 0 | anomalies=0 | x\n"
        "10:00:25 ERROR   arbbot.risk: TRADING HALTED: daily loss cap 25.00 USD reached\n"
        "10:00:30 INFO    arbbot.report:   disconnects: binance=1, kraken=2\n"
        "10:00:31 INFO    arbbot.report:   handler errors 1, executions skipped while one was in flight 0\n"
        "10:01:10 INFO    arbbot.report: [    10s] quotes=10 (binance=10) 1/s unchanged=0 markets=100 stale=20 | gross>0: cross_exchange=1 triangular=1 | net>=1bps: 0 | anomalies=0 | x\n"
        "10:01:30 INFO    arbbot.report:   disconnects: binance=1\n",
        encoding="utf-8")
    ops = rollup.parse_scan_log(log)
    assert ops["gross_by_kind"] == {"cross_exchange": 10, "triangular": 5}  # 9+1, 4+1: last line of each segment
    assert ops["stale_share_mean"] == pytest.approx(0.2)
    assert ops["disconnects"] == {"binance": 2, "kraken": 2}
    assert ops["handler_errors"] == 1
    assert ops["daily_loss_halts"] == 1 and len(ops["halts"]) == 1


def test_rtt_log_parser_ignores_failed_probes(tmp_path):
    log = tmp_path / "rtt.log"
    log.write_text("2026-09-19T00:00:00Z binance warm_ms 20 total_ms 180 http 200\n"
                   "2026-09-19T00:01:00Z binance warm_ms 30 total_ms 190 http 200\n"
                   "2026-09-19T00:02:00Z binance warm_ms nan total_ms nan http 000\n"
                   "2026-09-19T00:00:00Z kraken warm_ms 250 total_ms 900 http 200\n", encoding="utf-8")
    rtt = rollup.parse_rtt_log(log)
    assert rtt["binance"]["n"] == 2 and rtt["binance"]["p50_ms"] == 25.0
    assert rtt["kraken"]["p90_ms"] == 250.0


def test_cli_runs_end_to_end_and_skips_malformed_rows(tmp_path, capsys):
    opps, trades = _week([1.0] * 2, trades_per_day=3)
    run = tmp_path / "logs" / "run1"
    write_run(run, opps, trades, [])
    with (run / "trades.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    rc = rollup.main([str(tmp_path / "logs"), "--include-today", "--json"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["malformed_rows"] == 1 and len(rep["daily"]) == 2
    assert rollup.main([str(tmp_path / "nowhere")]) == 2


def test_measure7d_config_loads_and_is_paper_only():
    sys.path.insert(0, str(ROOT))
    from arbbot.config import load_config

    cfg = load_config(ROOT / "measure7d.toml")
    assert cfg.live.enabled is False and cfg.live.real_orders is False
    assert cfg.paper.fill_model == "arrival" and cfg.paper.fill_fraction == 0.5
    assert cfg.risk.min_profit_usd == 0.01
