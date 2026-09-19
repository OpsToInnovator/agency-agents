#!/usr/bin/env python3
"""Fee and assumption sensitivity sweep over a recorded tape.

Runs the replay pipeline for a grid of taker-fee levels, minimum edges,
stablecoin haircuts and slippage, and prints how many observations were
gross-positive, how many survived, how many paper orders were sent and
filled, and the realized PnL. The point: watch where the "profit" appears.
It appears exactly when the fees stop being the ones a retail account pays.

    python3 scripts/sweep.py                       # default grid on the bundled fixture
    python3 scripts/sweep.py my_tape.jsonl --fee-scale 1 0.5 0 --min-edge 1 0
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
from arbbot.fees import DEFAULT_TAKER_BPS  # noqa: E402
from arbbot.universe import static_universe  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "feed_fixture.jsonl"


def run_once(fixture: Path, fee_scale: float, min_edge: float, haircut: float, slippage: float, fill_model: str) -> dict:
    cfg = load_config(None, {
        "venues": {"taker_fee_bps": {k: v * fee_scale for k, v in DEFAULT_TAKER_BPS.items()}},
        "detection": {"min_net_edge_bps": min_edge, "stable_haircut_bps": haircut},
        "risk": {"cooldown_s": 0.0, "max_trades_per_minute": 100000, "min_profit_usd": 0.0},
        "paper": {"slippage_bps": slippage, "fill_model": fill_model},
        "report": {"write_jsonl": False, "interval_s": 3600},
    })
    markets = static_universe(cfg)
    clock = Clock()
    feed = ReplayFeed(fixture, markets, speed=0.0, clock_setter=clock.set)
    for row in feed.rows():
        clock.set(float(row["t"]))
        break
    engine = make_engine(cfg, markets, [feed], clock, state_file=None)
    stats = asyncio.run(engine.run())
    ex = engine.executor
    return {
        "fee_scale": fee_scale, "min_edge_bps": min_edge, "haircut_bps": haircut, "slippage_bps": slippage,
        "gross_positive": sum(v for k, v in stats.gross_by_kind.items() if k != "anomaly"),
        "net_positive": sum(stats.actionable_by_kind.values()),
        "sent": stats.pending + sum(v for k, v in stats.trades_by_status.items() if k in ("filled", "partial", "rejected")),
        "filled": stats.trades_by_status.get("filled", 0) + stats.trades_by_status.get("partial", 0),
        "missed_legs": getattr(ex, "missed_legs", 0),
        "realized_usd": round(getattr(ex, "realized_pnl_usd", 0.0), 4),
        "best_net_bps": round(max((v[0] for k, v in stats.best_net.items() if k != "anomaly"), default=float("nan")), 2),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE))
    p.add_argument("--fee-scale", type=float, nargs="+", default=[1.0, 0.5, 0.25, 0.0],
                   help="multiply the default taker fees (1 = what a retail account pays)")
    p.add_argument("--min-edge", type=float, nargs="+", default=[1.0])
    p.add_argument("--haircut", type=float, nargs="+", default=[5.0, 0.0])
    p.add_argument("--slippage", type=float, nargs="+", default=[2.0, 0.0])
    p.add_argument("--fill-model", choices=["arrival", "instant"], default="arrival")
    p.add_argument("--json", action="store_true", help="print rows as JSON lines instead of a table")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    rows = []
    for fee_scale, min_edge, haircut, slippage in itertools.product(args.fee_scale, args.min_edge, args.haircut, args.slippage):
        rows.append(run_once(Path(args.fixture), fee_scale, min_edge, haircut, slippage, args.fill_model))
    if args.json:
        for r in rows:
            print(json.dumps(r))
        return 0
    cols = ["fee_scale", "min_edge_bps", "haircut_bps", "slippage_bps", "gross_positive", "net_positive", "sent",
            "filled", "missed_legs", "realized_usd", "best_net_bps"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.rjust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).rjust(widths[c]) for c in cols))
    print(f"\nfee_scale 1.0 = default taker fees ({', '.join(f'{k} {v:g} bps' for k, v in DEFAULT_TAKER_BPS.items())}); "
          f"fill model: {args.fill_model}; tape: {args.fixture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
