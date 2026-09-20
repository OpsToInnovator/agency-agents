#!/usr/bin/env python3
"""Is an apparent edge real, or an artifact of comparing prices from different moments?

Robot James ("pairs trading for dickheads") names three causes of an apparent divergence:
temporary forced flow, which may converge; genuine informed repricing, which may not; and a
bad comparison model, where the divergence was never there. Only the first is tradable, and
the third is the one a fast scanner manufactures for itself.

A triangle is priced from three quotes. If one of them is a second old, the "edge" may be
nothing but the market having moved between them. This replays a recorded tape and, for
every triangular opportunity, records how old the oldest leg was at detection. It then
answers two questions:

    1. Does apparent edge grow with staleness? (a comparison artifact)
    2. What survives when every leg must be fresh? (the tradable remainder)

    python3 scripts/edge_decay.py tape.jsonl
    python3 scripts/edge_decay.py tape.jsonl --fresh-ms 50 --fee-bps 10 --json

The fee floor it compares against is three taker legs, so the default 10 bps fee means a
30 bps hurdle. Nothing here sends an order or needs a key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbbot.config import DetectionConfig
from arbbot.detectors import TriangularDetector
from arbbot.fees import FeeSchedule
from arbbot.feeds.replay import ReplayFeed
from arbbot.models import BINANCE
from arbbot.quotes import QuoteBook
from arbbot.universe import _binance_markets

BANDS = [(-1e9, 0.0, "negative"), (0.0, 5.0, "0 to 5 bps"), (5.0, 10.0, "5 to 10 bps"),
         (10.0, 20.0, "10 to 20 bps"), (20.0, 50.0, "20 to 50 bps"), (50.0, 1e9, "over 50 bps")]


def symbols_in(path: Path) -> list[str]:
    """The Binance symbols this tape actually carries, so the universe matches the data."""
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                raw = json.loads(line).get("raw", "")
            except Exception:
                continue
            if isinstance(raw, str):
                for part in raw.split('"s":"')[1:]:
                    seen.add(part.split('"')[0])
    return sorted(seen)


async def collect(tape: Path, fee_bps: float) -> list[tuple[float, float, float]]:
    """(gross_bps, net_bps, oldest_leg_ms) for every triangular opportunity on the tape."""
    markets = _binance_markets(symbols_in(tape))
    book = QuoteBook(max_age_s=2.0)
    for m in markets:
        book.register(m)
    det = TriangularDetector(markets, FeeSchedule({BINANCE: fee_bps}), DetectionConfig(), 100.0)
    rows: list[tuple[float, float, float]] = []

    def sink(q) -> None:
        if book.update(q) == 0:
            return
        for opp in det.on_quote(q, book, q.recv_ts):
            if opp.kind == "triangular":
                rows.append((opp.gross_edge_bps, opp.net_edge_bps, max(opp.quote_ages_ms or [0.0])))

    await ReplayFeed(tape, markets, speed=0.0).run(sink)
    return rows


def summarise(rows: list[tuple[float, float, float]], fresh_ms: float, fee_bps: float) -> dict:
    if not rows:
        return {"scored": 0}
    stale_ms = fresh_ms * 4  # "clearly stale" for the share column
    bands = []
    for lo, hi, name in BANDS:
        sel = [r for r in rows if lo <= r[0] < hi]
        if sel:
            bands.append({"band": name, "count": len(sel),
                          "median_oldest_ms": round(st.median([r[2] for r in sel]), 1),
                          "worst_ms": round(max(r[2] for r in sel)),
                          "share_stale": round(sum(1 for r in sel if r[2] > stale_ms) / len(sel), 4)})
    top = sorted(rows, key=lambda r: -r[0])[:200]
    rest = sorted(rows, key=lambda r: -r[0])[200:]
    fresh = [r for r in rows if r[2] <= fresh_ms]
    return {
        "scored": len(rows), "fee_bps": fee_bps, "hurdle_bps": 3 * fee_bps, "fresh_ms": fresh_ms,
        "bands": bands,
        "top200_median_oldest_ms": round(st.median([r[2] for r in top]), 1),
        "rest_median_oldest_ms": round(st.median([r[2] for r in rest]), 1) if rest else None,
        "net_positive": sum(1 for r in rows if r[1] > 0),
        "fresh_count": len(fresh),
        "fresh_best_gross_bps": round(max(r[0] for r in fresh), 2) if fresh else None,
        "fresh_best_net_bps": round(max(r[1] for r in fresh), 2) if fresh else None,
    }


def render(s: dict) -> str:
    if not s.get("scored"):
        return "no triangular opportunities scored: is this a Binance tape?"
    out = [f"scored {s['scored']} triangular opportunities at {s['fee_bps']:g} bps taker "
           f"(a {s['hurdle_bps']:g} bps hurdle for three legs)", "",
           "gross edge band      count   median oldest leg    worst   share clearly stale"]
    for b in s["bands"]:
        out.append(f"{b['band']:18}  {b['count']:7}   {b['median_oldest_ms']:>10.1f} ms  {b['worst_ms']:>7} ms"
                   f"   {b['share_stale'] * 100:>5.1f}%")
    out += ["", f"top 200 by gross edge: median oldest leg {s['top200_median_oldest_ms']:.1f} ms"]
    if s["rest_median_oldest_ms"] is not None:
        out.append(f"everything else:       median oldest leg {s['rest_median_oldest_ms']:.1f} ms")
        worse = s["top200_median_oldest_ms"] > s["rest_median_oldest_ms"]
        out.append("the biggest edges are drawn from staler quotes: some of that edge is a comparison artifact"
                   if worse else "the biggest edges are not staler than the rest: staleness is not what is creating them")
    out += ["", f"net-positive after fees: {s['net_positive']} of {s['scored']}"]
    if s["fresh_count"]:
        out.append(f"with every leg under {s['fresh_ms']:g} ms ({s['fresh_count']} of {s['scored']}): "
                   f"best gross {s['fresh_best_gross_bps']:+.2f} bps, best net {s['fresh_best_net_bps']:+.2f} bps")
    else:
        out.append(f"not one opportunity had every leg under {s['fresh_ms']:g} ms: on this tape every apparent "
                   f"edge was priced across different moments")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tape", type=Path, help="a JSONL tape recorded with `scan --record`")
    p.add_argument("--fresh-ms", type=float, default=50.0, help="every leg must be this fresh to count (default 50)")
    p.add_argument("--fee-bps", type=float, default=10.0, help="taker fee per leg (default 10)")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = p.parse_args()
    if not args.tape.exists():
        print(f"no such tape: {args.tape}", file=sys.stderr)
        return 2
    rows = asyncio.run(collect(args.tape, args.fee_bps))
    summary = summarise(rows, args.fresh_ms, args.fee_bps)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
