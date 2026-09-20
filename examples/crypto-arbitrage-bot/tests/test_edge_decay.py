"""`scripts/edge_decay.py` answers whether an apparent triangular edge is real or an
artifact of pricing three legs from three different moments. It must never claim an edge
the fee floor eats, and it must be honest when nothing on the tape was fresh."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.conftest import ROOT

spec = importlib.util.spec_from_file_location("edge_decay", ROOT / "scripts" / "edge_decay.py")
edge_decay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge_decay)


def test_symbols_in_reads_the_tape_and_ignores_junk(tmp_path):
    tape = tmp_path / "t.jsonl"
    tape.write_text('{"t": 1.0, "venue": "binance", "raw": "{\\"s\\":\\"BTCUSDT\\",\\"b\\":\\"1\\"}"}\n'
                    "not json at all\n"
                    '{"t": 2.0, "venue": "binance", "raw": "{\\"s\\":\\"ETHBTC\\"}"}\n', encoding="utf-8")
    assert edge_decay.symbols_in(tape) == ["BTCUSDT", "ETHBTC"]


def test_summarise_bands_by_edge_and_reports_the_fresh_remainder():
    # (gross_bps, net_bps, oldest_leg_ms): two fresh, one stale with the biggest apparent edge
    rows = [(1.0, -29.0, 10.0), (7.0, -23.0, 20.0), (40.0, 10.0, 900.0)]
    s = edge_decay.summarise(rows, fresh_ms=50.0, fee_bps=10.0)
    assert s["scored"] == 3 and s["hurdle_bps"] == 30.0
    assert [b["band"] for b in s["bands"]] == ["0 to 5 bps", "5 to 10 bps", "20 to 50 bps"]
    assert s["net_positive"] == 1  # the stale one, and only the stale one
    assert s["fresh_count"] == 2
    assert s["fresh_best_gross_bps"] == 7.0 and s["fresh_best_net_bps"] == -23.0
    text = edge_decay.render(s)
    assert "net-positive after fees: 1 of 3" in text
    assert "best net -23.00 bps" in text


def test_summarise_says_so_when_nothing_was_fresh():
    s = edge_decay.summarise([(3.0, -27.0, 800.0)], fresh_ms=50.0, fee_bps=10.0)
    assert s["fresh_count"] == 0 and s["fresh_best_net_bps"] is None
    assert "not one opportunity had every leg under 50 ms" in edge_decay.render(s)
    json.dumps(s)  # the --json path must stay serialisable, so no NaN


def test_summarise_names_which_way_staleness_cuts():
    staler_at_the_top = [(50.0, 20.0, 900.0)] * 200 + [(1.0, -29.0, 10.0)] * 50
    assert "comparison artifact" in edge_decay.render(edge_decay.summarise(staler_at_the_top, 50.0, 10.0))
    fresh_at_the_top = [(50.0, 20.0, 5.0)] * 200 + [(1.0, -29.0, 900.0)] * 50
    assert "staleness is not what is creating them" in edge_decay.render(edge_decay.summarise(fresh_at_the_top, 50.0, 10.0))


def test_runs_end_to_end_on_the_bundled_fixture():
    rows = edge_decay.asyncio.run(edge_decay.collect(ROOT / "tests" / "fixtures" / "feed_fixture.jsonl", 10.0))
    assert rows, "the fixture must score at least one triangle"
    assert all(len(r) == 3 and r[2] >= 0.0 for r in rows)
    s = edge_decay.summarise(rows, 50.0, 10.0)
    assert s["net_positive"] == 0  # nothing on the fixture clears three taker legs
    json.dumps(s)


def test_empty_tape_is_reported_not_crashed(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert edge_decay.summarise([], 50.0, 10.0) == {"scored": 0}
    assert "no triangular opportunities scored" in edge_decay.render({"scored": 0})
