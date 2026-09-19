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
def tri_desc(path: str) -> dict:
    return {"kind": "triangular", "description": f"binance {path}: buy X @ 1, sell Y @ 1, sell Z @ 1", "legs": []}


def test_asset_extraction_matches_the_detectors_description_formats():
    assert rollup.assets_of(tri_opp(T0)) == ["UNI", "BTC"]
    assert rollup.assets_of(cross_opp(T0, "AR")) == ["AR"]
    assert rollup.base_of_symbol("UNIBTC") == "UNI"
    assert rollup.base_of_symbol("ETH-USD") == "ETH"
    assert rollup.base_of_symbol("XBT/USD") == "XBT"
    assert rollup.anomaly_assets(anomaly(T0, "binance UNIBTC jump: +80.0 bps", "jump")) == ["UNI"]
    assert rollup.anomaly_assets(anomaly(T0, "ONE identity_mismatch: kraken ONE/USD ...", "identity_mismatch")) == ["ONE"]
    assert rollup.anomaly_assets(anomaly(T0, "kraken AR/USD venue_disagreement: -90.0 bps", "venue_disagreement")) == ["AR"]
    assert rollup.anomaly_asset(anomaly(T0, "kraken AR/USD venue_disagreement: -90.0 bps", "venue_disagreement")) == "AR"
    assert rollup.anomaly_assets(anomaly(T0, "garbage", "jump")) == [] and rollup.anomaly_asset({"description": ""}) is None


def test_triangle_attribution_is_independent_of_cycle_direction():
    # the detector enumerates both directions of every triangle as separate cycles
    assert rollup.assets_of(tri_desc("USDT -> UNI -> BTC -> USDT")) == ["UNI", "BTC"]
    assert rollup.assets_of(tri_desc("USDT -> BTC -> UNI -> USDT")) == ["UNI", "BTC"]
    assert rollup.assets_of(tri_desc("USDT -> BNB -> ARB -> USDT")) == ["ARB", "BNB"]
    # quote-only triangles (universe.py adds ETHBTC / BNBBTC / BNBETH) get one canonical member
    assert rollup.assets_of(tri_desc("USDT -> BTC -> ETH -> USDT")) == ["BTC", "ETH"]
    assert rollup.assets_of(tri_desc("USDT -> ETH -> BTC -> USDT")) == ["BTC", "ETH"]


def test_implausible_edge_rows_are_parsed_from_the_engines_rewritten_description():
    # engine._process rewrites the whole opportunity: kind="anomaly", legs=[], description prefixed
    tri = anomaly(T0, "triangular net +480.0 bps is implausible: binance USDT -> BTC -> UNI -> USDT: buy BTCUSDT @ 81000, "
                      "buy UNIBTC @ 0.0001, sell UNIUSDT @ 9", "implausible_edge")
    cross = anomaly(T0, "cross_exchange net +480.0 bps is implausible: AR: buy kraken AR/USD @ 4.29, sell binance ARUSDT @ 4.35",
                    "implausible_edge")
    assert rollup.anomaly_assets(tri) == ["UNI", "BTC"]
    assert rollup.anomaly_assets(cross) == ["AR"]
    o = tri_opp(T0 + 100, "UNI")
    table = rollup.rollup([o], [trade(o, "filled", 0.5, 0.6)], [tri], cooldown_s=2.0)
    assert table[("2026-09-19", "triangular")].flagged_pnl == pytest.approx(0.5)


@pytest.mark.parametrize("subtype,counts", [("price_error", True), ("venue_disagreement", True), ("crossed_book", True),
                                            ("identity_mismatch", True), ("implausible_edge", True), ("jump", False)])
def test_which_anomaly_subtypes_flag_pnl_is_pinned(subtype, counts):
    o = tri_opp(T0 + 100, "UNI")
    if subtype == "implausible_edge":
        desc = "triangular net +480.0 bps is implausible: binance USDT -> UNI -> BTC -> USDT: buy UNIUSDT @ 9"
    else:
        desc = f"binance UNIUSDT {subtype}: +70.0 bps"
    table = rollup.rollup([o], [trade(o, "filled", 0.5, 0.6)], [anomaly(T0 + 50, desc, subtype)], cooldown_s=2.0)
    assert (table[("2026-09-19", "triangular")].flagged_pnl == pytest.approx(0.5)) is counts


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
            # spread over the day so a synthetic run's uptime covers >= 95% of it
            o = tri_opp(T0 + day * DAY + (DAY * 0.96 / max(1, trades_per_day - 1)) * i, asset_cycle[i % len(asset_cycle)])
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
    # mean clears the bar but three losing days, and one asset carrying all the PnL: G2 and G7 fail
    opps, trades = _week([16.0, -1.0, -1.0, -1.0, 2.0, 2.0, 2.0], asset_cycle=("UNI",))
    write_run(tmp_path / "logs2" / "run1", opps, trades, [])
    rep = rollup.build_report(tmp_path / "logs2", 7, 2.0, 1000.0, False, None, None, today="2026-09-27")
    results = {c["code"]: c["result"] for c in rep["scorecard"]}
    details = {c["code"]: c["detail"] for c in rep["scorecard"]}
    assert results["G2"] == "FAIL" and "3 negative" in details["G2"] and "+2.7143/day" in details["G2"]
    assert results["G7"] == "FAIL" and rep["verdict"] == "NO-GO"


def test_g7_concentration_sees_through_cycle_direction():
    def week(directions_by_asset):
        opps, trades = [], []
        for day in range(7):
            i = 0
            for asset, dirs in directions_by_asset.items():
                for d in dirs:
                    path = f"USDT -> {asset} -> BTC -> USDT" if d == "fwd" else f"USDT -> BTC -> {asset} -> USDT"
                    o = {**tri_opp(T0 + day * DAY + 3300 * i, asset), "description": f"binance {path}: buy x @ 1"}
                    opps.append(o)
                    trades.append(trade(o, "filled", 0.5, 0.6))
                    i += 1
        return opps, trades
    # one alt carries 60% of the PnL, half of it via the reverse cycle: must FAIL G7
    opps, trades = week({"UNI": ["fwd", "rev", "fwd", "rev", "fwd", "rev"], "ARB": ["fwd"] * 4})
    tri = [dk for (d, k), dk in rollup.rollup(opps, trades, [], 2.0).items() if k == "triangular"]
    crit, _ = rollup.scorecard(tri, 1000.0, 7, None)
    g7 = next(c for c in crit if c.code == "G7")
    assert g7.passed is False and "UNI 60%" in g7.detail
    # three alts at a third each, all traded through the reverse cycle: must PASS G7 (not "BTC 100%")
    opps, trades = week({"UNI": ["rev"] * 3, "ARB": ["rev"] * 3, "SOL": ["rev"] * 3})
    tri = [dk for (d, k), dk in rollup.rollup(opps, trades, [], 2.0).items() if k == "triangular"]
    crit, _ = rollup.scorecard(tri, 1000.0, 7, None)
    g7 = next(c for c in crit if c.code == "G7")
    assert g7.passed is True and "33%" in g7.detail


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


def test_a_short_sample_is_never_scored_as_a_complete_day(tmp_path):
    # a run directory named like report.py's run_id, up for 10 minutes on a past day
    o = tri_opp(T0 + 100.0)
    write_run(tmp_path / "logs" / "20260919T000000Z", [o], [trade(o, "filled", 0.03, 0.05, ts=T0 + 600.0)], [])
    rep = rollup.build_report(tmp_path / "logs", 1, 2.0, 1000.0, False, None, None, today="2026-09-20")
    assert rep["window"] == [] and rep["partial_days"] == {"2026-09-19": pytest.approx(600 / 3600, abs=0.01)}
    assert rep["verdict"].startswith("INCOMPLETE: 0 of 1") and "2026-09-19" in rep["verdict"]
    # --min-day-coverage 0 restores "any past day with data counts"
    rep = rollup.build_report(tmp_path / "logs", 1, 2.0, 1000.0, False, None, None, today="2026-09-20", min_day_coverage=0.0)
    assert rep["window"] == ["2026-09-19"]


def test_restart_gaps_are_bridged_but_an_outage_day_breaks_the_window(tmp_path):
    # two processes covering day 0 back to back (restart within seconds) = one complete day
    def run(name, start, end):
        o = tri_opp(start)
        write_run(tmp_path / "logs" / name, [o], [trade(o, "filled", 0.5, 0.6, ts=end)], [])
    run("20260919T000000Z", T0 + 10, T0 + 12 * 3600)
    run("20260919T120003Z", T0 + 12 * 3600 + 10, T0 + DAY - 5)
    rep = rollup.build_report(tmp_path / "logs", 1, 2.0, 1000.0, False, None, None, today="2026-09-21")
    assert rep["window"] == ["2026-09-19"] and rep["coverage_hours"]["2026-09-19"] == pytest.approx(24.0, abs=0.01)
    # day 1 (the 20th) is missing entirely, day 2 is complete: the last 2 complete days are not consecutive
    run("20260921T000000Z", T0 + 2 * DAY + 10, T0 + 3 * DAY - 5)
    rep = rollup.build_report(tmp_path / "logs", 2, 2.0, 1000.0, False, None, None, today="2026-09-22")
    assert rep["window"] == ["2026-09-19", "2026-09-21"] and "not consecutive" in rep["verdict"]


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


def test_scan_log_segments_on_the_process_start_line(tmp_path):
    start = "10:00:00 INFO    arbbot: arbbot 0.1.0 starting in PAPER mode: 196 markets, 3 feeds, min net edge 1.00 bps, fees {}\n"
    summary = "{t} INFO    arbbot.report: [    10s] quotes=10 (binance=10) 1/s unchanged=0 markets=100 stale=10 | gross>0: triangular={n} | net>=1bps: 0 | anomalies=0 | x\n"
    log = tmp_path / "scan.log"
    # two runs hard-killed after their first periodic line: elapsed never goes backwards, only the start line separates them
    log.write_text(start + summary.format(t="10:00:10", n=100) + start + summary.format(t="10:01:10", n=7), encoding="utf-8")
    assert rollup.parse_scan_log(log)["gross_by_kind"] == {"triangular": 107}
    # a FINAL line dated by the last quote (earlier than the last periodic line) must not lower or double count
    final = "10:00:20 INFO    arbbot.report: [     9s] quotes=9 (binance=9) 1/s unchanged=0 markets=100 stale=10 | gross>0: triangular=99 | net>=1bps: 0 | anomalies=0 | x\n"
    log.write_text(start + summary.format(t="10:00:10", n=100) + final, encoding="utf-8")
    assert rollup.parse_scan_log(log)["gross_by_kind"] == {"triangular": 100}


def test_rtt_log_parser_ignores_failed_probes(tmp_path):
    log = tmp_path / "rtt.log"
    log.write_text("2026-09-19T00:00:00Z binance warm_ms 20 total_ms 180 http 200\n"
                   "2026-09-19T00:01:00Z binance warm_ms 30 total_ms 190 http 200\n"
                   "2026-09-19T00:02:00Z binance warm_ms nan total_ms nan http 000\n"
                   "2026-09-19T00:03:00Z binance warm_ms 12 total_ms 40 http 451\n"  # an edge refusal is not a round trip
                   "2026-09-19T00:04:00Z binance warm_ms nan total_ms nan http 403\n"
                   "2026-09-19T00:00:00Z kraken warm_ms 250 total_ms 900 http 200\n"
                   "2026-09-19T00:00:00Z coinbase warm_ms 9 total_ms 30 http 451\n", encoding="utf-8")
    rtt = rollup.parse_rtt_log(log)
    assert rtt["binance"]["n"] == 2 and rtt["binance"]["p50_ms"] == 25.0
    assert rtt["binance"]["blocked"] == 1 and rtt["binance"]["failed"] == 2
    assert rtt["kraken"]["p90_ms"] == 250.0 and rtt["kraken"]["blocked"] == 0
    assert rtt["coinbase"]["n"] == 0 and rtt["coinbase"]["blocked"] == 1  # a fully blocked venue still appears


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


def test_json_output_has_no_nan_tokens_when_nothing_settled(tmp_path, capsys):
    write_run(tmp_path / "logs" / "run1", [tri_opp(T0)], [], [])
    rtt = tmp_path / "rtt.log"
    rtt.write_text("2026-09-19T00:00:00Z binance warm_ms 9 total_ms 30 http 451\n", encoding="utf-8")
    scan = tmp_path / "scan.log"
    scan.write_text("nothing here\n", encoding="utf-8")
    assert rollup.main([str(tmp_path / "logs"), "--include-today", "--json", "--rtt-log", str(rtt), "--scan-log", str(scan)]) == 0
    out = capsys.readouterr().out

    def strict(c):
        raise ValueError(f"bare {c} in JSON output")

    rep = json.loads(out, parse_constant=strict)
    row = rep["daily"][0]
    assert row["fill_rate"] is None and row["realized_bps"] is None and rep["ops"]["stale_share_mean"] is None
    assert rep["rtt"]["binance"]["p90_ms"] is None and rep["rtt"]["binance"]["blocked"] == 1
    # and the text report warns about the blocked venue instead of printing a fast p90
    assert rollup.main([str(tmp_path / "logs"), "--include-today", "--rtt-log", str(rtt)]) == 0
    captured = capsys.readouterr()
    assert "binance p50=n/a p90=n/a (n=0, blocked=1" in captured.out and "geo-blocked" in captured.err


def test_measure7d_config_loads_and_is_paper_only():
    sys.path.insert(0, str(ROOT))
    from arbbot.config import load_config

    cfg = load_config(ROOT / "measure7d.toml")
    assert cfg.live.enabled is False and cfg.live.real_orders is False
    assert cfg.paper.fill_model == "arrival" and cfg.paper.fill_fraction == 0.5
    assert cfg.risk.min_profit_usd == 0.01
