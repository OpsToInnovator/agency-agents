"""Does this strategy read the future? Not a warning -- a demonstration.

Every other check in this battery reports a number the customer can argue with. This one
reports an event: at 14:00 your signal said LONG, and when we changed data your strategy
could not have seen, it said FLAT. There is no modelling assumption in that, and no
threshold to dispute. Either the output moved or it did not.

Two probes, because neither alone is enough, and the gap between them is the whole reason
this needs the code rather than a trade list:

    TRUNCATION   cut the tape at bar k and re-run. Anything that reads past the end of the
                 data moves. Catches full-sample statistics, centred windows, back-fills --
                 leaks whose horizon runs off into the future.

    PERTURBATION nudge the fields that had not happened yet at the moment the position was
                 chosen, and re-run. At bar i's open you know bars[0..i-1] entirely, plus
                 bar i's timestamp and open. You do not know bar i's own high, low, close
                 or volume. Truncation is structurally blind to that -- cut at bar k and
                 bar k's close is still sitting in the data -- and deciding at the open
                 using the close is the most common real lookahead there is.

The perturbation is drawn from the tape's own scale and re-threaded into a plausible walk,
for one reason: a strategy must not be able to tell probe data from real data. A red team's
first evasion keyed on the seam an early version left -- opens redrawn independently, so
open no longer equalled the previous close -- and its second on a fixed one-percent nudge
that pushed moves outside anything the tape ever showed. Both strategies leaked on real data
and behaved when they smelled a probe, and both got a clean report.

What actually convicts them is not the absence of tells. It is that every perturbed run is
compared against the PRISTINE run: for i <= k a causal strategy must reproduce the pristine
output exactly, because nothing it may legitimately read has changed. A strategy that leaks
on real data used the boundary bar's close there, and no causal fallback can reproduce that
value on a tape where the close has moved. Measured after this landed, detection no longer
depends on the size of the nudge at all -- every leak, at every sigma from 0.002 to 1.5 --
which overturned a table an earlier version published about a tradeoff between them. The
tradeoff was an artifact of comparing perturbed runs only to each other.

What this proves, and what it does not. A divergence is proof of a causal dependency: the
output is a function of something in the future, and the evidence is the pair of runs. The
absence of a divergence is not proof of innocence -- a leak can hide in a branch this tape
never takes. That asymmetry is structural here: a ``Proven`` cannot be constructed without
the two conflicting outputs that demonstrate it, and nothing in this module ever returns a
clean bill of health, only "nothing moved on this data".
"""
from __future__ import annotations

import bisect
import dataclasses
import math
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple, Protocol, Sequence

__all__ = ["Bar", "Strategy", "Divergence", "Proven", "Suspected", "Report",
           "check_causality", "default_boundaries", "sparse_boundaries", "continuation", "sign_design",
           "draw_plans", "Plan", "beyond_reach",
           "realized_sigma", "DEFAULT_DRAWS", "SIGMA_FLOOR", "MIN_BOUNDARY"]

DEFAULT_DRAWS = 2
SIGMA_FLOOR = 0.002
JITTER = 0.1
EDGE = 1e-9   # a level exactly as far as the tape's largest size is out of reach, not within it
MIN_BOUNDARY = 4


class Bar(Protocol):
    """The minimum a bar must expose. A frozen dataclass satisfies this."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


Strategy = Callable[[Sequence[Any]], Sequence[int]]


@dataclass(frozen=True, slots=True)
class Divergence:
    """The two runs that disagree, and where. This is the evidence, not a summary of it."""

    index: int
    boundary: int
    baseline: int
    variant: int
    probe: str
    detail: str

    @property
    def horizon(self) -> int | None:
        """How many bars past the decision point the output reached, as a LOWER BOUND.

        The probe shows that signals[index] depends on some bar at or after ``boundary``,
        so the reach is at least ``boundary - index``. It may be further: the strategy
        might read bar boundary + 40 as well, and one divergence cannot tell them apart.
        Report it as "at least", never as the exact depth.

        Zero means the output at a bar moved when that bar and everything after it varied:
        the strategy read something of its own bar it could not have known at the open, or
        something later still. A seventh red team's next-bar-timestamp reader was headlined
        as reading its own close; the evidence never said so.
        Truncation carries no reach at all: it also shortens the tape, and a dependence on
        how much data there is diverges under it without any future value being read.
        """
        if self.probe != "perturbation":
            return None
        return max(0, self.boundary - self.index)


@dataclass(frozen=True, slots=True)
class Proven:
    """A demonstrated causal dependency on data the strategy could not have had.

    Cannot exist without a Divergence. That is the point: the type system will not let a
    suspicion be filed as a proof, so a reader never has to trust that someone set a flag
    correctly.
    """

    evidence: Divergence
    summary: str

    @property
    def horizon(self) -> int | None:
        return self.evidence.horizon


@dataclass(frozen=True, slots=True)
class Suspected:
    """A pattern worth a human's attention. Carries no evidence and claims none."""

    summary: str
    reason: str


@dataclass(frozen=True, slots=True)
class Report:
    proven: tuple[Proven, ...] = ()
    suspected: tuple[Suspected, ...] = ()
    probes_run: int = 0
    bars_tested: int = 0
    nondeterministic: bool = False
    recognises_input: bool = False
    boundaries: tuple[int, ...] = ()
    seed: int | None = None

    @property
    def coverage(self) -> float:
        """Share of bars at which a same-bar read could have been caught: one per perturbed
        boundary, and none at all when no perturbation ran."""
        if self.draws <= 0 or not self.boundaries:
            return 0.0
        probeable = max(1, self.bars_tested - MIN_BOUNDARY)
        return min(1.0, len(self.boundaries) / probeable)

    @property
    def leaks(self) -> bool:
        return bool(self.proven)

    @property
    def worst_horizon(self) -> int | None:
        """How far into future VALUES the output demonstrably reached. Only the perturbation
        probe can bound that: truncation also shortens the tape, and a strategy that sizes
        something from len(bars) diverges under it without reading any future value. A
        third red team's count-dependent strategy was reported as reading 172 bars ahead;
        it read none. Truncation-only findings carry no reach."""
        return max((p.horizon for p in self.proven if p.horizon is not None), default=None)

    draws: int = DEFAULT_DRAWS
    beyond_reach: tuple[int, ...] = ()
    undelivered: tuple[int, ...] = ()
    repairs: int = 0
    truncations: int = 0
    floors: tuple[str, ...] = ()
    sigma: float | None = None          # the caller's sigma, where one was given

    def coverage_note(self) -> str:
        if self.draws <= 0 or not self.boundaries:
            cut = (f"truncation ran at {self.truncations} cut(s)" if self.truncations
                   else "truncation did not run either")
            return ("no perturbation ran: nothing a bar had not yet printed at its open was varied, so a "
                    f"read of a bar's own close, high, low or volume cannot show up here; {cut}")
        levels = "the previous bar's open, close, high and low"
        caveat = ("where the open, or a previous bar that did not trade, does not already decide it and a "
                  "size the tape has made can get there")
        if self.draws == 1:
            combos = (f"one draw at each bar, pushing the close past its open and past the farthest of "
                      f"{levels} it could reach, the high and the low past the previous bar's, the "
                      f"volume past the previous bar's, and the next bar's gap and timing, each one way "
                      f"chosen at random ({caveat}), plus a repair draw where that draw fell short of its "
                      f"own plan")
            residual = ("a read that only the other way would flip, a read of a magnitude rather than a "
                        "direction (how far the close is from the open, where it sits within its own range, how far the next bar gaps), "
                        "or of how two relations combine, can still go unseen")
        else:
            combos = (f"the close pushed both ways past its open; the close, the high and the low each pushed "
                      f"both ways past each of {levels}; the volume both ways past the previous bar's, and to "
                      f"zero and away from it where the tape prints zeros; the next bar's open both ways past "
                      f"this bar's close and open and each of the previous bar's levels, and the next bar early, "
                      f"on time and late where the tape prints each; each of these also set level with its "
                      f"reference where the tape prints that kind of tie -- a doji, a close, high or low on a "
                      f"previous level, a repeated volume, an ungapped open; prices on the tape's own tick and "
                      f"volumes on its own lot where it has them; and move, wick and volume each both ways")
            if self.draws >= 4:
                pairs = ("every sign combination of move, wick and volume" if self.draws >= 8 else
                         f"every pair of move, wick and volume pushed apart ({min(self.draws, 8)} of 8 sign "
                         f"combinations)")
                combos += (f"; on the further draws the close in a band between those levels chosen at random, "
                           f"the range once inside and once outside the previous bar's, and {pairs}")
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range, how far the next bar gaps), against a level further back than "
                            "the previous bar, or of how two of these relations combine at one bar -- where the "
                            "close sits between two of the previous bar's levels, a failed breakout, an inside "
                            "or outside range together with where the close sits"
                            + ("" if self.draws >= 8 else ", a pattern across move, wick and volume at once")
                            + " -- is tried only on the draws that happen to produce it, and can go unseen")
            else:
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range, how far the next bar gaps), against a level further back than "
                            "the previous bar, of where the close sits between two of the previous bar's levels, "
                            "of an inside or outside range, or of how two directions relate, can still go unseen")
            combos += (f" ({caveat}), each checked on the bar as built, with a repair draw wherever the "
                       f"planned draws fell short")
        stopped = ("; at a bar that diverged, probing stopped at the first divergence"
                   if any(p.evidence.probe == "perturbation" for p in self.proven) else "")
        largest = ((f"the move of the given sigma {self.sigma:g}" if self.sigma is not None
                    else "the floor-sized move the close was given") if "moves" in self.floors
                   else "the largest move the tape has made")
        far = (f"; at {len(self.beyond_reach)} of the probed bars one of {levels} lay at least as far from "
               f"the open as {largest}, and the close was not pushed past that level"
               if self.beyond_reach else "")
        if "moves" in self.floors:
            far += (f"; the tape has made no moves, so the close was pushed by "
                    + (f"the given sigma {self.sigma:g}" if self.sigma is not None else "a floor size")
                    + ", not by a move of its own")
        if "volume changes" in self.floors:
            far += "; the tape has made no volume changes, so the volume was pushed by a floor ratio"
        if self.undelivered:
            far += (f"; at {len(self.undelivered)} of the probed bars a push listed here could not be made "
                    f"with sizes the tape has made, even on a repair draw, and was not")
        if self.coverage >= 1.0:
            return (f"every bar from {MIN_BOUNDARY} on was probed (bars 0-{MIN_BOUNDARY - 1} never are, so a "
                    f"leak confined to them would not show up here), {combos}, at the bars that did not "
                    f"diverge{stopped}{far}; {residual}")
        return (f"probed at {len(self.boundaries)} of {self.bars_tested - MIN_BOUNDARY} possible boundaries "
                f"({self.coverage:.1%}), {combos}, at the bars that did not diverge{stopped}{far}; a leak "
                f"confined to bars that were not probed would not show up here, and {residual}")

    def describe(self) -> str:
        if self.nondeterministic:
            return ("NOTHING PROVED: the strategy gave different output on identical input, so no "
                    "divergence can be attributed to the data.\n" +
                    "\n".join(f"  {s.summary}\n    {s.reason}" for s in self.suspected))
        if self.recognises_input:
            return ("NOTHING PROVED: the strategy reproduced its output on the real tape three times and "
                    "failed to reproduce it on a varied or shortened copy of it (named below). Either it distinguishes real data from varied "
                    "data, or it is intermittently nondeterministic; neither can be audited by probing.\n" +
                    "\n".join(f"  {s.summary}\n    {s.reason}" for s in self.suspected))
        if not self.proven:
            base = (f"No causal dependency on future data was demonstrated over {self.bars_tested} bars "
                    f"and {self.probes_run} runs of the strategy; {self.coverage_note()}. This is not a clean bill of "
                    f"health: a leak on a branch this data never took would not show up here.")
            if self.suspected:
                base += "\n\nSUSPECTED, not proven:\n" + "\n".join(
                    f"  {s.summary}\n    {s.reason}" for s in self.suspected)
            return base
        h = self.worst_horizon
        trunc = [p for p in self.proven if p.evidence.probe == "truncation"]
        if h is None:
            first = min(p.evidence.boundary for p in self.proven)
            corroboration = ("No perturbation ran to corroborate it" if self.draws <= 0 or not self.boundaries
                             else "The perturbation probe did not corroborate it")
            head = (f"PROVEN: this strategy's output changed when the tape was shortened past bar {first - 1} -- "
                    f"a dependence on data after that bar, or on how much data there is. {corroboration}, "
                    f"so no reach into future values is claimed.")
        else:
            reach = ("something from its own bar on that had not happened at its open -- its own "
                     "close, high, low or volume, or a later bar; the evidence does not say which" if h == 0
                     else f"at least {h} bar(s) into the future")
            head = f"PROVEN: this strategy reads {reach}."
            if trunc:
                head += (" Its output also changed when the tape was shortened, which is a dependence on "
                         "later data or on how much data there is; no reach is claimed for that part.")
        lines = [head, f"  ({self.coverage_note()})", ""]
        for p in self.proven:
            e = p.evidence
            tail = f"horizon {e.horizon}" if e.horizon is not None else "reach not bounded"
            lines.append(f"  signals[{e.index}] = {e.baseline} normally, {e.variant} once bar {e.boundary} "
                         f"onward was {e.detail} ({e.probe} probe, {tail})")
        return "\n".join(lines)


def _replace(bar: Any, **kw: float) -> Any:
    if dataclasses.is_dataclass(bar):
        return dataclasses.replace(bar, **kw)
    raise TypeError(f"cannot vary a {type(bar).__name__}; bars must be a dataclass")


def default_boundaries(n: int) -> list[int]:
    """Where truncation cuts: four fixed fractions of the tape. Fine for a probe that catches
    anything reaching past the cut; useless on its own for a same-bar read, which is only
    ever visible at the boundary bar itself."""
    return sorted({max(MIN_BOUNDARY, int(n * f)) for f in (0.15, 0.4, 0.65, 0.9) if max(MIN_BOUNDARY, int(n * f)) < n})


def sparse_boundaries(n: int, seed: int, count: int | None = None) -> list[int]:
    """Where perturbation cuts, by default: a random set drawn from a nonce the strategy
    never sees. A second red team leaked on every bar except the four fixed boundaries and
    walked; a fixed public schedule is a schedule a leak can be written around. With a
    random set of B boundaries, a same-bar leak confined to a single bar is caught with
    probability B / (n - 4) per audit, and the report states that number."""
    lo, hi = MIN_BOUNDARY, n - 1
    if hi < lo:
        return []
    count = count if count is not None else max(8, n // 8)
    pool = list(range(lo, hi + 1))
    if count >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, count))


def realized_sigma(tape: Sequence[Any]) -> float:
    """The tape's own per-bar |log(close/open)| scale. The nudge is drawn from this, not from
    a constant, so a perturbed bar sits inside the envelope a genuine bar sits in."""
    moves = [abs(math.log(b.close / b.open)) for b in tape if b.open > 0 and b.close > 0]
    if len(moves) < 2:
        return SIGMA_FLOOR
    mu = sum(moves) / len(moves)
    var = sum((m - mu) ** 2 for m in moves) / (len(moves) - 1)
    # The floor is for a tape with no moves at all. Flooring a quiet tape made the rebuilt walk
    # move seven times as far as the tape ever did, and every proof line still said "at the
    # tape's own scale" (an eleventh red team).
    scale = math.sqrt(mu * mu + var)
    return scale if scale > 0 else SIGMA_FLOOR


def _jitter(x: float, rng: random.Random) -> float:
    """A size of the tape's own, moved off its exact value. Copying a donor's wick, gap or
    volume ratio verbatim put a value on the rebuilt tape that already existed elsewhere on
    it -- a duplicate no real tape prints, and so a tell. Zero stays zero: a tape with no
    gaps must stay a tape with no gaps."""
    return x * math.exp(rng.gauss(0.0, JITTER)) if x else x


def _rethread(tape: Sequence[Any], boundary: int, rng: random.Random, move_of, volume_of,
              first: tuple[float, float, float, float] | None = None,
              after: tuple[float | None, float | None] = (None, None),
              grid: tuple[float | None, float | None] = (None, None)) -> list[Any]:
    """Rebuild the walk from ``boundary`` on, keeping every invariant the pristine tape has --
    IN DISTRIBUTION, never per bar.

    An earlier version copied each pristine bar's own wick ratios and opening gap onto the
    rebuilt bar, and left every timestamp alone, so that nothing about the tape's shape
    would give a probe away. That preserved those properties for each bar exactly, and a
    third red team read exactly those properties: a bar's own wick ratio, the next bar's
    opening gap, the next bar's timestamp. None of them ever moved under any probe, so a
    strategy deciding on them was clean on every run. Everything unknowable at bar i's open
    must vary at bar i. Everything unknowable about later bars must vary there too.

    So the rebuilt bar takes its wick sizes, its gap to the previous close and its time
    step from RANDOM DONOR bars of the pristine tape, the sizes jittered off their exact
    values. On a tape with no gaps and a regular clock that is exactly a no-op for those
    two, and the shape of the tape -- how often it gaps, how late its bars run, how long its
    wicks are -- is unchanged, so there is still no seam to find. Bar ``boundary`` keeps its
    own open and timestamp, which the strategy was entitled to see; its high, low, close and
    volume are not, and all four vary. ``first``, when given, is that bar's close, high, low
    and volume, built by ``_forced_bar``; ``after`` is the log gap and the time step the NEXT
    bar opens with, where a plan forces them. ``grid`` is the tape's price tick and volume lot:
    a tape printed on a grid is rebuilt on it, because a price off the grid is a price no bar
    of that tape could have printed (an eleventh red team's tell).
    """
    tick, lot = grid
    n = len(tape)
    donors = list(range(1, n)) or [0]
    out = list(tape)
    prev_close = tape[boundary - 1].close
    prev_ts = tape[boundary - 1].ts
    for i in range(boundary, n):
        b = tape[i]
        if i == boundary and first is not None:
            closed, high, low, volume = first
            out[i] = _replace(b, close=closed, high=high, low=low, volume=volume)
            prev_close, prev_ts = closed, b.ts
            continue
        if i == boundary:
            opened, ts = b.open, b.ts
        else:
            forced_gap, forced_step = after if i == boundary + 1 else (None, None)
            if forced_gap is not None:
                opened = prev_close * math.exp(forced_gap) if forced_gap else prev_close
            else:
                gap = _gap_of(tape, tape[rng.choice(donors)])
                opened = prev_close * math.exp(_jitter(math.log(gap), rng)) if gap > 0 else prev_close
            if opened != prev_close:
                opened = _snap(opened, tick, "round")
            ts = prev_ts + (forced_step if forced_step is not None
                            else _step_of(tape, tape[rng.choice(donors)]))
        closed = _snap(opened * move_of(i, b), tick, "round")
        hi, lo = max(opened, closed), min(opened, closed)
        d = tape[rng.choice(donors)]
        d_top, d_bot = max(d.open, d.close), min(d.open, d.close)
        up = _jitter(max(0.0, d.high / d_top - 1.0), rng) if d_top > 0 else 0.0
        dn = min(_jitter(max(0.0, 1.0 - d.low / d_bot), rng), 0.99) if d_bot > 0 else 0.0
        low = _snap(lo * (1.0 - dn), tick, "down")
        out[i] = _replace(b, ts=ts, open=opened, close=closed, volume=_snap(volume_of(i, b), lot, "round"),
                          high=max(_snap(hi * (1.0 + up), tick, "up"), hi), low=low if low > 0 else lo)
        prev_close, prev_ts = closed, ts
    return out


class Sizes(NamedTuple):
    """The sizes the tape itself has printed, each sorted: every bar's move as |log(close/open)|,
    every wick as a fraction of the body end it hangs from, every bar-to-bar volume ratio as
    |log|, every nonzero volume, every opening gap up and down as |log(open/previous close)|,
    every distinct time step -- whether it has printed a zero volume, an ungapped bar and a
    wick of exactly zero -- its price tick and volume lot, where it is printed on a grid -- the
    kinds of tie it prints (a doji; a close, high or low level with a previous-bar level; a
    volume repeated) -- and its commonest time step. The probed bar, and the bar after it,
    are built from these."""

    moves: list[float]
    wicks: list[float]
    ratios: list[float]
    volumes: list[float]
    zero: bool
    gaps_up: list[float]
    gaps_dn: list[float]
    gap0: bool
    steps: list[float]
    wick0: bool
    tick: float | None = None
    lot: float | None = None
    ties: frozenset = frozenset()
    step_mode: float | None = None


def _grid(values: Sequence[float]) -> float | None:
    """The grid a tape's values are printed on -- the largest step every value is a whole
    multiple of, read from their shortest decimal form -- or None for values off any grid of
    eight decimal places or fewer. An exchange prints prices on a tick and volumes on a lot."""
    from decimal import Decimal
    vals = sorted({v for v in values if v > 0 and math.isfinite(v)})
    if len(vals) < 3:
        return None
    decs = [Decimal(repr(v)) for v in vals]
    places = max(-d.as_tuple().exponent for d in decs)
    if places > 8:
        return None
    scale = 10 ** max(places, 0)
    ints = [int(d * scale) for d in decs]
    g = 0
    for a, b in zip(ints, ints[1:]):
        g = math.gcd(g, b - a)
    return g / scale if g > 0 else None


def _snap(x: float, step: float | None, how: str) -> float:
    """``x`` on the grid ``step``: rounded, or up, or down, to a whole step; unchanged off a grid."""
    if not step or not math.isfinite(x):
        return x
    q = x / step
    n = math.ceil(q - 1e-9) if how == "up" else math.floor(q + 1e-9) if how == "down" else round(q)
    return round(n * step, 10)


def _sizes(tape: Sequence[Any]) -> Sizes:
    moves: list[float] = []
    wicks: list[float] = []
    ratios: list[float] = []
    volumes: list[float] = []
    gaps_up: list[float] = []
    gaps_dn: list[float] = []
    steps: dict[float, int] = {}
    ties: set[str] = set()
    zero = gap0 = wick0 = False
    before = None
    for b in tape:
        if b.open > 0 and b.close > 0:
            m = abs(math.log(b.close / b.open))
            if m > 0:
                moves.append(m)
            else:
                ties.add("doji")
            top, bot = max(b.open, b.close), min(b.open, b.close)
            up, dn = max(0.0, b.high / top - 1.0), min(max(0.0, 1.0 - b.low / bot), 0.99)
            wicks += [up, dn]
            wick0 = wick0 or up == 0.0 or dn == 0.0
        if b.volume > 0:
            volumes.append(b.volume)
        elif b.volume == 0:
            zero = True
        if before is not None:
            if before.volume > 0 and b.volume > 0:
                r = abs(math.log(b.volume / before.volume))
                if r > 0:
                    ratios.append(r)
                else:
                    ties.add("volume")
            if before.close > 0 and b.open > 0:
                g = math.log(b.open / before.close)
                if g > 0:
                    gaps_up.append(g)
                elif g < 0:
                    gaps_dn.append(-g)
                else:
                    gap0 = True
            levels = (before.open, before.close, before.high, before.low)
            for name, value in (("close", b.close), ("high", b.high), ("low", b.low)):
                if value in levels:
                    ties.add(name)
            step = b.ts - before.ts
            steps[step] = steps.get(step, 0) + 1
        before = b
    prices = [x for b in tape for x in (b.open, b.high, b.low, b.close)]
    mode = max(steps, key=lambda st: (steps[st], -st)) if steps else None
    return Sizes(sorted(moves), sorted(wicks), sorted(ratios), sorted(volumes), zero,
                 sorted(gaps_up), sorted(gaps_dn), gap0, sorted(steps), wick0,
                 _grid(prices), _grid([b.volume for b in tape]), frozenset(ties), mode)


def _volume_of(tape: Sequence[Any], rng: random.Random, sizes: Sizes):
    """How a rebuilt bar after the boundary gets its volume: its own, nudged -- and, on a tape
    that prints zero volumes, zero or not as a random donor bar was. Scaling each bar's own
    volume kept an untraded bar untraded under every probe, a per-bar invariant of exactly
    the kind a third red team read; an eighth read it."""
    donors = range(1, len(tape)) if len(tape) > 1 else range(1)

    def volume(i: int, b: Any) -> float:
        if sizes.zero and tape[rng.choice(donors)].volume == 0:
            return 0.0
        base = b.volume if b.volume > 0 else (rng.choice(sizes.volumes) if sizes.volumes else 0.0)
        return base * math.exp(rng.gauss(0.0, 0.25))
    return volume


def _draw_within(pool: Sequence[float], lo: float, hi: float, rng: random.Random) -> float | None:
    """One of the tape's own sizes (``pool``, sorted), jittered but never past the largest of
    them, STRICTLY inside (lo, hi). Where no size lands there but the interval starts short
    of the largest size the tape has made, a point inside it is drawn instead -- a size the
    tape could have made. None where the interval starts at or beyond the largest: nothing
    the tape has printed reaches it.

    Strictly, because the range pin a seventh red team read landed the low EXACTLY on the
    previous low, so ``low <= previous low`` held on the up draws as well as the down ones."""
    top = pool[-1] if pool else 0.0
    inside = pool[bisect.bisect_right(pool, lo):bisect.bisect_left(pool, hi)]
    if inside:
        x = rng.choice(inside)
        if x == 0.0:
            return 0.0
        for _ in range(16):
            y = x * math.exp(rng.gauss(0.0, JITTER))
            if lo < y < hi and y <= top:          # never past the largest the tape has made
                return y
    a, b = max(lo, 0.0), min(hi, top)
    if a < b:
        return a + (b - a) * rng.uniform(0.05, 0.95)
    return None


def _levels(prev: Any) -> list[float]:
    """Everything of the previous bar an unknown field of this one can be compared against."""
    return [x for x in (prev.open, prev.close, prev.high, prev.low) if x > 0]


def _bands(open_: float, levels: Sequence[float], side: int) -> list[tuple[float, float]]:
    """Where a close on ``side`` of the open can land, as intervals of log-distance from the
    open, nearest first: short of the nearest previous-bar level on that side, between that
    level and the next, ..., and past the farthest."""
    if side > 0:
        ds = sorted({math.log(x / open_) for x in levels if x > open_})
    else:
        ds = sorted({math.log(open_ / x) for x in levels if x < open_})
    edges = [0.0, *ds, math.inf]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def beyond_reach(tape: Sequence[Any], boundary: int, top_move: float | None = None,
                 exact: bool = False) -> bool:
    """True when one of the previous bar's levels lies further from bar ``boundary``'s open
    than any move the tape has made, so no probe pushes the close past it. A seventh red team
    showed what pushing it anyway costs: at a bar that gapped 3%, a close forced past the
    previous high was six times the largest move on the tape, and a strategy that fell back
    to a causal rule on any move that size walked. Such a level is left alone and counted."""
    if top_move is None:
        moves = _sizes(tape).moves
        top_move = moves[-1] if moves else 0.0
    o = tape[boundary].open
    if o <= 0:
        return False
    reach = top_move if exact else top_move * (1.0 - EDGE)
    return any(abs(math.log(x / o)) >= reach for x in _levels(tape[boundary - 1]) if x != o)


def _gap_of(tape: Sequence[Any], bar: Any) -> float:
    """A donor's opening gap: its open over the close before it. 1.0 where the tape is continuous."""
    i = _index_of(tape, bar)
    if i <= 0 or tape[i - 1].close <= 0:
        return 1.0
    return bar.open / tape[i - 1].close


def _step_of(tape: Sequence[Any], bar: Any) -> float:
    """A donor's time step from the bar before it; the tape's own spacing where regular."""
    i = _index_of(tape, bar)
    if i <= 0:
        return (tape[1].ts - tape[0].ts) if len(tape) > 1 else 0.0
    return bar.ts - tape[i - 1].ts


def _index_of(tape: Sequence[Any], bar: Any) -> int:
    # bars are frozen dataclasses; identity lookup avoids the equality scan
    for i, b in enumerate(tape):
        if b is bar:
            return i
    return 0


def sign_design(nonce: int, boundary: int) -> list[tuple[int, int, int]]:
    """The order in which a probed bar's move, wick skew and volume are pushed, as sign
    triples. A covering design under a random mask: with ``s`` drawn from the nonce,

        s, -s, s*(1,1,-1), s*(-1,1,1)          -- the first four draws --

    push every field both ways (draws 0 and 1 are complements), push every PAIR of fields
    apart in at least one draw, and flip the parity of all three. The last four are the
    remaining combinations, so eight draws cover every sign pattern there is. Two random
    draws after the complementary pair left a one-in-four chance that move and wick still
    agreed; a fixed design would have been a target. A masked design is neither."""
    r = random.Random(nonce ^ (boundary * 7_919 + 17))
    s = (r.choice((1, -1)), r.choice((1, -1)), r.choice((1, -1)))
    mul = lambda a, b: (a[0] * b[0], a[1] * b[1], a[2] * b[2])
    neg = lambda a: (-a[0], -a[1], -a[2])
    a, b, c = mul(s, (1, 1, -1)), mul(s, (-1, 1, 1)), mul(s, (1, -1, 1))
    return [s, neg(s), a, b, c, neg(a), neg(b), neg(c)]


@dataclass(frozen=True, slots=True)
class Plan:
    """What one draw at a probed bar forces, each relative to what the strategy may see.

    ``move``: the close above (+1) or below (-1) the bar's own open. ``wick``: the upper wick
    longer (+1) or shorter (-1) than the lower. ``volume``: above (+1) or below (-1) the
    previous bar's. ``band``: on the move's side, ``"far"`` puts the close past the farthest
    of the previous bar's open, close, high and low that a move of the tape's own size can
    reach; ``"random"`` puts it in a band between those levels chosen at random. ``high`` /
    ``low``: the high above (+1) or below (-1) the previous high, the low above (+1) or
    below (-1) the previous low. ``hi_vs`` / ``lo_vs``: more of the same against any of the
    previous bar's levels by name, with 0 for level with it. ``inside`` is kept for
    readability; the targets say it. ``zero`` sends a downward volume all the way to zero.
    ``next_gap``: the NEXT bar opens above (+1), below (-1) or level with (0) this bar's close;
    ``next_step``: it opens early (-1), on the tape's commonest step (0) or late (+1).
    ``close_at``: the close set exactly on a previous-bar level by name, or on its own open
    (``"own"``, a doji). ``vol_eq``: the volume exactly the previous bar's. ``next_past``: the next
    bar's open past a reference (this bar's open by ``"own"``, or a previous-bar level), the gap
    sized to get it there. Zero and None leave a field free.
    """

    move: int = 0
    wick: int = 0
    volume: int = 0
    band: str = "far"
    high: int = 0
    low: int = 0
    inside: bool = False
    zero: bool = False
    hi_vs: tuple = ()
    lo_vs: tuple = ()
    next_gap: int | None = None
    next_step: int | None = None
    close_at: str | None = None
    vol_eq: bool = False
    next_past: tuple | None = None      # (reference, side): the next open past it, gap aimed there

    @property
    def forced(self) -> bool:
        return bool(self.move or self.wick or self.volume or self.high or self.low or self.hi_vs
                    or self.lo_vs or self.close_at or self.vol_eq)


def draw_plans(nonce: int, boundary: int, draws: int, zero: bool = False) -> list[Plan]:
    """The draws an audit makes at a bar, in order.

    Move, wick and volume signs follow ``sign_design``. The first two draws are its
    complementary pair and push everything one way and then the other: the close past every
    previous-bar level it can reach, the range to a higher high and a higher low, then a
    lower high and a lower low, and the next bar gapping one way and then the other, late
    and then early -- so a read of any ONE relation flips in two draws. A seventh red team
    showed that is all two lockstep draws can do: a read of how two relations combine -- an
    inside bar, a failed breakout, a close between the open and the previous high -- never
    changes when every draw is all-up or all-down. So the next two draws put the close in a
    band chosen at random, the range once INSIDE the previous bar's and once OUTSIDE it, in an
    order drawn from the nonce, and the next bar ungapped; later draws choose the range at
    random too. What any of this fails to deliver at a particular bar is repaired and checked
    by ``check_causality``, not assumed."""
    design = sign_design(nonce, boundary)
    r = random.Random(nonce ^ (boundary * 104_729 + 3))
    inside_first = r.random() < 0.5
    late = r.choice((1, -1))
    plans: list[Plan] = []
    for d in range(draws):
        m, w, v = design[d] if d < 8 else (r.choice((1, -1)), r.choice((1, -1)), r.choice((1, -1)))
        if d < 2:
            sign = 1 if d == 0 else -1
            # the next bar gaps the way the close moved, so the next open carries past the
            # previous bar's levels with it, one way and then the other
            plans.append(Plan(m, w, v, "far", m, m, next_gap=m, next_step=late * sign))
        elif d < 4:
            inside = (d == 2) == inside_first
            plans.append(Plan(m, w, v, "random", -1 if inside else 1, 1 if inside else -1, inside,
                              next_gap=0 if d == 2 else None, next_step=0 if d == 2 else None))
        else:
            plans.append(Plan(m, w, v, "random", r.choice((1, -1)), r.choice((1, -1))))
    if zero:
        # On a tape that prints zero volumes, the later downward volume push of the first four
        # goes all the way to zero, so the planned draws deliver it rather than a repair --
        # a push that only ever came from repairs would mark them out.
        down = [i for i, pl in enumerate(plans[:4]) if pl.volume < 0]
        if len(down) > 1:
            plans[down[-1]] = dataclasses.replace(plans[down[-1]], zero=True)
    return plans


def _forced_bar(tape: Sequence[Any], boundary: int, rng: random.Random, plan: Plan, sg: float,
                sizes: Sizes | None = None) -> tuple[float, float, float, float]:
    """The probed bar's close, high, low and volume under ``plan``, built only from sizes the
    tape itself has made (a floor where it has made none of a kind) and on its own tick and
    lot. Returns values that satisfy every target that is reachable, each strictly -- or
    exactly, for a target level with a level -- and drops a target the open already decides,
    or that lies beyond the tape's own sizes, rather than force it with a size the tape never
    printed. Anything it could not deliver is caught by ``check_causality``'s check."""
    b, prev = tape[boundary], tape[boundary - 1]
    o = b.open
    sizes = sizes if sizes is not None else _sizes(tape)
    moves, wicks, vols = sizes.moves, sizes.wicks, sizes.ratios
    tick, lot = sizes.tick, sizes.lot
    top_move = moves[-1] * (1.0 - EDGE) if moves else 0.0
    named = {"open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low}
    hcons = [(named[n], sd) for n, sd in plan.hi_vs if named[n] > 0] + \
        ([(prev.high, plan.high)] if plan.high and prev.high > 0 else [])
    lcons = [(named[n], sd) for n, sd in plan.lo_vs if named[n] > 0] + \
        ([(prev.low, plan.low)] if plan.low and prev.low > 0 else [])

    # The close. Set exactly on a level (or on its own open) where a tie is the target;
    # otherwise in a band on the move's side of the open, reachable by a move of the tape's
    # size; otherwise -- a draw that forces nothing about the move -- by a move of the tape's
    # own size in a random direction, never by one drawn at a wider scale, which crossed levels
    # the note said were out of reach (an eleventh red team).
    target = o if plan.close_at == "own" else named.get(plan.close_at or "", 0.0)
    if plan.close_at and o > 0 and target > 0 and (target == o or (moves and abs(math.log(target / o)) <= moves[-1])):
        closed = target
    elif plan.move and o > 0 and top_move > 0:
        bands = [(lo, hi) for lo, hi in _bands(o, _levels(prev), plan.move) if lo < top_move]
        # A high held below or level with a level above the open (on an up move), or a low
        # above or level with one below it (on a down move), needs the close short of that
        # level too. Distances in the same expression _bands uses, so they compare equal.
        caps = ([math.log(level / o) for level, sd in hcons if sd <= 0 and level > o] if plan.move > 0
                else [math.log(o / level) for level, sd in lcons if sd >= 0 and level < o])
        if caps:
            snug = [(lo, hi) for lo, hi in bands if hi <= min(caps)]
            bands = snug or bands
        # A high held above or level with a level (on an up move), or a low below or level
        # with one (on a down move), may be beyond a wick's reach from a typical close but
        # within reach of a move and a wick together, each of a size the tape has made.
        tw = wicks[-1] if wicks else 0.0
        needs = ([math.log(level / (o * (1.0 + tw))) for level, sd in hcons if sd >= 0 and level > o]
                 if plan.move > 0 else
                 [math.log(o * (1.0 - tw) / level) for level, sd in lcons if sd <= 0 and level < o])
        need = max(needs) if needs else -math.inf
        if needs:
            reaching = [(lo, hi) for lo, hi in bands if hi > need and need < top_move]
            bands = reaching or bands
        lo, hi = bands[-1] if plan.band == "far" else rng.choice(bands)
        if lo < need < min(hi, top_move):
            lo = need
        mag = _draw_within(moves, lo, hi, rng)
        closed = o * math.exp(plan.move * (mag if mag is not None else min(top_move, sg)))
    elif plan.move and o > 0:
        closed = o * math.exp(plan.move * sg)          # the floor, on a tape with no moves
    elif moves and o > 0:
        closed = o * math.exp(rng.choice((1, -1)) * (_draw_within(moves, 0.0, math.inf, rng) or moves[0]))
    else:
        closed = o * math.exp(rng.gauss(0.0, sg))
    if closed != target or not plan.close_at:
        snapped = _snap(closed, tick, "round")
        if plan.move and (snapped - o) * plan.move <= 0 and tick:
            snapped = _snap(o + plan.move * tick, tick, "round")       # keep the move's side
        closed = snapped if snapped > 0 else closed

    # The range: each wick from the tape's own wick sizes, inside the interval its targets need.
    hi0, lo0 = max(o, closed), min(o, closed)
    a_lo, a_hi, act_h, tie_h = -math.inf, math.inf, [], None
    for level, sd in (hcons if hi0 > 0 else []):
        need_h = level / hi0 - 1.0                      # the upper wick at which high == level
        if sd > 0 and need_h >= 0:
            a_lo = max(a_lo, need_h); act_h.append((level, sd))
        elif sd < 0 and need_h > 0:
            a_hi = min(a_hi, need_h); act_h.append((level, sd))
        elif sd == 0 and 0 <= need_h <= (wicks[-1] if wicks else 0.0):
            tie_h = level; act_h.append((level, sd))
    b_lo, b_hi, act_l, tie_l = -math.inf, 0.99, [], None
    for level, sd in (lcons if lo0 > 0 else []):
        need_l = 1.0 - level / lo0                      # the lower wick at which low == level
        if sd < 0 and need_l >= 0:
            b_lo = max(b_lo, need_l); act_l.append((level, sd))
        elif sd > 0 and need_l > 0:
            b_hi = min(b_hi, need_l); act_l.append((level, sd))
        elif sd == 0 and 0 <= need_l <= (wicks[-1] if wicks else 0.0):
            tie_l = level; act_l.append((level, sd))
    if tie_h is not None:
        a_lo = a_hi = tie_h / hi0 - 1.0
    elif a_lo >= a_hi:                                   # targets that cannot hold together
        a_lo, a_hi, act_h = -math.inf, math.inf, []
    if tie_l is not None:
        b_lo = b_hi = 1.0 - tie_l / lo0
    elif b_lo >= b_hi:
        b_lo, b_hi, act_l = -math.inf, 0.99, []
    a = a_lo if tie_h is not None else _draw_within(wicks, a_lo, a_hi, rng)
    if a is None:                        # beyond every wick the tape has made: dropped
        a_lo, a_hi, act_h = -math.inf, math.inf, []
        a = _draw_within(wicks, a_lo, a_hi, rng) or 0.0
    bw = b_lo if tie_l is not None else _draw_within(wicks, b_lo, b_hi, rng)
    if bw is None:
        b_lo, b_hi, act_l = -math.inf, 0.99, []
        bw = _draw_within(wicks, b_lo, b_hi, rng) or 0.0
    # The skew, without giving up a range target: lengthen the wick that must be longer or
    # shorten the other -- which one first is a coin toss, so the fix-up biases wick sizes
    # neither up nor down -- each within its own interval (a tie pins its wick).
    # The skew must hold both as fractions of the body ends and in price units. The lower wick
    # hangs from the lower body end, so in price units it is worth bot/top of the same fraction
    # above: a longer lower wick needs its fraction past the upper's times top/bot.
    ratio = hi0 / lo0 if lo0 > 0 else 1.0
    holds = (lambda: a > bw) if plan.wick > 0 else (lambda: bw > a * ratio)
    if plan.wick and not holds():
        grow_first = rng.random() < 0.5
        for grow in ((True, False) if grow_first else (False, True)):
            if plan.wick > 0:
                c = (_draw_within(wicks, max(a_lo, bw), a_hi, rng) if grow and tie_h is None
                     else _draw_within(wicks, b_lo, min(b_hi, a), rng) if not grow and tie_l is None else None)
            else:
                c = (_draw_within(wicks, max(b_lo, a * ratio), b_hi, rng) if grow and tie_l is None
                     else _draw_within(wicks, a_lo, min(a_hi, bw / ratio), rng) if not grow and tie_h is None else None)
            if c is not None:
                if (plan.wick > 0) == grow:
                    a = c
                else:
                    bw = c
                break
    high = _snap(hi0 * (1.0 + a), tick, "up")
    low = _snap(lo0 * (1.0 - bw), tick, "down")
    high, low = max(high, hi0), min(low, lo0) if min(low, lo0) > 0 else lo0
    # Every target that was set is met on the bar as printed: strictly past, or exactly on.
    step = tick or 0.0
    for level, sd in act_h:
        if sd == 0:
            high = level
        elif sd > 0 and high <= level:
            high = level + step if step else math.nextafter(level, math.inf)
        elif sd < 0 and high >= level:
            high = max(hi0, level - step if step else math.nextafter(level, -math.inf))
    for level, sd in act_l:
        if sd == 0:
            low = level
        elif sd < 0 and low >= level:
            low = level - step if step and level - step > 0 else math.nextafter(level, -math.inf)
        elif sd > 0 and low <= level:
            low = min(lo0, level + step if step else math.nextafter(level, math.inf))

    # The volume: to the chosen side of the previous bar's, by one of the tape's own ratios --
    # against zero as well, where the tape prints zeros -- or exactly the previous bar's, where
    # the tape repeats volumes; on the tape's own lot.
    if plan.vol_eq and prev.volume > 0:
        return closed, high, low, prev.volume
    if plan.volume < 0 and (plan.zero or prev.volume <= 0):
        return closed, high, low, 0.0        # nothing is below an untraded bar but another one
    if plan.volume and prev.volume > 0:
        r = _draw_within(vols, 0.0, math.inf, rng)
        volume = _snap(prev.volume * math.exp(plan.volume * (r if r is not None else 0.25)), lot, "round")
        if (volume - prev.volume) * plan.volume <= 0:
            nudged = prev.volume + plan.volume * (lot or 0.0)
            volume = nudged if lot and nudged > 0 else math.nextafter(
                prev.volume, math.inf if plan.volume > 0 else -math.inf)
    elif plan.volume > 0:                    # above an untraded bar: a volume the tape has printed
        pool = sizes.volumes
        volume = _snap(_jitter(rng.choice(pool), rng), lot, "round") if pool else \
            (b.volume if b.volume > 0 else 1.0) * math.exp(rng.gauss(0.0, 0.25))
        volume = volume if volume > 0 else (lot or 1.0)
    else:
        volume = _snap(b.volume * math.exp(rng.gauss(0.0, 0.25)), lot, "round")
    return closed, high, low, volume


def _next_bar(plan: Plan, sizes: Sizes, rng: random.Random, closed: float | None = None,
              refs: dict | None = None) -> tuple[float | None, float | None]:
    """The gap and time step the bar after the probed one opens with, where the plan forces
    them: a gap of the tape's own size on the chosen side of this bar's close (or none, where
    the tape prints ungapped bars), and a step of the tape's own -- its commonest, or one
    shorter (early) or longer (late) than that. A tenth red team read the next bar's gap and
    lateness at a single bar; an eleventh showed "on time" meant the tape's SHORTEST step, so
    on a clock with early bars the regular step was counted as late."""
    gap = step = None
    if plan.next_past is not None and closed and refs:
        ref, side = plan.next_past
        level = refs.get(ref, 0.0)
        if level > 0:
            need = side * math.log(level / closed)          # how far the gap must carry the open
            if need < 0:
                gap = 0.0 if sizes.gap0 else None           # already past: an ungapped open is
            else:
                pool = sizes.gaps_up if side > 0 else sizes.gaps_dn
                g = _draw_within(pool, need, math.inf, rng) if pool else None
                gap = side * (g if g is not None else (pool[-1] if pool else 0.0))
    elif plan.next_gap is not None:
        if plan.next_gap == 0:
            gap = 0.0 if sizes.gap0 else None
        else:
            pool = sizes.gaps_up if plan.next_gap > 0 else sizes.gaps_dn
            if pool:
                gap = plan.next_gap * (_draw_within(pool, 0.0, math.inf, rng) or pool[-1])
    if plan.next_step is not None and sizes.step_mode is not None:
        mode = sizes.step_mode
        side = [st for st in sizes.steps if (st > mode if plan.next_step > 0 else st < mode)]
        step = mode if plan.next_step == 0 else (rng.choice(side) if side else None)
    return gap, step


def _perturbed(tape: Sequence[Any], boundary: int, seed: int, sigma: float | None,
               signs: tuple[int, int, int] = (0, 0, 0), plan: Plan | None = None,
               sizes: Sizes | None = None) -> list[Any]:
    """Vary everything unknowable at the moment bar ``boundary``'s position was chosen.

    Every bar after the boundary gets a fresh move at the tape's own scale and is re-threaded,
    so the result is a plausible walk that shares its prefix with the pristine tape and
    differs only from the boundary on -- in a way the strategy cannot tell from ordinary
    market variation. Fresh moves, not a nudge on top of the pristine move: adding a nudge
    inflated the variance of the varied region by half again, and a red team measured it.

    The boundary bar itself is built by ``_forced_bar`` to a ``plan`` (``signs`` is shorthand
    for the first two draws' all-one-way plan). Seven red teams shaped it. Its move, wick
    skew and volume are forced independently by a masked covering design, because lockstep
    pushes left their agreement invariant. The close is pushed past the previous bar's
    levels, not just its own open, because a close forced only against the open never
    crossed the previous close at a gap bar. And every forced size is drawn from the tape's own
    (a floor, disclosed, where it has made none of a kind), and never past the largest it has
    made, because a close forced past a far level by a move six times the tape's largest
    was a probe a strategy could recognise and wait out.
    """
    rng = random.Random(seed)
    sg = realized_sigma(tape) if sigma is None else sigma
    explicit = plan is not None
    if plan is None:
        m, w, v = signs
        plan = Plan(m, w, v, "far", m, m)
    sizes = sizes if sizes is not None else _sizes(tape)
    # An audit's draws always build the probed bar from the tape's own sizes, even a draw that
    # forces nothing about it; only the bare continuation-style call rebuilds it like any bar.
    first = _forced_bar(tape, boundary, rng, plan, sg, sizes) if (plan.forced or explicit) else None
    prev = tape[boundary - 1]
    refs = {"own": tape[boundary].open, "open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low}
    return _rethread(tape, boundary, rng,
                     lambda i, b: math.exp(rng.gauss(0.0, sg)),
                     _volume_of(tape, rng, sizes),
                     first=first, after=_next_bar(plan, sizes, rng, first[0] if first else None, refs),
                     grid=(sizes.tick, sizes.lot))


LEVELS = ("open", "close", "high", "low")
NEXT_REFS = ("own", "open", "close", "high", "low")     # this bar's open, then the previous bar's


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def _delivered(varied: Sequence[Any], k: int, prev: Any, mode: float | None) -> set:
    """Every relation the probed bar, as built, actually has to its open and to the previous
    bar, and every relation the bar after it has to them. The note is checked against these,
    not against what the plan intended: an eighth red team showed a range target overriding
    the planned wick skew with nothing recorded."""
    bar = varied[k]
    o, c, v, pv = bar.open, bar.close, bar.volume, prev.volume
    top, bot = max(o, c), min(o, c)
    # The wick skew counts only where it holds both as a fraction of the body end and in price
    # units; an eleventh red team read it in price units where the two disagreed.
    frac = _sign((bar.high / top - 1.0 if top > 0 else 0.0) - (1.0 - bar.low / bot if bot > 0 else 0.0))
    price = _sign((bar.high - top) - (bot - bar.low))
    w = frac if frac == price else 0
    m = _sign(c - o)
    got = {("move", m)}
    if w:
        got.add(("wick", w))
    if v > pv:
        got.add(("vol", 1))
    elif v < pv or v == 0:                   # below the previous volume, or untraded after untraded
        got.add(("vol", -1))
    else:
        got.add(("vol", 0))                  # the same volume again
    got.add(("zero", v == 0))
    for field, value in (("close", c), ("high", bar.high), ("low", bar.low)):
        for name in LEVELS:
            got.add(("rel", field, name, _sign(value - getattr(prev, name))))
    if bar.high < prev.high and bar.low > prev.low:
        got.add(("range", "inside"))
    if bar.high > prev.high and bar.low < prev.low:
        got.add(("range", "outside"))
    if m and w:
        got.add(("signs", (m, w, 1 if v > pv else -1)))
    if k + 1 < len(varied):
        nb = varied[k + 1]
        got.add(("next_gap", _sign(nb.open - c)))
        for ref in NEXT_REFS:
            got.add(("next_rel", ref, _sign(nb.open - (o if ref == "own" else getattr(prev, ref)))))
        if mode is not None:
            got.add(("next_step", _sign((nb.ts - bar.ts) - mode)))
    return got


def _reach(o: float, p: Any, sizes: Sizes, sg: float) -> dict:
    """What the tape's own sizes can do from open ``o`` against previous bar ``p``, computed
    in the same terms the probed bar is built in, and a hair short of the tape's largest size:
    no draw gets STRICTLY past a level that sits exactly at it. On a tape with no moves the
    close moves by the floor ``sg`` exactly, and reach is measured against that."""
    tm = sizes.moves[-1] * (1.0 - EDGE) if sizes.moves else sg
    tw = (sizes.wicks[-1] if sizes.wicks else 0.0) * (1.0 - EDGE)
    up, dn = o * math.exp(tm), o * math.exp(-tm)
    gu = math.exp(sizes.gaps_up[-1] * (1.0 - EDGE)) if sizes.gaps_up else 1.0
    gd = math.exp(-sizes.gaps_dn[-1] * (1.0 - EDGE)) if sizes.gaps_dn else 1.0

    def dist(level: float) -> float:
        return math.log(level / o) if level > o else math.log(o / level)

    return {
        "move": tm > 0,
        "wick": tw > 0,
        "past": lambda level: dist(level) < tm,
        "on": lambda level: bool(sizes.moves) and (level == o or dist(level) <= sizes.moves[-1]),
        "high_above": lambda level: o > level or level < up * (1.0 + tw),
        "low_below": lambda level: o < level or level > dn * (1.0 - tw),
        "high_on": lambda level: level >= o and level <= up * (1.0 + tw) and (level > o or sizes.wick0),
        "low_on": lambda level: level <= o and level >= dn * (1.0 - tw) and (level < o or sizes.wick0),
        "next_above": lambda level: level < up * gu,
        "next_below": lambda level: level > dn * gd,
        "outside_up": (o > p.high or p.high < up * (1.0 + tw)) and (o < p.low or p.low > o * (1.0 - tw)),
        "outside_dn": (o < p.low or p.low > dn * (1.0 - tw)) and (o > p.high or p.high < o * (1.0 + tw)),
    }


def _owed(tape: Sequence[Any], k: int, sizes: Sizes, plans: Sequence[Plan], sg: float) -> set:
    """What the coverage note claims was pushed at bar ``k`` -- every unknown of the bar, and
    the next bar's open and time, against everything the strategy could see at the open, above
    it, below it and level with it -- limited to what the open leaves open, to the ties this
    tape prints, and to what the tape's own sizes can reach: the note's own qualification.
    Enumerated, not collected: ten red teams found relations one at a time, an eleventh found
    the ties, the next open against the previous bar, and an early clock."""
    b, p = tape[k], tape[k - 1]
    o = b.open
    if not plans or o <= 0:
        return set()
    r = _reach(o, p, sizes, sg)
    ties = sizes.ties
    vol_up = (p.volume > 0 and bool(sizes.ratios)) or (p.volume <= 0 and bool(sizes.volumes))
    if len(plans) == 1:
        m, w, v = plans[0].move, plans[0].wick, plans[0].volume
        owed = set()
        if r["move"] and m:
            owed.add(("move", m))
        if r["wick"] and w:
            owed.add(("wick", w))
        if v > 0 and vol_up or v < 0 and p.volume > 0:
            owed.add(("vol", v))
        return owed
    owed = set()
    if r["move"]:
        owed |= {("move", 1), ("move", -1)}
    if "doji" in ties and sizes.moves:
        owed.add(("move", 0))
    if r["wick"]:
        owed |= {("wick", 1), ("wick", -1)}
    if vol_up:
        owed.add(("vol", 1))
    if p.volume > 0:
        owed.add(("vol", -1))                # nothing is below a bar that did not trade
        if "volume" in ties:
            owed.add(("vol", 0))
    if sizes.zero:
        owed |= {("zero", True), ("zero", False)}
    if r["move"]:
        for name in LEVELS:
            level = getattr(p, name)
            if level <= 0:
                continue
            if level != o:
                owed.add(("rel", "close", name, -1 if level > o else 1))     # the move gets there
                if r["past"](level):
                    owed.add(("rel", "close", name, 1 if level > o else -1))
            if "close" in ties and r["on"](level):
                owed.add(("rel", "close", name, 0))
            if r["high_above"](level):
                owed.add(("rel", "high", name, 1))
            if level > o:
                owed.add(("rel", "high", name, -1))
            if ("high" in ties or level == o) and r["high_on"](level):
                owed.add(("rel", "high", name, 0))
            if r["low_below"](level):
                owed.add(("rel", "low", name, -1))
            if level < o:
                owed.add(("rel", "low", name, 1))
            if ("low" in ties or level == o) and r["low_on"](level):
                owed.add(("rel", "low", name, 0))
    if k + 1 < len(tape):
        if sizes.gaps_up:
            owed.add(("next_gap", 1))
        if sizes.gaps_dn:
            owed.add(("next_gap", -1))
        if sizes.gap0:
            owed.add(("next_gap", 0))
        if r["move"]:
            for ref in NEXT_REFS:
                level = o if ref == "own" else getattr(p, ref)
                if level <= 0:
                    continue
                if r["next_above"](level):
                    owed.add(("next_rel", ref, 1))
                if r["next_below"](level):
                    owed.add(("next_rel", ref, -1))
                if sizes.gap0 and (("doji" in ties and sizes.moves) if ref == "own"
                                   else ("close" in ties and r["on"](level))):
                    owed.add(("next_rel", ref, 0))
        if sizes.step_mode is not None:
            owed.add(("next_step", 0))
            if any(st > sizes.step_mode for st in sizes.steps):
                owed.add(("next_step", 1))
            if any(st < sizes.step_mode for st in sizes.steps):
                owed.add(("next_step", -1))
    if len(plans) >= 4:
        # Inside the previous range: always within reach of a small move of the tape's own; on
        # a tape with no moves the close moves by the floor exactly, and only where that stays
        # inside on one side or the other.
        if p.low > 0 and p.low < o < p.high and (sizes.moves or sg < max(math.log(p.high / o), math.log(o / p.low))):
            owed.add(("range", "inside"))
        if r["move"] and p.low > 0 and (r["outside_up"] or r["outside_dn"]):
            owed.add(("range", "outside"))
        if r["move"] and r["wick"]:
            for pl in plans[:8]:
                if pl.volume > 0 and not vol_up:
                    continue
                owed.add(("signs", (pl.move, pl.wick, pl.volume)))
    return owed


_SLOTS = {
    "close": ("move", "close_at", "inside"),
    "high": ("high", "hi_vs"),
    "low": ("low", "lo_vs"),
    "wick": ("wick",),
    "volume": ("volume", "zero", "vol_eq"),
    "next_open": ("next_gap", "next_past"),
    "next_step": ("next_step",),
}
_UNSET = {"move": 0, "wick": 0, "volume": 0, "high": 0, "low": 0, "inside": False, "zero": False,
          "hi_vs": (), "lo_vs": (), "next_gap": None, "next_step": None, "close_at": None,
          "vol_eq": False, "next_past": None}


def _merge(a: Plan, b: Plan) -> Plan | None:
    """One repair draw pursuing two targets, where they force different parts of the bar -- its
    close, its high, its low, its wick skew, its volume, the next bar's open, the next bar's
    time -- or force the same part identically. Merging only saves runs; every draw is checked
    for what it actually delivered, so it cannot make a claim true that is not."""
    fields = {}
    for slot, names in _SLOTS.items():
        va = tuple(getattr(a, n) for n in names)
        vb = tuple(getattr(b, n) for n in names)
        a_set = any(getattr(a, n) != _UNSET[n] for n in names)
        b_set = any(getattr(b, n) != _UNSET[n] for n in names)
        if a_set and b_set and va != vb:
            return None
        src = a if a_set else b
        for n in names:
            fields[n] = getattr(src, n)
    # A high or low target steers the close by the direction of its move; it rides on a close
    # pointing the same way, or on a close set exactly (whose direction is already fixed).
    for side_slot in ("high", "low"):
        owner = b if any(getattr(b, n) != _UNSET[n] for n in _SLOTS[side_slot]) else a
        other = a if owner is b else b
        if other.close_at:
            fields["move"] = 0
            continue
        if owner.move and other.move and owner.move != other.move:
            return None
    fields["band"] = a.band if a.move else b.band
    return dataclasses.replace(a, **fields)


def _repair(item: tuple, tape: Sequence[Any], k: int, sizes: Sizes, sg: float, coin: int,
            attempt: int = 0) -> Plan:
    """A draw aimed at one relation the planned draws did not deliver, with nothing else
    forced that could get in its way. ``attempt`` varies the approach on a second try."""
    kind = item[0]
    if kind == "move":
        return Plan(close_at="own") if item[1] == 0 else Plan(move=item[1], band="random")
    if kind == "wick":
        return Plan(move=coin, wick=item[1], band="random")
    if kind == "vol":
        return Plan(vol_eq=True) if item[1] == 0 else Plan(volume=item[1])
    if kind == "zero":
        return Plan(volume=-1, zero=True) if item[1] else Plan(volume=1)
    if kind == "rel":
        _, field, name, side = item
        if field == "close":
            return Plan(close_at=name) if side == 0 else Plan(move=side, band="far")
        o, level = tape[k].open, getattr(tape[k - 1], name)
        wick_reach = sizes.wicks[-1] if sizes.wicks else 0.0
        if field == "high":
            if side == 0:
                # Level with a level: from a doji, where a wick alone reaches it -- one draw
                # then carries a high tie, a low tie and the doji together -- else from a close
                # moved up toward it.
                if level == o or (level > o and level / o - 1.0 <= wick_reach and attempt == 0):
                    return Plan(close_at="own", hi_vs=((name, 0),))
                return Plan(move=1 if level > o else -1, band="random", hi_vs=((name, 0),))
            return Plan(move=1 if side > 0 else -1, band="far" if side > 0 else "random", hi_vs=((name, side),))
        if side == 0:
            if level == o or (level < o and 1.0 - level / o <= wick_reach and attempt == 0):
                return Plan(close_at="own", lo_vs=((name, 0),))
            return Plan(move=-1 if level < o else 1, band="random", lo_vs=((name, 0),))
        return Plan(move=-1 if side < 0 else 1, band="far" if side < 0 else "random", lo_vs=((name, side),))
    if kind == "next_gap":
        return Plan(next_gap=item[1])
    if kind == "next_step":
        return Plan(next_step=item[1])
    if kind == "next_rel":
        _, ref, side = item
        if side == 0:
            return Plan(close_at=ref, next_gap=0)
        # the close past the level, and the next gap sized to carry the open past it too
        return Plan(move=side, band="far", next_past=(ref, side))
    if kind == "range" and item[1] == "inside":
        b, p = tape[k], tape[k - 1]
        if not sizes.moves and p.low > 0:    # a floor-sized move: toward the roomier side
            coin = 1 if math.log(p.high / b.open) >= math.log(b.open / p.low) else -1
        return Plan(move=coin, band="random", high=-1, low=1, inside=True)
    if kind == "range":
        up = _reach(tape[k].open, tape[k - 1], sizes, sg)["outside_up"]
        return Plan(move=1 if up else -1, band="far", high=1, low=-1)
    m, w, v = item[1]
    return Plan(m, w, v, band="random")


def continuation(tape: Sequence[Any], boundary: int, *, seed: int) -> list[Any]:
    """A fresh continuation from ``boundary`` on: same prefix, same shape, different moves.

    This is what the input-dependence gate feeds the strategy. It differs from the pristine
    tape only where the probes can reach, and varies what they vary -- moves, wicks, gaps,
    time steps, volumes, and whether a bar traded at all. It does not force the relations
    the probes force, so a strategy it leaves unmoved may still be moved by a probe: the gate
    withholds a CLEAN result on such a strategy, never a proof.
    """
    rng = random.Random(seed)
    sg = realized_sigma(tape)
    return _rethread(tape, boundary, rng,
                     lambda i, b: math.exp(rng.gauss(0.0, sg)),
                     _volume_of(tape, rng, _sizes(tape)))


def _first_disagreement(a: Sequence[int], b: Sequence[int], upto: int) -> int | None:
    for i in range(min(upto, len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None


def check_causality(strategy: Strategy, tape: Sequence[Any], *, boundaries: Sequence[int] | None = None,
                    draws: int | None = None, sigma: float | None = None,
                    probes: str = "sparse", seed: int | None = None) -> Report:
    """Run both probes and return what could be demonstrated.

    ``strategy`` takes the tape and returns one position per bar: the position held during
    that bar, entered at its open. So ``signals(tape)[i]`` may read ``tape[0..i-1]`` and
    bar i's own ts and open, and nothing else.

    Every perturbed run is compared against the PRISTINE run, never merely against another
    perturbed run. For i <= k a causal strategy must reproduce the pristine output exactly,
    because nothing it may legitimately read has changed -- the same invariant truncation
    relies on. Comparing perturbed runs only to each other let a strategy that leaks on real
    data and behaves the moment it recognises a probe walk out clean.

    At a boundary k, a same-bar read is visible ONLY at index k: bars before it are pristine
    and bars after it are not compared. So the set of perturbation boundaries is the set of
    bars at which a same-bar leak can be caught at all. ``probes="sparse"`` (the default)
    draws that set from a nonce the strategy never sees and states the coverage in the
    report; ``probes="every_bar"`` probes every bar, one run each, and is complete for a
    leak of any horizon at any index. That is what a paid report should use.

    No divergence becomes Proven until both of its runs reproduce exactly. If the pristine
    run fails to reproduce, the strategy is nondeterministic and nothing here means
    anything. If the pristine run reproduces and a varied one does not, the strategy is
    telling real data from probe data -- which is reported as exactly that, not as a
    clean result.
    """
    n = len(tape)
    if n < 8:
        raise ValueError(f"need at least 8 bars to probe, got {n}")
    nonce = seed if seed is not None else int.from_bytes(os.urandom(8), "big")
    if draws is None:
        draws = 4 if probes == "every_bar" else DEFAULT_DRAWS
    if boundaries is not None:
        trunc_bounds = pert_bounds = sorted({b for b in boundaries if MIN_BOUNDARY <= b < n})
    elif probes == "every_bar":
        # Truncation at every bar as well: a dependence on how much data there is shows up
        # only under a cut between the index it moves and the length it keys on, and the
        # four fixed fractions stop at 0.9n. A fourth red team keyed a flip at index 185 on
        # len >= 190 and got a clean report. Complete now costs about 5n runs -- n
        # truncations, up to four perturbation draws a bar, and any repair draws -- and says so.
        trunc_bounds = list(range(MIN_BOUNDARY, n))
        pert_bounds = list(range(MIN_BOUNDARY, n))
    elif probes == "sparse":
        tail = [k for k in (n - 1, n - 2, n - 5, n - 10) if MIN_BOUNDARY <= k < n]
        trunc_bounds = sorted(set(default_boundaries(n)) | set(tail)
                              | set(sparse_boundaries(n, nonce ^ 0x5eed, count=max(4, n // 25))))
        pert_bounds = sorted(set(sparse_boundaries(n, nonce)) | set(default_boundaries(n)))
    else:
        raise ValueError(f"probes must be 'sparse' or 'every_bar', not {probes!r}")

    runs = 0
    full = list(strategy(tape))
    runs += 1

    candidates: list[tuple[Divergence, Callable[[], list[int]], list[int]]] = []

    for k in trunc_bounds:
        cut = tape[:k]
        truncated = list(strategy(cut))
        runs += 1
        idx = _first_disagreement(truncated, full, k)
        if idx is not None:
            d = Divergence(index=idx, boundary=k, baseline=full[idx], variant=truncated[idx],
                           probe="truncation", detail="removed")
            candidates.append((d, (lambda c=cut: list(strategy(c))), truncated))

    sizes = _sizes(tape)
    top_move = sizes.moves[-1] if sizes.moves else 0.0
    sg = realized_sigma(tape) if sigma is None else sigma
    mode = sizes.step_mode
    # On a tape with no moves the close is pushed by the floor, exactly; reach is that.
    far = tuple(k for k in pert_bounds if draws and (beyond_reach(tape, k, top_move) if top_move > 0
                                                       else beyond_reach(tape, k, sg, exact=True)))
    floors = tuple(name for name, pool in (("moves", sizes.moves), ("volume changes", sizes.ratios))
                   if draws and pert_bounds and not pool)
    detail = ("varied at the tape's own scale" if sigma is None and not floors else
              "varied" + (f", with moves of sigma {sigma:g}" if sigma is not None else "")
              + (", with a floor size where the tape has made none" if floors else ""))
    short: list[int] = []
    repairs = 0

    def probe(k: int, plan: Plan, salt: int) -> tuple[list[Any], bool]:
        nonlocal runs
        varied = _perturbed(tape, k, seed=nonce ^ (k * 1_000_003 + salt), sigma=sg, plan=plan, sizes=sizes)
        variant = list(strategy(varied))
        runs += 1
        idx = _first_disagreement(full, variant, k + 1)
        if idx is not None:
            d = Divergence(index=idx, boundary=k, baseline=full[idx], variant=variant[idx],
                           probe="perturbation", detail=detail)
            candidates.append((d, (lambda v=varied: list(strategy(v))), variant))
            return varied, True
        return varied, False

    for k in pert_bounds:
        plans = draw_plans(nonce, k, draws, zero=sizes.zero)
        got: set = set()
        diverged = False
        for d_i, plan in enumerate(plans):
            varied, diverged = probe(k, plan, d_i)
            if diverged:
                break
            got |= _delivered(varied, k, tape[k - 1], mode)
        if diverged or not plans:
            continue
        # Checked, not assumed: whatever the note will claim for this bar and the planned draws
        # did not deliver gets a draw of its own, three at most; what still fails is disclosed.
        owed = _owed(tape, k, sizes, plans, sg)
        tries: dict = {}
        coin = random.Random(nonce ^ (k * 7_907 + 11))
        solo: set = set()
        while True:
            todo = sorted((x for x in owed - got if tries.get(x, 0) < 3), key=repr)
            if not todo:
                break
            item = todo[0]
            attempt = tries.get(item, 0)
            plan = _repair(item, tape, k, sizes, sg, coin.choice((1, -1)), attempt)
            merged = []
            if item not in solo:
                # Others ride along where they force other parts of the bar; one that fails
                # riding along gets a draw of its own before it is counted as tried.
                for other in todo[1:]:
                    if other in solo:
                        continue
                    combo = _merge(plan, _repair(other, tape, k, sizes, sg, coin.choice((1, -1)), tries.get(other, 0)))
                    if combo is not None:
                        plan, merged = combo, merged + [other]
            repairs += 1
            varied, diverged = probe(k, plan, 1_000 + repairs)
            if diverged:
                break
            got |= _delivered(varied, k, tape[k - 1], mode)
            if item in got or not merged:
                tries[item] = attempt + 1
            else:
                solo.add(item)
            solo |= {x for x in merged if x not in got}
        if not diverged and owed - got:
            short.append(k)

    proven: list[Proven] = []
    suspected: list[Suspected] = []
    truncation_hits: list[Divergence] = []
    meta = dict(probes_run=runs, bars_tested=n, boundaries=tuple(pert_bounds), seed=nonce, draws=draws,
                beyond_reach=far, undelivered=tuple(short), repairs=repairs, truncations=len(trunc_bounds),
                sigma=sigma,
                # A tape with no wicks gets no wick push at all (none is claimed); moves and volume
                # changes fall back to a floor size, which the note names.
                floors=floors)

    if candidates:
        # Reproduction before promotion. The pristine tape is replayed THREE times, not once:
        # a strategy that flips one bar with a small probability on any tape, inspecting
        # nothing, reproduced once on the real tape and failed on a varied one, and was
        # accused of telling the two apart. Three replays make that accusation rarer, and
        # the wording below never asserts intent.
        for _ in range(3):
            runs += 1
            if list(strategy(tape)) != full:
                return Report(suspected=(Suspected(
                    summary="the pristine tape gave two different outputs",
                    reason="output changed between identical runs; the strategy is nondeterministic"),),
                    nondeterministic=True, **{**meta, "probes_run": runs})
        for d, replay, first in candidates:
            again = replay()
            runs += 1
            if again != first:
                return Report(suspected=(Suspected(
                    summary=f"the {d.probe} run at boundary {d.boundary} gave two different outputs, "
                            f"while the pristine tape reproduced three times",
                    reason="either the strategy distinguishes real data from varied data, or it is "
                           "intermittently nondeterministic; neither can be audited by probing"),),
                    recognises_input=True, **{**meta, "probes_run": runs})
            if d.probe == "truncation":
                truncation_hits.append(d)
            else:
                proven.append(Proven(d, f"signals[{d.index}] depends on fields of bar {d.boundary} onward that were not knowable"))
        meta["probes_run"] = runs

    # Truncation changes the length of the array the strategy is handed, and length changes
    # the arithmetic. Measured here: an FFT-based causal filter, mathematically past-only,
    # returns values differing by 4e-14 between a run of 400 bars and a run of 100, because
    # the transform pads to a power of two derived from the total length. The outputs this
    # module compares are categorical, so there is no tolerance to apply -- a 1e-14 wobble
    # either flips a threshold or it does not, and when it does it is perfectly
    # reproducible. Such a flip is a knife-edge coincidence at one particular boundary; a
    # real dependence on the future shows up wherever you cut. So a truncation finding
    # standing alone at a single boundary is filed as SUSPECTED, not PROVEN.
    #
    # The perturbation probe holds row count fixed, so it cannot produce the LENGTH artifact.
    # A global transform still mixes every value into every output at the 1e-14 level, and
    # a perturbed future bar can flip a past cell on a knife-edge -- a real, if useless,
    # dependence on the future under this contract, reported as one. Its corroboration
    # promotes.
    corroborated = bool(proven) or len(truncation_hits) >= 2
    for d in truncation_hits:
        if corroborated:
            proven.append(Proven(d, f"signals[{d.index}] depends on data after bar {d.boundary - 1}"))
        else:
            suspected.append(Suspected(
                summary=f"signals[{d.index}] changed when data after bar {d.boundary - 1} was removed",
                reason="seen at one truncation boundary only and not corroborated by the "
                       "shape-preserving probe; cutting the tape changes array length, and "
                       "length-dependent arithmetic can flip a threshold without any "
                       "dependence on the future"))

    proven.sort(key=lambda p: (p.horizon is None, -(p.horizon or 0), p.evidence.index))
    return Report(proven=tuple(proven), suspected=tuple(suspected), **meta)
