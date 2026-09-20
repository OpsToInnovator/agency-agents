"""`arbbot reconcile` is what the operator looks with after a sticky halt: it must mark
every balance honestly, name the inventory to sell, never send an order, and lift a halt
only when the account is flat."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbbot.cli import build_parser, cmd_reconcile
from arbbot.execution.live import BinanceRest
from arbbot.execution.reconcile import clear_halt, describe_entry, mark_in_usdt, reconcile_report, tail_journal
from tests.test_execution import PING, TIME, FakeSession, run

PRICES = [{"symbol": "BTCUSDT", "price": "81000"}, {"symbol": "UNIUSDT", "price": "9.02"}, {"symbol": "BNBUSDT", "price": "612.3"},
          {"symbol": "XYZBTC", "price": "0.0001"}, {"symbol": "DOGEUSDT", "price": "0.12"}]


def _session(balances, open_orders=None, extra=()):
    return FakeSession([PING, TIME, ("/api/v3/account", 200, {"balances": balances}), ("/api/v3/ticker/price", 200, PRICES),
                        ("/api/v3/openOrders", 200, open_orders or []), *extra])


def _rest(session):
    rest = BinanceRest("https://trade.example", "k", "s", 5000, session, public_base_url="https://mirror.example")
    run(rest.sync_time())
    return rest


def _state(tmp_path, **kw):
    p = tmp_path / "risk_state.json"
    payload = {"day": "2026-09-20", "daily_realized_usd": -1.2345, "halted": False, "halt_sticky": False, "halt_reason": ""}
    payload.update(kw)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_marks_use_usdt_then_btc_cross_then_unknown():
    prices = {r["symbol"]: float(r["price"]) for r in PRICES}
    assert mark_in_usdt("USDT", prices) == 1.0 and mark_in_usdt("FDUSD", prices) == 1.0
    assert mark_in_usdt("UNI", prices) == 9.02
    assert mark_in_usdt("XYZ", prices) == pytest.approx(0.0001 * 81000)
    assert mark_in_usdt("NOPE", prices) is None


def test_report_marks_balances_and_names_the_inventory(tmp_path):
    balances = [{"asset": "USDT", "free": "487.35", "locked": "0"}, {"asset": "BNB", "free": "0.0421", "locked": "0"},
                {"asset": "UNI", "free": "5.5", "locked": "0"}, {"asset": "DOGE", "free": "0.9", "locked": "0"},
                {"asset": "NOPE", "free": "3", "locked": "0"}, {"asset": "ZERO", "free": "0", "locked": "0"}]
    session = _session(balances)
    state = _state(tmp_path, halted=True, halt_sticky=True, halt_reason="cycle aborted mid-way (leg 2)")
    journal = tmp_path / "intents.jsonl"
    journal.write_text("\n".join([
        json.dumps({"ts": 1789866123.0, "kind": "intent", "real": True, "client_id": "arbabcdef0123456789",
                    "params": {"symbol": "UNIUSDT", "side": "BUY", "quantity": "5.5", "price": "9.05"}}),
        json.dumps({"ts": 1789866123.2, "kind": "response", "client_id": "arbabcdef0123456789",
                    "payload": {"status": "FILLED", "executedQty": "5.5", "cummulativeQuoteQty": "49.78"}}),
        "{not json",
        json.dumps({"ts": 1789866123.5, "kind": "error", "client_id": "arb9999", "error": "OSError(28, 'No space left on device')"}),
    ]) + "\n", encoding="utf-8")
    rep = run(reconcile_report(_rest(session), 500.0, 10.0, state, journal, last_n=10))
    roles = {r["asset"]: r["role"] for r in rep["balances"]}
    assert roles == {"USDT": "cash", "BNB": "fee float", "UNI": "INVENTORY", "DOGE": "dust", "NOPE": "UNPRICED"}
    assert "ZERO" not in roles
    assert rep["total_usdt"] == pytest.approx(487.35 + 0.0421 * 612.3 + 5.5 * 9.02 + 0.9 * 0.12)
    assert rep["usdt_free"] == 487.35 and rep["kill_floor_usd"] == 450.0
    assert rep["stake_status"] == "free USDT inside the kill budget"
    assert rep["halted"] and not rep["flat"]
    assert rep["verdict"].startswith("NOT FLAT: sell 5.5 UNI (about 49.61 USDT) back to USDT; price and settle NOPE by hand")
    assert [j.split()[2] for j in rep["journal"]] == ["intent", "response", "error"]  # the malformed line is skipped
    assert all(x in rep["journal"][0] for x in ("BUY", "UNIUSDT", "qty 5.5 @ 9.05", "(REAL)"))
    assert "FILLED executedQty 5.5 cummulativeQuoteQty 49.78" in rep["journal"][1]
    # nothing but GETs left the machine, and the price list came from the public mirror
    assert {c[0] for c in session.calls} == {"GET"}
    assert any(url.startswith("https://mirror.example/api/v3/ticker/price") for _, url, _ in session.calls)
    assert any(url.startswith("https://trade.example/api/v3/account?") for _, url, _ in session.calls)


def test_report_is_flat_with_only_cash_fee_float_and_dust(tmp_path):
    balances = [{"asset": "USDT", "free": "500", "locked": "0"}, {"asset": "BNB", "free": "0.08", "locked": "0"},
                {"asset": "DOGE", "free": "1", "locked": "0"}]
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, None, None))
    assert rep["flat"] and not rep["halted"] and rep["verdict"] == "FLAT: nothing to reconcile"
    assert rep["state"] is None and rep["journal"] == [] and rep["stake_status"] == "free USDT at or above the stake"
    below = run(reconcile_report(_rest(_session([{"asset": "USDT", "free": "440", "locked": "0"}])), 500.0, 10.0, None, None))
    assert below["stake_status"].startswith("free USDT BELOW THE KILL FLOOR")


def test_open_orders_failure_does_not_hide_the_report():
    session = FakeSession([PING, TIME, ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "500", "locked": "0"}]}),
                           ("/api/v3/ticker/price", 200, PRICES), ("/api/v3/openOrders", 500, {"raw": "<html>boom</html>"})])
    rep = run(reconcile_report(_rest(session), 500.0, 10.0, None, None))
    assert rep["flat"] and "error" in rep["open_orders"][0]


def test_clear_halt_keeps_the_day_and_its_loss(tmp_path):
    state = _state(tmp_path, halted=True, halt_sticky=True, halt_reason="reconcile me")
    previous = clear_halt(state)
    assert previous["halt_reason"] == "reconcile me"
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after == {"day": "2026-09-20", "daily_realized_usd": -1.2345, "halted": False, "halt_sticky": False, "halt_reason": ""}
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        clear_halt(tmp_path / "bad.json")


def test_tail_journal_and_describe_entry_are_tolerant():
    assert tail_journal(None, 5) == [] and tail_journal(Path("/nonexistent/x.jsonl"), 5) == []
    assert describe_entry({"ts": 1789866123.0, "kind": "response", "client_id": "c", "payload": "weird"}).split()[2] == "response"
    assert describe_entry({"ts": "bad", "kind": "other", "x": 1}).startswith("?  other")


def _args(tmp_path, *extra):
    cfg = tmp_path / "live.toml"
    cfg.write_text("[risk]\nstate_file = 'logs/live/risk_state.json'\n[live]\nenabled = true\ncapital_usd = 500.0\n"
                   "intent_log = 'logs/live/live_intents.jsonl'\n", encoding="utf-8")
    return build_parser().parse_args(["reconcile", "--config", str(cfg), *extra])


def test_cli_exit_codes_and_clear_halt_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    # keys missing -> 2
    monkeypatch.delenv("BINANCE_API_SECRET")
    assert run(cmd_reconcile(_args(tmp_path), session=_session([]))) == 2
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    # geo-blocked -> 3, nothing signed sent
    blocked = FakeSession([("/api/v3/ping", 451, {"raw": "<html>"})])
    assert run(cmd_reconcile(_args(tmp_path), session=blocked)) == 3
    assert [c[0] for c in blocked.calls] == ["GET"] and "signature=" not in blocked.calls[0][1]
    # flat, not halted -> 0
    flat = [{"asset": "USDT", "free": "500", "locked": "0"}]
    assert run(cmd_reconcile(_args(tmp_path), session=_session(flat))) == 0
    out = capsys.readouterr().out
    assert "verdict: FLAT: nothing to reconcile" in out and "risk state: no file" in out
    # inventory outstanding and halted -> 1; --clear-halt refused; --force clears
    state_dir = tmp_path / "logs" / "live"
    state_dir.mkdir(parents=True)
    state = state_dir / "risk_state.json"
    state.write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": -2.0, "halted": True, "halt_sticky": True,
                                 "halt_reason": "cycle aborted mid-way"}), encoding="utf-8")
    held = [{"asset": "USDT", "free": "450.5", "locked": "0"}, {"asset": "UNI", "free": "5.5", "locked": "0"}]
    assert run(cmd_reconcile(_args(tmp_path), session=_session(held))) == 1
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(held))) == 1
    assert "refusing --clear-halt" in capsys.readouterr().err
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is True
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt", "--force"), session=_session(held))) == 0
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is False
    # flat and halted -> --clear-halt lifts it without --force; --json parses
    state.write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": -2.0, "halted": True, "halt_sticky": True,
                                 "halt_reason": "reconcile me"}), encoding="utf-8")
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(flat))) == 0
    assert json.loads(state.read_text(encoding="utf-8")) == {"day": "2026-09-20", "daily_realized_usd": -2.0, "halted": False,
                                                              "halt_sticky": False, "halt_reason": ""}
    capsys.readouterr()  # drop the text reports printed so far
    assert run(cmd_reconcile(_args(tmp_path, "--json"), session=_session(flat))) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["flat"] is True and rep["capital_usd"] == 500.0
