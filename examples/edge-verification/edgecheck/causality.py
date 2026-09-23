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
    ties: tuple[str, ...] = ()          # the kinds of tie set level at some probed bar
    grids: tuple[str, ...] = ()         # "tick", "lot": the grids the tape was found printed on

    def _tie_clause(self) -> str:
        """The ties the note may say were set: the kinds that were owed somewhere, and no others. A
        twelfth red team's doji tape was told dojis were set; none was ever built."""
        if not self.ties:
            return ""
        words = []
        if "doji" in self.ties:
            words.append("a doji")
        on_prev = [f for f in ("close", "high", "low") if f in self.ties]
        if on_prev:
            head = ", ".join(on_prev[:-1]) + (" or " if len(on_prev) > 1 else "") + on_prev[-1]
            words.append(f"a {head} on a previous-bar level")
        if "own" in self.ties:
            words.append("a high or low on the bar's own open")
        if "volume" in self.ties:
            words.append("a repeated volume")
        if "ungapped" in self.ties:
            words.append("an ungapped next open")
        if "next" in self.ties:
            words.append("a next open on one of those levels")
        return ("; each of these also set level with its reference where the tape prints that kind of "
                "tie -- " + ", ".join(words))

    def _grid_clause(self) -> str:
        parts = (["prices on the tape's own tick"] if "tick" in self.grids else []) + \
            ([("volumes on its own lot" if "tick" in self.grids else "volumes on the tape's own lot")]
             if "lot" in self.grids else [])
        return " and ".join(parts)

    def coverage_note(self) -> str:
        if self.draws <= 0 or not self.boundaries:
            cut = (f"truncation ran at {self.truncations} cut(s)" if self.truncations
                   else "truncation did not run either")
            return ("no perturbation ran: nothing a bar had not yet printed at its open was varied, so a "
                    f"read of a bar's own close, high, low or volume cannot show up here; {cut}")
        levels = "the previous bar's open, close, high and low"
        caveat = ("where the open, or a previous bar that did not trade, does not already decide it and a "
                  "size the tape has made can get there")
        counted = ("checked on the bar as built, and counted only where that bar carried no tie the real bar "
                   "did not print, other than one being set")
        grid = self._grid_clause()
        tie_residual = ("; at a bar that itself printed a tie, a read that changes with whether that tie is "
                        "there is tried only on the draws that happen to keep it")
        if self.draws == 1:
            combos = (f"one draw at each bar, pushing the close past its open and past the farthest of "
                      f"{levels} it could reach, the high and the low past the previous bar's, the "
                      f"volume past the previous bar's, and the next bar's gap and timing, each one way "
                      f"chosen at random ({caveat})" + (f", {grid}" if grid else "") +
                      f", each {counted}, plus a repair draw where that draw fell short of its own plan")
            residual = ("a read that only the other way would flip, a read of a magnitude rather than a "
                        "direction (how far the close is from the open, where it sits within its own range, how far the next bar gaps), "
                        "or of how two relations combine, can still go unseen" + tie_residual)
        else:
            combos = (f"the close pushed both ways past its open; the close, the high and the low each pushed "
                      f"both ways past each of {levels}, and the high above and the low below the bar's own "
                      f"open; the volume both ways past the previous bar's, and to "
                      f"zero and away from it where the tape prints zeros; the next bar's open both ways past "
                      f"this bar's close and open and each of the previous bar's levels, and the next bar early, "
                      f"on time and late where the tape prints each" + self._tie_clause()
                      + (f"; {grid}" if grid else "") + "; and move, wick and volume each both ways")
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
                            + " -- is tried only on the draws that happen to produce it, and can go unseen"
                            + tie_residual)
            else:
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range, how far the next bar gaps), against a level further back than "
                            "the previous bar, of where the close sits between two of the previous bar's levels, "
                            "of an inside or outside range, or of how two directions relate, can still go unseen"
                            + tie_residual)
            combos += (f" ({caveat}), each {counted}, with a repair draw wherever the "
                       f"planned draws fell short")
        stopped = ("; at a bar that diverged, probing stopped at the first divergence"
                   if any(p.evidence.probe == "perturbation" for p in self.proven) else "")
        on_tick = "tick" in self.grids
        largest = ((f"the move of the given sigma {self.sigma:g}" if self.sigma is not None
                    else "the floor-sized move the close was given") if "moves" in self.floors
                   else "the largest move the tape has made")
        far = ""
        if self.beyond_reach:
            far = (f"; at {len(self.beyond_reach)} of the probed bars one of {levels} lay "
                   + (f"out of reach of {largest}, on the tape's price grid," if on_tick
                      else f"at least as far from the open as {largest},")
                   + " and the close was not pushed past that level")
        if "moves" in self.floors:
            far += ("; the tape has made no moves, so the close was pushed by "
                    + (f"the given sigma {self.sigma:g}" if self.sigma is not None else "a floor size")
                    + ", not by a move of its own"
                    + (" (to the point of its price grid nearest that, at least one step from the open and "
                       "on no level)" if on_tick else ""))
        if "volume changes" in self.floors:
            far += ("; the tape's traded volume never changed from one traded bar to the next, so the volume "
                    "was pushed by a floor ratio" + (" (rounded to its lot, and by at least one lot)"
                                                     if "lot" in self.grids else ""))
        if self.undelivered:
            far += (f"; at {len(self.undelivered)} of the probed bars a push listed here could not be made "
                    f"with sizes the tape has made and without a tie the real bar did not print, even on a "
                    f"repair draw, and was not counted")
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
              after: tuple = (None, None),
              grid: tuple = (None, None), no_zero: bool = False) -> list[Any]:
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
    and volume, built by ``_forced_bar``; ``after`` is the NEXT bar's open and time step, where
    ``_next_bar`` set them. ``grid`` is the tape's price and volume grids: a tape printed on a
    grid is rebuilt on it, because a price off the grid is a price no bar of that tape could have
    printed (an eleventh red team's tell). Nothing rebuilt is ever zero or negative, and no volume
    is zero on a tape that never prints one.
    """
    pgrid, vgrid = grid

    def positive(x: float, fallback: float) -> float:
        y = _snap(x, pgrid, "round")
        if y <= 0:
            y = _snap(x, pgrid, "up")
        return y if y > 0 else fallback

    def traded(v: float) -> float:
        y = _snap(v, vgrid, "round")
        if no_zero and y <= 0:
            y = _snap(max(v, 1e-12), vgrid, "up") if vgrid else v
            y = y if y > 0 else (vgrid.step + vgrid.off if vgrid else v)
        return max(y, 0.0)
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
            exact_open, forced_step = after if i == boundary + 1 else (None, None)
            if exact_open is not None:
                opened = exact_open
            else:
                gap = _gap_of(tape, tape[rng.choice(donors)])
                opened = prev_close * math.exp(_jitter(math.log(gap), rng)) if gap > 0 else prev_close
                if opened != prev_close:
                    opened = positive(opened, prev_close)
            ts = prev_ts + (forced_step if forced_step is not None
                            else _step_of(tape, tape[rng.choice(donors)]))
        closed = positive(opened * move_of(i, b), opened)
        hi, lo = max(opened, closed), min(opened, closed)
        d = tape[rng.choice(donors)]
        d_top, d_bot = max(d.open, d.close), min(d.open, d.close)
        up = _jitter(max(0.0, d.high / d_top - 1.0), rng) if d_top > 0 else 0.0
        dn = min(_jitter(max(0.0, 1.0 - d.low / d_bot), rng), 0.99) if d_bot > 0 else 0.0
        low = _snap(lo * (1.0 - dn), pgrid, "down")
        out[i] = _replace(b, ts=ts, open=opened, close=closed, volume=traded(volume_of(i, b)),
                          high=max(_snap(hi * (1.0 + up), pgrid, "up"), hi), low=low if 0 < low <= lo else lo)
        prev_close, prev_ts = closed, ts
    return out


class Grid(NamedTuple):
    """A grid a tape's values sit on: ``off + n * step`` for whole ``n``, and for every grid point
    the tape itself printed, the exact float it printed there -- so a value set on that point is
    bit-for-bit the tape's own, whichever way the tape's floats were produced."""

    step: float
    off: float
    vals: dict


class Sizes(NamedTuple):
    """The sizes the tape itself has printed, each sorted: every bar's move as |log(close/open)|,
    every wick as a fraction of the body end it hangs from, every bar-to-bar volume ratio as
    |log|, every nonzero volume, every opening gap up and down as |log(open/previous close)|,
    every distinct time step -- whether it has printed a zero volume, an ungapped bar and a
    wick of exactly zero -- its price tick and volume lot, where it is printed on a grid -- the
    kinds of tie it prints (a doji; a close, high or low level with a previous-bar level; a
    volume repeated; a next open on a level) -- and its commonest time step. The probed bar,
    and the bar after it, are built from these."""

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
    price_grid: Grid | None = None
    vol_grid: Grid | None = None


def _grid(values: Sequence[float]) -> Grid | None:
    """The coarsest grid every value sits on -- a step of 1, 2, 2.5 or 5 times a power of ten,
    from 5000 down to 1e-8, at the offset the values share -- or None. Tested with a tolerance,
    not read from decimal forms: a tape stored as whole ticks times 0.01 prints 100.19000000000001,
    whose shortest form has fourteen places, and a twelfth red team's such tape was taken for no
    grid at all. The offset matters too: a mid-price tape sits on x.xx5. A step so fine that a
    hundred million of them do not reach the largest value is not tested: the tolerance would admit
    any float there, and float volumes near a thousand were taken for a lot of 1e-8."""
    vals = sorted({v for v in values if v > 0 and math.isfinite(v)})
    if len(vals) < 3:
        return None
    low = vals[0]
    for power in range(-3, 9):
        for mult in (5.0, 2.5, 2.0, 1.0):
            step = mult * 10.0 ** (-power)
            if vals[-1] / step > 1e8:
                return None     # the tolerance below would admit any value at all
            off = low - step * math.floor(low / step + 1e-9)
            if abs(off) < step * 1e-6 or abs(off - step) < step * 1e-6:
                off = 0.0
            points: dict[int, float] = {}
            for v in vals:
                q = (v - off) / step
                n = round(q)
                if abs(q - n) > max(1e-6, abs(q) * 1e-11):
                    break
                points.setdefault(n, v)
            else:
                return Grid(step, off, points)
    return None


def _snap(x: float, grid: Grid | float | None, how: str) -> float:
    """``x`` on the grid: rounded, or up, or down, to a whole step past its offset, and printed as
    the tape printed that point where it did; unchanged off a grid."""
    if grid is None or not math.isfinite(x):
        return x
    if not isinstance(grid, Grid):
        grid = Grid(float(grid), 0.0, {}) if grid else None
        if grid is None:
            return x
    q = (x - grid.off) / grid.step
    n = math.ceil(q - 1e-9) if how == "up" else math.floor(q + 1e-9) if how == "down" else round(q)
    return _point(grid, n)


def _point(grid: Grid, n: int) -> float:
    """Grid point ``n``, as the tape printed it where it did."""
    got = grid.vals.get(n)
    return got if got is not None else grid.off + n * grid.step


def _index(grid: Grid, x: float) -> float:
    return (x - grid.off) / grid.step


def _grid_pick(x: float, grid: Grid | None, lo: float, hi: float,
               lo_in: bool = False, hi_in: bool = False) -> float | None:
    """The point nearest ``x`` in the interval from ``lo`` to ``hi`` -- an end included only where
    flagged, and nothing at or below zero -- that the tape could have printed: a point of its grid,
    exactly as the tape printed it where it did; off a grid, ``x`` itself, or the nearest float
    inside. None where the interval holds no such point."""
    if lo <= 0.0:
        lo, lo_in = 0.0, False
    if grid is None:
        if not (lo < hi or (lo == hi and lo_in and hi_in)):
            return None
        y = x
        if not (y > lo or (lo_in and y == lo)):
            y = lo if lo_in else math.nextafter(lo, math.inf)
        if not (y < hi or (hi_in and y == hi)):
            y = hi if hi_in else math.nextafter(hi, -math.inf)
        inside = (y > lo or (lo_in and y == lo)) and (y < hi or (hi_in and y == hi))
        return y if inside and y > 0 else None
    first = math.ceil(_index(grid, lo) - 1e-9) if lo_in else math.floor(_index(grid, lo) + 1e-9) + 1
    last = None
    if math.isfinite(hi):
        last = math.floor(_index(grid, hi) + 1e-9) if hi_in else math.ceil(_index(grid, hi) - 1e-9) - 1
        if first > last:
            return None
    n = round(_index(grid, x)) if math.isfinite(x) else first
    n = max(n, first) if last is None else min(max(n, first), last)
    y = _point(grid, n)
    return y if y > 0 else None


def _free(x: float, grid: Grid | None, lo: float, hi: float, lo_in: bool, hi_in: bool,
          bad: frozenset = frozenset()) -> float | None:
    """As ``_grid_pick``, stepping past any point in ``bad`` -- a value the strategy could see, which
    nothing aimed at -- to the nearest one inside the interval that is not. Returns the tied point
    only where the interval has no other."""
    y = _grid_pick(x, grid, lo, hi, lo_in, hi_in)
    if y is None or y not in bad:
        return y
    if grid is None:
        for z in (math.nextafter(y, math.inf), math.nextafter(y, -math.inf)):
            if z not in bad and _grid_pick(z, None, lo, hi, lo_in, hi_in) == z:
                return z
        return y
    n = round(_index(grid, y))
    for d in range(1, 9):
        for s in (1, -1):
            z = _point(grid, n + s * d)
            if z not in bad and z > 0 and _grid_pick(z, grid, lo, hi, lo_in, hi_in) == z:
                return z
    return y


def _floor_close(o: float, side: int, sg: float, grid: Grid | None,
                 bad: frozenset = frozenset()) -> float | None:
    """On a tape that has made no moves, the close a push of ``sg`` gives on ``side`` of the open:
    exactly that far off a grid; on one, the grid point nearest it, at least one step from the
    open, and past any point that is a level -- a tie nothing aimed at. None where the grid has
    no such point above zero. The same function sets the close and says what it reaches, so the
    note and the bar cannot disagree -- they did, when a tick nudge carried a floor push past
    levels counted as out of its reach (a twelfth red team)."""
    x = o * math.exp(side * sg)
    if grid is None:
        return x
    n = round(_index(grid, x))
    o_n = round(_index(grid, o))
    n = max(n, o_n + 1) if side > 0 else min(n, o_n - 1)
    for _ in range(64):
        y = _point(grid, n)
        if y <= 0:
            return None
        if y not in bad:
            return y
        n += side
    return None


def _extremes(o: float, sizes: Sizes, sg: float, refs: frozenset) -> tuple[float | None, float | None]:
    """The farthest close each way from open ``o`` a move of the tape's own reaches, on its grid
    (None where not one grid point is within the largest move it has made); on a tape with no
    moves, the floor push each way."""
    if o <= 0:
        return None, None
    pg = sizes.price_grid
    if sizes.moves:
        top = sizes.moves[-1]
        if pg is None:
            return o * math.exp(top * (1.0 - EDGE)), o * math.exp(-top * (1.0 - EDGE))
        cu, cd = o * math.exp(top), o * math.exp(-top)
        return _grid_pick(cu, pg, o, cu, False, True), _grid_pick(cd, pg, cd, o, True, False)
    return _floor_close(o, 1, sg, pg, refs), _floor_close(o, -1, sg, pg, refs)


ALL_TIES = frozenset(("close", "high", "low", "volume", "next_open"))


def _tie_fields(bars: Sequence[Any], k: int, sizes: Sizes) -> frozenset:
    """Which of bar ``k``'s unknowns sit exactly on something the strategy could see at its open:
    the close, high or low on the bar's own open or on a level of the previous bar, the volume on
    the previous bar's, the next bar's open on any of those or on this bar's close. The last only
    where the tape gaps at all: on a tape that never gaps, every next open is its close, and its
    ties are the close's.

    A twelfth red team's evader answered as on real data whenever the probed bar carried a tie
    and read the future otherwise. The probes made ties the real bar never had -- a close snapped
    onto the previous high, a merged repair carrying three ties at once -- at three times the
    rate real bars did, and every draw that flipped the read carried one. So a draw is credited
    with a relation only where it carries no tie the real bar did not print, other than the one
    that relation sets."""
    bar, prev = bars[k], bars[k - 1]
    refs = {bar.open, prev.open, prev.close, prev.high, prev.low}
    out = {name for name in ("close", "high", "low") if getattr(bar, name) in refs}
    if bar.volume == prev.volume:
        out.add("volume")
    if k + 1 < len(bars) and (sizes.gaps_up or sizes.gaps_dn) and bars[k + 1].open in refs | {bar.close}:
        out.add("next_open")
    return frozenset(out)


def _tie_of(rel: tuple, gapped: bool) -> frozenset | None:
    """The fields a relation sets level, or None for a strict relation."""
    kind = rel[0]
    if kind == "move" and rel[1] == 0:
        return frozenset(("close",))
    if kind == "rel" and rel[3] == 0:
        return frozenset((rel[1],))
    if rel == ("vol", 0) or (kind == "zero" and rel[1] is True):
        return frozenset(("volume",))          # a zero is a tie only after a bar that did not trade
    if kind == "next_gap" and rel[1] == 0:
        return frozenset(("next_open",))
    if kind == "next_rel" and rel[2] == 0:
        return frozenset(("next_open",)) if gapped else frozenset(("next_open", "close"))
    return None


def _credited(rels: set, extra: frozenset, gapped: bool) -> set:
    """The relations a draw may be credited with, given ``extra``: the ties it carried that the
    real bar did not."""
    out = set()
    for rel in rels:
        tie = _tie_of(rel, gapped)
        if (not extra) if tie is None else extra <= tie:
            out.add(rel)
    return out


def _tie_kind(item: tuple, gapped: bool) -> str | None:
    """The kind of tie an owed relation sets, for the note."""
    if item == ("move", 0):
        return "doji"
    if item[0] == "rel" and item[3] == 0:
        return "own" if item[2] == "own" else item[1]
    if item == ("vol", 0):
        return "volume"
    if item == ("next_gap", 0):
        return "ungapped" if gapped else None
    if item[0] == "next_rel" and item[2] == 0:
        return "next"
    return None


def _is_tie(plan: Plan) -> bool:
    """A plan that sets something level with its reference."""
    return bool(plan.close_at or plan.vol_eq or any(sd == 0 for _, sd in (*plan.hi_vs, *plan.lo_vs))
                or plan.next_gap == 0 or (plan.next_past is not None and plan.next_past[1] == 0))


def _detail(sigma: float | None, floors: Sequence[str], off_scale: bool = False) -> str:
    """How a proof's varied tape was varied, in words that are true of it."""
    if sigma is None and not floors and not off_scale:
        return "varied at the tape's own scale"
    return ("varied" + (f", with moves of sigma {sigma:g}" if sigma is not None else "")
            + (", with a floor size where the tape has made none" if floors else "")
            + (", with its close one step of the tape's price grid off its open, farther than any move "
               "the tape has made at that price" if off_scale else ""))


def _validate(tape: Sequence[Any]) -> None:
    """Every timestamp, price and volume a number the arithmetic can use. An infinite high crashed
    the builder with a bare math domain error (a twelfth red team); a NaN compares false with
    everything and made relations silently meaningless."""
    for i, b in enumerate(tape):
        for name in ("ts", "open", "high", "low", "close", "volume"):
            v = getattr(b, name)
            try:
                ok = math.isfinite(v)
            except TypeError:
                ok = False
            if not ok:
                raise ValueError(f"bar {i} has a {name} of {v!r}; every timestamp, price and volume "
                                 f"must be a finite number")


def _close_near(level: float, o: float, up: float | None, dn: float | None, sizes: Sizes,
                rng: random.Random, bad: frozenset) -> float | None:
    """A close from which a gap of the tape's own reaches ``level`` exactly: near it, on the side a
    gap the tape has made can come from, within a move of the tape's own, and not on a level. So
    the next bar can open on a level without the close sitting on one too -- a draw carrying two
    ties is a draw an evader keyed on ties could hide behind."""
    if level <= 0:
        return None
    pg = sizes.price_grid
    lo_c = dn if dn is not None else o
    hi_c = up if up is not None else o
    sides = [s for s, pool in ((1, sizes.gaps_up), (-1, sizes.gaps_dn)) if pool]
    rng.shuffle(sides)
    for s in sides:
        pool = sizes.gaps_up if s > 0 else sizes.gaps_dn
        g = _draw_within(pool, 0.0, math.inf, rng) or pool[-1]
        x = level * math.exp(-s * g)
        if s > 0:               # the next bar gaps up onto the level: the close below it
            lo, hi, hi_in = max(lo_c, level * math.exp(-pool[-1])), level, False
            if hi_c < level:
                hi, hi_in = hi_c, True
            c = _free(x, pg, lo, hi, True, hi_in, bad)
        else:
            lo, lo_in, hi = level, False, min(hi_c, level * math.exp(pool[-1]))
            if lo_c > level:
                lo, lo_in = lo_c, True
            c = _free(x, pg, lo, hi, lo_in, True, bad)
        if c is not None and c not in bad and c != level:
            return c
    return None


def _close_toward(plan: Plan, o: float, up: float | None, dn: float | None, levels: Sequence[float],
                  hcons: list, lcons: list, tw: float, tw_dn: float, sizes: Sizes, rng: random.Random,
                  bad: frozenset, notes: dict, reach_need: float | None = None) -> float | None:
    """The probed bar's close where no tie sets it: on the plan's side of the open (a random side
    where it forces none), in a band between the previous bar's levels -- the farthest a move of
    the tape's own reaches, or one at random -- and on the tape's grid STRICTLY inside that band,
    so it never lands on the level it was meant to pass. Snapping to the nearest tick used to put
    a far close exactly on the farthest level, and a one-draw audit counted it delivered (a
    twelfth red team)."""
    pg = sizes.price_grid
    forced = bool(plan.move)
    side = plan.move or rng.choice((1, -1))
    if not sizes.moves:
        # No moves: the floor push, exactly -- a draw that forces none gets it too, one way or the
        # other, never a size drawn wider (a twelfth red team counted those crossing levels the
        # note said were out of reach).
        c = up if side > 0 else dn
        if c is None and not forced:
            c = dn if side > 0 else up
        return c
    cap = up if side > 0 else dn
    if cap is None and not forced:
        side, cap = -side, (dn if side > 0 else up)
    if cap is None:
        if not forced:
            return o
        # Not one step of the grid lies within the largest move the tape has made at this price:
        # pushed one step anyway, and a proof from it says so rather than "at the tape's own scale".
        c = _free(o, pg, o, math.inf, False, False, bad) if side > 0 else _free(o, pg, 0.0, o, False, False, bad)
        if c is not None:
            notes["off_scale"] = True
        return c
    if not forced:
        mag = _draw_within(sizes.moves, 0.0, math.inf, rng) or sizes.moves[0]
        x = o * math.exp(side * mag)
        return _free(x, pg, o, cap, False, True, bad) if side > 0 else _free(x, pg, cap, o, True, False, bad)
    if side > 0:
        edges = [o, *sorted({x for x in levels if x > o}), math.inf]
    else:
        edges = [o, *sorted({x for x in levels if x < o}, reverse=True), 0.0]
    spans = []
    for i in range(len(edges) - 1):
        a, b_ = edges[i], edges[i + 1]
        if side > 0:
            lo, lo_in = a, False
            hi, hi_in = (cap, True) if cap < b_ else (b_, False)
        else:
            hi, hi_in = a, False
            lo, lo_in = (cap, True) if cap > b_ else (b_, False)
        if lo < hi and _grid_pick((lo + hi) / 2.0, pg, lo, hi, lo_in, hi_in) is not None:
            spans.append((lo, hi, lo_in, hi_in))
    if not spans:
        # Every point the move reaches is a level: the nearest of them, a tie the check will see.
        return _free(cap, pg, o, cap, False, True, bad) if side > 0 else _free(cap, pg, cap, o, True, False, bad)
    need = None
    if side > 0:
        # A high held below or level with a level above the open needs the close short of it;
        # a high above or level with one needs the close within a wick of the tape's of it.
        caps = [lv for lv, sd in hcons if sd <= 0 and lv > o]
        if caps:
            m = min(caps)
            spans = [s for s in spans if s[1] < m or (s[1] == m and not s[3])] or spans
        needs = [lv / (1.0 + tw * (1.0 - EDGE)) for lv, sd in hcons if sd >= 0 and lv > o]
        needs += [reach_need] if reach_need is not None else []
        if needs:
            need = max(needs)
            spans = [s for s in spans if s[1] > need or (s[1] == need and s[3])] or spans
    else:
        caps = [lv for lv, sd in lcons if sd >= 0 and lv < o]
        if caps:
            m = max(caps)
            spans = [s for s in spans if s[0] > m or (s[0] == m and not s[2])] or spans
        needs = [lv / (1.0 - tw_dn * (1.0 - EDGE)) for lv, sd in lcons if sd <= 0 and lv < o]
        needs += [reach_need] if reach_need is not None else []
        if needs:
            need = min(needs)
            spans = [s for s in spans if s[0] < need or (s[0] == need and s[2])] or spans
    span = spans[-1] if plan.band == "far" else rng.choice(spans)
    lo, hi, lo_in, hi_in = span
    if need is not None:
        if side > 0 and lo < need <= hi:
            lo, lo_in = need, True
        elif side < 0 and lo <= need < hi:
            hi, hi_in = need, True
        if _grid_pick((lo + hi) / 2.0, pg, lo, hi, lo_in, hi_in) is None:
            lo, hi, lo_in, hi_in = span
    d_lo, d_hi = (math.log(lo / o), math.log(hi / o)) if side > 0 else (math.log(o / hi), math.log(o / lo))
    mag = _draw_within(sizes.moves, d_lo, d_hi, rng)
    x = o * math.exp(side * mag) if mag is not None else (lo + hi) / 2.0
    c = _free(x, pg, lo, hi, lo_in, hi_in, bad)
    return c if c is not None else _free(x, pg, *span, bad)


def _range_of(plan: Plan, hi0: float, lo0: float, o: float, hcons: list, lcons: list, tw: float,
              tw_dn: float, sizes: Sizes, rng: random.Random, bad_h: frozenset,
              bad_l: frozenset) -> tuple[float, float]:
    """The probed bar's high and low: each wick from the tape's own wick sizes, inside the price
    interval its targets need, on the tape's grid, and off every level it was not aimed at."""
    wicks, pg = sizes.wicks, sizes.price_grid

    def interval(body: float, reach: float, upper: bool, cons: list):
        # [lo, hi, lo_in, hi_in] and the tie target, if any
        iv = [body, reach, True, True] if upper else [reach, body, True, True]
        tie, active = None, False
        for level, sd in cons:
            if sd == 0:
                if (body <= level <= reach if upper else reach <= level <= body) and \
                        (level != body or sizes.wick0):
                    tie = level
            elif upper and sd > 0 and level >= body or not upper and sd < 0 and level <= body:
                if upper and (level > iv[0] or (level == iv[0] and iv[2])):
                    iv[0], iv[2] = level, False
                elif not upper and (level < iv[1] or (level == iv[1] and iv[3])):
                    iv[1], iv[3] = level, False
                active = True
            elif upper and sd < 0 and level > body or not upper and sd > 0 and level < body:
                if upper and (level < iv[1] or (level == iv[1] and iv[3])):
                    iv[1], iv[3] = level, False
                elif not upper and (level > iv[0] or (level == iv[0] and iv[2])):
                    iv[0], iv[2] = level, False
                active = True
        if tie is not None:
            return [tie, tie, True, True], tie
        if active and _grid_pick((iv[0] + iv[1]) / 2.0, pg, *iv) is None:
            # targets that cannot all hold, or lie beyond every wick the tape has made: dropped
            iv = [body, reach, True, True] if upper else [reach, body, True, True]
        return iv, None

    H, tie_h = interval(hi0, hi0 * (1.0 + tw), True, hcons)
    L, tie_l = interval(lo0, lo0 * (1.0 - tw_dn), False, lcons)
    # the same intervals as fractions of the body ends; open where only the body or reach bounds them
    a_lo = -math.inf if H[0] == hi0 and H[2] else H[0] / hi0 - 1.0
    a_hi = math.inf if H[1] == hi0 * (1.0 + tw) and H[3] else H[1] / hi0 - 1.0
    b_lo = -math.inf if L[1] == lo0 and L[3] else 1.0 - L[1] / lo0
    b_hi = 0.99 if L[0] == lo0 * (1.0 - tw_dn) and L[2] else 1.0 - L[0] / lo0
    a = tie_h / hi0 - 1.0 if tie_h is not None else _draw_within(wicks, a_lo, a_hi, rng)
    if a is None:
        a = max(a_lo, 0.0) if math.isfinite(a_lo) else 0.0
    bw = 1.0 - tie_l / lo0 if tie_l is not None else _draw_within(wicks, b_lo, b_hi, rng)
    if bw is None:
        bw = max(b_lo, 0.0) if math.isfinite(b_lo) else 0.0
    # The skew, without giving up a range target: lengthen the wick that must be longer or
    # shorten the other -- which one first is a coin toss, so the fix-up biases wick sizes
    # neither up nor down -- each within its own interval (a tie pins its wick). It must hold both
    # as fractions of the body ends and in price units; the lower wick hangs from the lower body
    # end, so in price units it is worth bot/top of the same fraction above.
    ratio = hi0 / lo0
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
    # A zero wick on the open's side puts the high (or low) on the open: a tie with it. Off a grid,
    # where stepping would make a one-ulp wick, a nonzero wick of the tape's own is drawn instead.
    if pg is None and tie_h is None and a == 0.0 and hi0 in bad_h:
        a2 = _draw_within(wicks, max(a_lo, 0.0), min(a_hi, bw / ratio) if plan.wick < 0 else a_hi, rng)
        a = a2 if a2 is not None else a
    if pg is None and tie_l is None and bw == 0.0 and lo0 in bad_l:
        b2 = _draw_within(wicks, max(b_lo, 0.0), min(b_hi, a) if plan.wick > 0 else b_hi, rng)
        bw = b2 if b2 is not None else bw
    high = tie_h if tie_h is not None else _free(hi0 * (1.0 + a), pg, *H, bad_h)
    low = tie_l if tie_l is not None else _free(lo0 * (1.0 - bw), pg, *L, bad_l)
    high = hi0 if high is None else high
    low = lo0 if low is None else low

    def skew_ok(h: float, lw: float) -> bool:
        f = _sign((h / hi0 - 1.0) - (1.0 - lw / lo0))
        return f == _sign((h - hi0) - (lo0 - lw)) == plan.wick

    if pg is not None and plan.wick and not skew_ok(high, low):
        # Rounding both wicks to the grid can even them out: one step on whichever is free to move.
        steps = [(1, 0), (0, 1)] if plan.wick > 0 else [(0, -1), (-1, 0)]
        for _ in range(3):
            if skew_ok(high, low):
                break
            for dh, dl in steps:
                if (dh and tie_h is not None) or (dl and tie_l is not None):
                    continue
                h2 = _point(pg, round(_index(pg, high)) + dh) if dh else high
                l2 = _point(pg, round(_index(pg, low)) + dl) if dl else low
                if dh and (_grid_pick(h2, pg, *H) != h2 or h2 in bad_h):
                    continue
                if dl and (_grid_pick(l2, pg, *L) != l2 or l2 in bad_l):
                    continue
                high, low = h2, l2
                break
            else:
                break
    return high, low


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
    before = two_back = None
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
            if b.high == b.open or b.low == b.open:
                ties.add("own")
            if two_back is not None and b.open in (before.open, two_back.open, two_back.close,
                                                   two_back.high, two_back.low):
                ties.add("next")                 # an open on a level of the bar before, or two back
            step = b.ts - before.ts
            steps[step] = steps.get(step, 0) + 1
        two_back, before = before, b
    prices = [x for b in tape for x in (b.open, b.high, b.low, b.close)]
    mode = max(steps, key=lambda st: (steps[st], -st)) if steps else None
    pg, vg = _grid(prices), _grid([b.volume for b in tape])
    return Sizes(sorted(moves), sorted(wicks), sorted(ratios), sorted(volumes), zero,
                 sorted(gaps_up), sorted(gaps_dn), gap0, sorted(steps), wick0,
                 pg.step if pg else None, vg.step if vg else None, frozenset(ties), mode, pg, vg)


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


def beyond_reach(tape: Sequence[Any], boundary: int, top_move: float | None = None,
                 exact: bool = False, *, sizes: Sizes | None = None, sigma: float | None = None,
                 avoid: frozenset | None = None) -> bool:
    """True when one of the previous bar's levels lies out of the close's reach from bar
    ``boundary``'s open -- further than any move the tape has made gets on its grid (the floor
    push, on a tape with no moves) -- so no probe pushes the close past it. A seventh red team
    showed what pushing it anyway costs: at a bar that gapped 3%, a close forced past the
    previous high was six times the largest move on the tape, and a strategy that fell back
    to a causal rule on any move that size walked. Such a level is left alone and counted.
    ``top_move`` overrides the reach with a size of the caller's (``exact``: a level exactly
    that far is within it)."""
    o = tape[boundary].open
    if o <= 0:
        return False
    if top_move is not None:
        reach = top_move if exact else top_move * (1.0 - EDGE)
        return any(abs(math.log(x / o)) >= reach for x in _levels(tape[boundary - 1]) if x != o)
    sizes = sizes if sizes is not None else _sizes(tape)
    sg = sigma if sigma is not None else realized_sigma(tape)
    if avoid is None:
        avoid = ALL_TIES - _tie_fields(tape, boundary, sizes)
    r = _reach(o, tape[boundary - 1], sizes, sg, avoid)
    return any(not r["past"](x) for x in _levels(tape[boundary - 1]) if x != o)


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
                sizes: Sizes | None = None, avoid: frozenset = ALL_TIES,
                notes: dict | None = None) -> tuple[float, float, float, float]:
    """The probed bar's close, high, low and volume under ``plan``, built only from sizes the
    tape itself has made (a floor where it has made none of a kind) and on its own grids. Returns
    values that satisfy every target that is reachable, each strictly -- or exactly, for a target
    level with a level -- and drops a target the open already decides, or that lies beyond the
    tape's own sizes, rather than force it with a size the tape never printed. A field in
    ``avoid`` -- one the real bar did not print a tie on -- is kept off every level it was not
    aimed at. Anything it could not deliver is caught by ``check_causality``'s check; ``notes``
    records a push it had to make off the tape's scale."""
    b, prev = tape[boundary], tape[boundary - 1]
    o = b.open
    sizes = sizes if sizes is not None else _sizes(tape)
    notes = notes if notes is not None else {}
    wicks, vols = sizes.wicks, sizes.ratios
    vg = sizes.vol_grid
    tw = wicks[-1] if wicks else 0.0
    tw_dn = min(tw, 0.99)                 # a lower wick of a whole body end or more is a zero price
    levels = _levels(prev)
    refs = frozenset(x for x in (o, *levels) if x > 0)
    none: frozenset = frozenset()
    bad_c, bad_h, bad_l = (refs if f in avoid else none for f in ("close", "high", "low"))
    named = {"open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low, "own": o}
    hcons = [(named[n], sd) for n, sd in plan.hi_vs if named[n] > 0] + \
        ([(prev.high, plan.high)] if plan.high and prev.high > 0 else [])
    lcons = [(named[n], sd) for n, sd in plan.lo_vs if named[n] > 0] + \
        ([(prev.low, plan.low)] if plan.low and prev.low > 0 else [])
    up, dn = _extremes(o, sizes, sg, refs)

    # The close: exactly on a level (or its own open) where a tie is the target; near a level
    # where the next bar is to open on it by a gap; otherwise in a band on the move's side.
    closed = None
    if plan.close_at and o > 0:
        target = named.get(plan.close_at, 0.0)
        if target > 0 and (target == o or (sizes.moves and abs(math.log(target / o)) <= sizes.moves[-1])):
            closed = target
    if closed is None and o > 0 and plan.next_past and plan.next_past[1] == 0 and not plan.move and sizes.moves:
        closed = _close_near(named.get(plan.next_past[0], 0.0), o, up, dn, sizes, rng, bad_c)
    # Where the next bar is to open past a level on the move's side, the close goes within a gap of
    # the tape's own of that level: a far close short of it by more than the largest gap left the
    # next open unable to get there, three attempts running.
    reach_need = None
    if plan.next_past and plan.next_past[1] and plan.move == plan.next_past[1]:
        level = named.get(plan.next_past[0], 0.0)
        if level > 0 and (level - o) * plan.move > 0:
            pool = sizes.gaps_up if plan.move > 0 else sizes.gaps_dn
            reach_need = level * math.exp(-plan.move * (pool[-1] if pool else 0.0) * (1.0 - EDGE))
    if closed is None and o > 0:
        closed = _close_toward(plan, o, up, dn, levels, hcons, lcons, tw, tw_dn, sizes, rng, bad_c, notes,
                               reach_need)
    if closed is None or not closed > 0:
        closed = o

    hi0, lo0 = max(o, closed), min(o, closed)
    if lo0 > 0:
        high, low = _range_of(plan, hi0, lo0, o, hcons, lcons, tw, tw_dn, sizes, rng, bad_h, bad_l)
    else:                                        # a tape that prints a zero price: left as it was
        high, low = max(hi0, b.high), min(lo0, b.low)

    # The volume: to the chosen side of the previous bar's, by one of the tape's own ratios --
    # against zero as well, where the tape prints zeros -- or exactly the previous bar's, where
    # the tape repeats volumes; on the tape's own lot, and never zero on a tape that never
    # prints a zero (snapping to a lot of 100 did, a twelfth red team).
    pv = prev.volume
    bad_v = frozenset((pv,)) if "volume" in avoid else none

    def natural() -> float:
        if b.volume <= 0 and sizes.zero:
            return 0.0
        base = b.volume if b.volume > 0 else (rng.choice(sizes.volumes) if sizes.volumes else 1.0)
        v = _free(base * math.exp(rng.gauss(0.0, 0.25)), vg, 0.0, math.inf, False, False, bad_v)
        return v if v is not None else base

    volume = None
    if plan.vol_eq and pv > 0:
        volume = pv
    elif plan.volume < 0 and (plan.zero or pv <= 0):
        volume = 0.0                           # nothing is below an untraded bar but another one
    elif plan.volume and pv > 0:
        r = _draw_within(vols, 0.0, math.inf, rng)
        x = pv * math.exp(plan.volume * (r if r is not None else 0.25))
        volume = _grid_pick(x, vg, pv, math.inf) if plan.volume > 0 else _grid_pick(x, vg, 0.0, pv)
        if volume is None and plan.volume < 0 and sizes.zero:
            volume = 0.0
    elif plan.volume > 0 and sizes.volumes:  # above an untraded bar: a volume the tape has printed
        volume = _grid_pick(_jitter(rng.choice(sizes.volumes), rng), vg, 0.0, math.inf)
    if volume is None:
        volume = natural()
    return closed, high, low, volume


def _next_bar(plan: Plan, sizes: Sizes, rng: random.Random, closed: float | None = None,
              refs: dict | None = None, tape: Sequence[Any] | None = None,
              avoid: frozenset = frozenset()) -> tuple[float | None, float | None]:
    """The open and the time step of the bar after the probed one, where the plan forces them --
    or where the tape's own gap would land its open on a tie the real next bar did not print --
    and None for whatever the rebuild may draw freely. The open: past this bar's close by a gap
    of the tape's own, on the chosen side; carried past a reference by a gap sized to get there;
    exactly on one, by a gap of the tape's own size, where the tape gaps at all; or ungapped,
    where the tape prints ungapped bars. The step: the tape's commonest, or one shorter (early) or
    longer (late) than that. A tenth red team read the next bar's gap and lateness at a single
    bar; an eleventh showed "on time" meant the tape's SHORTEST step; a twelfth that the next open
    was owed on a level only through an ungapped bar."""
    pg = sizes.price_grid
    pools = {1: sizes.gaps_up, -1: sizes.gaps_dn}
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    refs = refs or {}
    opened = None
    if closed is not None and closed > 0:
        bad = (frozenset(v for v in refs.values() if v > 0) | {closed}
               if gapped and "next_open" in avoid else frozenset())

        def gap_open(side: int, g: float, beyond: float) -> float | None:
            # an open strictly past ``beyond`` on ``side``, a gap of about g, no larger than the largest
            far = closed * math.exp(side * pools[side][-1])
            x = closed * math.exp(side * g)
            return (_free(x, pg, beyond, far, False, True, bad) if side > 0
                    else _free(x, pg, far, beyond, True, False, bad))

        if plan.next_past is not None:
            ref, side = plan.next_past
            level = refs.get(ref, 0.0)
            if level > 0 and side == 0:
                need = math.log(level / closed)
                if need == 0.0:
                    opened = closed if sizes.gap0 else None
                else:
                    pool = pools[1 if need > 0 else -1]
                    if pool and abs(need) <= pool[-1] * (1.0 + 1e-12) + 1e-15:
                        opened = level
            elif level > 0:
                pool = pools[side]
                past = side * math.log(level / closed)   # how far the gap must carry the open (<0: already past)
                if pool:
                    g = (_draw_within(pool, 0.0, math.inf, rng) or pool[0]) if past < 0 else \
                        _draw_within(pool, past, math.inf, rng)
                    if g is not None:
                        opened = gap_open(side, g, max(closed, level) if side > 0 else min(closed, level))
                elif past < 0 and sizes.gap0:
                    opened = closed                      # already past it: an ungapped open is past too
        elif plan.next_gap is not None:
            if plan.next_gap == 0:
                opened = closed if sizes.gap0 else None
            elif pools[plan.next_gap]:
                pool = pools[plan.next_gap]
                opened = gap_open(plan.next_gap, _draw_within(pool, 0.0, math.inf, rng) or pool[-1], closed)
        if opened is None and bad and tape is not None and len(tape) > 1:
            # Free: the tape's own gap from a random donor, as the rebuild draws it -- but an open
            # that lands on a tie the real next bar did not print is drawn again from its gaps.
            gap = _gap_of(tape, tape[rng.choice(range(1, len(tape)))])
            x = closed * math.exp(_jitter(math.log(gap), rng)) if gap > 0 else closed
            y = closed if x == closed else _grid_pick(x, pg, 0.0, math.inf)
            if y is None or y in bad:
                sides = [s for s in (1, -1) if pools[s]]
                if sides:
                    s = rng.choice(sides)
                    y = gap_open(s, _draw_within(pools[s], 0.0, math.inf, rng) or pools[s][-1], closed)
            opened = y
    step = None
    if plan.next_step is not None and sizes.step_mode is not None:
        mode = sizes.step_mode
        side = [st for st in sizes.steps if (st > mode if plan.next_step > 0 else st < mode)]
        step = mode if plan.next_step == 0 else (rng.choice(side) if side else None)
    return opened, step


def _perturbed(tape: Sequence[Any], boundary: int, seed: int, sigma: float | None,
               signs: tuple[int, int, int] = (0, 0, 0), plan: Plan | None = None,
               sizes: Sizes | None = None, avoid: frozenset | None = None,
               notes: dict | None = None) -> list[Any]:
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
    was a probe a strategy could recognise and wait out. ``avoid`` names the fields kept off
    ties nothing aimed at -- by default every field the real bar printed no tie on.
    """
    rng = random.Random(seed)
    sg = realized_sigma(tape) if sigma is None else sigma
    explicit = plan is not None
    if plan is None:
        m, w, v = signs
        plan = Plan(m, w, v, "far", m, m)
    sizes = sizes if sizes is not None else _sizes(tape)
    if avoid is None:
        avoid = ALL_TIES - _tie_fields(tape, boundary, sizes)
    # An audit's draws always build the probed bar from the tape's own sizes, even a draw that
    # forces nothing about it; only the bare continuation-style call rebuilds it like any bar.
    first = _forced_bar(tape, boundary, rng, plan, sg, sizes, avoid, notes) if (plan.forced or explicit) else None
    prev = tape[boundary - 1]
    refs = {"own": tape[boundary].open, "open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low}
    after = _next_bar(plan, sizes, rng, first[0] if first else None, refs, tape, avoid)
    return _rethread(tape, boundary, rng,
                     lambda i, b: math.exp(rng.gauss(0.0, sg)),
                     _volume_of(tape, rng, sizes),
                     first=first, after=after, grid=(sizes.price_grid, sizes.vol_grid),
                     no_zero=not sizes.zero)


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
    # and the high and the low against the bar's own open: level with it, or not
    got.add(("rel", "high", "own", _sign(bar.high - o)))
    got.add(("rel", "low", "own", _sign(bar.low - o)))
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


def _reach(o: float, p: Any, sizes: Sizes, sg: float, avoid: frozenset = ALL_TIES) -> dict:
    """What the tape's own sizes can do from open ``o`` against previous bar ``p``, computed from
    the same extremes the probed bar is built from, on the tape's grid, and never counting a point
    that is a level where the field it is for must stay off levels (``avoid``: the fields the real
    bar printed no tie on): no draw gets STRICTLY past a level that sits exactly at the largest
    size, or past one whose only reachable grid points are levels. On a tape with no moves the
    close moves by the floor push exactly, and reach is measured against that."""
    pg = sizes.price_grid
    refs = frozenset(x for x in (o, *_levels(p)) if x > 0)
    none: frozenset = frozenset()
    bad_c, bad_h, bad_l = (refs if f in avoid else none for f in ("close", "high", "low"))
    up, dn = _extremes(o, sizes, sg, refs)
    tw = sizes.wicks[-1] if sizes.wicks else 0.0
    tw_dn = min(tw, 0.99)
    e = 1.0 - EDGE if pg is None else 1.0
    top = up if up is not None else o
    bottom = dn if dn is not None else o
    hmax, lmin = top * (1.0 + tw * e), bottom * (1.0 - tw_dn * e)
    gu = math.exp(sizes.gaps_up[-1] * e) if sizes.gaps_up else 1.0
    gd = math.exp(-sizes.gaps_dn[-1] * e) if sizes.gaps_dn else 1.0

    def room(lo: float, hi: float, lo_in: bool, hi_in: bool, bad: frozenset = refs) -> bool:
        """A point the tape could print there that is none of ``bad``."""
        if not (lo < hi or (lo == hi and lo_in and hi_in)):
            return False
        y = _free((lo + hi) / 2.0, pg, lo, hi, lo_in, hi_in, bad)
        return y is not None and y not in bad

    def above(level: float, ceiling: float, bad: frozenset = refs) -> bool:
        return room(level, ceiling, False, True, bad)

    def below(level: float, floor_: float, bad: frozenset = refs) -> bool:
        return room(floor_, level, True, False, bad)

    up_ok = up is not None and room(o, up, False, True, bad_c)
    dn_ok = dn is not None and room(dn, o, True, False, bad_c)
    cap_w, floor_w = o * (1.0 + tw * e), o * (1.0 - tw_dn * e)
    # A wick at all, on the grid: the upper one from the highest body top a move reaches, the lower
    # one from the open, the highest lower body end there is. At a penny on a one-cent tick neither
    # exists, and a skew was owed that no draw could make (a twelfth red team's penny tape).
    upper_wick = tw > 0 and room(top, top * (1.0 + tw * e), False, True, none)
    lower_wick = tw > 0 and room(floor_w, o, True, False, none)

    def high_below(level: float) -> bool:
        """The high strictly below a level above the open, with nothing on a level that must not
        be: a down close and an upper wick short of it; an up close short of it and no upper wick;
        or, where the real bar's high sat on a level itself, a down close and no wick at all."""
        if level <= o:
            return False
        if dn_ok and tw > 0 and room(o, min(level, cap_w), False, cap_w < level, bad_h):
            return True
        if sizes.wick0 and up_ok and (room(o, min(level, top), False, top < level, bad_c | bad_h)
                                      if sizes.moves else up < level):
            return True
        return "high" not in avoid and sizes.wick0 and dn_ok

    def low_above(level: float) -> bool:
        if level >= o:
            return False
        if up_ok and tw > 0 and room(max(level, floor_w), o, floor_w > level, False, bad_l):
            return True
        if sizes.wick0 and dn_ok and (room(max(level, bottom), o, bottom > level, False, bad_c | bad_l)
                                      if sizes.moves else dn > level):
            return True
        return "low" not in avoid and sizes.wick0 and up_ok

    def inside() -> bool:
        """The range strictly inside the previous bar's: the close inside it and off the levels, and
        each wick short of the previous extreme on its side."""
        if not (p.low > 0 and p.low < o < p.high):
            return False
        high_ok = (tw > 0 and room(o, min(p.high, cap_w), False, cap_w < p.high, bad_h)) or \
            ("high" not in avoid and sizes.wick0)
        low_ok = (tw > 0 and room(max(p.low, floor_w), o, floor_w > p.low, False, bad_l)) or \
            ("low" not in avoid and sizes.wick0)
        close_up = up_ok and (room(o, min(p.high, top), False, top < p.high, bad_c) if sizes.moves else up < p.high)
        close_dn = dn_ok and (room(max(p.low, bottom), o, bottom > p.low, False, bad_c) if sizes.moves else dn > p.low)
        return (close_up and (sizes.wick0 or tw > 0) and low_ok) or (close_dn and high_ok and (sizes.wick0 or tw > 0))

    def high_on(level: float) -> bool:
        if level == o:
            return sizes.wick0 and dn_ok          # a down close and no upper wick
        if level < o:
            return False
        if sizes.moves:
            return level <= hmax
        # no moves: the body tops a draw can have are the floor close up, and the open
        return any(h is not None and h <= level <= h * (1.0 + tw * e) and (level > h or sizes.wick0)
                   for h in (up if up_ok else None, o if dn_ok else None))

    def low_on(level: float) -> bool:
        if level == o:
            return sizes.wick0 and up_ok
        if level > o:
            return False
        if sizes.moves:
            return level >= lmin
        return any(lw is not None and lw * (1.0 - tw_dn * e) <= level <= lw and (level < lw or sizes.wick0)
                   for lw in (dn if dn_ok else None, o if up_ok else None))

    def next_on(level: float) -> bool:
        """A close near ``level`` from which a gap of the tape's own lands the next open on it."""
        for s, pool in ((1, sizes.gaps_up), (-1, sizes.gaps_dn)):
            if not pool:
                continue
            if s > 0:
                lo, hi, hi_in = max(bottom, level * math.exp(-pool[-1])), level, False
                if top < level:
                    hi, hi_in = top, True
                if room(lo, hi, True, hi_in):
                    return True
            else:
                lo, lo_in, hi = level, False, min(top, level * math.exp(pool[-1]))
                if bottom > level:
                    lo, lo_in = bottom, True
                if room(lo, hi, lo_in, True):
                    return True
        return False

    vol_dn = p.volume > 0 and (sizes.zero or _grid_pick(p.volume / 2.0, sizes.vol_grid, 0.0, p.volume) is not None)
    return {
        "up": up_ok,
        "dn": dn_ok,
        "up_close": up,
        "dn_close": dn,
        "wick": tw > 0,
        "wick_up": upper_wick,
        "wick_dn": lower_wick,
        "high_off_open": up_ok or (tw > 0 and room(o, cap_w, False, True, none)),
        "low_off_open": dn_ok or lower_wick,
        "vol_dn": vol_dn,
        "past": lambda level: (level > o and up_ok and above(level, up, bad_c))
        or (level < o and dn_ok and below(level, dn, bad_c)),
        "on": lambda level: level == o or (bool(sizes.moves) and abs(math.log(level / o)) <= sizes.moves[-1]),
        "high_above": lambda level: o > level or above(level, hmax, bad_h),
        "low_below": lambda level: o < level or below(level, lmin, bad_l),
        "high_below": high_below,
        "low_above": low_above,
        "inside": inside(),
        "high_on": high_on,
        "low_on": low_on,
        "next_above": lambda level: above(level, top * gu),
        "next_below": lambda level: below(level, bottom * gd),
        "next_on": next_on,
        "outside_up": (o > p.high or above(p.high, hmax, bad_h)) and (o < p.low or below(p.low, floor_w, bad_l)),
        "outside_dn": (o < p.low or below(p.low, lmin, bad_l)) and (o > p.high or above(p.high, cap_w, bad_h)),
    }


def _owed(tape: Sequence[Any], k: int, sizes: Sizes, plans: Sequence[Plan], sg: float,
          avoid: frozenset = ALL_TIES) -> set:
    """What the coverage note claims was pushed at bar ``k`` -- every unknown of the bar, and
    the next bar's open and time, against everything the strategy could see at the open, above
    it, below it and level with it -- limited to what the open leaves open, to the ties this
    tape prints, and to what the tape's own sizes can reach: the note's own qualification.
    Enumerated, not collected: ten red teams found relations one at a time, an eleventh found
    the ties, the next open against the previous bar, and an early clock, and a twelfth the high
    and low level with the bar's own open, the next open on a level by a gap, and a one-draw
    audit whose far close and range pushes were claimed and never checked."""
    b, p = tape[k], tape[k - 1]
    o = b.open
    if not plans or o <= 0:
        return set()
    r = _reach(o, p, sizes, sg, avoid)
    ties = sizes.ties
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    vol_up = p.volume > 0 or bool(sizes.volumes)
    has_next = k + 1 < len(tape)
    owed: set = set()
    if len(plans) == 1:
        pl = plans[0]
        m = pl.move
        if m and r["up" if m > 0 else "dn"]:
            owed.add(("move", m))
            for name in LEVELS:              # past the farthest of the levels it reaches
                level = getattr(p, name)
                if level > 0 and (level - o) * m > 0 and r["past"](level):
                    owed.add(("rel", "close", name, m))
        if pl.wick and r["wick_up" if pl.wick > 0 else "wick_dn"]:
            owed.add(("wick", pl.wick))
        if pl.volume > 0 and vol_up or pl.volume < 0 and r["vol_dn"]:
            owed.add(("vol", pl.volume))
        if p.high > 0:
            if pl.high > 0 and r["high_above"](p.high):
                owed.add(("rel", "high", "high", 1))
            if pl.high < 0 and r["high_below"](p.high):
                owed.add(("rel", "high", "high", -1))
        if p.low > 0:
            if pl.low < 0 and r["low_below"](p.low):
                owed.add(("rel", "low", "low", -1))
            if pl.low > 0 and r["low_above"](p.low):
                owed.add(("rel", "low", "low", 1))
        if has_next:
            if pl.next_gap and (sizes.gaps_up if pl.next_gap > 0 else sizes.gaps_dn):
                owed.add(("next_gap", pl.next_gap))
            if pl.next_step is not None and sizes.step_mode is not None and (
                    pl.next_step == 0 or any((st > sizes.step_mode) if pl.next_step > 0 else (st < sizes.step_mode)
                                             for st in sizes.steps)):
                owed.add(("next_step", pl.next_step))
        return owed
    if r["up"]:
        owed.add(("move", 1))
    if r["dn"]:
        owed.add(("move", -1))
    if "doji" in ties:
        owed.add(("move", 0))
    if r["wick_up"]:
        owed.add(("wick", 1))
    if r["wick_dn"]:
        owed.add(("wick", -1))
    if vol_up:
        owed.add(("vol", 1))
    if r["vol_dn"]:
        owed.add(("vol", -1))                # nothing is below a bar that did not trade
    if p.volume > 0 and "volume" in ties:
        owed.add(("vol", 0))
    if sizes.zero:
        owed |= {("zero", True), ("zero", False)}
    if r["up"] or r["dn"]:
        for name in LEVELS:
            level = getattr(p, name)
            if level <= 0:
                continue
            if level != o:
                if r["dn" if level > o else "up"]:
                    owed.add(("rel", "close", name, -1 if level > o else 1))     # the close short of it
                if r["past"](level):
                    owed.add(("rel", "close", name, 1 if level > o else -1))
                if "close" in ties and r["on"](level):
                    owed.add(("rel", "close", name, 0))
            elif "doji" in ties:
                owed.add(("rel", "close", name, 0))                              # on it: a doji
            if r["high_above"](level):
                owed.add(("rel", "high", name, 1))
            if r["high_below"](level):
                owed.add(("rel", "high", name, -1))
            if ("own" if level == o else "high") in ties and r["high_on"](level):
                owed.add(("rel", "high", name, 0))
            if r["low_below"](level):
                owed.add(("rel", "low", name, -1))
            if r["low_above"](level):
                owed.add(("rel", "low", name, 1))
            if ("own" if level == o else "low") in ties and r["low_on"](level):
                owed.add(("rel", "low", name, 0))
        # the high and the low against the bar's own open
        if r["high_off_open"]:
            owed.add(("rel", "high", "own", 1))
        if r["low_off_open"]:
            owed.add(("rel", "low", "own", -1))
        if "own" in ties and r["high_on"](o):
            owed.add(("rel", "high", "own", 0))
        if "own" in ties and r["low_on"](o):
            owed.add(("rel", "low", "own", 0))
    if has_next:
        if sizes.gaps_up:
            owed.add(("next_gap", 1))
        if sizes.gaps_dn:
            owed.add(("next_gap", -1))
        if sizes.gap0:
            owed.add(("next_gap", 0))
        if r["up"] or r["dn"]:
            for ref in NEXT_REFS:
                level = o if ref == "own" else getattr(p, ref)
                if level <= 0:
                    continue
                if r["next_above"](level):
                    owed.add(("next_rel", ref, 1))
                if r["next_below"](level):
                    owed.add(("next_rel", ref, -1))
                if gapped:
                    # on a tape that gaps, the next open on a level by a gap of its own, from a
                    # close near it -- one tie, not the close's and the open's together
                    if "next" in ties and sizes.moves and r["next_on"](level):
                        owed.add(("next_rel", ref, 0))
                elif sizes.gap0 and (("doji" in ties) if level == o else ("close" in ties and r["on"](level))):
                    owed.add(("next_rel", ref, 0))
        if sizes.step_mode is not None:
            owed.add(("next_step", 0))
            if any(st > sizes.step_mode for st in sizes.steps):
                owed.add(("next_step", 1))
            if any(st < sizes.step_mode for st in sizes.steps):
                owed.add(("next_step", -1))
    if len(plans) >= 4:
        # Inside the previous range: always within reach of a small move of the tape's own; on
        # a tape with no moves the close moves by the floor push exactly, and only where that
        # stays inside on one side or the other.
        if r["inside"]:
            owed.add(("range", "inside"))
        if p.low > 0 and ((r["outside_up"] and r["up"]) or (r["outside_dn"] and r["dn"])):
            owed.add(("range", "outside"))
        if r["wick"]:
            for pl in plans[:8]:
                if not r["up" if pl.move > 0 else "dn"] or not r["wick_up" if pl.wick > 0 else "wick_dn"]:
                    continue
                if pl.volume > 0 and not vol_up or pl.volume < 0 and not r["vol_dn"]:
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
    for what it actually delivered, so it cannot make a claim true that is not. A draw that sets
    a tie merges with nothing: a draw carrying two ties is credited with neither, and a twelfth
    red team's evader hid behind merged repairs carrying a doji, a high tie and a low tie at once."""
    if _is_tie(a) or _is_tie(b):
        return None
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
    # pointing the same way.
    for side_slot in ("high", "low"):
        owner = b if any(getattr(b, n) != _UNSET[n] for n in _SLOTS[side_slot]) else a
        other = a if owner is b else b
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
        o = tape[k].open
        level = o if name == "own" else getattr(tape[k - 1], name)
        wick_reach = sizes.wicks[-1] if sizes.wicks else 0.0
        if field == "high":
            if side == 0:
                # Level with a level and nothing else level: on the open, from a down close with
                # no upper wick; above it, from a close short of it with the wick reaching up to
                # it -- the close moved up toward it, or on a second try moved down.
                if level == o:
                    return Plan(move=-1, band="random", hi_vs=((name, 0),))
                mv = -1 if attempt % 2 and level / o - 1.0 <= wick_reach else 1
                return Plan(move=mv, band="random", hi_vs=((name, 0),))
            return Plan(move=1 if side > 0 else -1, band="far" if side > 0 else "random", hi_vs=((name, side),))
        if side == 0:
            if level == o:
                return Plan(move=1, band="random", lo_vs=((name, 0),))
            mv = 1 if attempt % 2 and 1.0 - level / o <= min(wick_reach, 0.99) else -1
            return Plan(move=mv, band="random", lo_vs=((name, 0),))
        return Plan(move=-1 if side < 0 else 1, band="far" if side < 0 else "random", lo_vs=((name, side),))
    if kind == "next_gap":
        return Plan(next_gap=item[1])
    if kind == "next_step":
        return Plan(next_step=item[1])
    if kind == "next_rel":
        _, ref, side = item
        if side == 0:
            # by a gap of the tape's own where it gaps, the close near the level; else ungapped,
            # the close on it
            return Plan(next_past=(ref, 0)) if (sizes.gaps_up or sizes.gaps_dn) else Plan(close_at=ref, next_gap=0)
        # the close past the level, and the next gap sized to carry the open past it too
        return Plan(move=side, band="far", next_past=(ref, side))
    if kind == "range" and item[1] == "inside":
        b, p = tape[k], tape[k - 1]
        if not sizes.moves and p.low > 0:    # a floor push: toward a side where it stays inside
            r = _reach(b.open, p, sizes, sg)
            coin = 1 if r["up"] and r["up_close"] < p.high else -1
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
    _validate(tape)
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
    sg = realized_sigma(tape) if sigma is None else sigma
    mode = sizes.step_mode
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    # Reach is measured from the same extremes the probed bar is built from: the tape's largest
    # move on its grid, or on a tape with no moves the floor push, exactly.
    far = tuple(k for k in pert_bounds if draws and beyond_reach(tape, k, sizes=sizes, sigma=sg))
    floors = tuple(name for name, pool in (("moves", sizes.moves), ("volume changes", sizes.ratios))
                   if draws and pert_bounds and not pool)
    short: list[int] = []
    repairs = 0
    tie_kinds: set = set()

    def probe(k: int, plan: Plan, salt: int, avoid: frozenset) -> tuple[list[Any], bool]:
        nonlocal runs
        notes: dict = {}
        varied = _perturbed(tape, k, seed=nonce ^ (k * 1_000_003 + salt), sigma=sg, plan=plan, sizes=sizes,
                            avoid=avoid, notes=notes)
        variant = list(strategy(varied))
        runs += 1
        idx = _first_disagreement(full, variant, k + 1)
        if idx is not None:
            d = Divergence(index=idx, boundary=k, baseline=full[idx], variant=variant[idx],
                           probe="perturbation", detail=_detail(sigma, floors, bool(notes.get("off_scale"))))
            candidates.append((d, (lambda v=varied: list(strategy(v))), variant))
            return varied, True
        return varied, False

    for k in pert_bounds:
        plans = draw_plans(nonce, k, draws, zero=sizes.zero)
        got: set = set()
        diverged = False
        # A draw is credited only with what it delivered on a bar carrying no tie the real bar did
        # not print, other than the one being set: an evader that answered as on real data
        # whenever a tie showed hid behind the ties the probes made (a twelfth red team).
        real = _tie_fields(tape, k, sizes)
        avoid = ALL_TIES - real
        if gapped and "next_open" in avoid:
            # The real next bar gapped, so an ungapped next open is a tie it did not print: a planned
            # draw forcing one would be credited with nothing else. Its next open is left free; the
            # ungapped open gets a draw of its own, where it is owed.
            plans = [dataclasses.replace(pl, next_gap=None) if pl.next_gap == 0 else pl for pl in plans]

        def credit(varied: list[Any]) -> set:
            return _credited(_delivered(varied, k, tape[k - 1], mode), _tie_fields(varied, k, sizes) - real, gapped)

        for d_i, plan in enumerate(plans):
            varied, diverged = probe(k, plan, d_i, avoid)
            if diverged:
                break
            got |= credit(varied)
        if diverged or not plans:
            continue
        # Checked, not assumed: whatever the note will claim for this bar and the planned draws
        # did not deliver gets a draw of its own, three at most; what still fails is disclosed.
        owed = _owed(tape, k, sizes, plans, sg, avoid)
        tie_kinds |= {t for t in (_tie_kind(x, gapped) for x in owed) if t}
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
            varied, diverged = probe(k, plan, 1_000 + repairs, avoid)
            if diverged:
                break
            got |= credit(varied)
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
                floors=floors,
                ties=tuple(t for t in ("doji", "close", "high", "low", "own", "volume", "ungapped", "next")
                           if t in tie_kinds),
                grids=tuple(g for g, found in (("tick", sizes.price_grid), ("lot", sizes.vol_grid)) if found))

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
