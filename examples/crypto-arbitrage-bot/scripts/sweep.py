#!/usr/bin/env python3
"""Fee and assumption sensitivity sweep over a recorded tape.

Runs the replay pipeline for a grid of taker-fee levels, minimum edges,
stablecoin haircuts and slippage, and prints how many observations were
gross-positive, how many survived, how many paper orders were sent and
filled, and the realized PnL. The point: watch where the "profit" appears.
It appears exactly when the fees stop being the ones a retail account pays.

    python3 scripts/sweep.py                       # default grid on the bundled fixture
    python3 scripts/sweep.py my_tape.jsonl --fee-scale 1 0.5 0 --min-edge 1 0
    python3 scripts/sweep.py tape.jsonl --config measure7d.toml --fee-scale 1.25 --slippage 5   # the README's G10 check

Without --config the sweep uses the built-in default fees and generous paper settings (no
cooldown, no minimum profit, deep balances) so the fee effect is isolated. With --config it
replays under that run's own settings (fees read from the accounts, fill fraction, RTT,
cooldown, minimum profit, balances) and stresses only what the flags say: --fee-scale
multiplies the config's fee table and --slippage / --min-edge / --haircut / --fill-model
override the file only when given.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arbbot.cli import make_engine  # noqa: E402
from arbbot.config import load_config  # noqa: E402
from arbbot.engine import Clock  # noqa: E402
from arbbot.feeds import ReplayFeed  # noqa: E402
from arbbot.feeds.replay import read_tape_header  # noqa: E402
from arbbot.fees import DEFAULT_TAKER_BPS  # noqa: E402
from arbbot.universe import static_universe  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "feed_fixture.jsonl"


def run_once(fixture: Path, fee_scale: float, min_edge: float | None, haircut: float | None, slippage: float | None,
             fill_model: str | None, config: Path | None = None) -> dict:
    overrides: dict = {"report": {"write_jsonl": False, "interval_s": 3600}, "detection": {}, "paper": {}}
    if config is None:
        # isolate the fee effect: no cooldown, no dust floor, balances that never run out
        overrides["risk"] = {"cooldown_s": 0.0, "max_trades_per_minute": 100000, "min_profit_usd": 0.0}
        overrides["paper"].update({"starting_quote_per_venue_usd": 1e6, "starting_base_inventory_usd": 1e5})
    if min_edge is not None:
        overrides["detection"]["min_net_edge_bps"] = min_edge
    if haircut is not None:
        overrides["detection"]["stable_haircut_bps"] = haircut
    if slippage is not None:
        overrides["paper"]["slippage_bps"] = slippage
    if fill_model is not None:
        overrides["paper"]["fill_model"] = fill_model
    cfg = load_config(config, overrides)
    # the fee table in a config only holds overrides that FeeSchedule merges over the defaults,
    # so scale the merged table or a venue the file omits would stay at its unscaled default
    base_fees = {**DEFAULT_TAKER_BPS, **cfg.venues.taker_fee_bps}
    cfg.venues.taker_fee_bps = {k: v * fee_scale for k, v in base_fees.items()}
    fill_model = cfg.paper.fill_model
    markets = read_tape_header(fixture)
    markets = static_universe(cfg) if markets is None else [m for m in markets if m.venue in cfg.enabled_venues()]
    clock = Clock()
    feed = ReplayFeed(fixture, markets, speed=0.0, clock_setter=clock.set)
    first = feed.first_row_ts()
    if first is not None:
        clock.set(first)
    cfg.risk.kill_switch_file = ""  # offline: a stray STOP file must not zero the sweep
    engine = make_engine(cfg, markets, [feed], clock, state_file=None)
    stats = asyncio.run(engine.run())
    ex = engine.executor
    return {
        "fee_scale": fee_scale, "min_edge_bps": cfg.detection.min_net_edge_bps, "haircut_bps": cfg.detection.stable_haircut_bps,
        "slippage_bps": cfg.paper.slippage_bps, "taker_fee_bps": dict(cfg.venues.taker_fee_bps),
        "gross_positive": sum(v for k, v in stats.gross_by_kind.items() if k != "anomaly"),
        "net_positive": sum(stats.actionable_by_kind.values()),
        # orders that left the paper desk: one per accepted opportunity
        "sent": stats.pending if fill_model == "arrival" else sum(
            v for k, v in stats.trades_by_status.items() if k in ("filled", "partial", "rejected")),
        "filled": stats.trades_by_status.get("filled", 0) + stats.trades_by_status.get("partial", 0),
        "missed_legs": getattr(ex, "missed_legs", 0),
        "unknown_symbol_rows": feed.unknown_symbols,
        "realized_usd": round(getattr(ex, "realized_pnl_usd", 0.0), 4),
        "best_net_bps": round(max((v[0] for k, v in stats.best_net.items() if k != "anomaly"), default=float("nan")), 2),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE))
    p.add_argument("--config", type=Path, default=None,
                   help="replay under this run config (fees, fill fraction, RTT, cooldown, balances) instead of the defaults")
    p.add_argument("--fee-scale", type=float, nargs="+", default=[1.0, 0.5, 0.25, 0.0],
                   help="multiply the taker fees (1 = what the account pays; the config's table when --config is given)")
    p.add_argument("--min-edge", type=float, nargs="+", default=None, help="default 1.0, or the config's value")
    p.add_argument("--haircut", type=float, nargs="+", default=None, help="default 5 0, or the config's value")
    p.add_argument("--slippage", type=float, nargs="+", default=None, help="default 2 0, or the config's value")
    p.add_argument("--fill-model", choices=["arrival", "instant"], default=None, help="default arrival, or the config's value")
    p.add_argument("--json", action="store_true", help="print rows as JSON lines instead of a table")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    if args.config is None:  # the historical default grid
        min_edges = args.min_edge or [1.0]
        haircuts = args.haircut or [5.0, 0.0]
        slippages = args.slippage or [2.0, 0.0]
        fill_model = args.fill_model or "arrival"
    else:  # None = keep the config file's value
        min_edges, haircuts, slippages = args.min_edge or [None], args.haircut or [None], args.slippage or [None]
        fill_model = args.fill_model
    rows = []
    for fee_scale, min_edge, haircut, slippage in itertools.product(args.fee_scale, min_edges, haircuts, slippages):
        rows.append(run_once(Path(args.fixture), fee_scale, min_edge, haircut, slippage, fill_model, args.config))
    if args.json:
        for r in rows:
            print(json.dumps(r))
        return 0
    cols = ["fee_scale", "min_edge_bps", "haircut_bps", "slippage_bps", "gross_positive", "net_positive", "sent",
            "filled", "missed_legs", "realized_usd", "best_net_bps"]
    if any(r["unknown_symbol_rows"] for r in rows):
        print(f"warning: {rows[0]['unknown_symbol_rows']} rows are for markets outside the replay universe", file=sys.stderr)
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.rjust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).rjust(widths[c]) for c in cols))
    base = {k: v / rows[0]["fee_scale"] for k, v in rows[0]["taker_fee_bps"].items()} if rows and rows[0]["fee_scale"] else \
        {**DEFAULT_TAKER_BPS, **(load_config(args.config).venues.taker_fee_bps if args.config else {})}
    print(f"\nfee_scale 1.0 = {'the config' if args.config else 'default'} taker fees "
          f"({', '.join(f'{k} {v:g} bps' for k, v in base.items())}); fill model: {fill_model or 'from config'}; tape: {args.fixture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
