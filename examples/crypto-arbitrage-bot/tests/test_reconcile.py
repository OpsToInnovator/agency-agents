"""`arbbot reconcile` is what the operator looks with after a sticky halt: it must mark
every balance honestly, name the inventory to sell, never send an order, and lift a halt
only when the account is flat."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbbot.cli import build_parser, cmd_reconcile
from arbbot.execution.live import BinanceRest
from arbbot.execution.reconcile import (clear_halt, describe_entry, format_report, mark_in_usdt,
                                        reconcile_report, tail_journal)
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
    rep = run(reconcile_report(_rest(session), 500.0, 10.0, state, journal, last_n=10, fee_float_usd=50.0))
    roles = {r["asset"]: r["role"] for r in rep["balances"]}
    assert roles == {"USDT": "cash", "BNB": "fee float", "UNI": "INVENTORY", "DOGE": "dust", "NOPE": "UNPRICED"}
    assert "ZERO" not in roles
    assert rep["total_usdt"] == pytest.approx(487.35 + 0.0421 * 612.3 + 5.5 * 9.02 + 0.9 * 0.12)
    assert rep["usdt_free"] == 487.35 and rep["kill_floor_usd"] == 450.0
    assert rep["stake_status"] == "free USDT inside the kill budget"
    assert rep["halted"] is True and not rep["flat"]
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
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, None, None, fee_float_usd=50.0))
    assert rep["flat"] and rep["halted"] is False and rep["verdict"] == "FLAT: nothing to reconcile"
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
    assert after == {"day": "2026-09-20", "daily_realized_usd": -1.2345, "halted": False, "halt_sticky": False,
                     "halt_reason": "", "halt_token": ""}
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        clear_halt(tmp_path / "bad.json")


def test_tail_journal_and_describe_entry_are_tolerant():
    assert tail_journal(None, 5) == [] and tail_journal(Path("/nonexistent/x.jsonl"), 5) == []
    assert describe_entry({"ts": 1789866123.0, "kind": "response", "client_id": "c", "payload": "weird"}).split()[2] == "response"
    assert describe_entry({"ts": "bad", "kind": "other", "x": 1}).startswith("?  other")


def _args(tmp_path, *extra):
    cfg = tmp_path / "live.toml"
    cfg.write_text("[venues]\nbinance_rest = 'https://mirror.example'\nbinance_trade_rest = 'https://trade.example'\n"
                   "[risk]\nstate_file = 'logs/live/risk_state.json'\nkill_switch_file = 'STOP'\n[live]\nenabled = true\n"
                   "capital_usd = 500.0\nintent_log = 'logs/live/live_intents.jsonl'\n", encoding="utf-8")
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
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt", "--force"), session=_session(held))) == 1  # cleared, still not flat
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is False
    # flat and halted -> --clear-halt lifts it without --force; --json parses
    state.write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": -2.0, "halted": True, "halt_sticky": True,
                                 "halt_reason": "reconcile me"}), encoding="utf-8")
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(flat))) == 0
    assert json.loads(state.read_text(encoding="utf-8")) == {"day": "2026-09-20", "daily_realized_usd": -2.0,
                                                              "halted": False, "halt_sticky": False,
                                                              "halt_reason": "", "halt_token": ""}
    capsys.readouterr()  # drop the text reports printed so far
    assert run(cmd_reconcile(_args(tmp_path, "--json"), session=_session(flat))) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["flat"] is True and rep["capital_usd"] == 500.0


def _intent(symbol, side="BUY", qty="0.08", price="612"):
    return json.dumps({"ts": 1789866123.0, "kind": "intent", "real": True, "client_id": "arb1",
                       "params": {"symbol": symbol, "side": side, "quantity": qty, "price": price}})


def test_bnb_above_the_declared_fee_float_is_inventory(tmp_path):
    # 0.1633 BNB = 99.99 USDT against a 50 float: the excess is a leftover position, not float
    balances = [{"asset": "USDT", "free": "450.02", "locked": "0"}, {"asset": "BNB", "free": "0.1633", "locked": "0"}]
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, None, None, fee_float_usd=50.0))
    bnb = next(r for r in rep["balances"] if r["asset"] == "BNB")
    assert bnb["role"] == "INVENTORY" and "above the declared 50.00 fee float" in bnb["note"]
    assert not rep["flat"] and "sell about 49.99 USDT of BNB back to USDT, keeping the 50.00 fee float" in rep["verdict"]
    # with no float declared (the default), any BNB is inventory
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, None, None))
    assert next(r for r in rep["balances"] if r["asset"] == "BNB")["role"] == "INVENTORY"


def test_bnb_is_inventory_whatever_its_size_while_the_halted_cycle_traded_a_bnb_market(tmp_path):
    balances = [{"asset": "USDT", "free": "480", "locked": "0"}, {"asset": "BNB", "free": "0.03", "locked": "0"}]  # 18.37 USDT
    state = _state(tmp_path, halted=True, halt_sticky=True, halt_reason="cycle aborted mid-way (SOLBNB: IOC order filled nothing)")
    journal = tmp_path / "intents.jsonl"
    journal.write_text(_intent("BNBUSDT") + "\n" + _intent("SOLBNB", "BUY", "0.5", "0.3") + "\n", encoding="utf-8")
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, state, journal, fee_float_usd=50.0))
    bnb = next(r for r in rep["balances"] if r["asset"] == "BNB")
    assert bnb["role"] == "INVENTORY" and bnb["note"] == "a BNB market is in the halted cycle"
    assert not rep["flat"] and "sell 0.03 BNB" in rep["verdict"]
    # the same balance and journal, not halted: float
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, _state(tmp_path), journal, fee_float_usd=50.0))
    assert next(r for r in rep["balances"] if r["asset"] == "BNB")["role"] == "fee float" and rep["flat"]
    # a symbol that merely contains the letters is not a BNB market
    journal.write_text(_intent("BNBXUSDT") + "\n", encoding="utf-8")
    rep = run(reconcile_report(_rest(_session(balances)), 500.0, 10.0, state, journal, fee_float_usd=50.0))
    assert next(r for r in rep["balances"] if r["asset"] == "BNB")["role"] == "fee float"


def test_unreadable_state_file_is_attention_not_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    flat = [{"asset": "USDT", "free": "500", "locked": "0"}]
    state_dir = tmp_path / "logs" / "live"
    state_dir.mkdir(parents=True)
    (state_dir / "risk_state.json").write_text("{\"halted\": tru", encoding="utf-8")  # truncated mid-write
    rep = run(reconcile_report(_rest(_session(flat)), 500.0, 10.0, state_dir / "risk_state.json", None))
    assert rep["flat"] and rep["halted"] is None and rep["state_error"] and "unreadable" in rep["verdict"]
    assert run(cmd_reconcile(_args(tmp_path), session=_session(flat))) == 1
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt", "--force"), session=_session(flat))) == 1
    assert "cannot be read" in capsys.readouterr().err
    assert (state_dir / "risk_state.json").read_text(encoding="utf-8") == "{\"halted\": tru"  # untouched
    # a directory where the file should be counts the same way
    (state_dir / "risk_state.json").unlink()
    (state_dir / "risk_state.json").mkdir()
    assert run(cmd_reconcile(_args(tmp_path), session=_session(flat))) == 1


def test_locked_balances_and_resting_orders_are_not_flat():
    held = [{"asset": "USDT", "free": "400", "locked": "100"}, {"asset": "UNI", "free": "0", "locked": "5.5"}]
    order = [{"symbol": "UNIUSDT", "side": "SELL", "origQty": "5.5", "price": "9.5", "status": "NEW", "clientOrderId": "mine1"}]
    rep = run(reconcile_report(_rest(_session(held, order)), 500.0, 10.0, None, None))
    assert not rep["flat"] and rep["resting_orders"] == order
    assert "sell 5.5 UNI (about 49.61 USDT) back to USDT (5.5 of it is locked in an open order" in rep["verdict"]
    assert "cancel open SELL 5.5 UNIUSDT @ 9.5 (id mine1) or let it fill" in rep["verdict"]
    assert rep["stake_status"].startswith("free USDT below the kill floor because 100.00 USDT is locked")
    # a resting order alone, with cash otherwise flat, is still not flat
    rep = run(reconcile_report(_rest(_session([{"asset": "USDT", "free": "500", "locked": "0"}], order)), 500.0, 10.0, None, None))
    assert not rep["flat"] and rep["verdict"].startswith("NOT FLAT: cancel open SELL")


def test_unlisted_open_orders_block_clear_halt_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    state_dir = tmp_path / "logs" / "live"
    state_dir.mkdir(parents=True)
    (state_dir / "risk_state.json").write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": 0.0, "halted": True,
                                                            "halt_sticky": True, "halt_reason": "reconcile me"}), encoding="utf-8")

    def broken():
        return FakeSession([PING, TIME, ("/api/v3/account", 200, {"balances": [{"asset": "USDT", "free": "500", "locked": "0"}]}),
                            ("/api/v3/ticker/price", 200, PRICES), ("/api/v3/openOrders", 500, {"raw": "<html>boom</html>"})])

    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=broken())) == 1
    assert "flatness is unproven" in capsys.readouterr().err
    assert json.loads((state_dir / "risk_state.json").read_text(encoding="utf-8"))["halted"] is True
    assert "unproven" in run(reconcile_report(_rest(broken()), 500.0, 10.0, None, None))["verdict"]
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt", "--force"), session=broken())) == 0


def test_kill_switch_file_is_reported(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("", encoding="utf-8")
    rep = run(reconcile_report(_rest(_session([{"asset": "USDT", "free": "500", "locked": "0"}])), 500.0, 10.0, None, None,
                               kill_switch_file=stop))
    assert rep["kill_switch_present"] and rep["flat"] and "kill switch file" in rep["verdict"]
    from arbbot.execution.reconcile import format_report
    assert "is PRESENT" in format_report(rep)


def test_clear_halt_preserves_the_files_mode(tmp_path):
    state = _state(tmp_path, halted=True, halt_sticky=True, halt_reason="x")
    state.chmod(0o640)
    clear_halt(state)
    import stat as st
    assert st.S_IMODE(state.stat().st_mode) == 0o640
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is False


def test_clear_halt_exit_codes_follow_flatness(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    state_dir = tmp_path / "logs" / "live"
    state_dir.mkdir(parents=True)
    (state_dir / "risk_state.json").write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": 0.0, "halted": False,
                                                            "halt_sticky": False, "halt_reason": ""}), encoding="utf-8")
    held = [{"asset": "USDT", "free": "450.5", "locked": "0"}, {"asset": "UNI", "free": "5.5", "locked": "0"}]
    # not halted but not flat: plain and --clear-halt both say attention
    assert run(cmd_reconcile(_args(tmp_path), session=_session(held))) == 1
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(held))) == 1
    assert "nothing to clear: not halted" in capsys.readouterr().err
    # no state file at all, flat: 0 either way
    (state_dir / "risk_state.json").unlink()
    flat = [{"asset": "USDT", "free": "500", "locked": "0"}]
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(flat))) == 0


def test_network_failures_after_the_ping_are_fail_lines_with_the_documented_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    # the market-data mirror is down: 3
    mirror_down = FakeSession([PING, ("/api/v3/time", 500, {"raw": "<html>502 bad gateway</html>"})])
    assert run(cmd_reconcile(_args(tmp_path), session=mirror_down)) == 3
    err = capsys.readouterr().err
    assert "FAIL cannot read server time from https://mirror.example" in err and "bad gateway" in err
    assert "note: no risk state file" in err  # the wrong-directory hint comes first
    # the key is refused: 2, and the message names the cause
    bad_key = FakeSession([PING, TIME, ("/api/v3/account", 401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})])
    assert run(cmd_reconcile(_args(tmp_path), session=bad_key)) == 2
    assert "key was refused" in capsys.readouterr().err
    # any other HTTP failure on the trading host: 3
    flaky = FakeSession([PING, TIME, ("/api/v3/account", 503, {"raw": "maintenance"})])
    assert run(cmd_reconcile(_args(tmp_path), session=flaky)) == 3
    assert "HTTP 503" in capsys.readouterr().err


def test_reconcile_report_flags_an_unwind_with_no_answer(tmp_path, capsys):
    """An unwind intent with no recorded answer is the one state in which a human selling by
    hand can double-sell, so it must reach the verdict and block --clear-halt."""
    journal = tmp_path / "intents.jsonl"
    entries = [
        {"ts": 1789795661.0, "kind": "intent", "client_id": "arb0001", "real": True,
         "params": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.00999", "price": "100000"}},
        {"ts": 1789795661.1, "kind": "response", "client_id": "arb0001",
         "payload": {"status": "FILLED", "executedQty": "0.00999", "cummulativeQuoteQty": "999"}},
        {"ts": 1789795661.2, "kind": "unwind_plan", "start_asset": "USDT", "stranded": {"BTC": 0.00998001},
         "route": {"BTC": "BTCUSDT"}, "opportunity": "USDT->BTC->ETH->USDT"},
        {"ts": 1789795661.3, "kind": "unwind_intent", "client_id": "unw1f3c8a", "attempt": 1, "of": 3,
         "params": {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.00998", "price": "99749.00"}},
    ]
    journal.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    held = [{"asset": "USDT", "free": "450.5", "locked": "0"}, {"asset": "BTC", "free": "0.00998", "locked": "0"}]
    rep = run(reconcile_report(_rest(_session(held)), 500.0, 10.0, None, journal, 10, 5.0, "BNB", 0.0, None))
    assert rep["unwind_in_flight"] == ["unw1f3c8a"]
    assert "look them up on Binance by client order id" in rep["verdict"]
    text = format_report(rep)
    assert "unw1f3c8a" in text and "LOOK THIS ORDER UP BY CLIENT ID" in text
    assert "[unwind] intent" in "\n".join(rep["journal"])
    assert "sell 0.00998001 BTC via BTCUSDT" in "\n".join(rep["journal"])


def test_unwind_in_flight_blocks_clear_halt_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    state = tmp_path / "logs" / "live" / "risk_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"day": "2026-09-20", "daily_realized_usd": -2.0, "halted": True, "halt_sticky": True,
                                 "halt_reason": "cycle aborted mid-way"}), encoding="utf-8")
    journal = tmp_path / "logs" / "live" / "live_intents.jsonl"
    journal.write_text(json.dumps({"ts": 1789795661.2, "kind": "unwind_plan", "start_asset": "USDT",
                                   "stranded": {"BTC": 0.00998001}, "route": {"BTC": "BTCUSDT"}}) + "\n"
                       + json.dumps({"ts": 1789795661.3, "kind": "unwind_intent", "client_id": "unw1f3c8a",
                                     "attempt": 1, "of": 3,
                                     "params": {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.00998",
                                                "price": "99749.00"}}) + "\n", encoding="utf-8")
    flat = [{"asset": "USDT", "free": "500", "locked": "0"}]
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt"), session=_session(flat))) == 1
    assert "have no recorded answer" in capsys.readouterr().err
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is True  # untouched
    assert run(cmd_reconcile(_args(tmp_path, "--clear-halt", "--force"), session=_session(flat))) == 0
    assert json.loads(state.read_text(encoding="utf-8"))["halted"] is False
