"""Read-only reconciliation of a live Binance account against the bot's own records.

The live executor never unwinds a cycle that fails halfway: it books what it knows,
halts sticky and leaves the human to look. This is what the human looks with. It
prints every balance marked in USDT at the current price, whether the stake declared
in [live] is still on the exchange, any resting orders, the persisted risk state, the
kill-switch file and the last order intents from the journal, then says in plain words
whether the account is flat (everything back in USDT, apart from the declared BNB fee
float and dust) and what to sell or cancel if it is not. Nothing here sends an order.
The only write it can do, on request, is to lift the halt flag in the risk state file
once the account is flat.
"""
from __future__ import annotations

import json
import os
import stat as stat_module
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import USD_FAMILY
from ..universe import split_binance_symbol

DUST_USD = 5.0  # Binance's usual NOTIONAL minimum: below it a balance cannot even be sold
FEE_ASSET = "BNB"


def mark_in_usdt(asset: str, prices: dict[str, float]) -> float | None:
    """USDT per unit of `asset` from the public price list: a stable at par, else the
    <ASSET>USDT price, else the <ASSET>BTC price times BTCUSDT, else unknown."""
    if asset in USD_FAMILY:
        return 1.0
    direct = prices.get(f"{asset}USDT")
    if direct is not None:
        return direct
    via_btc = prices.get(f"{asset}BTC")
    btc = prices.get("BTCUSDT")
    if via_btc is not None and btc is not None:
        return via_btc * btc
    return None


def read_state(path: Path | None) -> dict[str, Any] | None:
    """The persisted risk state, None when there is no file, {"error": ...} when it exists
    but cannot be read or parsed (a halt may be hiding in it: never treat that as clear)."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"error": "not a JSON object"}
    return data


def tail_journal(path: Path | None, last_n: int) -> list[dict[str, Any]]:
    """The last `last_n` well-formed entries of the intent journal, oldest first."""
    if path is None or not path.exists() or last_n <= 0:
        return []
    entries: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries[-last_n:]


def describe_entry(entry: dict[str, Any]) -> str:
    ts = entry.get("ts")
    when = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, (int, float)) else "?"
    kind = str(entry.get("kind", "?"))
    cid = str(entry.get("client_id", ""))[:14]
    if kind == "intent":
        p = entry.get("params") or {}
        real = "REAL" if entry.get("real") else "test"
        return f"{when}  intent    {cid:14}  {p.get('side', '?'):4} {p.get('symbol', '?'):10} qty {p.get('quantity', '?')} @ {p.get('price', '?')}  ({real})"
    if kind == "response":
        payload = entry.get("payload")
        if isinstance(payload, dict):
            return (f"{when}  response  {cid:14}  {payload.get('status', '?')} executedQty {payload.get('executedQty', '?')} "
                    f"cummulativeQuoteQty {payload.get('cummulativeQuoteQty', '?')}")
        return f"{when}  response  {cid:14}  {str(payload)[:80]}"
    if kind == "error":
        return f"{when}  error     {cid:14}  {str(entry.get('error', ''))[:100]}"
    return f"{when}  {kind:9} {cid:14}  {json.dumps({k: v for k, v in entry.items() if k not in ('ts', 'kind', 'client_id')})[:100]}"


def journal_touches(entries: list[dict[str, Any]], asset: str) -> bool:
    """Did any order intent in these entries trade a market with `asset` as base or quote?"""
    for e in entries:
        if e.get("kind") != "intent":
            continue
        symbol = str((e.get("params") or {}).get("symbol", ""))
        parts = split_binance_symbol(symbol)
        if parts and asset in parts:
            return True
    return False


async def reconcile_report(rest: Any, capital_usd: float, max_cumulative_loss_pct: float, state_file: Path | None,
                           intent_log: Path | None, last_n: int = 10, dust_usd: float = DUST_USD,
                           fee_asset: str = FEE_ASSET, fee_float_usd: float = 0.0,
                           kill_switch_file: Path | None = None) -> dict[str, Any]:
    """Build the report. `rest` is a BinanceRest whose clock is already synced.

    `fee_float_usd` is the BNB the operator deposited to pay fees with: that much (plus
    dust) is a float, anything more is inventory a cycle left behind. When the bot is
    halted and the journal shows it trading a BNB market, BNB is inventory whatever its
    size, because a partial fill under the allowance is exactly what a halt looks like.
    """
    account = await rest.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
    price_rows = await rest.request("GET", "/api/v3/ticker/price", signed=False, base=rest.public_base_url)
    prices: dict[str, float] = {}
    for row in price_rows if isinstance(price_rows, list) else []:
        try:
            prices[str(row["symbol"])] = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
    try:
        open_orders = await rest.request("GET", "/api/v3/openOrders")
        if not isinstance(open_orders, list):
            open_orders = []
    except Exception as exc:  # the report must still print; but flatness is then unproven
        open_orders = [{"error": f"{type(exc).__name__}: {exc}"}]
    resting = [o for o in open_orders if isinstance(o, dict) and "error" not in o]
    orders_unknown = any(isinstance(o, dict) and "error" in o for o in open_orders)

    state = read_state(state_file)
    state_error = state.get("error") if isinstance(state, dict) and "error" in state else None
    halted: bool | None = None if state_error else bool(state and state.get("halted"))
    journal_entries = tail_journal(intent_log, last_n)
    fee_asset_in_halted_cycle = bool(halted) and journal_touches(journal_entries, fee_asset)

    rows: list[dict[str, Any]] = []
    total = 0.0
    usdt_free = 0.0
    usdt_locked = 0.0
    inventory: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for b in account.get("balances", []):
        asset = str(b.get("asset", "?"))
        try:
            free = float(b.get("free", 0) or 0)
            locked = float(b.get("locked", 0) or 0)
        except (TypeError, ValueError):
            continue
        qty = free + locked
        if qty <= 0:
            continue
        mark = mark_in_usdt(asset, prices)
        usd = qty * mark if mark is not None else None
        if asset == "USDT":
            usdt_free, usdt_locked = free, locked
        if usd is not None:
            total += usd
        note = ""
        if asset in USD_FAMILY:
            role = "cash"
        elif usd is None:
            role = "UNPRICED"
            unpriced.append(asset)
        elif asset == fee_asset and fee_asset_in_halted_cycle:
            role = "INVENTORY"
            note = f"a {fee_asset} market is in the halted cycle"
        elif asset == fee_asset and usd <= fee_float_usd + dust_usd:
            role = "fee float"
        elif asset == fee_asset:
            role = "INVENTORY"
            note = f"{usd - fee_float_usd:.2f} USDT above the declared {fee_float_usd:.2f} fee float"
        elif usd < dust_usd:
            role = "dust"
        else:
            role = "INVENTORY"
        row = {"asset": asset, "free": free, "locked": locked, "mark": mark, "usd": usd, "role": role, "note": note}
        rows.append(row)
        if role == "INVENTORY":
            inventory.append(row)
    rows.sort(key=lambda r: -(r["usd"] or 0.0))
    floor = capital_usd * (1.0 - max_cumulative_loss_pct / 100.0) if capital_usd else 0.0
    if not capital_usd:
        stake_status = "no stake declared"
    elif usdt_free >= capital_usd - 0.01:
        stake_status = "free USDT at or above the stake"
    elif usdt_free >= floor - 0.01:
        stake_status = "free USDT inside the kill budget"
    elif usdt_free + usdt_locked >= floor - 0.01:
        stake_status = (f"free USDT below the kill floor because {usdt_locked:.2f} USDT is locked in open orders: "
                        f"cancel them and re-check; the locked USDT still counts as stake, not as loss")
    else:
        stake_status = "free USDT BELOW THE KILL FLOOR: preflight will refuse to re-arm; do not restart on these settings"
    kill_switch_present = bool(kill_switch_file and Path(kill_switch_file).exists())
    flat = not inventory and not unpriced and not resting

    def sell_instruction(r: dict[str, Any]) -> str:
        qty = r["free"] + r["locked"]
        if r["asset"] == fee_asset and r["note"].endswith("fee float"):
            excess = max(0.0, (r["usd"] or 0.0) - fee_float_usd)
            text = f"sell about {excess:.2f} USDT of {fee_asset} back to USDT, keeping the {fee_float_usd:.2f} fee float"
        else:
            text = f"sell {qty:g} {r['asset']} (about {r['usd']:.2f} USDT) back to USDT"
        if r["locked"] > 0:
            text += (f" ({r['locked']:g} of it is locked in an open order: cancel that order, or let it fill, before selling; "
                     f"see the open orders above)")
        if r["note"] and not r["note"].endswith("fee float"):
            text += f" ({r['note']})"
        return text

    parts = [sell_instruction(r) for r in inventory]
    parts += [f"price and settle {a} by hand (no USDT or BTC market found for it)" for a in unpriced]
    parts += [f"cancel open {o.get('side', '?')} {o.get('origQty', '?')} {o.get('symbol', '?')} @ {o.get('price', '?')} "
              f"(id {o.get('clientOrderId', '?')}) or let it fill and run reconcile again" for o in resting]
    if flat and state_error:
        verdict = (f"FLAT but the risk state file {state_file} is unreadable ({state_error}): halt state unknown. "
                   f"If arbbot wrote it, run reconcile as that user; otherwise inspect or move the file by hand, then run again")
    elif flat and halted:
        verdict = "FLAT but halted: run with --clear-halt to lift the halt once you have read the reason below"
    elif flat:
        verdict = "FLAT: nothing to reconcile"
    else:
        verdict = "NOT FLAT: " + "; ".join(parts) + ". Do it on the Binance spot page, then run reconcile again"
        if state_error:
            verdict += f" (and the risk state file is unreadable: {state_error})"
    if orders_unknown:
        verdict += " [open orders could not be listed, so flatness is unproven]"
    if kill_switch_present:
        verdict += f" [kill switch file {kill_switch_file} is present: no leg will be sent until it is removed]"
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "balances": rows, "total_usdt": total, "usdt_free": usdt_free, "usdt_locked": usdt_locked,
        "capital_usd": capital_usd, "kill_floor_usd": floor, "fee_float_usd": fee_float_usd, "stake_status": stake_status,
        "open_orders": open_orders, "resting_orders": resting, "orders_unknown": orders_unknown,
        "state_file": str(state_file) if state_file else None, "state": state, "state_error": state_error,
        "kill_switch_file": str(kill_switch_file) if kill_switch_file else None, "kill_switch_present": kill_switch_present,
        "journal": [describe_entry(e) for e in journal_entries],
        "inventory": inventory, "unpriced": unpriced, "flat": flat, "halted": halted, "verdict": verdict,
    }


def clear_halt(state_file: Path) -> dict[str, Any]:
    """Lift the halt in the persisted risk state, keeping the day and its realized loss.
    Returns the state as it was. Written atomically like the risk manager does, and with
    the file's owner and mode preserved so the service can still read it when the
    operator runs this as root."""
    previous = read_state(state_file) or {}
    if "error" in previous:
        raise ValueError(f"cannot read {state_file}: {previous['error']}")
    updated = dict(previous)
    updated.update({"halted": False, "halt_sticky": False, "halt_reason": ""})
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = state_file.stat()
    except FileNotFoundError:
        st = None
    fd, tmp = tempfile.mkstemp(prefix=".risk_state", dir=str(state_file.parent))
    try:
        if st is not None:
            try:
                os.fchown(fd, st.st_uid, st.st_gid)  # a no-op for the owner; root gives the file back to the service user
            except PermissionError:
                pass
            os.fchmod(fd, stat_module.S_IMODE(st.st_mode))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(updated, fh)
        os.replace(tmp, state_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return previous


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"account as of {rep['as_of']}, marked in USDT at current prices:"]
    for r in rep["balances"]:
        mark = f"@ {r['mark']:.6g}" if r["mark"] is not None else "@ ?"
        usd = f"{r['usd']:10.2f}" if r["usd"] is not None else "         ?"
        note = f"  ({r['note']})" if r.get("note") else ""
        lines.append(f"  {r['asset']:8} free {r['free']:<14.8g} locked {r['locked']:<10.8g} {mark:>16} {usd}   {r['role']}{note}")
    lines.append(f"  total {rep['total_usdt']:.2f} USDT | stake {rep['capital_usd']:.2f} | kill floor {rep['kill_floor_usd']:.2f} | "
                 f"fee float {rep['fee_float_usd']:.2f} | free USDT {rep['usdt_free']:.2f}: {rep['stake_status']}")
    oo = rep["open_orders"]
    if not oo:
        lines.append("open orders: none")
    else:
        lines.append(f"open orders: {len(oo)}")
        for o in oo[:10]:
            if "error" in o:
                lines.append(f"  could not list open orders: {o['error']}")
            else:
                lines.append(f"  {o.get('symbol', '?')} {o.get('side', '?')} {o.get('origQty', '?')} @ {o.get('price', '?')} "
                             f"{o.get('status', '?')} id {o.get('clientOrderId', '?')}")
    st = rep["state"]
    if st is None:
        lines.append(f"risk state: no file at {rep['state_file']} (never traded, or a different state_file, or run from the "
                     f"wrong directory: relative paths resolve against the current directory)")
    elif rep.get("state_error"):
        lines.append(f"risk state ({rep['state_file']}): UNREADABLE ({rep['state_error']}); halt state unknown")
    else:
        halted = "HALTED" + (" (sticky)" if st.get("halt_sticky") else "") + f": {st.get('halt_reason', '')}" if st.get("halted") else "not halted"
        lines.append(f"risk state ({rep['state_file']}): day {st.get('day')}, realized today {float(st.get('daily_realized_usd', 0) or 0):+.4f} USD, {halted}")
    if rep.get("kill_switch_present"):
        lines.append(f"kill switch: {rep['kill_switch_file']} is PRESENT (no leg will be sent; preflight refuses to arm)")
    if rep["journal"]:
        lines.append(f"last {len(rep['journal'])} journal entries:")
        lines.extend("  " + j for j in rep["journal"])
    else:
        lines.append("journal: no entries")
    lines.append(f"verdict: {rep['verdict']}")
    return "\n".join(lines)
