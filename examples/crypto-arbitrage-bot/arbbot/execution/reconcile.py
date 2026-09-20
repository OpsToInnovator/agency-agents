"""Read-only reconciliation of a live Binance account against the bot's own records.

The live executor never unwinds a cycle that fails halfway: it books what it knows,
halts sticky and leaves the human to look. This is what the human looks with. It
prints every balance marked in USDT at the current price, whether the stake declared
in [live] is still on the exchange, any open orders, the persisted risk state and the
last order intents from the journal, then says in plain words whether the account is
flat (everything back in USDT, apart from the BNB fee float and dust) and what to
sell if it is not. Nothing here sends an order. The only write it can do, on request,
is to lift the halt flag in the risk state file once the account is flat.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import USD_FAMILY

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
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def tail_journal(path: Path | None, last_n: int) -> list[dict[str, Any]]:
    """The last `last_n` well-formed entries of the intent journal, oldest first."""
    if path is None or not path.exists() or last_n <= 0:
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


async def reconcile_report(rest: Any, capital_usd: float, max_cumulative_loss_pct: float, state_file: Path | None,
                           intent_log: Path | None, last_n: int = 10, dust_usd: float = DUST_USD,
                           fee_asset: str = FEE_ASSET) -> dict[str, Any]:
    """Build the report. `rest` is a BinanceRest whose clock is already synced."""
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
    except Exception as exc:  # the report must still print; open orders are advisory
        open_orders = [{"error": f"{type(exc).__name__}: {exc}"}]

    rows: list[dict[str, Any]] = []
    total = 0.0
    usdt_free = 0.0
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
            usdt_free = free
        if usd is not None:
            total += usd
        if asset in USD_FAMILY:
            role = "cash"
        elif asset == fee_asset:
            role = "fee float"
        elif usd is None:
            role = "UNPRICED"
            unpriced.append(asset)
        elif usd < dust_usd:
            role = "dust"
        else:
            role = "INVENTORY"
        row = {"asset": asset, "free": free, "locked": locked, "mark": mark, "usd": usd, "role": role}
        rows.append(row)
        if role == "INVENTORY":
            inventory.append(row)
    rows.sort(key=lambda r: -(r["usd"] or 0.0))
    floor = capital_usd * (1.0 - max_cumulative_loss_pct / 100.0) if capital_usd else 0.0
    state = read_state(state_file)
    halted = bool(state and state.get("halted"))
    flat = not inventory and not unpriced
    if flat and not halted:
        verdict = "FLAT: nothing to reconcile"
    elif flat:
        verdict = "FLAT but halted: run with --clear-halt to lift the halt once you have read the reason below"
    else:
        parts = [f"sell {r['free']:g} {r['asset']} (about {r['usd']:.2f} USDT) back to USDT" for r in inventory]
        parts += [f"price and settle {a} by hand (no USDT or BTC market found for it)" for a in unpriced]
        verdict = "NOT FLAT: " + "; ".join(parts) + ". Do it on the Binance spot page, then run reconcile again"
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "balances": rows, "total_usdt": total, "usdt_free": usdt_free,
        "capital_usd": capital_usd, "kill_floor_usd": floor,
        "stake_status": ("no stake declared" if not capital_usd else
                         "free USDT at or above the stake" if usdt_free >= capital_usd - 0.01 else
                         "free USDT inside the kill budget" if usdt_free >= floor - 0.01 else
                         "free USDT BELOW THE KILL FLOOR: preflight will refuse to re-arm; do not restart on these settings"),
        "open_orders": open_orders,
        "state_file": str(state_file) if state_file else None, "state": state,
        "journal": [describe_entry(e) for e in tail_journal(intent_log, last_n)],
        "inventory": inventory, "unpriced": unpriced, "flat": flat, "halted": halted, "verdict": verdict,
    }


def clear_halt(state_file: Path) -> dict[str, Any]:
    """Lift the halt in the persisted risk state, keeping the day and its realized loss.
    Returns the state as it was. Written atomically, like the risk manager does."""
    previous = read_state(state_file) or {}
    if "error" in previous:
        raise ValueError(f"cannot read {state_file}: {previous['error']}")
    updated = dict(previous)
    updated.update({"halted": False, "halt_sticky": False, "halt_reason": ""})
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".risk_state", dir=str(state_file.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(updated, fh)
    os.replace(tmp, state_file)
    return previous


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"account as of {rep['as_of']}, marked in USDT at current prices:"]
    for r in rep["balances"]:
        mark = f"@ {r['mark']:.6g}" if r["mark"] is not None else "@ ?"
        usd = f"{r['usd']:10.2f}" if r["usd"] is not None else "         ?"
        lines.append(f"  {r['asset']:8} free {r['free']:<14.8g} locked {r['locked']:<10.8g} {mark:>16} {usd}   {r['role']}")
    lines.append(f"  total {rep['total_usdt']:.2f} USDT | stake {rep['capital_usd']:.2f} | kill floor {rep['kill_floor_usd']:.2f} | "
                 f"free USDT {rep['usdt_free']:.2f}: {rep['stake_status']}")
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
        lines.append(f"risk state: no file at {rep['state_file']} (never traded, or a different state_file)")
    elif "error" in st:
        lines.append(f"risk state: unreadable ({st['error']})")
    else:
        halted = "HALTED" + (" (sticky)" if st.get("halt_sticky") else "") + f": {st.get('halt_reason', '')}" if st.get("halted") else "not halted"
        lines.append(f"risk state ({rep['state_file']}): day {st.get('day')}, realized today {float(st.get('daily_realized_usd', 0) or 0):+.4f} USD, {halted}")
    if rep["journal"]:
        lines.append(f"last {len(rep['journal'])} journal entries:")
        lines.extend("  " + j for j in rep["journal"])
    else:
        lines.append("journal: no entries")
    lines.append(f"verdict: {rep['verdict']}")
    return "\n".join(lines)
