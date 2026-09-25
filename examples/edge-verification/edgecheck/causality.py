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
import collections
import dataclasses
import math
import numbers
import operator
import os
import random
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, NamedTuple, Protocol, Sequence

__all__ = ["Bar", "Strategy", "Divergence", "Proven", "Suspected", "Report",
           "check_causality", "default_boundaries", "sparse_boundaries", "continuation", "sign_design",
           "draw_plans", "Plan", "beyond_reach",
           "realized_sigma", "DEFAULT_DRAWS", "SIGMA_FLOOR", "MIN_BOUNDARY"]

DEFAULT_DRAWS = 2
SIGMA_FLOOR = 0.002
JITTER = 0.1
VOLUME_JITTER = 0.05   # a rebuilt volume is its donor's, off its exact value by about this much in log
EDGE = 1e-9   # a level exactly as far as the tape's largest size is out of reach, not within it
MIN_BOUNDARY = 4
MAX_DRAWS = 1000   # draws past a bar's eighth repeat its covering design's sign combinations at random


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
    beyond_gridded: tuple[int, ...] | None = None   # those beyond_reach bars measured on a price grid
    calendar: bool = False              # whether the tape keeps a calendar its rebuilt bars were set on

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
        if "edge" in self.ties:
            words.append("a next open on the bar's own high or low")
        return ("; each of these also set level with its reference where the tape prints that kind of "
                "tie -- " + ", ".join(words))

    def _tail_clause(self) -> str:
        """What the rebuilt tail keeps of the tape, and what that leaves unvaried. A sixteenth red team:
        donors from anywhere gave the tail the whole tape's level and clock; from near the bar they keep
        its level -- and so vary it less. A seventeenth: the clause said the tail kept the tape's times
        of day on tapes with no calendar, and its volatility under a caller's sigma. An eighteenth: a tail
        of mostly the tape's own later bars, reordered, ends nearer the tape's own end than a walk of its
        own would."""
        where = (", at the times of day and on the days it prints" if self.calendar
                 else ", their times, after the next bar's, stepping as those bars' own do")
        level = ("a read of the level of volume further ahead, which stays near the tape's own there, of "
                 + ("volatility, which is the given sigma's, the tape having made no moves" if "moves" in self.floors
                    else "volatility, which is the given sigma's where the tape moves -- its gaps kept at the tape's "
                         "own sizes -- and still where it is still")
                 if self.sigma is not None else
                 "a read of the level of volume or volatility further ahead, which stays near the tape's own there")
        # under a caller's sigma the far end can stray farther than a walk at that sigma (the tape's own
        # drift, rescaled): said as what it is (a nineteenth red team)
        level += (", of where the price is far ahead, which the rebuild takes from the tape's own moves near it, net "
                  "of the trend they follow and rescaled to the sigma (normal draws of it, where they are all trend)"
                  if self.sigma is not None else
                  ", of where the price is far ahead, which strays from the tape's own less than a walk of its own would")
        when = ", or of a later bar's exact date" if self.calendar else ", or of when a later bar comes"
        return f"; later bars are rebuilt from the tape's own bars near them{where}, so {level}{when}, can go unseen"

    def _grid_clause(self) -> str:
        tick = ("prices on the tick the tape prints at their price level and time" if "eras" in self.grids
                else "prices on the tape's own tick")
        parts = ([tick] if "tick" in self.grids else []) + \
            ([("volumes on the lot found in it" if "tick" in self.grids else "volumes on the lot found in the tape")]
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
                        "there is tried only on the draws that happen to keep it" + self._tail_clause())
        # a fourteenth red team: mid prices printed as 49.974999999999994 and 49.975 alike; a fifteenth's
        # tape had plain prices and two feeds' volumes, and was told its prices were the ones
        unspelled = [f for f, flag in (("prices", "unspelled"), ("volumes", "unspelled volumes")) if flag in self.grids]
        if unspelled:
            what = " and ".join(unspelled)
            one = "a price or volume" if len(unspelled) == 2 else "a " + unspelled[0][:-1]
            tie_residual += (f"; the tape's {what} are not written as plain roundings, so {one} it never "
                             "printed is written as its grid's own sum, which the tape's own arithmetic may "
                             "spell differently in the last bits of the float, and a read of that spelling "
                             "can go unseen")
        if "tick" in self.grids:
            # the rules a tape's prices were found to follow are the ones the probe keeps; a fifteenth
            # red team found four it did not -- float32 storage, a tick changed in time, a split's
            # adjustment, a spread table's sparse top -- and each is now found; others may not be
            tie_residual += ("; prices are set on the grid found in the tape itself -- by price level, by era "
                             "where its tick changed in time, split-adjusted where it sits on an adjusted tick, "
                             "and at float32 where it is stored so -- and a read of whether a price is one the "
                             "data source could print under a rule not found in the tape (an adjustment by a "
                             "factor other than the split ratios it tries, or to a tick finer than three of the "
                             "places the tape writes, a change of tick lasting under twenty bars or at some price "
                             "levels only) can go unseen")
        else:
            # a twentieth red team: with no grid found, the note said nothing of the prices at all
            tie_residual += ("; no price grid was found in the tape, so a rebuilt price is any float, and a read "
                             "of whether a price is one the data source could print can go unseen")
        tie_residual += ("; no bar of the tape traded, and no rebuilt bar does" if "no trades" in self.floors else
                         "; volumes are set on the lot found in the tape, and a read of whether a volume is one "
                         "the data source could print under a lot or rule not found there can go unseen"
                         if "lot" in self.grids else
                         "; no volume lot was found in the tape, so a rebuilt volume is any float, and a read of "
                         "whether a volume is one the data source could print can go unseen")
        if self.draws == 1:
            combos = (f"one draw at each bar, pushing the close past its open and past the farthest of "
                      f"{levels} it could reach, the high and the low past the previous bar's, the "
                      f"volume past the previous bar's, and the next bar's gap and timing, each one way "
                      f"chosen at random ({caveat})" + (f", {grid}" if grid else "") +
                      f", each {counted}, plus a repair draw where that draw fell short of its own plan")
            residual = ("a read that only the other way would flip, a read of a magnitude rather than a "
                        "direction (how far the close is from the open, where it sits within its own range, how far the next bar gaps, how far a price sits from a level), "
                        "or of how two relations combine, can still go unseen" + tie_residual)
        else:
            combos = (f"the close pushed both ways past its open; the close, the high and the low each pushed "
                      f"both ways past each of {levels}, and the high above and the low below the bar's own "
                      f"open; the volume both ways past the previous bar's, and to "
                      f"zero and away from it where the tape prints zeros; the next bar's open both ways past "
                      f"this bar's close and open and each of the previous bar's levels, and above this bar's own "
                      f"high and below its own low, and the next bar early, "
                      f"on time and late where the tape prints each" + self._tie_clause()
                      + (f"; {grid}" if grid else "") + "; and move, wick and volume each both ways")
            if self.draws >= 4:
                pairs = ("every sign combination of move, wick and volume" if self.draws >= 8 else
                         f"every pair of move, wick and volume pushed apart ({min(self.draws, 8)} of 8 sign "
                         f"combinations)")
                combos += (f"; on the further draws the close in a band between those levels chosen at random, "
                           f"the range once inside and once outside the previous bar's, and {pairs}")
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range, how far the next bar gaps, how far a price sits from a level), against a level further back than "
                            "the previous bar, or of how two of these relations combine at one bar -- where the "
                            "close sits between two of the previous bar's levels, a failed breakout, an inside "
                            "or outside range together with where the close sits"
                            + ("" if self.draws >= 8 else ", a pattern across move, wick and volume at once")
                            + " -- is tried only on the draws that happen to produce it, and can go unseen"
                            + tie_residual)
            else:
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range, how far the next bar gaps, how far a price sits from a level), against a level further back than "
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
            # worded by how each bar was measured -- on a price grid or not -- and not by whether the
            # tape has one somewhere: a fifteenth red team's off-grid bar was given the on-grid words
            n_far = len(self.beyond_reach)
            on = len(self.beyond_gridded) if self.beyond_gridded is not None else (n_far if on_tick else 0)
            on_words = f"out of reach of {largest} on the tape's price grid"
            off_words = f"at least as far from the open as {largest} (or within a part in a billion of it)"
            if on == n_far:
                where = on_words + ","
            elif on == 0:
                where = off_words + ","
            else:
                where = (f"out of the close's reach -- at {on} of them {on_words}, at {n_far - on} "
                         f"{off_words} --")
            far = (f"; at {n_far} of the probed bars one of {levels} lay {where} and the close was not "
                   f"pushed past that level")
        if "moves" in self.floors:
            far += ("; the tape has made no moves, so the close was pushed by "
                    + (f"the given sigma {self.sigma:g}" if self.sigma is not None else "a floor size")
                    + ", not by a move of its own"
                    + (" (to the first point of its price grid at least that far from the open, and at least "
                       "one step, that is on no level)" if on_tick else ""))
        if "volume changes" in self.floors:
            far += ("; the tape's traded volume never changed from one traded bar to the next, so the volume "
                    "was pushed by a floor ratio" + (" (rounded to its lot, and by at least one lot)"
                                                     if "lot" in self.grids else ""))
        if "no trades" in self.floors:
            # a fifteenth red team: a tape on which no bar traded was told its volume was pushed by a
            # floor ratio; none moved
            far += "; no bar of the tape traded, so no volume was pushed"
        if self.undelivered:
            # was not made, not could not be: three repair draws on random coins miss what a fourth
            # would make, and a bar counted on one seed was delivered on the next (a twenty-fifth red team)
            far += (f"; at {len(self.undelivered)} of the probed bars a push listed here was not made with "
                    f"sizes the tape has made and without a tie the real bar did not print, on the planned "
                    f"draws or on up to three repair draws, and was not counted")
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


class _Calendar(NamedTuple):
    """The times a tape prints at, where it does not print around the clock: the times of day it has a
    bar at on each day of the week, between each real day's own first and last bar. A rebuilt tail
    whose clock came from donor runs started anywhere put more than half its bars outside a
    09:30-15:55 session (a sixteenth red team); one day's times of day merged across a change of
    clock (DST) gave every rebuilt day the union of both sessions (a seventeenth). Each real day keeps
    its own open and close -- a half day, a holiday, a shifted session; the tape's last day, which may
    end mid-session, the close of a full day of its weekday; a day past the tape's ends, the hours of
    the nearest real day of its weekday. The next day after a halt of any length is found among the
    tape's own days: searched day by day for twenty days, the rebuild stepped blindly through
    weekends and nights past a longer suspension (an eighteenth red team)."""

    step: float
    days: dict            # weekday (or None) -> the sorted times of day printed that day of the week
    weekly: bool
    bounds: dict          # day number -> (first, last) time of day the tape printed that day
    first: int
    last: int
    order: tuple          # the day numbers the tape has bars on, in order
    full: dict            # weekday (or None) -> the widest (first, last) of a day of that weekday
    steps_after: dict     # (weekday or None, time of day) -> the steps the tape makes after a bar there

    def _wd(self, day: int):
        return (day + 3) % 7 if self.weekly else None

    def key(self, t: float) -> tuple:
        return (self._wd(math.floor(t / 86400)), round(t % 86400, 3))

    def day_bounds(self, day: int):
        if self.first <= day <= self.last:
            b = self.bounds.get(day)
            if b is not None and day == self.last:
                f = self.full.get(self._wd(day), b)
                b = (b[0], max(b[1], f[1]))
            return b
        step = 7 if self.weekly else 1
        d = day
        for _ in range(60):
            d += -step if day > self.last else step
            if self.first <= d <= self.last and d in self.bounds:
                return self.full.get(self._wd(d), self.bounds[d]) if d == self.last else self.bounds[d]
        return None

    def prints(self, t: float) -> bool:
        day, tod = math.floor(t / 86400), t % 86400
        b = self.day_bounds(day)
        tods = self.days.get(self._wd(day))
        if not b or not tods or not (b[0] - 1e-6 <= tod <= b[1] + 1e-6):
            return False
        j = bisect.bisect_left(tods, tod - 1e-6)
        return j < len(tods) and abs(tods[j] - tod) <= 1e-6

    def _first_in(self, d: int, t: float) -> float | None:
        b, tods = self.day_bounds(d), self.days.get(self._wd(d))
        if not b or not tods:
            return None
        j = bisect.bisect_right(tods, max(t - d * 86400, b[0] - 1e-3) + 1e-6)
        return d * 86400 + tods[j] if j < len(tods) and tods[j] <= b[1] + 1e-6 else None

    def after(self, t: float) -> float:
        """The first time after ``t`` the tape prints at."""
        day = math.floor(t / 86400)
        j = bisect.bisect_left(self.order, day)
        for d in self.order[j:j + 2]:            # this day, if the tape has it, and its next real day
            x = self._first_in(d, t)
            if x is not None:
                return x
        start = max(day, self.last + 1)
        for d in range(start, start + 15):        # past the tape: day by day, as its weekdays print
            x = self._first_in(d, t)
            if x is not None:
                return x
        return t + self.step

    def makes(self, t: float, step: float) -> bool:
        """Whether the tape makes ``step`` after a bar at ``t``'s time of day (and weekday)."""
        steps = self.steps_after.get(self.key(t))
        return steps is None or any(abs(step - s) <= 1e-6 for s in steps)


def _calendar(tape: Sequence[Any]) -> _Calendar | None:
    """The tape's calendar, where it keeps one: it spans two days or more and prints at under nine
    in ten of the times of day its step allows, or -- over a week or more -- on fewer than seven days
    of the week. None for a tape that prints around the clock, or whose times keep no step -- under a
    fifth of its steps its commonest. Computed once, in ``_sizes``: once per rebuild, its step count
    made an audit quadratic in the tape (an eighteenth red team)."""
    import collections
    ts = [b.ts for b in tape]
    if len(ts) < 3 or ts[-1] - ts[0] < 2 * 86400:
        return None
    counts = collections.Counter(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if not counts:
        return None
    step = max(counts, key=lambda st: (counts[st], -st))
    if counts[step] * 5 < len(ts) - 1:
        return None           # its times keep no step (event bars): nothing to hold them to
    weekly = ts[-1] - ts[0] >= 7 * 86400
    days: dict = {}
    bounds: dict = {}
    for t in ts:
        day, tod = math.floor(t / 86400), t % 86400
        days.setdefault((day + 3) % 7 if weekly else None, set()).add(tod)
        lo, hi = bounds.get(day, (tod, tod))
        bounds[day] = (min(lo, tod), max(hi, tod))
    tods = set().union(*days.values())
    slots = max(1, round(86400 / step)) if step < 86400 else 1
    if len(tods) >= 0.9 * slots and (not weekly or len(days) == 7):
        return None
    full: dict = {}
    for day, (lo, hi) in bounds.items():
        wd = (day + 3) % 7 if weekly else None
        f = full.get(wd, (lo, hi))
        full[wd] = (min(f[0], lo), max(f[1], hi))
    steps_after: dict = {}
    for a, b in zip(ts, ts[1:]):
        if b > a:
            day = math.floor(a / 86400)
            steps_after.setdefault(((day + 3) % 7 if weekly else None, round(a % 86400, 3)), collections.Counter())[b - a] += 1
    return _Calendar(step, {k: sorted(v) for k, v in days.items()}, weekly, bounds, min(bounds), max(bounds),
                     tuple(sorted(bounds)), full, steps_after)


_NONE = -1e6      # the log of nothing: an untraded bar's volume, a still stretch's volatility


def _donor_index(tape: Sequence[Any], calendar: _Calendar | None, rms: Sequence[float] | None) -> tuple:
    """What ``_Donors`` looks bars up by, found once per tape: every bar's log volume, the width a
    stand-in's log volume is matched within, every bar's log local volatility, and on a tape with a
    calendar the bars after each time of day. An untraded bar's volume, and a still stretch's
    volatility, are a value no real one comes near (``_NONE``): a stand-in matches them only with its
    own kind."""
    lv = [math.log(b.volume) if b.volume > 0 else _NONE for b in tape]
    jumps = sorted(abs(b - a) for a, b in zip(lv, lv[1:]) if a != _NONE and b != _NONE and a != b)
    vw = (jumps[len(jumps) // 2] if jumps else 1.0) * SINGLE_VOLUME
    lrms = [math.log(r) if r > 0 else _NONE for r in rms] if rms else [0.0] * len(tape)
    by_tod: dict | None = None
    if calendar is not None:
        by_tod = {}
        for d in range(1, len(tape)):
            by_tod.setdefault(round(tape[d - 1].ts % 86400, 3), []).append(d)
    return lv, vw, lrms, by_tod


P_SINGLE = 0.3    # the share of a rebuilt tail's bars that stand in for their run's next bar
# how close a stand-in's volume and local volatility must be to the bar it stands in for, as a share
# of the tape's median bar-to-bar change of log volume, and in log volatility: loosely matched, the
# stand-ins cut the tail's volatility and volume clustering by a third (the same red team's tapes)
SINGLE_VOLUME = 0.1
SINGLE_RMS = 0.05


class _Donors:
    """For each rebuilt bar, the bar of the tape it is rebuilt from: mostly runs of 16 to 48
    consecutive bars, never the bar's own place. A rebuilt bar takes its gap, time step, move, wicks
    and volume together from one real bar, and a run keeps real bars in their order -- so what goes
    with what on the tape (a wide move with a heavy volume, a quiet stretch after a quiet bar, a run
    of untraded bars) goes together in the rebuild. Drawing each of those from its own random donor
    left a statistic a thirteenth and a fourteenth red team could compute on the rebuilt tail and
    never on a real one.

    Each run starts near the bar being rebuilt, in the part of the tape after the probed bar, and --
    on a tape with a calendar -- at the same time of day where it can. Runs started anywhere gave the
    tail the whole tape's level, not the level where it stands (a sixteenth red team). No run passes
    a bar another has: drawn again and again from a window of sixty-odd bars, half a tail came from
    bars used twice, and it repeated its own runs as no real tape does (a seventeenth). But a tail
    made of every later bar once, in runs, ends where the real tape does: a strategy reading the
    price at the tape's last bar saw the same side on every rebuild (an eighteenth). So about one
    bar in three (``P_SINGLE``) is a single bar from near it -- of the same time of day, traded or
    not as the bar it stands in for, of about its volume -- and the run passes that bar by: some
    later bars go unused, some are used twice, and no two donors ever follow each other twice, so no
    run repeats. A tail too short for runs, and every bar before the probed one, is used only this
    way: runs of the head copied into the tail were a tell of their own (the same). A bar whose
    time the calendar moved -- across a weekend, a holiday, a shorter day -- takes a donor that
    came after the same kind of gap, so a Monday is rebuilt from a Monday (a seventeenth)."""

    def __init__(self, tape: Sequence[Any], boundary: int, rng: random.Random,
                 calendar: _Calendar | None = None, rms: Sequence[float] | None = None,
                 index: tuple | None = None) -> None:
        self.tape, self.rng, self.cal = tape, rng, calendar
        n = len(tape)
        self.lo = boundary + 1
        self.short = n - boundary - 1 < 8
        # far enough to reach the same time of day a few sessions away
        per_day = max(len(v) for v in calendar.days.values()) if calendar and calendar.step < 86400 else 1
        self.reach = int(max(64, 3 * per_day + 8))
        self.lv, self.vw, self.lrms, self.by_tod = index if index is not None else _donor_index(tape, calendar, rms)
        self.chosen: dict[int, int] = {}
        self.placed: dict[int, int] = {}
        self.used: set[int] = set()          # bars a run has taken or passed by
        self.follow: dict[int, set] = {}     # donor -> every donor the rebuild has put after it
        self.run, self.d, self.prev = 0, None, None
        self.last: tuple | None = None

    def at(self, i: int, prev_ts: float) -> int:
        if i in self.chosen:
            return self.chosen[i]
        state, n = (self.run, self.d, self.prev), len(self.tape)
        nxt = None
        if not self.short:
            c = self.d + 1 if self.run > 0 and self.d is not None else None
            if c is not None and c < n and c != i and c not in self.used and c not in self.follow.get(self.prev, ()):
                nxt = c
            else:
                nxt, _ = self._start(i, prev_ts)
                if nxt is not None:
                    self.run = self.rng.randint(16, 48)
        if nxt is None or self.rng.random() < P_SINGLE:
            d, _ = self._single(i, prev_ts, nxt)
        else:
            d = nxt
        if nxt is not None:
            self.used.add(nxt)          # taken, or passed by
            self.d, self.run = nxt, self.run - 1
        return self._take(i, d, state, nxt)

    def again(self, i: int, prev_ts: float, step: float) -> int:
        """A new donor for bar ``i``, whose time the calendar set ``step`` after the bar before it:
        one that came the same step after its own previous bar, where the tape has one."""
        if self.last is not None and self.last[0] == i:
            _, d, (self.run, self.d, self.prev), passed, new_pair = self.last
            self.chosen.pop(i, None)
            self.placed[d] -= 1
            if new_pair:
                self.follow[self.prev].discard(d)
            if passed is not None:
                self.used.discard(passed)
        state = (self.run, self.d, self.prev)
        d, exact = (None, False) if self.short else self._start(i, prev_ts, step)
        if d is not None and exact:
            self.run = self.rng.randint(16, 48)
            self.used.add(d)
            self.d, self.run = d, self.run - 1
            return self._take(i, d, state, d)
        one, one_exact = self._single(i, prev_ts, None, step)
        if d is not None and not one_exact:
            self.run = self.rng.randint(16, 48)
            self.used.add(d)
            self.d, self.run = d, self.run - 1
            return self._take(i, d, state, d)
        return self._take(i, one, state, None)

    def _take(self, i: int, d: int, state: tuple, passed: int | None) -> int:
        new_pair = self.prev is not None and d not in self.follow.get(self.prev, ())
        if new_pair:
            self.follow.setdefault(self.prev, set()).add(d)
        self.last = (i, d, state, passed, new_pair)
        self.chosen[i] = self.prev = d
        self.placed[d] = self.placed.get(d, 0) + 1
        return d

    def _fit(self, cands: list, prev_ts: float, step: float | None) -> tuple[list, bool]:
        """``cands`` narrowed to those that came ``step`` after their own previous bar (or the nearest
        such step) and, on a tape with a calendar, at the time of day the rebuild has reached."""
        tape, exact = self.tape, True
        if step is not None:
            same = [d for d in cands if abs((tape[d].ts - tape[d - 1].ts) - step) <= 1e-6]
            if not same:
                exact = False
                gap = min(cands, key=lambda d: abs((tape[d].ts - tape[d - 1].ts) - step))
                g = tape[gap].ts - tape[gap - 1].ts
                same = [d for d in cands if abs((tape[d].ts - tape[d - 1].ts) - g) <= 1e-6]
            cands = same
        if self.cal is not None:
            # the same time of day, whatever the day: the run carries the day's own shape (a heavy
            # open and close) from where it stands
            want = prev_ts % 86400
            cands = [d for d in cands if abs(tape[d - 1].ts % 86400 - want) < 1e-6] or cands
        return cands, exact

    def _pick(self, cands: list, i: int) -> int:
        """One of ``cands``, the nearer to bar ``i`` the likelier -- weighed from the nearest, so a
        pool of only far bars (the whole tape, late in a long tail) never weighs to nothing."""
        scale = self.reach / 3
        near = min(abs(d - i) for d in cands)
        return self.rng.choices(cands, [math.exp(-(abs(d - i) - near) / scale) for d in cands])[0]

    def _near(self, i: int, prev_ts: float, lo: int, step: float | None, fresh: bool) -> tuple[list, bool]:
        """The bars from ``lo`` on within reach of bar ``i`` that may follow the donor before it --
        never ``i`` itself, never the same pair twice, and with ``fresh`` none a run has taken or passed
        -- narrowed by ``_fit``. On a tape with a calendar and no step to match, looked up by time of
        day: scanned bar by bar, a tape of 276 bars a day scanned 1,600 of them for every bar of the
        tail."""
        n = len(self.tape)
        a, b = max(lo, i - self.reach), min(n - 1, i + self.reach)
        bad, used = self._barred(i), (self.used if fresh else ())
        if self.by_tod is not None and step is None:
            at = self.by_tod.get(round(prev_ts % 86400, 3), ())
            same = [d for d in at[bisect.bisect_left(at, a):bisect.bisect_right(at, b)] if d not in bad and d not in used]
            if same:
                return same, True
        ok = [d for d in range(a, b + 1) if d not in bad and d not in used]
        return self._fit(ok, prev_ts, step) if ok else (ok, False)

    def _barred(self, i: int) -> set:
        return self.follow.get(self.prev, set()) | {i, self.prev}

    def _start(self, i: int, prev_ts: float, step: float | None = None) -> tuple[int | None, bool]:
        """Where a new run starts: a bar after the probed one no run has taken or passed."""
        cands, exact = self._near(i, prev_ts, self.lo, step, True)
        if not cands:
            bad, used = self._barred(i), self.used
            ok = [d for d in range(self.lo, len(self.tape)) if d not in bad and d not in used]
            if not ok:
                return None, False
            cands, exact = self._fit(ok, prev_ts, step)
        return self._pick(cands, i), exact

    def _single(self, i: int, prev_ts: float, like: int | None, step: float | None = None) -> tuple[int, bool]:
        """One bar from near bar ``i``, anywhere on the tape, never following the donor before it
        twice; like ``like`` -- the run's next bar, which it stands in for -- in whether it traded and
        about how much, and in how much the bars around it move; one the rebuild has not used yet,
        where it can."""
        n, tape = len(self.tape), self.tape
        cands, exact = self._near(i, prev_ts, 1, step, False)
        if step is not None and not exact:
            bad = self._barred(i)
            wide = [d for d in range(1, n) if d not in bad and abs((tape[d].ts - tape[d - 1].ts) - step) <= 1e-6]
            if wide:
                cands, exact = self._fit(wide, prev_ts, step)
        if not cands:
            cands, exact = self._fit([d for d in range(1, n) if d != i] or [max(1, min(n - 1, i - 1))], prev_ts, step)
        inv_d = 3.0 / self.reach
        if like is None:
            xs = [abs(d - i) * inv_d for d in cands]
        else:
            lv, lr, inv_v, inv_r = self.lv, self.lrms, 1.0 / self.vw, 1.0 / SINGLE_RMS
            v0, r0 = lv[like], lr[like]
            xs = [abs(d - i) * inv_d + abs(lv[d] - v0) * inv_v + abs(lr[d] - r0) * inv_r for d in cands]
        least, placed, exp = min(xs), self.placed, math.exp
        weights = [exp(least - x) * (0.25 if placed.get(d) else 1.0) for d, x in zip(cands, xs)]
        return self.rng.choices(cands, weights)[0], exact


def _rethread(tape: Sequence[Any], boundary: int, rng: random.Random, donors: _Donors,
              sigma: float | None = None,
              first: tuple[float, float, float, float] | None = None,
              after: tuple = (None, None),
              grid: tuple = (None, None), no_zero: bool = False, widths: tuple = (64, 64),
              caps: tuple = (None, None, None, None, None), rms: Sequence[float] = (),
              z: Sequence[float | None] = ()) -> list[Any]:
    """Rebuild the walk from ``boundary`` on, keeping every invariant the pristine tape has --
    IN DISTRIBUTION, never per bar.

    An earlier version copied each pristine bar's own wick ratios and opening gap onto the
    rebuilt bar, and left every timestamp alone, so that nothing about the tape's shape
    would give a probe away. That preserved those properties for each bar exactly, and a
    third red team read exactly those properties: a bar's own wick ratio, the next bar's
    opening gap, the next bar's timestamp. None of them ever moved under any probe, so a
    strategy deciding on them was clean on every run. Everything unknowable at bar i's open
    must vary at bar i. Everything unknowable about later bars must vary there too.

    So each rebuilt bar is another real bar of the tape -- ``donors`` names which (``_Donors``)
    -- continued from wherever the rebuild has got to: its gap to the previous close, its time
    step, its move, its wicks and its volume, the sizes jittered off their exact values so no
    rebuilt value duplicates one already on the tape. Under a caller's sigma the move is a
    normal one of that width instead, as the report says. Bar ``boundary`` keeps its own open and
    timestamp, which the strategy was entitled to see; its high, low, close and volume are not,
    and all four vary. ``first``, when given, is that bar's close, high, low and volume, built by
    ``_forced_bar``; ``after`` is the NEXT bar's open and time step, where ``_next_bar`` set them.
    ``grid`` is the tape's price grids and volume grid: a tape printed on a grid is rebuilt on
    it, because a price off the grid is a price no bar of that tape could have printed (an
    eleventh red team's tell). Nothing rebuilt is ever zero or negative, and no volume is zero on a
    tape that never prints one.
    """
    by_bar, vgrid = grid
    pgrid = by_bar
    # whether the tape moves at all: realized_sigma is floored, so a tape of no moves read as moving,
    # and its tail was a run of dojis under 'moves of sigma' (a twentieth red team)
    moving = any(x > 0 for x in rms) if rms else any(b.close != b.open for b in tape)
    lo_b, hi_b = 0.0, math.inf
    if sigma is not None:
        # Under a caller's sigma the rebuilt walk is kept where its prices can move by the sigma and a
        # float can hold them: above the price where the tape's tick at its lowest close would be the
        # whole sigma, and within e^10 of the prices the tape closed at. A trending tape's drift,
        # rescaled, took the tail to a cent, a tail of dojis under 'the given sigma's', and to
        # infinity, which crashed the audit (a twenty-first red team).
        cl = [(b.close, b.ts) for b in tape if b.close > 0]
        if cl:
            (lc, lts), hc = min(cl), max(c for c, _ in cl)
            g = _grid_at(by_bar(lts) if callable(by_bar) else by_bar, lc)
            # never above the tape's own lowest close: taken from a coarser grid than the check priced,
            # the bound held a tail 3.6 times above the tape (a twenty-second red team)
            lo_b = min(max(g.step / g.scale / sigma if isinstance(g, Grid) else 0.0, lc * math.exp(-10.0)), lc)
            hi_b = hc * math.exp(10.0)
    top_move, top_wick, top_up, top_dn, top_volume = (tuple(caps) + (None,) * 5)[:5]

    def on(x: float, how: str) -> float:
        return _snap(x, _grid_at(pgrid, x, rng, snap=True), how)

    def positive(x: float, fallback: float) -> float:
        y = on(x, "round")
        if y <= 0:
            y = on(x, "up")
        return y if y > 0 else fallback

    def traded(v: float) -> float:
        y = _snap(v, vgrid, "round")
        if no_zero and y <= 0:
            y = _snap(max(v, 1e-12), vgrid, "up") if vgrid else v
            y = y if y > 0 else (vgrid.step + vgrid.off if vgrid else v)
        return max(y, 0.0)
    n = len(tape)
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
        d = donors.at(i, prev_ts)
        donor, before = tape[d], tape[d - 1] if d >= 1 else tape[d]
        if i == boundary:
            opened, ts = b.open, b.ts
            pgrid = by_bar(ts) if callable(by_bar) else by_bar      # the grids of this bar's era
        else:
            exact_open, forced_step = after if i == boundary + 1 else (None, None)
            own_step = donor.ts - before.ts if d >= 1 else _step_of(tape, donor)
            ts = prev_ts + (forced_step if forced_step is not None else own_step)
            cal = donors.cal
            if cal is not None:
                if forced_step is not None:
                    # the next bar's planned timing, landed on the first time the tape prints at from
                    # there: moved to the next open instead, a late next bar was never late (a
                    # seventeenth red team's session tape came up short at every bar)
                    ts = ts if cal.prints(ts) else cal.after(ts - 1e-6)
                elif not (cal.prints(ts) and cal.makes(prev_ts, ts - prev_ts)):
                    # a donor's overnight or weekend gap belongs to its own day, not the rebuilt bar's:
                    # a step the tape never makes from this time of day (and weekday) is the tape's next
                    # time instead -- judged by the day's length, a session crossing midnight UTC took
                    # its donors' breaks where the tape has none (an eighteenth red team)
                    ts = cal.after(prev_ts)
                if forced_step is None and abs((ts - prev_ts) - own_step) > 1e-6:
                    d = donors.again(i, prev_ts, ts - prev_ts)
                    donor, before = tape[d], tape[d - 1] if d >= 1 else tape[d]
            pgrid = by_bar(ts) if callable(by_bar) else by_bar
            if exact_open is not None:
                # the next open, set on the grid of its own level and era; what that undoes of the
                # plan, the note's check of the bars as built sees
                opened = _on_level(pgrid, exact_open, "round") if pgrid is not None else exact_open
                side = 1 if opened > prev_close else -1
                top_gap = top_up if side > 0 else top_dn
                if (top_gap is not None and opened > 0 and prev_close > 0
                        and abs(math.log(opened / prev_close)) > top_gap * (1 + 1e-9) and pgrid is not None):
                    # set on a coarser tick past the tape's largest gap: the other way, where the grid
                    # has a point between (a nineteenth red team's sub-dollar open, 1.19 times past it)
                    back = _on_level(pgrid, prev_close * math.exp(side * top_gap), "down" if side > 0 else "up")
                    if back > 0 and (back - prev_close) * side > 0 and abs(math.log(back / prev_close)) <= top_gap * (1 + 1e-9):
                        opened = back
            else:
                gap = donor.open / before.close if d >= 1 and before.close > 0 and donor.open > 0 else 1.0
                g = _jitter(math.log(gap), rng) if gap != 1.0 else 0.0
                top_gap = top_up if g > 0 else top_dn
                if top_gap is not None:
                    g = max(-top_gap, min(top_gap, g))          # never past the tape's largest gap that way
                opened = prev_close * math.exp(g) if g else prev_close
                if opened != prev_close:
                    opened = positive(opened, prev_close)
                    if top_gap is not None and opened > 0 and abs(math.log(opened / prev_close)) > top_gap * (1 + 1e-9):
                        back = on(prev_close * math.exp(g), "down" if g > 0 else "up")
                        opened = back if back > 0 and abs(math.log(back / prev_close)) <= top_gap * (1 + 1e-9) else prev_close
                elif pgrid is not None:
                    # an ungapped open carries the last close -- across a split, onto a grid the new
                    # era never prints on; set on its own era's grid
                    opened = _on_level(pgrid, opened, "round")
        if not lo_b <= opened <= hi_b:
            opened = prev_close if lo_b <= prev_close <= hi_b else min(max(opened, lo_b), hi_b)
            if pgrid is not None:
                opened = _on_level(pgrid, opened, "round")
        raw = math.log(donor.close / donor.open) if donor.open > 0 and donor.close > 0 else 0.0
        own = _jitter(raw, rng) if raw else 0.0
        if top_move is not None and sigma is None:
            own = max(-top_move, min(top_move, own))        # jittered, never past the tape's largest
        if sigma is not None:
            # the donor's own move, at the caller's width: a normal draw of its own lost the moves'
            # clustering and their coupling with volume and wicks (a sixteenth red team). Scaled by the
            # moves around the donor, not the whole tape's: on a tape whose volatility changes, a
            # donor from its calm stretch came out far under sigma (an eighteenth red team)
            # A donor from a still stretch of a moving tape has no move to scale: it stays still. Given a
            # normal move of sigma, an untraded bar of a still stretch came back moving, which the tape
            # never prints, and a strategy keyed on that walked (a nineteenth red team). Only a tape
            # with no moves at all is moved by normal draws of the sigma, as the note says.
            # Net of the trend around the donor, standardized, and only then jittered: jittered first,
            # a trend's drift came through as moves of up to 5.6 times the sigma (a twenty-second red
            # team). A donor that did not move stays still (an untraded bar at the edge of a still
            # stretch came back moving); one from a stretch whose moves are all trend -- the same
            # amount each bar, or one changing steadily -- is moved by the sigma, not frozen or blown up.
            z_d = z[d] if 0 <= d < len(z) else None
            if not moving:
                move = rng.gauss(0.0, sigma)
            elif raw == 0.0:
                move = 0.0
            elif z_d is None:
                move = rng.gauss(0.0, sigma)
            else:
                move = z_d * sigma * math.exp(rng.gauss(0.0, JITTER) - JITTER * JITTER / 2)
            move = max(-50.0, min(50.0, move))     # never past what a float's exp can hold
            if not lo_b <= opened * math.exp(move) <= hi_b:
                move = -move                       # the walk turned back at its bounds
                if not lo_b <= opened * math.exp(move) <= hi_b:
                    move = 0.0
        else:
            move = own
        closed = positive(opened * math.exp(move), opened)
        if top_move is not None and sigma is None and closed > 0 and opened > 0 and abs(math.log(closed / opened)) > top_move:
            # a close set on a coarse tick past the largest move: the other way, toward the open
            back = on(opened * math.exp(move), "down" if move > 0 else "up")
            closed = back if back > 0 and abs(math.log(back / opened)) <= top_move else opened
        hi, lo = max(opened, closed), min(opened, closed)
        d_top, d_bot = max(donor.open, donor.close), min(donor.open, donor.close)
        up = _jitter(max(0.0, donor.high / d_top - 1.0), rng) if d_top > 0 else 0.0
        dn = min(_jitter(max(0.0, 1.0 - donor.low / d_bot), rng), 0.99) if d_bot > 0 else 0.0
        if top_wick is not None:
            up, dn = min(up, top_wick), min(dn, top_wick)
        low = on(lo * (1.0 - dn), "down")
        high = max(on(hi * (1.0 + up), "up"), hi)
        if top_wick is not None and hi > 0 and high / hi - 1 > top_wick + 1e-12:
            # a wick set on a coarse tick past the largest the tape has made (a seventeenth red team's
            # one-tick wicks came out two ticks): the other way
            high = max(on(hi * (1.0 + up), "down"), hi)
        if top_wick is not None and lo > 0 and 0 < low and 1 - low / lo > top_wick + 1e-12:
            low = min(on(lo * (1.0 - dn), "up"), lo)
        volume = donor.volume * math.exp(rng.gauss(0.0, VOLUME_JITTER)) if donor.volume > 0 else 0.0
        volume = traded(min(volume, top_volume) if top_volume is not None else volume)
        if top_volume is not None and volume > top_volume:
            # set on a coarse lot past the largest the tape has printed: the other way
            down = _snap(top_volume, vgrid, "down") if vgrid else top_volume
            volume = down if down > 0 else top_volume
        out[i] = _replace(b, ts=ts, open=opened, close=closed, volume=volume,
                          high=high, low=low if 0 < low <= lo else lo)
        prev_close, prev_ts = closed, ts
    if widths != (64, 64):
        # a float32 tape's values read back at float32: rounding is monotone, so every bar stays valid
        pw, vw = (_f32 if w == 32 else (lambda x: x) for w in widths)
        for i in range(boundary, n):
            b = out[i]
            out[i] = _replace(b, open=pw(b.open), high=pw(b.high), low=pw(b.low), close=pw(b.close),
                              volume=vw(b.volume))
    return _typed(tape, out, boundary)


def _typed(tape: Sequence[Any], out: list, boundary: int) -> list:
    """The rebuilt bars in the tape's own number types: each field the tape prints in one type
    throughout set back in it -- an int where the tape's are ints and the value is whole, a numpy
    float32 where it stores those -- and the probed bar's time and open the tape's own. Left as
    Python floats, bar k's own open changed type though not value: a strategy that read only its
    own open was PROVEN on an int or float32 tape, a read gated on an int close walked, and honest
    integer indexing crashed (a twenty-fifth red team)."""
    kinds = {}
    for f in ("ts", "open", "high", "low", "close", "volume"):
        seen = {type(getattr(b, f, None)) for b in tape}
        if len(seen) == 1 and (t := seen.pop()) is not float and issubclass(t, numbers.Real) and t is not bool:
            kinds[f] = t
    real = tape[boundary] if boundary < len(tape) else None
    for i in range(boundary, len(out)):
        b = out[i]
        kw = {}
        for f, t in kinds.items():
            v = getattr(b, f)
            if type(v) is not t and (w := _as_kind(t, v)) is not None:
                kw[f] = w
        if i == boundary and real is not None:
            for f in ("ts", "open"):
                if getattr(b, f) == getattr(real, f):
                    kw[f] = getattr(real, f)
        if kw:
            out[i] = _replace(b, **kw)
    return out


def _as_kind(t: Any, v: Any) -> Any:
    """``v`` as a ``t`` of the same value, or None where it has none: an int only for a whole value,
    and a float32 or Fraction from the decimal the float writes (20.01, not its binary expansion)."""
    try:
        if issubclass(t, numbers.Integral):
            return t(int(v)) if math.isfinite(v) and float(v).is_integer() else None
        try:
            w = t(repr(float(v)))
        except (TypeError, ValueError, ArithmeticError):
            w = t(v)
        return w if w == v or float(w) == float(v) else None
    except (TypeError, ValueError, ArithmeticError, OverflowError):
        return None


class Grid(NamedTuple):
    """A grid a tape's values sit on: ``off + n * step`` for whole ``n``; for every grid point the
    tape itself printed, the exact float it printed there; and, where every value it printed is
    rounded to ``digits`` decimal places, that rounding -- so a value set on a point the tape
    printed is bit-for-bit its own, and one set on a point it never printed is written the way it
    writes them. A thirteenth red team's evader answered as on real data whenever a price differed
    from its own two-place rounding: 100.19000000000001 is a price no such tape prints."""

    step: float
    off: float
    vals: dict
    digits: int | None = None
    spelled: bool = True           # False where neither rounding nor off + n*step writes every printed value
    fine: Any = None               # a finer grid a few of the prices around sit on,
    rate: float = 0.0              # the share of the prices where they lie that do,
    span: tuple = ()               # and the prices they lie between
    scale: float = 1.0             # prices times this sit on the grid: a split-adjusted tick
    width: int = 64                # 32 where the tape stores its values as float32
    pair: bool = False             # only the two printed prices a change of tick lies between


class Grids(NamedTuple):
    """The grids a tape's prices sit on, by price level: for every distinct printed price, the grid
    most of the prices around it share. Where the tick changes with the price -- a sub-dollar tick
    below a dollar, an exchange's spread table -- or a few prints carry a place more than the rest,
    one grid for the whole tape put the finest step everywhere, and a fourteenth red team's evader
    answered as on real data wherever a rebuilt price used a place the tape never did there."""

    prices: list
    grids: list

    def at(self, x: float, snap: bool = False) -> Grid | None:
        """The grid of the printed price nearest ``x``. With ``snap``, for setting ``x`` itself:
        between two printed prices on different ticks, the tape shows no price at all but those
        two, and a price set there by either tick was one the tape never printed on the other side
        of the change (19.95, under a spread table's 20, on a fourteenth red team's tape)."""
        ps = self.prices
        j = bisect.bisect_left(ps, x)
        if j >= len(ps):
            return self.grids[-1]
        if j == 0 or ps[j] == x:
            return self.grids[j]
        a, b = self.grids[j - 1], self.grids[j]
        if snap and not _same_grid(a, b):
            return Grid(ps[j] - ps[j - 1], ps[j - 1], {0: ps[j - 1], 1: ps[j]}, pair=True)
        return a if x - ps[j - 1] < ps[j] - x else b


def _same_grid(a: Grid | None, b: Grid | None) -> bool:
    if a is None or b is None:
        return a is b
    if abs(a.step - b.step) > 1e-9 * a.step:
        return False
    q = (a.off - b.off) / a.step
    return abs(q - round(q)) < 1e-6


def _on_level(grids: Grids, x: float, how: str) -> float:
    """``x`` as a price the tape prints at its own level: unchanged where it sits on that level's
    grid, or on the finer one there; set on it otherwise. The probed bar is built on its open's
    grid, and a bar opening just under a change of tick reached across it with the tick of the
    level below (the same tape)."""
    if not (x > 0 and math.isfinite(x)):
        return x
    g = grids.at(x, snap=True) if hasattr(grids, "at") else grids
    if g is None:
        return x
    for h in (g, g.fine):
        if h is not None and _on_grid(h, x):
            return x
    return _snap(x, g, how)


def _grid_at(grid: Grids | Grid | float | None, x: float, rng: random.Random | None = None,
             snap: bool = False) -> Grid | float | None:
    """The grid that applies at price ``x``: the one most prices there share -- or, drawn with
    ``rng``, the finer one a few of them sit on, as often as the tape prints on it there. A rare
    finer print put under the whole rebuild made it everywhere at half the points; left out
    entirely, a strategy keyed on its absence would tell the rebuild apart the other way."""
    g = grid.at(x, snap) if hasattr(grid, "at") else grid
    if rng is not None and isinstance(g, Grid) and g.fine is not None and rng.random() < g.rate:
        # only where those prints lie: a band whose tick is finer only on its own side of a change
        # (an exchange's spread table) put that finer tick under the prices below the change,
        # where the tape never printed it (the same tape)
        if not g.span or g.span[0] <= x <= g.span[1]:
            return g.fine
    return g


def _bar_grid(sizes: Sizes, o: float, bar: Any = None) -> Grid | None:
    """The grid a probed bar is built on: its price level's, and the finer one there where the
    real bar itself printed on it -- the rule ties follow: a draw carries only what the real bar
    printed, or what it is testing."""
    g = _grid_at(sizes.price_grids or sizes.price_grid, o)
    if isinstance(g, Grid) and g.fine is not None and bar is not None:
        if any(v > 0 and not _on_grid(g, v)
               for v in (bar.close, bar.high, bar.low)):
            return g.fine
    return g


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
    signed: tuple = ()                  # every bar's move as log(close/open), sign kept, in tape order
    price_grids: Grids | None = None    # the grid of each price level, where the tape has one
    eras: Any = None                    # where the grid the tape prints on changes in time (Eras)
    widths: tuple = (64, 64)            # the float width the tape stores its prices and volumes at
    timing: tuple | None = None         # on a tape with a calendar, per bar: the steps the tape prints
                                        # after a bar at that time of day (and weekday), and their mode
    calendar: Any = None                # the tape's calendar (_Calendar), where it keeps one
    local_rms: tuple = ()               # per bar, the root mean square move of the bars around it
    donors: Any = None                  # what the rebuild looks donor bars up by (_donor_index)
    local_z: tuple = ()                 # per bar, its move against the moves around it (_local_z)


# Candidate steps, coarsest first: 1, 2, 2.5 and 5 times a power of ten, and the binary fractions
# of one -- 1/8 to 1/256 -- times a power of ten, on which treasury futures and grains print. From
# 1e-18 to 5e15: stopping at 1e-9 and 5000, a token's 1e-10 tick and a volume lot of 100,000 were
# never found, and rebuilt values went off the tape's rule under a note that named it (a twentieth
# red team).
_STEPS = sorted({m * 10.0 ** p for p in range(-18, 16) for m in (5.0, 2.5, 2.0, 1.0)}
                | {10.0 ** p / 2 ** j for p in range(-6, 4) for j in range(3, 9)}, reverse=True)


_DECIMALS = sorted({m * 10.0 ** p for p in range(-18, 16) for m in (5.0, 2.5, 2.0, 1.0)}, reverse=True)


def _grid(values: Sequence[float], share: float = 1.0, steps: Sequence[float] | None = None,
          prints: bool = False) -> Grid | None:
    """The coarsest grid at least ``share`` of the values sit on, at the offset they share, or None:
    of the distinct values, or with ``prints`` of the values as printed, each as often as it was.

    Tested with a tolerance, not read from decimal forms: a tape stored as whole ticks times 0.01
    prints 100.19000000000001, and a twelfth red team's such tape was taken for no grid at all. A
    thirteenth showed what an absolute tolerance does: every value under about 0.005 sat within it
    of the first point of a 5000 grid. So a value must sit on its point to within a thousandth of a
    step and a part in ten billion of itself (or a unit in its last place, whichever is larger: whole
    volumes of nine trillion sit on a lot of one exactly, and a fourteenth red team's were given no
    lot), and binary ticks (1/32) are candidates. Two floats on one point are one point spelled two
    ways -- a mid-price tape prints 49.974999999999994 and 49.975 -- not two points."""
    vals = sorted({v for v in values if v > 0 and math.isfinite(v)})
    if len(vals) < 3:
        return None
    top = vals[-1]
    allowed = int(len(vals) * (1.0 - share) + 1e-9)          # values that may sit off the grid
    counts = None
    if prints:
        counts = collections.Counter(v for v in values if v > 0 and math.isfinite(v))
        allowed = int(sum(counts.values()) * (1.0 - share) + 1e-9)     # prints that may
    anchors = [vals[0]] if not allowed else list(dict.fromkeys(
        vals[i] for i in (len(vals) // 2, len(vals) // 4, (3 * len(vals)) // 4, 0)))
    for step in (_STEPS if steps is None else steps):
        if step > top:
            continue
        fit = 2.0 * math.ulp(top) / step           # the chance a float falls on a point by chance alone
        if fit >= 0.5 or len(vals) * -math.log(fit) < math.log(1e6):
            # the floats cannot hold a step this fine at this size: every value could sit on it by
            # chance. As a flat hundredth of a step, the guard dropped the cent on prices of 5.5e11
            # and the lot of one on volumes of 2**46 (a twenty-first red team)
            return None
        for anchor in anchors:
            off = anchor - step * math.floor(anchor / step + 1e-9)
            if abs(off) < step * 1e-6 or abs(off - step) < step * 1e-6:
                off = 0.0
            # the offset as the tape would write it: 0.005, not 0.0049999999999954525
            off = next((round(off, d) for d in range(13) if abs(round(off, d) - off) < step * 1e-6), off)
            points: dict[int, float] = {}
            on: list[tuple[int, float]] = []
            misses = 0
            for v in vals:
                n = round((v - off) / step)
                if abs(v - (off + n * step)) > max(min(step * 1e-3, max(v, step) * 1e-10), math.ulp(v)):
                    misses += counts[v] if counts is not None else 1
                    if misses > allowed:
                        break
                    continue
                points.setdefault(n, v)
                on.append((n, v))
            else:
                digits = _digits(step, off, [v for _, v in on])
                spelled = digits is not None or all(v == off + n * step for n, v in on)
                return Grid(step, off, points, digits, spelled)
    return None


def _local_grids(values: Sequence[float], reach: float = 0.05, least: int = 12, most: int = 48,
                 share: float = 0.9) -> Grids | None:
    """For every distinct price, the grid of its own price level: the grid nine in ten of the prices
    just above it share, and the grid nine in ten of those just below it share -- whichever is
    coarser. A tick that changes with the price (a sub-dollar tick below a dollar, an exchange's
    spread table) rises with it, so just above a change the prices above decide, and just below
    it the finer prices on both sides do. Counting the nearest prices instead let the dense
    sub-dollar prices decide for the sparse cent prices just above a dollar (a fourteenth red
    team). A print or two on a finer step do not put that step under the whole rebuild: the finer
    grid is kept with the rate at which the prices there sit on it."""
    u = sorted({v for v in values if v > 0 and math.isfinite(v)})
    if len(u) < 3:
        return None
    # each price as often as it was printed: ten strays printed once each were a tenth of a window's
    # distinct prices on a nickel tape, and put a grid of 2e-06 under it (a twenty-third red team)
    counts = collections.Counter(v for v in values if v > 0 and math.isfinite(v))
    base = _grid(u) or _grid(u, share)
    if base is None:
        return None                  # no grid anywhere on the tape, and none by price level either
    # every local grid is a whole multiple of the finest the tape's prices share
    # four decades above the grid most prices share, not the finest all of them do: one stray print
    # of seven places set that, and a nickel tick was dropped from the candidates (a twenty-second
    # red team). Most PRINTS, not most distinct prices: on a nickel tape of 77 prices, ten strays
    # printed once each were a tenth of the distinct ones, and dropped it again (a twenty-third)
    common = _grid(values, 0.9, prints=True) or base
    steps = [st for st in _STEPS if base.step * (1 - 1e-9) <= st <= min(u[-1], max(base.step, common.step) * 1e4)
             and abs(st / base.step - round(st / base.step)) < 1e-6]
    cache: dict[tuple[int, int], Grid | None] = {}

    def grid_of(a: int, b: int) -> Grid | None:
        if b - a < 3:
            return None
        if (a, b) not in cache:
            window = u[a:b]
            printed = [v for v in window for _ in range(counts[v])]
            every = _grid(window, 1.0, steps)
            # by prints only where the prices off the grid share one of their own, which the rebuild
            # prints them on at their rate; raw floats at the end of a cent tape share none, and by
            # prints were set on cents the tape never printed there (a twenty-third round's own test)
            coarse = _grid(printed, share, steps, prints=True) if every is not None else _grid(window, share, steps)
            if coarse is not None and every is not None and (every.step, every.off) != (coarse.step, coarse.off):
                offs = [v for v in window if not _on_grid(coarse, v)]
                if offs:       # _grid's tolerance and this one can part near 2**53 (a fifteenth red team's crash)
                    span = (offs[0] - coarse.step, offs[-1] + coarse.step)
                    inside = sum(counts[v] for v in window if span[0] <= v <= span[1])
                    rate = sum(counts[v] for v in offs) / max(1, inside)
                    coarse = coarse._replace(fine=every, rate=rate, span=span)
            cache[(a, b)] = coarse if coarse is not None else every
        return cache[(a, b)]

    grids = []
    for j, x in enumerate(u):
        hi = bisect.bisect_right(u, x * (1.0 + reach))
        hi = min(max(hi, j + least), j + most, len(u))
        lo = bisect.bisect_left(u, x / (1.0 + reach))
        lo = max(min(lo, j + 1 - least), j + 1 - most, 0)
        above, below = grid_of(j, hi), grid_of(lo, j + 1)
        pick = sorted((g for g in (above, below) if g is not None), key=lambda g: -g.step)
        # the coarsest the price itself sits on; where it sits on neither, the finer, with its own
        # finer grid and rate -- a price just below a dollar keeps its sub-penny tick
        sits = [g for g in pick if _on_grid(g, x)]
        grids.append(sits[0] if sits else (pick[-1] if pick else None))
    # and a coarser grid of any window a price lies in, where it sits on that one: at the top of a
    # tape the window above the last prices is empty and the one below reached down past a change
    # of tick, so a stock's last cent prices above a dollar were given the sub-dollar tick (a
    # fourteenth red team's tape). A grid coarser than the tape's is never a price it could not print.
    # Only where chance would not put that many prices on it: the top three prices of a tape of
    # cents are all even one time in eight, and seven of them under one time in a hundred. At the
    # ends of the tape, where no prices lie past the window to judge by, one in ten will do: a
    # grid coarser than the tape's is never a price it could not print, and the last five prices
    # over a spread table's 0.50 were given the 0.005 tick below it (a fifteenth red team).
    for (a, b), g in cache.items():
        if g is None:
            continue
        need = math.log(10) if a == 0 or b == len(u) else math.log(100)
        for k in range(a, b):
            if (grids[k] is None or (g.step > grids[k].step * (1 + 1e-9)
                                     and (b - a) * math.log(g.step / grids[k].step) > need)) and \
                    _on_grid(g, u[k]):
                grids[k] = g
    return Grids(u, grids) if any(g is not None for g in grids) else None


def _covers(fine: Grid, coarse: Grid) -> bool:
    """Whether every point of ``coarse`` is a point of ``fine``."""
    if fine.scale != 1.0:
        return coarse.scale == fine.scale and _same_grid(fine, coarse)
    r = fine.off / fine.step
    if coarse.scale != 1.0:
        # a split-adjusted grid's points are written to its places: on any grid those places sit on
        if coarse.digits is None:
            return False
        q = 10.0 ** -coarse.digits / fine.step
        return round(q) >= 1 and abs(q - round(q)) < 1e-6 and abs(r - round(r)) < 1e-6
    q = coarse.step / fine.step
    r = (coarse.off - fine.off) / fine.step
    return round(q) >= 1 and abs(q - round(q)) < 1e-6 and abs(r - round(r)) < 1e-6


def _joined(era: Grid | None, whole: Grid | None, near: Callable[[], bool] | None = None,
            beyond: Callable[[], bool] | None = None) -> Grid | None:
    """The grid a price is set on where its era and its price level each have one: the coarser,
    where one's points are all the other's; where neither's are, the points on both. Either alone
    let a price through that the other forbids: an era's tick carried to a price level it never
    reached, or a price level's tick carried back to an era that printed coarser."""
    if era is None or whole is None:
        return era if whole is None else whole
    if era.pair and whole.pair:
        # two changes of tick, each with only its two printed prices: the era's. Joined by the lcm of
        # their float-noise widths (0.007999999999999896 and 0.0030000000000000027), they made a grid
        # of one reachable point that a snap up returned below its input (a nineteenth red team)
        return era
    if _covers(whole, era):
        return era
    if ((whole.fine is not None and _covers(whole.fine, era)) or (near is not None and _covers(era, whole))) \
            and (near is None or near()):
        # the era printed a finer tick throughout: joined to the level's coarser grid a 25-bar
        # half-cent era was rebuilt on cents at the level's rate (a twenty-fourth red team), and at
        # its own lowest print, where the level has no finer grid at all (a twenty-fifth). Only where
        # the era printed that tick near the price, off what the rest of the tape prints there
        # (``near``): sixteen sub-dollar bars of four places, kept as an era, carried their tick to
        # $1.06, where the era and the tape printed only cents (the round-fourteen test of banded ticks)
        return era
    if _covers(era, whole):
        return whole
    if era.scale == 1.0 and whole.scale == 1.0 and abs(era.off - whole.off) < 1e-12:
        # the steps as written to twelve places: their float noise is no part of the tick
        a, b = Fraction(f"{era.step:.12g}"), Fraction(f"{whole.step:.12g}")
        step = Fraction(math.lcm(a.numerator, b.numerator), math.gcd(a.denominator, b.denominator))
        if step > 100 * max(a, b):
            return era                      # no common tick worth the name
        digits = None if era.digits is None or whole.digits is None else max(era.digits, whole.digits)
        return Grid(float(step), era.off, {}, digits, era.spelled and whole.spelled, width=era.width)
    # between two prices the tape printed on different ticks, where neither grid holds the other's
    # points and the price lies past every price the era printed (``beyond``), the tape printed
    # nothing but those two: the era's tick there set 20.18 above a spread table's 20, where the tape
    # prints 0.05, for an era under 20 on 0.02 (the round-fourteen test of banded ticks, once a band
    # excursion split that era). Within the era's own prices its tick stands: a nickel era's 20.77
    # lies between its own 20.75 and a later cent era's 20.78
    return whole if whole.pair and beyond is not None and beyond() else era


class Joint(NamedTuple):
    """An era's grids and the whole tape's grids by price level, read together; and, built on first
    use, the grids by price level of the tape's prices outside the era."""

    era: Any
    whole: Any
    rest: Callable[[], Any] | None = None

    def at(self, x: float, snap: bool = False) -> Grid | None:
        a = self.era.at(x, snap) if hasattr(self.era, "at") else self.era
        b = self.whole.at(x, snap) if hasattr(self.whole, "at") else self.whole
        return _joined(a, b, lambda: self._finer_near(x), lambda: self._beyond(x))

    def _beyond(self, x: float) -> bool:
        """Whether ``x`` lies past every price the era printed, above them or below; never for an era
        of one grid, which names no prices."""
        prices = getattr(self.era, "prices", None)
        return prices is not None and len(prices) > 0 and not prices[0] <= x <= prices[-1]

    def _finer_near(self, x: float) -> bool:
        """Whether the era printed, within 5% of ``x``, a price off the grid the rest of the tape
        prints at that price's level. Against the whole tape's, which the era's own prints help set,
        no price of a half-cent era sat off it, and the era was rebuilt on cents at its own lowest
        print (a twenty-fifth red team); against the rest, a sub-dollar stretch of four places
        prints what the tape prints below a dollar. An era of one grid (a split's adjustment) names
        no prices, and keeps its tick everywhere; so does an era the rest of the tape gives no grid."""
        prices = getattr(self.era, "prices", None)
        rest = self.rest() if prices is not None and self.rest is not None else None
        if prices is None or rest is None:
            return True
        lo, hi = bisect.bisect_left(prices, x / 1.05), bisect.bisect_right(prices, x * 1.05)
        level = rest.at if hasattr(rest, "at") else lambda p: rest
        # off the level's grid and off the finer one it prints at a rate -- just under a dollar a level
        # read from both sides is cents with four places at a rate, and a sub-dollar era's prints there
        # took its four places to $1.02 -- and more of them than the one in ten a level's grid leaves
        # off it: one sub-dollar print off a 0.0002 the rest shared by chance took them to $1.0065
        near = prices[lo:hi]
        off = sum(1 for p in near if (g := level(p)) is not None and not _on_grid(g, p)
                  and (g.fine is None or not _on_grid(g.fine, p)))
        return off >= 2 and 10 * off > len(near)


class Eras(NamedTuple):
    """Where the grid a tape prints on changes in time -- a tick-size change, a split, a
    split-adjusted history before the split -- the first bar of each era after the first, each
    era's own grids, and those read together with the whole tape's by price level. Found by price
    level alone, a tick that went from 0.05 to 0.01 at the same prices put 0.01 under the bars
    that printed 0.05, and a strategy keyed on the date walked (a fifteenth red team)."""

    starts: tuple
    times: tuple          # the timestamp of each of those bars
    grids: tuple          # each era's grid by price level (or its one grid), and its one grid
    joints: tuple         # each era's grids joined with the whole tape's by price level
    singles: tuple        # each era's one grid joined with the whole tape's one grid

    def of(self, i: int) -> int:
        return bisect.bisect_right(self.starts, i)

    def at_time(self, ts: float) -> int:
        """The era a rebuilt bar at ``ts`` falls in: by its time, not its place in the tape --
        a rebuilt tail's clock drifts from the real one, and the date is what a strategy knows."""
        return bisect.bisect_right(self.times, ts)


def _era_starts(per_bar: Sequence[Sequence[float]], least: int = 20) -> list[int]:
    """The bars at which the grid the tape prints on changes in time. Every bar is labelled with
    the coarsest grid of any long run of bars on it -- a run of at least ``least`` bars whose prices
    all sit on that grid, a stray bar or two off it allowed, and significant: its distinct prices all
    on that grid would be a one-in-a-million chance under the grid its bars would otherwise have.
    The eras are the stretches of one label. Binary segmentation missed an era in the middle (a tick
    that went to 0.05 and back), and one stray cent print in a nickel era moved its cut to that print
    (a sixteenth red team)."""
    flat = sorted({v for ps in per_bar for v in ps if v > 0 and math.isfinite(v)})
    n = len(per_bar)
    if n < 2 * least or len(flat) < 3:
        return []
    base = _grid(flat) or _grid(flat, 0.9)
    if base is None:
        return []
    # no step past the largest price: a tolerance that grows with the step put every price on one
    common = _grid([v for ps in per_bar for v in ps], 0.9, prints=True) or base
    steps = [st for st in _STEPS if base.step * (1 + 1e-9) < st <= min(flat[-1], max(base.step, common.step) * 1e4)
             and abs(st / base.step - round(st / base.step)) < 1e-6]

    def on(v: float, st: float) -> bool:
        m = round((v - base.off) / st)
        return abs(v - (base.off + m * st)) <= max(min(st * 1e-3, max(v, st) * 1e-10), math.ulp(v))
    label = [base.step] * n
    for st in reversed(steps):                       # finest first, so a coarser run relabels a finer one
        good = [all(on(v, st) for v in ps if v > 0 and math.isfinite(v)) for ps in per_bar]
        runs: list[list[int]] = []
        i = 0
        while i < n:
            if not good[i]:
                i += 1
                continue
            j = i
            while j < n and good[j]:
                j += 1
            if runs and i - runs[-1][1] <= 2:
                # a stray bar or two off the grid inside a run, up to one bar in four: at one in fifty,
                # strays one bar in twenty broke a nickel tape's runs into one-bar eras of a finer tick
                # (a twenty-third red team), and at one in ten, four stray bars in a 30-bar nickel era
                # left no run of twenty, and the era was rebuilt on cents (a twenty-fifth). A finer tick
                # puts most bars off, not one in four.
                a, b = runs[-1][0], j
                if sum(1 for t in range(a, b) if not good[t]) <= max(1, (b - a) // 4):
                    runs[-1][1] = j
                    i = j
                    continue
            runs.append([i, j])
            i = j
        for a, b in runs:
            if b - a < least:
                continue
            ref = max(label[a:b])
            if st <= ref * (1 + 1e-9):
                continue
            distinct = {v for t in range(a, b) if good[t] for v in per_bar[t] if v > 0 and math.isfinite(v)}
            if len(distinct) * math.log(st / ref) > math.log(1e6):
                for t in range(a, b):
                    label[t] = st
    # A stretch shorter than a run -- bars left between two runs of one grid by strays more than a
    # run lets by -- is not an era of its own: it takes its neighbours' grid, or the finer of two, where
    # most of its bars sit on that grid. Where they do not, it is a real era the runs around it ate into
    # across its chance bars: a 25-bar half-cent era shrank to 18 and was taken for strays (a
    # twenty-fourth red team).
    segs: list[list] = []
    for t in range(n):
        if segs and segs[-1][0] == label[t]:
            segs[-1][2] = t + 1
        else:
            segs.append([label[t], t, t + 1])
    for j, (lab, a, b) in enumerate(segs):
        if b - a >= least or len(segs) == 1:
            continue
        near = [segs[x][0] for x in (j - 1, j + 1) if 0 <= x < len(segs) and segs[x][2] - segs[x][1] >= least]
        if near and 2 * sum(1 for t in range(a, b) if all(on(v, min(near)) for v in per_bar[t]
                                                          if v > 0 and math.isfinite(v))) > b - a:
            to = min(near)
            for t in range(a, b):
                label[t] = to
            segs[j][0] = to
    return [t for t in range(1, n) if label[t] != label[t - 1]]


# A split's adjustment: prices before a 3-for-2 split divided by 1.5 and written to four places sit
# on no decimal grid coarser than 0.0001, of which one point in 67 is an adjusted cent. The ratios
# of p-for-q splits and reverse splits up to ten, the stock dividends booked as splits (11-for-10,
# 21-for-20), and the larger reverse splits. 1 first: a tick rounded to fewer places than it needs
# (1/32 written to four places) sits on no decimal grid either (a sixteenth red team).
_RATIOS = [1.0] + sorted({float(Fraction(a, b)) for a in range(1, 11) for b in range(1, 11)
                          if math.gcd(a, b) == 1 and a != b}
                         | {1.1, 1.05, 1 / 1.1, 1 / 1.05, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 100.0,
                            1 / 12, 1 / 15, 1 / 20, 1 / 25, 1 / 30, 1 / 40, 1 / 50, 1 / 100})


def _scaled(values: Sequence[float], grid: Grid | None, share: float = 0.9) -> Grid | None:
    """A grid the values sit on, written to the places the tape writes, coarser than ``grid``: a
    tick the tape rounds (1/32 to four places), or a decimal tick divided by a split's ratio -- or
    None. Its step must be at least three of the tape's last places: finer than that, the rounding
    itself lets structured prices fit a grid they were never on, and an 11-for-10 history was fit to
    a tick of 1/5600 (a sixteenth red team).

    The places are those most prints are written to, and prints off the rule -- up to ``share``'s
    remainder of them -- are kept as its finer grid, at the rate they print where they lie: one stray
    print of six places set the places of every price, no rounded or adjusted tick fit, and a 1/32
    tick and a 3-for-2 adjusted cent were each rebuilt off their rule (a twenty-third red team)."""
    if grid is None or grid.scale != 1.0:
        return None
    vals = sorted({v for v in values if v > 0 and math.isfinite(v)})
    if len(vals) < 6:
        return None
    counts = collections.Counter(v for v in values if v > 0 and math.isfinite(v))
    common = _grid(values, share, prints=True)
    ref = common if common is not None and common.digits is not None else grid
    if ref.digits is None:
        return None
    def search(allowed: int, decimal_at_one: bool, ref: Grid) -> tuple:
        digits = ref.digits
        assert digits is not None
        unit = 10.0 ** -digits
        best, best_eff, best_off = None, 0.0, ()
        for r in _RATIOS:
            for base in _STEPS:
                eff = base / r
                # at least three of the tape's last places: a 30-for-1 reverse split's 0.3 at one place,
                # and a 10-for-3's 0.003 at three, sat on the floor and were never tried
                if eff <= ref.step * 1.5 or eff < 3 * unit * (1 - 1e-9) or eff <= best_eff * (1 + 1e-9):
                    break
                decimal = any(abs(eff / st - 1) < 1e-9 for st in _DECIMALS)
                if r != 1.0 and decimal:
                    # a decimal tick: tried unscaled, at r == 1. Not a binary one: an 8-for-5 history's
                    # 0.01/1.6 is 1/160, and unscaled its halfway prices rounded the other way (a
                    # twenty-fifth red team)
                    continue
                if r == 1.0 and decimal and not decimal_at_one:
                    continue                   # a decimal tick with prints off it: _grid's and _local_grids' job
                if len(vals) * math.log(eff / unit) <= math.log(1e6):
                    continue
                points: dict[int, float] = {}
                off: list[float] = []
                missed = 0
                places = _places(base)
                for v in vals:
                    k = round(v * r / base)
                    if round(round(k * base, places) / r, digits) != v:
                        off.append(v)
                        missed += counts[v]
                        if missed > allowed:
                            break
                        continue
                    points.setdefault(k, v)
                else:
                    # and not by chance: only a fitted price off every coarser decimal grid this one
                    # refines is evidence for it -- every cent is a point of 0.001/0.9, and a cent tape
                    # with a few sub-dollar prints was fit a 10-for-9 split it never had (a
                    # twenty-fourth red team) -- less the ways of choosing the prices that do not fit
                    # (a multiple within a million steps: past that a float has no fraction left, and a
                    # 4/3-adjusted cent's step took 2e13 for the decimal it refines -- no fitted price
                    # was evidence, and the rule was dropped for a plain 0.0025; a twenty-fifth red team)
                    coarse = next((d for d in reversed(_DECIMALS) if 1.5 * eff <= d <= 1e6 * eff
                                   and abs(d / eff - round(d / eff)) < 1e-6), None)
                    gone = set(off)
                    fits = [v for v in vals if v not in gone]
                    evidence = sum(1 for v in fits if coarse is None or abs(v / coarse - round(v / coarse)) > 1e-6)
                    if evidence * math.log(eff / unit) - _log_choose(len(vals), len(off)) <= math.log(1e6):
                        continue
                    best, best_eff, best_off = Grid(base, 0.0, points, digits, True, scale=r), eff, tuple(off)
                    break
        return best, best_eff, best_off
    # A rule every price fits first, as round twenty-two took it; prints off a rule only where none fits
    # them all: with strays allowed first, the coarsest rule that fit nine prints in ten won, and a tape
    # quoted in 64ths was rebuilt on 32nds (a twenty-fourth red team). Strays only where they share a
    # grid the rebuild can print them on at their rate: left out, a strategy keyed on their absence
    # would tell the rebuild apart the other way.
    # The rule every price fits coarser than the grid every price fits, where the tape writes them
    # all to its places: coarser only than the grid nine prints in ten share, a tape of 64ths whose
    # odd 64ths print one time in twenty never tried 1/64, and was rebuilt on 32nds with prices of
    # five places no 64th is (a twenty-fifth red team)
    best, best_eff, best_off = search(0, True, grid if grid.digits is not None else ref)
    every = _grid(vals)
    if best is None and every is not None:
        best, best_eff, best_off = search(int(sum(counts.values()) * (1.0 - share) + 1e-9), False, ref)
        # never finer than the grid nine prices in ten share: one stray print of seven places let a
        # step of 3.125e-7 at 0.9 fit every cent price, and rebuilt prices left the cent (a
        # twenty-second red team)
        if best is not None and common is not None and best_eff < common.step / common.scale * (1 - 1e-9):
            return None
    if best is not None and best_off and every is not None:
        span = (best_off[0] - best_eff, best_off[-1] + best_eff)
        inside = sum(counts[v] for v in vals if span[0] <= v <= span[1])
        best = best._replace(fine=every, rate=sum(counts[v] for v in best_off) / max(1, inside), span=span)
    return best


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _digits(step: float, off: float, vals: Sequence[float]) -> int | None:
    """The decimal places every value was rounded to, where they all were and the grid needs no
    more; None where the tape writes its values some other way."""
    for d in range(13):
        if round(step, d) == step and round(off, d) == off:
            return d if all(v == round(v, d) for v in vals) else None
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
    if how == "up":
        return _point(grid, _ceil_n(grid, x))
    if how == "down":
        y = _point(grid, _floor_n(grid, x))
    else:
        n = round(_index(grid, x))
        y = min((_point(grid, m) for m in (n - 1, n, n + 1)), key=lambda p: (abs(p - x), p))
    # never a positive value set to zero or below: a rebuild carried across a 1:100 reverse split onto
    # the new era's coarser tick printed bars of 0.0, and an honest strategy's log of them escaped the
    # audit (a twenty-fourth red team)
    return y if y > 0 or x <= 0 else _point(grid, _ceil_n(grid, x))


def _point(grid: Grid, n: int) -> float:
    """Grid point ``n``, as the tape printed it where it did: otherwise written the way the tape
    writes its values -- rounded to its places, divided by its adjustment, stored at its width."""
    got = grid.vals.get(n)
    if got is not None:
        return got
    x = grid.off + n * grid.step
    if grid.scale != 1.0:
        # the unadjusted price as its tick writes it, then divided: n * 0.01 is not the float a vendor
        # divides, and its halfway prices of an 8-for-5 history rounded the other way (a
        # twenty-fifth red team)
        x = round(x, _places(grid.step)) / grid.scale
    if grid.digits is not None:
        x = round(x, grid.digits)
    return _f32(x) if grid.width == 32 else x


def _places(step: float) -> int:
    """The decimal places a tick is written to: 2 for a cent, 6 for 1/64."""
    return next((d for d in range(16) if round(step, d) == step), 15)


def _index(grid: Grid, x: float) -> float:
    return (x * grid.scale - grid.off) / grid.step


def _on_grid(grid: Grid, x: float) -> bool:
    """Whether ``x`` is a point of ``grid``, to a millionth of a step."""
    return abs(x - _point(grid, round(_index(grid, x)))) <= grid.step / grid.scale * 1e-6


def _tol(grid: Grid, x: float) -> float:
    step = grid.step / grid.scale
    return 1e-9 * (min(step, abs(x)) if x else step)


def _ceil_n(grid: Grid, x: float, strict: bool = False) -> int:
    """The first grid point at or past ``x`` -- strictly past, with ``strict`` -- a billionth of a
    step counting as on it. Found from the points themselves, not from the arithmetic of the
    index: a split-adjusted grid's points are rounded, and sit off their index by up to a part in
    a hundred of a step. The billionth is of the step or of ``x``, whichever is less: of a step
    far wider than the price, it counted a point below ``x`` as at it (a nineteenth red team)."""
    tol = _tol(grid, x)
    if math.ulp(x) >= grid.step / grid.scale:
        return round(_index(grid, x))       # the floats cannot tell its points apart here
    ok = (lambda p: p > x + tol) if strict else (lambda p: p >= x - tol)
    n = math.ceil(_index(grid, x) - 1e-9)
    while not ok(_point(grid, n)):
        n += 1
    while ok(_point(grid, n - 1)):
        n -= 1
    return n


def _floor_n(grid: Grid, x: float, strict: bool = False) -> int:
    """The last grid point at or before ``x`` -- strictly before, with ``strict``."""
    tol = _tol(grid, x)
    if math.ulp(x) >= grid.step / grid.scale:
        return round(_index(grid, x))
    ok = (lambda p: p < x - tol) if strict else (lambda p: p <= x + tol)
    n = math.floor(_index(grid, x) + 1e-9)
    while not ok(_point(grid, n)):
        n -= 1
    while ok(_point(grid, n + 1)):
        n += 1
    return n


def _f32(x: float) -> float:
    """``x`` stored as a float32 and read back."""
    if not math.isfinite(x) or abs(x) > 3.4e38:
        return x
    return struct.unpack("f", struct.pack("f", x))[0]


def _spell32(v: float) -> float:
    """The shortest decimal a float32 value was stored from: 20.520000457763672 is 20.52."""
    for d in range(10):
        r = round(v, d)
        if _f32(r) == v:
            return r
    return v


def _width(values: Sequence[float]) -> int:
    """32 where every value the tape stores is a float32 read back, 64 otherwise. A cent tape
    stored as float32 sat on no grid at a float64's precision, and was rebuilt in arbitrary
    doubles a strategy could tell from float32 cents (a fifteenth red team)."""
    vals = [v for v in values if math.isfinite(v) and v != 0]
    return 32 if vals and all(_f32(v) == v for v in vals) else 64


def _widen(g: Any, memo: dict | None = None) -> Any:
    """A grid found on float32 spellings, holding the float32 values the tape stores."""
    memo = {} if memo is None else memo
    if g is None:
        return None
    if id(g) in memo:
        return memo[id(g)]
    if isinstance(g, Grids):
        out: Any = Grids([_f32(p) for p in g.prices], [_widen(x, memo) for x in g.grids])
    else:
        out = g._replace(vals={n: _f32(v) for n, v in g.vals.items()}, width=32, fine=_widen(g.fine, memo),
                         span=tuple(_f32(v) for v in g.span))
    memo[id(g)] = out
    return out


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
    first = _ceil_n(grid, lo, strict=not lo_in)
    last = None
    if math.isfinite(hi):
        last = _floor_n(grid, hi, strict=not hi_in)
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
    exactly that far off a grid; on one, the first grid point at least that far from the open (and
    at least one step) that is not a level -- a tie nothing aimed at. None where the grid has no
    such point above zero. The same function sets the close and says what it reaches, so the
    note and the bar cannot disagree -- they did, when a tick nudge carried a floor push past
    levels counted as out of its reach (a twelfth red team)."""
    x = o * math.exp(side * sg)
    if grid is None:
        return x
    n = _ceil_n(grid, x) if side > 0 else _floor_n(grid, x)
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


def _extremes(o: float, sizes: Sizes, sg: float, refs: frozenset, bar: Any = None) -> tuple[float | None, float | None]:
    """The farthest close each way from open ``o`` a move of the tape's own reaches, on its grid
    (None where not one grid point is within the largest move it has made); on a tape with no
    moves, the floor push each way."""
    if o <= 0:
        return None, None
    pg = _bar_grid(sizes, o, bar)
    if sizes.moves:
        top = sizes.moves[-1]
        if pg is None:
            return o * math.exp(top * (1.0 - EDGE)), o * math.exp(-top * (1.0 - EDGE))
        cu, cd = o * math.exp(top), o * math.exp(-top)
        return _grid_pick(cu, pg, o, cu, False, True), _grid_pick(cd, pg, cd, o, True, False)
    return _floor_close(o, 1, sg, pg, refs), _floor_close(o, -1, sg, pg, refs)


# Each tie a field of the probed bar can carry, by what it sits on: the close, high or low on a level
# of the previous bar ("close", "high", "low"); the close on the bar's own open ("doji"); the high or
# low on its own open or close (no wick that side); the volume on the previous bar's; the next open on
# a level or the bar's own open, on its close (ungapped), or on its own high or low. Kept apart: a
# field holding two of them let a draw carrying the one the real bar did not print be credited because
# the real bar printed the other -- a wickless bar where the real bar's high sat on the previous high
# (a twenty-third red team).
PREV_LEVELS = ("open", "close", "high", "low")
# ... and one per previous-bar level, not one for all four: a field holding the four let a draw with the
# low on the previous low be credited where the real low sat on the previous close, and a next open
# on the previous high where the real one sat on this bar's own open (a twenty-fourth red team)
ALL_TIES = frozenset({f"{f}@{ref}" for f in ("close", "high", "low") for ref in PREV_LEVELS}
                     | {"doji", "high_open", "high_close", "low_open", "low_close", "volume"}
                     | {f"next@{ref}" for ref in ("own", *PREV_LEVELS)} | {"next_close", "next_high", "next_low"})


def _bad_levels(o: float, prev: Any, avoid: frozenset) -> tuple[frozenset, frozenset, frozenset]:
    """The prices the probed bar's close, high and low must each stay off: each of the previous bar's
    levels the real bar did not print that field on, and the bar's own open where it printed it not
    there."""
    def bad(name: str, on_open: str) -> frozenset:
        out = {lv for ref in PREV_LEVELS if (lv := getattr(prev, ref)) > 0 and f"{name}@{ref}" in avoid}
        if o > 0 and on_open in avoid:
            out.add(o)
        return frozenset(out)
    return bad("close", "doji"), bad("high", "high_open"), bad("low", "low_open")


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
    named = [(ref, getattr(prev, ref)) for ref in PREV_LEVELS]
    out = set()
    for name in ("close", "high", "low"):
        v = getattr(bar, name)
        out |= {f"{name}@{ref}" for ref, lv in named if v == lv}
        if v == bar.open:
            out.add("doji" if name == "close" else name + "_open")
    # a high or low on the bar's own close is a tie as much as one on its open: a bar with no wick
    # that side. Built only by the repair of the next open past its own high, it was a shape no
    # other draw printed and a strategy standing down on it walked (a twenty-second red team)
    out |= {name + "_close" for name in ("high", "low") if getattr(bar, name) == bar.close}
    if bar.volume == prev.volume:
        out.add("volume")
    if k + 1 < len(bars) and (sizes.gaps_up or sizes.gaps_dn):
        nxt = bars[k + 1].open
        out |= {f"next@{ref}" for ref, lv in named if nxt == lv}
        if nxt == bar.open:
            out.add("next@own")
        if nxt == bar.close:
            out.add("next_close")
        # its own high and its own low apart: one field for both let a real next open on the low
        # credit every draw that put it on the high (a twenty-fifth red team)
        if nxt == bar.high:
            out.add("next_high")
        if nxt == bar.low:
            out.add("next_low")
    return frozenset(out)


def _tie_of(rel: tuple, gapped: bool) -> frozenset | None:
    """The fields a relation sets level, or None for a strict relation."""
    kind = rel[0]
    on_levels = lambda name: {f"{name}@{ref}" for ref in PREV_LEVELS}    # noqa: E731
    if kind == "move" and rel[1] == 0:
        # on the open, and so on any of the previous bar's levels the open itself is on: one fact
        return frozenset({"doji"} | on_levels("close"))
    if kind == "rel" and rel[3] == 0:
        # on one level: on every other level, and on the bar's own open, only where they are that price
        return frozenset(on_levels(rel[1]) | {"doji" if rel[1] == "close" else rel[1] + "_open"})
    if rel == ("vol", 0) or (kind == "zero" and rel[1] is True):
        return frozenset(("volume",))          # a zero is a tie only after a bar that did not trade
    if kind == "next_gap" and rel[1] == 0:
        return frozenset(("next_close",))
    if kind == "next_rel" and rel[2] == 0:
        if rel[1] in ("own_high", "own_low"):
            # by a gap onto it; on a tape that never gaps, from a close with no wick that side. Not
            # both on a gappy tape: the close on the low, the next open on the close, carried two ties
            # the real bar did not print, and was credited (a twenty-fifth red team)
            if gapped:
                return frozenset(("next_high" if rel[1] == "own_high" else "next_low",))
            return frozenset(("high_close" if rel[1] == "own_high" else "low_close",))
        # on a tape that never gaps the next open is the close: the close's tie
        return (frozenset({f"next@{ref}" for ref in ("own", *PREV_LEVELS)}) if gapped
                else frozenset(on_levels("close") | {"doji"}))
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
        return "edge" if item[1] in ("own_high", "own_low") else "next"
    return None


def _is_tie(plan: Plan) -> bool:
    """A plan that sets something level with its reference."""
    return bool(plan.close_at or plan.vol_eq or any(sd == 0 for _, sd in (*plan.hi_vs, *plan.lo_vs))
                or plan.next_gap == 0 or (plan.next_past is not None and plan.next_past[1] == 0))


def _detail(sigma: float | None, floors: Sequence[str], off_scale: bool = False, moves: bool = True,
            past: Sequence[str] = ()) -> str:
    """How a proof's varied tape was varied, in words that are true of it. Under a caller's sigma
    the probed bar is still pushed by sizes the tape has made, where it has made any; only the
    bars after it move by the sigma (a fourteenth red team read the line as a claim about the bar).
    A caller's sigma is the size a tape with no moves is pushed by, not a floor (a fifteenth red
    team's line said both). ``past`` names prices that setting on their price level's tick carried
    past the largest move or wick the tape has made."""
    # 'no trades' pushed nothing, so no floor size was used (a sixteenth red team's line said one was)
    floors = [f for f in floors if f != "no trades" and not (f == "moves" and sigma is not None)]
    if sigma is None and not floors and not off_scale and not past:
        return "varied at the tape's own scale"
    by_sigma = ((", the bar itself by sizes the tape has made and later bars with moves of sigma "
                 if moves else ", with moves of sigma ") + f"{sigma:g}") if sigma is not None else ""
    return ("varied" + by_sigma
            + (", with a floor size where the tape has made none" if floors else "")
            + (", with its close one step of the tape's price grid off its open, farther than any move "
               "the tape has made at that price" if off_scale else "")
            + _past_clause(past))


def _past_clause(past: Sequence[str]) -> str:
    """Prices that setting on their price level's tick carried past the largest size of their kind
    the tape has made: the probed bar's close (a move), high and low (wicks), and the next bar's open
    (a gap)."""
    own = [p for p in past if p != "next open"]
    names = (["its " + " and ".join(own)] if own else []) + (["the next bar's open"] if "next open" in past else [])
    kinds = (["move"] if "close" in own else []) + (["wick"] if set(own) - {"close"} else []) \
        + (["gap"] if "next open" in past else [])
    if not names:
        return ""
    return (", with " + " and ".join(names) + " set on the tick of its price level, past the largest "
            + " or ".join(kinds) + " the tape has made")


SIGMA_MAX = 0.5     # the largest caller's sigma taken: a later bar's log move of a half, 65%


def _ceil_sig(x: float, digits: int = 3) -> float:
    """``x`` rounded UP to ``digits`` significant figures: a bound printed as the least that will do
    must be one that does. Rounded with an epsilon, 0.07/11.2 came out as 0.00625, below itself, and
    the sigma a refusal named was refused again (a twenty-first red team)."""
    if x <= 0 or not math.isfinite(x):
        return x
    q = 10.0 ** (math.floor(math.log10(x)) - digits + 1)
    y = float(f"{math.ceil(x / q) * q:.{digits}g}")
    while y < x:
        y = float(f"{y + q:.{digits}g}")
    return y


def _check_sigma(tape: Sequence[Any], sizes: Sizes, sigma: float) -> None:
    """A caller's sigma a later bar can move by: a positive finite number, no more than ``SIGMA_MAX``,
    and at least the tape's tick -- or, off a grid, a thousand units in the last place of the float --
    as a share of the price, at every price the tape closed at. Under a sigma below one tick at a $2
    stock's price, every rebuilt move rounded to nothing, the proof line said 'moves of sigma 0.0005'
    over a tail of dojis, and a strategy keyed on the dojis walked (a nineteenth red team); a float
    tape took a sigma of 1e-17 the same way, a sigma of a million overflowed, a sigma of five hung the
    snap on prices of 1e21, and the least sigma the refusal named, rounded down, was refused again (a
    twentieth)."""
    # a real number, taken as a float: a Decimal passed every check here and failed in the rebuild's
    # arithmetic after the strategy had run (a twenty-first red team). Each refusal says what is wrong
    # with it: 10**400, a Decimal and a sigma below the least float were each told they 'must be a
    # positive finite number', and 400 digits of the first were printed (a twenty-third red team).
    shown = _shown(sigma)
    if isinstance(sigma, bool) or not isinstance(sigma, numbers.Real):
        raise ValueError(f"sigma must be an int or a float, not {shown}")
    given = sigma
    try:
        sigma = float(sigma)
    except OverflowError:
        sigma = math.inf if given > 0 else -math.inf
    except (TypeError, ValueError, ArithmeticError):
        raise ValueError(f"sigma must be an int or a float, not {shown}") from None
    if math.isnan(sigma):
        raise ValueError(f"sigma must be a number, not {shown}")
    if not (sigma > 0 or (sigma == 0 and given > 0)):
        raise ValueError(f"sigma must be more than zero, not {shown}")
    by, whole = _prices_by_bar(sizes), sizes.price_grid
    worst, why = None, ""
    # the prices each era printed, for what the tape prints near a close
    cuts = [0, *(sizes.eras.starts if sizes.eras else ()), len(tape)]
    printed = [sorted({v for bb in tape[a:z] for v in (bb.open, bb.high, bb.low, bb.close) if v > 0})
               for a, z in zip(cuts, cuts[1:])]
    for i, b in enumerate(tape):
        x = b.close
        if x <= 0:
            continue
        g = by(b.ts) if callable(by) else by
        h = g.at(x) if hasattr(g, "at") else g
        if isinstance(h, Grid):
            share, kind = h.step / h.scale / x, "tick"
            near = sum(1 for v in h.vals.values() if abs(v - x) <= 0.05 * x)
            # the tape's own prices near the close, in its era: where most sit off the level grid and
            # on the whole one, the level grid is no tick the tape keeps there -- the top four prices
            # of a cent tape, even by chance, were named a tick of 0.02 at 28.6 while the tape printed
            # 28.59 beside it (a twenty-fourth red team)
            era = printed[bisect.bisect_right(cuts, i) - 1]
            lo_i, hi_i = bisect.bisect_left(era, x / 1.05), bisect.bisect_right(era, x * 1.05)
            around = era[lo_i:hi_i]
            finer = (isinstance(whole, Grid) and around and 2 * sum(1 for v in around if not _on_grid(h, v)
                                                                   and _on_grid(whole, v)) > len(around))
            # ... and only where chance would put that many prices on it: a band crossed by a fast rally
            # has a print or two near each close, and its nickel yielded to the tape's cent, taking a
            # sigma that made the tail 90% dojis there (a twenty-fourth red team)
            chance = (len(h.vals) * math.log((h.step / h.scale) / (whole.step / whole.scale))
                      if isinstance(whole, Grid) and whole.step / whole.scale < h.step / h.scale else math.inf)
            if (isinstance(whole, Grid) and whole.step / whole.scale < h.step / h.scale and _on_grid(whole, x)
                    and ((h.vals and near <= 2 and chance <= math.log(1e6)) or finer)):
                # a level grid with a print or two at this price is no tick the tape keeps there: its
                # whole grid. A real era's or band's is: yielding to the whole grid there, the check
                # misnamed a nickel era's tick and took a sigma that made a band's tail all dojis (a
                # twenty-second red team). Judged by its prints near the price, not by all of them: one
                # bad close at 25 on a tape at 50 shared a grid of 0.02 with three real prices near 50,
                # and the refusal named twice the tape's cent (a twenty-third). A grid joined from an
                # era's and the whole tape's has no prints of its own, and keeps its step.
                share = whole.step / whole.scale / x
        else:
            share, kind = 1e3 * math.ulp(x) / x, "float"
        if worst is None or share > worst[0]:
            worst, why = (share, i), kind
    least = _ceil_sig(worst[0]) if worst is not None else 0.0
    what = (f"the tick at bar {worst[1]}, about {least:g} of its price" if why == "tick" else
            f"what a float can move a price by at bar {worst[1]}, {least:g} of it") if worst else ""
    if worst is not None and least > SIGMA_MAX:
        raise ValueError(f"no sigma can be taken on this tape: {what}, is more than the largest sigma taken "
                         f"({SIGMA_MAX:g}); audit it without a sigma")
    if sigma > SIGMA_MAX:
        raise ValueError(f"sigma {shown} is more than the largest sigma taken, {SIGMA_MAX:g} (a later bar's log "
                         f"move of {SIGMA_MAX:g})")
    if worst is not None and sigma < least:
        raise ValueError(f"sigma {shown} is below {what}: a move that small cannot be printed there; give a "
                         f"sigma of at least {least:g}")
    if sigma == 0:
        raise ValueError(f"sigma {shown} is below the least float above zero: no move can be made of it")
    return sigma


def _shown(x: Any) -> str:
    """``x`` as a message prints it: its repr, cut short where it runs past sixty characters."""
    r = repr(x)
    return r if len(r) <= 60 else f"{r[:28]}...{r[-12:]} ({len(r)} characters)"


def _check_arguments(tape: Sequence[Any], *, boundaries: Sequence[int] | None = None, draws: int | None = None,
                     sigma: float | None = None, probes: str = "sparse", seed: int | None = None,
                     sizes: Sizes | None = None) -> tuple[dict, Sizes | None]:
    """Every argument of an audit checked, and taken in the form the audit uses, before the strategy
    runs once: whole numbers for the seed, the draws and the boundaries (numpy's included), a sigma
    as a float, the tape a tape. Checked after the gates, a bad tape cost six runs of the strategy
    and an infinite close came back as a bare 'math domain error'; a numpy seed crashed the audit
    after it had run (a twenty-first red team)."""
    try:
        n = len(tape)
        tape[0] if n else None
    except (TypeError, KeyError, IndexError):
        # a generator of bars has no length, and cannot be replayed: the audit runs the strategy on the
        # tape again and again (a twenty-third red team crashed on one with a bare TypeError)
        raise ValueError(f"the tape must be a sequence of bars (a list, say), not {_shown(tape)}") from None
    if n < 8:
        raise ValueError(f"need at least 8 bars to probe, got {n}")
    _validate(tape)
    if probes not in ("sparse", "every_bar"):
        raise ValueError(f"probes must be 'sparse' or 'every_bar', not {probes!r}")

    def whole(name: str, x: Any) -> int:
        if isinstance(x, bool):
            raise ValueError(f"{name} must be a whole number, not {x!r}")
        try:
            return operator.index(x)
        except TypeError:
            raise ValueError(f"{name} must be a whole number, not {x!r}") from None
    seed = None if seed is None else whole("seed", seed)
    draws = None if draws is None else whole("draws", draws)
    if draws is not None and draws < 0:
        raise ValueError(f"draws must be zero or more, not {draws}")
    if draws is not None and draws > MAX_DRAWS:
        # every bar's draws are built before its first run: ten million of them used a gigabyte and a
        # half of the auditor's own memory after two runs of the strategy (a twenty-third red team)
        raise ValueError(f"draws must be at most {MAX_DRAWS}, not {_shown(draws)}: past a bar's eighth, a draw "
                         f"repeats one of its eight sign combinations at random")
    if boundaries is not None:
        try:
            boundaries = [whole("a boundary", b) for b in boundaries]
        except TypeError:
            raise ValueError(f"boundaries must be a sequence of whole numbers, not {boundaries!r}") from None
    if sigma is not None:
        sizes = sizes if sizes is not None else _sizes(tape)
        sigma = _check_sigma(tape, sizes, sigma)
    # a list, whatever sequence it came as: a deque passed every check and failed at the first cut, tape[:k]
    # (a twenty-fourth red team)
    tape = tape if isinstance(tape, list) else list(tape)
    return dict(boundaries=boundaries, draws=draws, sigma=sigma, probes=probes, seed=seed, tape=tape), sizes


def _validate(tape: Sequence[Any]) -> None:
    """Every timestamp, price and volume a number the arithmetic can use. An infinite high crashed
    the builder with a bare math domain error (a twelfth red team); a NaN compares false with
    everything and made relations silently meaningless."""
    kinds: set = set()
    for i, b in enumerate(tape):
        # a bar the audit can vary: namedtuple bars were refused only after the truncation phase had run
        # the strategy once a bar, and tuples or dicts crashed with an AttributeError naming no bar (a
        # twenty-fourth red team)
        if not dataclasses.is_dataclass(b) or isinstance(b, type):
            raise ValueError(f"bar {i} is of type {type(b).__name__}, not a bar: every bar must be a dataclass with ts, "
                             f"open, high, low, close and volume (the audit varies bars with dataclasses.replace)")
        for name in ("ts", "open", "high", "low", "close", "volume"):
            if not hasattr(b, name):
                raise ValueError(f"bar {i} has no {name}; every bar needs ts, open, high, low, close and volume")
            v = getattr(b, name)
            if not isinstance(v, numbers.Real) or isinstance(v, bool):
                # a Decimal is finite and passed, and the audit then crashed on its first sum with a
                # float, a bare TypeError naming no bar (a twenty-third red team)
                raise ValueError(f"bar {i} has a {name} of {_shown(v)}; every timestamp, price and volume "
                                 f"must be a real number, an int or a float")
            try:
                ok = math.isfinite(v)
            except (TypeError, OverflowError):
                ok = False
            if not ok:
                raise ValueError(f"bar {i} has a {name} of {_shown(v)}; every timestamp, price and volume "
                                 f"must be a finite number")
        # ... each of the six a field the bar is made with, once for each kind of bar: a close that was a
        # property, or a field with init=False or an InitVar beside it, passed and crashed the audit with
        # a bare dataclasses error after the strategy had run (a twenty-fifth red team)
        if type(b) not in kinds:
            made = {f.name for f in dataclasses.fields(b) if f.init}
            for name in ("ts", "open", "high", "low", "close", "volume"):
                if name not in made:
                    raise ValueError(f"bar {i}'s {name} is not a field its {type(b).__name__} is made with (a "
                                     f"property, or a field with init=False): the audit varies bars with "
                                     f"dataclasses.replace, which cannot set it")
            try:
                dataclasses.replace(b, **{n: getattr(b, n) for n in ("ts", "open", "high", "low", "close", "volume")})
            except Exception as e:
                raise ValueError(f"bar {i} cannot be remade with its own values by dataclasses.replace, as the "
                                 f"audit varies bars: {type(e).__name__}: {e}") from None
            kinds.add(type(b))
        # Prices are varied by log moves, which a price below zero has none of: a tape of negative prices
        # was rebuilt at 0.0, a price it never printed, told it had made no moves, and an honest strategy
        # dividing by the last close crashed on it (a twenty-fourth red team).
        for name in ("open", "high", "low", "close"):
            if getattr(b, name) < 0:
                raise ValueError(f"bar {i} has a {name} of {_shown(getattr(b, name))}; the audit varies prices by "
                                 f"their log moves, which a price below zero does not have: shift the series above "
                                 f"zero to audit it (a spread, or a contract that went negative)")
        # Every volume relation, floor and zero rule reads volume as traded size. A signed volume --
        # net delta, say -- was probed upward only and told no bar traded (a sixteenth red team).
        if b.volume < 0:
            raise ValueError(f"bar {i} has a volume of {b.volume!r}; a volume is a traded size, zero or more "
                             f"(a signed volume such as net delta is not one)")


def _close_near(level: float, o: float, up: float | None, dn: float | None, sizes: Sizes,
                rng: random.Random, bad: frozenset, bar: Any = None) -> float | None:
    """A close from which a gap of the tape's own reaches ``level`` exactly: near it, on the side a
    gap the tape has made can come from, within a move of the tape's own, and not on a level. So
    the next bar can open on a level without the close sitting on one too -- a draw carrying two
    ties is a draw an evader keyed on ties could hide behind."""
    if level <= 0:
        return None
    pg = _bar_grid(sizes, o, bar)
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
                  bad: frozenset, notes: dict, reach_need: float | None = None, bar: Any = None) -> float | None:
    """The probed bar's close where no tie sets it: on the plan's side of the open (a random side
    where it forces none), in a band between the previous bar's levels -- the farthest a move of
    the tape's own reaches, or one at random -- and on the tape's grid STRICTLY inside that band,
    so it never lands on the level it was meant to pass. Snapping to the nearest tick used to put
    a far close exactly on the farthest level, and a one-draw audit counted it delivered (a
    twelfth red team)."""
    pg = _bar_grid(sizes, o, bar)
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
        # pushed one step anyway -- exactly one, even onto a level, so it passes none -- and a proof
        # from it says so rather than "at the tape's own scale".
        c = _grid_pick(o, pg, o, math.inf) if side > 0 else _grid_pick(o, pg, 0.0, o)
        if c is not None:
            notes["off_scale"] = True
        return c
    if not forced:
        mag = _draw_within(sizes.moves, 0.0, math.inf, rng) or sizes.moves[0]
        x = o * math.exp(side * mag)
        c = _free(x, pg, o, cap, False, True, bad) if side > 0 else _free(x, pg, cap, o, True, False, bad)
        if c is None or c in bad:
            # every point in reach a level: the first one, which passes none
            c = _grid_pick(o, pg, o, cap, False, True) if side > 0 else _grid_pick(o, pg, cap, o, True, False)
        return c
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
        # Every point the move reaches is a level: the first of them, a tie the check will see, and
        # never one past another level -- reach calls those out of reach, and the note says the
        # close was not pushed past them (a thirteenth red team found it was).
        return _grid_pick(o, pg, o, cap, False, True) if side > 0 else _grid_pick(o, pg, cap, o, True, False)
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
              bad_l: frozenset, bar: Any = None) -> tuple[float, float]:
    """The probed bar's high and low: each wick from the tape's own wick sizes, inside the price
    interval its targets need, on the tape's grid, and off every level it was not aimed at."""
    wicks, pg = sizes.wicks, _bar_grid(sizes, o, bar)

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
    # no wick on one side, where the tape prints bars with none: the next bar's open past this bar's
    # own high or low then needs a gap the tape has made from the close, not one past a wick too
    if plan.bare > 0 and sizes.wick0 and tie_h is None and a_lo <= 0.0:
        a = 0.0
    if plan.bare < 0 and sizes.wick0 and tie_l is None and b_lo <= 0.0:
        bw = 0.0
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
    last_traded = None
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
        if b.volume > 0 and last_traded is not None:
            # between successive traded bars, untraded ones between them or not: a tape whose traded
            # and untraded bars alternate still changes its traded volume (a fourteenth red team)
            r = abs(math.log(b.volume / last_traded))
            if r > 0:
                ratios.append(r)
        if b.volume > 0:
            last_traded = b.volume
        if before is not None:
            if before.volume > 0 and b.volume > 0 and b.volume == before.volume:
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
            if b.open in (before.high, before.low) and b.open != before.close:
                ties.add("edge")                 # an open on the bar before's own high or low
            step = b.ts - before.ts
            steps[step] = steps.get(step, 0) + 1
        two_back, before = before, b
    mode = max(steps, key=lambda st: (steps[st], -st)) if steps else None
    pg, vg, levels, eras, widths = _price_grids(tape)
    cal = _calendar(tape)
    _, rms = _local_stats(tape)
    signed = tuple(math.log(b.close / b.open) for b in tape if b.open > 0 and b.close > 0)
    return Sizes(sorted(moves), sorted(wicks), sorted(ratios), sorted(volumes), zero,
                 sorted(gaps_up), sorted(gaps_dn), gap0, sorted(steps), wick0,
                 pg.step if pg else None, vg.step if vg else None, frozenset(ties), mode, pg, vg, signed,
                 levels, eras, widths, _timing(tape, cal), cal, rms, _donor_index(tape, cal, rms),
                 _local_z(tape))


def _caps(sizes: Sizes) -> tuple:
    """The largest move, wick, gap up, gap down and volume the tape has printed: a rebuilt bar's never
    go past them."""
    return tuple(xs[-1] if xs else None for xs in (sizes.moves, sizes.wicks, sizes.gaps_up, sizes.gaps_dn,
                                                   sizes.volumes))


def _local_stats(tape: Sequence[Any], half: int = 32) -> tuple[tuple, tuple]:
    """Per bar, the mean and the root mean square log move of the bars within ``half`` of it that
    moved: the drift and the size of a move around it. Over every bar, still ones too, a tape where
    one bar in five moves gave its moving donors 2.3 times the sigma (a twenty-second red team). Zero
    where nothing around it moved."""
    mv = [math.log(b.close / b.open) if b.open > 0 and b.close > 0 else 0.0 for b in tape]
    s1, s2, cnt = [0.0], [0.0], [0]
    for x in mv:
        s1.append(s1[-1] + x)
        s2.append(s2[-1] + x * x)
        cnt.append(cnt[-1] + (x != 0.0))
    n = len(tape)
    mean, rms = [], []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        c = cnt[b] - cnt[a]
        mean.append((s1[b] - s1[a]) / c if c else 0.0)
        rms.append(math.sqrt((s2[b] - s2[a]) / c) if c else 0.0)
    return tuple(mean), tuple(rms)


def _local_mean(tape: Sequence[Any], half: int = 32) -> tuple:
    """Per bar, the mean log move of the bars around it that moved (``_local_stats``)."""
    return _local_stats(tape, half)[0]


TREND_ONLY = 0.05   # moves whose spread about the trend they follow is under this share of their size


def _local_z(tape: Sequence[Any], half: int = 32) -> tuple:
    """Per bar that moved, its log move as a share of the moves around it, net of the trend they
    follow: the move less a quadratic fitted through the moves of the bars within ``half`` of it
    that moved -- a full window, moved inward at the tape's ends -- over the root mean square of
    what the fit leaves there. None for a bar that did not move, and where what the fit leaves is
    under ``TREND_ONLY`` of the moves' own size: moves that are all trend, which a normal draw of
    the sigma stands in for. Against the mean alone, each bar of a steady ramp sat at its own
    window's mean, and a tail rebuilt from them moved at 0.3 times the sigma, 83% dojis (a
    twenty-third red team); against a line, a curving one would sit to one side of it throughout."""
    mv = [math.log(b.close / b.open) if b.open > 0 and b.close > 0 else 0.0 for b in tape]
    n, w = len(mv), 2 * half + 1
    out: list = []
    for i in range(n):
        if mv[i] == 0.0:
            out.append(None)
            continue
        a = max(0, min(i - half, n - w))
        pts = [((j - i) / half, mv[j]) for j in range(a, min(n, a + w)) if mv[j] != 0.0]
        size = math.sqrt(sum(y * y for _, y in pts) / len(pts))
        coef = _quadratic(pts) if len(pts) >= 6 else None
        if coef is None:
            mean = sum(y for _, y in pts) / len(pts)
            coef = (mean, 0.0, 0.0)
        c0, c1, c2 = coef
        left = math.sqrt(sum((y - (c0 + c1 * u + c2 * u * u)) ** 2 for u, y in pts) / len(pts))
        out.append((mv[i] - c0) / left if left > TREND_ONLY * size else None)
    return tuple(out)


def _quadratic(pts: Sequence[tuple[float, float]]) -> tuple[float, float, float] | None:
    """The least-squares c0 + c1*u + c2*u**2 through ``pts``; None where they do not fix one."""
    s = [0.0] * 5
    t = [0.0] * 3
    for u, y in pts:
        p = 1.0
        for k in range(5):
            s[k] += p
            if k < 3:
                t[k] += p * y
            p *= u
    m = [[s[0], s[1], s[2], t[0]], [s[1], s[2], s[3], t[1]], [s[2], s[3], s[4], t[2]]]
    for c in range(3):
        piv = max(range(c, 3), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) <= 1e-12 * max(1.0, s[0]):
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(3):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return m[0][3] / m[0][0], m[1][3] / m[1][1], m[2][3] / m[2][2]


def _price_grids(tape: Sequence[Any]) -> tuple:
    """The tape's price grid, volume grid, price grids by level, eras and float widths. Found on
    the decimals a float32 tape was stored from, and held at float32; a split-adjusted grid where
    the prices sit on one; eras where the grid changes in time."""
    prices = [x for b in tape for x in (b.open, b.high, b.low, b.close)]
    volumes = [b.volume for b in tape]
    pw, vw = _width(prices), _width(volumes)
    spell = (lambda v: _spell32(v)) if pw == 32 else (lambda v: v)
    per_bar = [[spell(x) for x in (b.open, b.high, b.low, b.close)] for b in tape]
    sp = [x for ps in per_bar for x in ps]
    pg, vg = _grid(sp), _grid([_spell32(v) for v in volumes] if vw == 32 else volumes)
    levels = _local_grids(sp)
    sc = _scaled(sp, pg or _grid(sp, 0.9))
    if sc is not None:
        pg, levels = sc, None
    starts = _era_starts(per_bar)
    own = []
    for a, b in zip([0] + starts, starts + [len(tape)]):
        ev = [x for ps in per_bar[a:b] for x in ps]
        eg = _grid(ev) or _grid(ev, 0.9)
        esc = _scaled(ev, eg)
        own.append((esc, esc) if esc is not None else (_local_grids(ev) or eg, eg))
    if pw == 32:
        memo: dict = {}
        pg, levels = _widen(pg, memo), _widen(levels, memo)
        own = [(_widen(a, memo), _widen(b, memo)) for a, b in own]
    if vw == 32:
        vg = _widen(vg)
    eras = None
    if starts:
        whole = levels or pg
        cuts = [0, *starts, len(tape)]

        def outside(a: int, b: int) -> Callable[[], Any]:
            memo: list = []

            def rest() -> Any:
                if not memo:
                    g = _local_grids([x for ps in per_bar[:a] + per_bar[b:] for x in ps])
                    memo.append(_widen(g, {}) if pw == 32 else g)
                return memo[0]
            return rest
        eras = Eras(tuple(starts), tuple(tape[i].ts for i in starts), tuple(own),
                    tuple(Joint(a, whole, outside(c, d)) for (a, _), c, d in zip(own, cuts, cuts[1:])),
                    tuple(_joined(b, pg) for _, b in own))
    return pg, vg, levels, eras, (pw, vw)


def _all_price_grids(sizes: Sizes) -> list:
    """Every price grid the tape was found printed on: its own, by price level, and by era."""
    out = [sizes.price_grid, *(sizes.price_grids.grids if sizes.price_grids else [])]
    for a, b in (sizes.eras.grids if sizes.eras else ()):
        out += [b, *(a.grids if isinstance(a, Grids) else [a])]
    return out


def _at_bar(sizes: Sizes, k: int) -> Sizes:
    """``sizes`` for bar ``k``: the grids of its era, where the tape has eras, and the steps the tape
    prints after a bar at its time of day, where it keeps a calendar. A late next bar in the middle of
    a session was one an overnight gap long, the only step the tape made longer than its commonest,
    and the rebuilt day ended at ten in the morning (a seventeenth red team)."""
    out = sizes
    if sizes.eras is not None:
        j = sizes.eras.of(k)
        out = out._replace(price_grids=sizes.eras.joints[j], price_grid=sizes.eras.singles[j], eras=None)
    if sizes.timing is not None:
        mode, steps = sizes.timing[k] if 0 <= k < len(sizes.timing) else (sizes.step_mode, tuple(sizes.steps))
        out = out._replace(step_mode=mode, steps=list(steps), timing=None)
    return out


def _timing(tape: Sequence[Any], cal: _Calendar | None) -> tuple | None:
    """Per bar of a tape with a calendar, the steps the tape prints after a bar at its time of day
    (and day of the week, over a week or more) and the commonest of them; None otherwise."""
    if cal is None:
        return None
    per_key = {k: (max(c, key=lambda st: (c[st], -st)), tuple(sorted(c))) for k, c in cal.steps_after.items()}
    return tuple(per_key.get(cal.key(b.ts), (None, ())) for b in tape)


def _prices_by_bar(sizes: Sizes) -> Any:
    """The price grids a rebuilt bar is set on: by its timestamp, where the tape has eras."""
    if sizes.eras is None:
        return sizes.price_grids or sizes.price_grid
    eras = sizes.eras
    return lambda ts: eras.joints[eras.at_time(ts)]


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
    sizes = _at_bar(sizes if sizes is not None else _sizes(tape), boundary)
    sg = sigma if sigma is not None else realized_sigma(tape)
    if avoid is None:
        avoid = ALL_TIES - _tie_fields(tape, boundary, sizes)
    r = _reach(o, tape[boundary - 1], sizes, sg, avoid, tape[boundary])
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
    bar's open past a reference (this bar's open by ``"own"``, its own high or low by ``"own_high"``
    or ``"own_low"``, or a previous-bar level), the gap sized to get it there. ``bare``: no upper
    (+1) or lower (-1) wick, where the tape prints a bar with none. Zero and None leave a field free.
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
    bare: int = 0                       # no upper (+1) or lower (-1) wick, where the tape prints one

    @property
    def forced(self) -> bool:
        return bool(self.move or self.wick or self.volume or self.high or self.low or self.hi_vs
                    or self.lo_vs or self.close_at or self.vol_eq or self.bare)


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
    sizes = _at_bar(sizes if sizes is not None else _sizes(tape), boundary)
    notes = notes if notes is not None else {}
    wicks, vols = sizes.wicks, sizes.ratios
    vg = sizes.vol_grid
    tw = wicks[-1] if wicks else 0.0
    tw_dn = min(tw, 0.99)                 # a lower wick of a whole body end or more is a zero price
    levels = _levels(prev)
    refs = frozenset(x for x in (o, *levels) if x > 0)
    none: frozenset = frozenset()
    bad_c, bad_h, bad_l = _bad_levels(o, prev, avoid)
    named = {"open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low, "own": o}
    hcons = [(named[n], sd) for n, sd in plan.hi_vs if named[n] > 0] + \
        ([(prev.high, plan.high)] if plan.high and prev.high > 0 else [])
    lcons = [(named[n], sd) for n, sd in plan.lo_vs if named[n] > 0] + \
        ([(prev.low, plan.low)] if plan.low and prev.low > 0 else [])
    up, dn = _extremes(o, sizes, sg, refs, b)

    # The close: exactly on a level (or its own open) where a tie is the target; near a level
    # where the next bar is to open on it by a gap; otherwise in a band on the move's side.
    closed = None
    if plan.close_at and o > 0:
        target = named.get(plan.close_at, 0.0)
        if target > 0 and (target == o or (sizes.moves and abs(math.log(target / o)) <= sizes.moves[-1])):
            closed = target
    if closed is None and o > 0 and plan.next_past and plan.next_past[1] == 0 and not plan.move and sizes.moves:
        closed = _close_near(named.get(plan.next_past[0], 0.0), o, up, dn, sizes, rng, bad_c, b)
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
                               reach_need, b)
    if closed is None or not closed > 0:
        closed = o

    hi0, lo0 = max(o, closed), min(o, closed)
    if lo0 > 0:
        high, low = _range_of(plan, hi0, lo0, o, hcons, lcons, tw, tw_dn, sizes, rng,
                              bad_h | {closed} if "high_close" in avoid else bad_h,
                              bad_l | {closed} if "low_close" in avoid else bad_l, b)
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
              avoid: frozenset = frozenset(), donor: int | None = None) -> tuple[float | None, float | None]:
    """The open and the time step of the bar after the probed one, where the plan forces them --
    or where the tape's own gap would land its open on a tie the real next bar did not print --
    and None for whatever the rebuild may draw freely. The open: past this bar's close by a gap
    of the tape's own, on the chosen side; carried past a reference by a gap sized to get there;
    exactly on one, by a gap of the tape's own size, where the tape gaps at all; or ungapped,
    where the tape prints ungapped bars. The step: the tape's commonest, or one shorter (early) or
    longer (late) than that. A tenth red team read the next bar's gap and lateness at a single
    bar; an eleventh showed "on time" meant the tape's SHORTEST step; a twelfth that the next open
    was owed on a level only through an ungapped bar."""
    pg = _grid_at(sizes.price_grids or sizes.price_grid, closed, rng) if closed else sizes.price_grid
    pools = {1: sizes.gaps_up, -1: sizes.gaps_dn}
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    refs = refs or {}
    opened = None
    if closed is not None and closed > 0:
        bad = frozenset()
        if gapped:
            # off every tie the real next bar did not print: a level or this bar's open, its close,
            # its own high or low
            edges = {"own_high": "next_high", "own_low": "next_low"}
            bad = frozenset(v for key, v in refs.items() if v > 0 and edges.get(key, f"next@{key}") in avoid)
            bad |= {closed} if "next_close" in avoid else frozenset()

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
            gap = _gap_of(tape, tape[donor if donor is not None else rng.choice(range(1, len(tape)))])
            g = _jitter(math.log(gap), rng) if gap > 0 else 0.0
            if g and pools[1 if g > 0 else -1]:
                # never past the tape's largest gap that way: jittered off a donor at the largest, the
                # next open went 1.06 times past it under 'the tape's own scale' (a nineteenth red team)
                top = pools[1 if g > 0 else -1][-1]
                g = max(-top, min(top, g))
            x = closed * math.exp(g) if g else closed
            lo_x = closed * math.exp(-pools[-1][-1]) if pools[-1] else 0.0
            hi_x = closed * math.exp(pools[1][-1]) if pools[1] else math.inf
            y = closed if x == closed else _grid_pick(x, pg, lo_x, hi_x, True, True)
            if y is None or y in bad:
                # a gap either way, in random order, and then no gap, where the tape prints ungapped
                # bars: on a cent tick at 37 cents one grid point lay within the largest gap each way,
                # and one random side left the open on the bar's own high (a twenty-third red team)
                sides = [s for s in (1, -1) if pools[s]]
                rng.shuffle(sides)
                tries = [gap_open(s, _draw_within(pools[s], 0.0, math.inf, rng) or pools[s][-1], closed)
                         for s in sides] + ([closed] if sizes.gap0 else [])
                y = next((z for z in tries if z is not None and z not in bad),
                         next((z for z in tries if z is not None), y))
            opened = y
    step = None
    if plan.next_step is not None and sizes.step_mode is not None:
        mode = sizes.step_mode
        side = [st for st in sizes.steps if (st > mode if plan.next_step > 0 else st < mode)]
        step = mode if plan.next_step == 0 else (rng.choice(side) if side else None)
    return opened, step


def _notes_off_scale(notes: dict | None) -> bool:
    return bool(notes and notes.get("off_scale"))


def _perturbed(tape: Sequence[Any], boundary: int, seed: int, sigma: float | None,
               signs: tuple[int, int, int] = (0, 0, 0), plan: Plan | None = None,
               sizes: Sizes | None = None, avoid: frozenset | None = None,
               notes: dict | None = None) -> list[Any]:
    """Vary everything unknowable at the moment bar ``boundary``'s position was chosen.

    Every bar after the boundary gets a fresh move, one of the tape's own, and is re-threaded,
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
    whole = sizes if sizes is not None else _sizes(tape)
    sizes = _at_bar(whole, boundary)
    if avoid is None:
        avoid = ALL_TIES - _tie_fields(tape, boundary, sizes)
    # An audit's draws always build the probed bar from the tape's own sizes, even a draw that
    # forces nothing about it; only the bare continuation-style call rebuilds it like any bar.
    first = _forced_bar(tape, boundary, rng, plan, sg, sizes, avoid, notes) if (plan.forced or explicit) else None
    prev = tape[boundary - 1]
    refs = {"own": tape[boundary].open, "open": prev.open, "close": prev.close, "high": prev.high, "low": prev.low}
    levels = sizes.price_grids
    if levels is not None and first is not None:
        # every price on the grid of its own level; what that undoes of the plan, the note's check
        # of the bar as built sees. A price that lands past the largest move or wick the tape has
        # made is set the other way where that stays valid, and named in the proof line where it
        # cannot: a fifteenth red team's high went up to a coarser tick, 1.4 times the largest wick,
        # under a line that said the bar was varied at the tape's own scale.
        o = tape[boundary].open
        top_move = sizes.moves[-1] * (1 + 1e-9) if sizes.moves else None
        top_wick = sizes.wicks[-1] + 1e-12 if sizes.wicks else None

        def too_far_move(x: float) -> bool:
            return top_move is not None and x > 0 and o > 0 and abs(math.log(x / o)) > top_move
        c = _on_level(levels, first[0], "round")
        if too_far_move(c) and not too_far_move(first[0]):
            c2 = _on_level(levels, first[0], "down" if c > first[0] else "up")
            c = c2 if c2 > 0 and not too_far_move(c2) else c
        top, bot = max(c, o), min(c, o)

        def up_wick(x: float) -> bool:
            # nothing hangs from a body end of zero: a tape of worthless bars crashed here (a
            # twenty-fourth red team)
            return top_wick is not None and top > 0 and x / top - 1 > top_wick

        def down_wick(x: float) -> bool:
            return top_wick is not None and x > 0 and bot > 0 and 1 - x / bot > top_wick
        # the wicks judged from the body as built: a close set down on its level's tick lowers the top
        # the high hangs from, and a sixteenth red team's high, within the largest wick before the
        # close moved, was past it after, under 'the tape's own scale'
        h = max(_on_level(levels, first[1], "up"), top)
        if up_wick(h):
            h = next((y for y in (_on_level(levels, first[1], "down"), top) if top <= y and not up_wick(y)), h)
        lw = min(_on_level(levels, first[2], "down"), bot)
        if down_wick(lw):
            lw = next((y for y in (_on_level(levels, first[2], "up"), bot) if 0 < y <= bot and not down_wick(y)), lw)
        past = [name for name, far_ in (("close", too_far_move(c) and not _notes_off_scale(notes)),
                                         ("high", up_wick(h)), ("low", down_wick(lw))) if far_]
        if past and notes is not None:
            notes["past"] = tuple(past)
        first = (c, h, lw, first[3])
    donors = _Donors(tape, boundary, rng, whole.calendar, whole.local_rms, whole.donors)
    if first is not None:
        refs.update(own_high=first[1], own_low=first[2])
    after = _next_bar(plan, sizes, rng, first[0] if first else None, refs, tape, avoid,
                      donors.at(boundary + 1, tape[boundary].ts) if first is not None and boundary + 1 < len(tape) else None)
    out = _rethread(tape, boundary, rng, donors, sigma, first=first, after=after,
                    grid=(_prices_by_bar(whole), sizes.vol_grid), no_zero=not sizes.zero, widths=whole.widths,
                    caps=_caps(sizes), rms=whole.local_rms, z=whole.local_z)
    if notes is not None and boundary + 1 < len(out) and after[0] is not None:
        a, b = out[boundary], out[boundary + 1]
        g = math.log(b.open / a.close) if a.close > 0 and b.open > 0 else 0.0
        pool = sizes.gaps_up if g > 0 else sizes.gaps_dn
        if g and pool and abs(g) > pool[-1] * (1 + 1e-9):
            notes["past"] = tuple(notes.get("past", ())) + ("next open",)
    return out


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
        got.add(("next_rel", "own_high", _sign(nb.open - bar.high)))
        got.add(("next_rel", "own_low", _sign(nb.open - bar.low)))
        if mode is not None:
            got.add(("next_step", _sign((nb.ts - bar.ts) - mode)))
    return got


def _reach(o: float, p: Any, sizes: Sizes, sg: float, avoid: frozenset = ALL_TIES, bar: Any = None) -> dict:
    """What the tape's own sizes can do from open ``o`` against previous bar ``p``, computed from
    the same extremes the probed bar is built from, on the tape's grid, and never counting a point
    that is a level where the field it is for must stay off levels (``avoid``: the fields the real
    bar printed no tie on): no draw gets STRICTLY past a level that sits exactly at the largest
    size, or past one whose only reachable grid points are levels. On a tape with no moves the
    close moves by the floor push exactly, and reach is measured against that."""
    pg = _bar_grid(sizes, o, bar)
    refs = frozenset(x for x in (o, *_levels(p)) if x > 0)
    none: frozenset = frozenset()
    bad_c, bad_h, bad_l = _bad_levels(o, p, avoid)
    up, dn = _extremes(o, sizes, sg, refs, bar)
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

    # no wick on a side: the high (low) on the bar's own open or close, a tie of its own, open to a
    # draw only where the real bar printed the same
    bare_h_open, bare_h_close = (sizes.wick0 and f not in avoid for f in ("high_open", "high_close"))
    bare_l_open, bare_l_close = (sizes.wick0 and f not in avoid for f in ("low_open", "low_close"))

    def high_below(level: float) -> bool:
        """The high strictly below a level above the open, with nothing on a level that must not
        be: a down close and an upper wick short of it; an up close short of it and no upper wick;
        or a down close and no wick at all -- the last two only where the real bar's high sat on its
        close, or its open, too."""
        if level <= o:
            return False
        if dn_ok and tw > 0 and room(o, min(level, cap_w), False, cap_w < level, bad_h):
            return True
        if bare_h_close and up_ok and (room(o, min(level, top), False, top < level, bad_c | bad_h)
                                       if sizes.moves else up < level):
            return True
        return bare_h_open and dn_ok

    def low_above(level: float) -> bool:
        if level >= o:
            return False
        if up_ok and tw > 0 and room(max(level, floor_w), o, floor_w > level, False, bad_l):
            return True
        if bare_l_close and dn_ok and (room(max(level, bottom), o, bottom > level, False, bad_c | bad_l)
                                       if sizes.moves else dn > level):
            return True
        return bare_l_open and up_ok

    def inside() -> bool:
        """The range strictly inside the previous bar's: the close inside it and off the levels, and
        each wick short of the previous extreme on its side."""
        if not (p.low > 0 and p.low < o < p.high):
            return False
        high_ok = (tw > 0 and room(o, min(p.high, cap_w), False, cap_w < p.high, bad_h)) or bare_h_open
        low_ok = (tw > 0 and room(max(p.low, floor_w), o, floor_w > p.low, False, bad_l)) or bare_l_open
        close_up = up_ok and (room(o, min(p.high, top), False, top < p.high, bad_c) if sizes.moves else up < p.high)
        close_dn = dn_ok and (room(max(p.low, bottom), o, bottom > p.low, False, bad_c) if sizes.moves else dn > p.low)
        return (close_up and (bare_h_close or tw > 0) and low_ok) or (close_dn and high_ok and (bare_l_close or tw > 0))

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
        # Past a level at all, even only onto another one: a draw setting the close on a farther
        # level passes the nearer, so the nearer is not out of reach, whatever can be credited (a
        # thirteenth red team). What can only be passed with a tie the real bar did not print is
        # owed all the same, and counted where it could not be made.
        "past": lambda level: (level > o and up is not None and above(level, up, none))
        or (level < o and dn is not None and below(level, dn, none)),
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
    sizes = _at_bar(sizes, k)
    b, p = tape[k], tape[k - 1]
    o = b.open
    if not plans or o <= 0:
        return set()
    r = _reach(o, p, sizes, sg, avoid, b)
    # Whether the high below a level, the low above one, or the range inside the previous one is owed
    # is judged blind to ties, as passing a level is: where only a bar with no wick on a side -- a tie
    # the real bar did not print -- gets there, it is owed all the same and counted where no draw could
    # make it. Judged by tie-free reach, it was left out of what was owed and never counted, and a read
    # of it walked at bars that printed no tie at all (a twenty-fourth red team).
    rb = _reach(o, p, sizes, sg, frozenset(), b)
    ties = sizes.ties
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    vol_up = p.volume > 0 or bool(sizes.volumes)
    has_next = k + 1 < len(tape)
    owed: set = set()
    if len(plans) == 1:
        pl = plans[0]
        m = pl.move
        # blind to ties, as the many-draw branch's high and low are: one draw has no other to make the
        # push with a tie, and where only a close on a level the real bar did not print got past the
        # open, nothing was owed and a plain read of the close walked (a twenty-fifth red team)
        if m and rb["up" if m > 0 else "dn"]:
            owed.add(("move", m))
            for name in LEVELS:              # past the farthest of the levels it reaches
                level = getattr(p, name)
                if level > 0 and (level - o) * m > 0 and rb["past"](level):
                    owed.add(("rel", "close", name, m))
        if pl.wick and r["wick_up" if pl.wick > 0 else "wick_dn"]:
            owed.add(("wick", pl.wick))
        if pl.volume > 0 and vol_up or pl.volume < 0 and r["vol_dn"]:
            owed.add(("vol", pl.volume))
        if p.high > 0:
            if pl.high > 0 and r["high_above"](p.high):
                owed.add(("rel", "high", "high", 1))
            if pl.high < 0 and rb["high_below"](p.high):
                owed.add(("rel", "high", "high", -1))
        if p.low > 0:
            if pl.low < 0 and r["low_below"](p.low):
                owed.add(("rel", "low", "low", -1))
            if pl.low > 0 and rb["low_above"](p.low):
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
        # away from zero only where some bar traded: on a tape where none did, every bar was owed it
        # and none could deliver it (a twenty-second red team)
        owed |= {("zero", True)} | ({("zero", False)} if sizes.volumes else set())
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
            if rb["high_below"](level):
                owed.add(("rel", "high", name, -1))
            if ("own" if level == o else "high") in ties and r["high_on"](level):
                owed.add(("rel", "high", name, 0))
            if r["low_below"](level):
                owed.add(("rel", "low", name, -1))
            if rb["low_above"](level):
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
            # and past the bar's own high or low: a gap of the tape's own from a close with no wick
            # that side (or one shorter than the gap). Pushed only against the bar's open and close
            # and the previous bar's levels, a read of the next open against this bar's own high
            # walked every_bar on a session tape (a twenty-first red team).
            bare = sizes.wick0 or bool(sizes.wicks and sizes.wicks[0] > 0)
            pg = _bar_grid(sizes, o, tape[k])
            tick = pg.step / pg.scale / o if isinstance(pg, Grid) and o > 0 else 0.0
            thin = next((w for w in sizes.wicks if w > 0), None) if sizes.wicks else None

            def clears(pool: list) -> bool:
                # a gap the tape has made past a wick it has made, and past a step of the grid there:
                # on a penny tape a cent was past every gap, and the push was owed at 80 bars in 95
                # that could never deliver it (a twenty-second red team)
                if not pool or pool[-1] < tick * (1 - 1e-9):
                    return False
                return sizes.wick0 or (thin is not None and thin < pool[-1])
            if r["up"] and clears(sizes.gaps_up):
                owed.add(("next_rel", "own_high", 1))
            if r["dn"] and clears(sizes.gaps_dn):
                owed.add(("next_rel", "own_low", -1))
            if r["dn"] and (sizes.gaps_dn or sizes.gap0) and bare:
                owed.add(("next_rel", "own_high", -1))
            if r["up"] and (sizes.gaps_up or sizes.gap0) and bare:
                owed.add(("next_rel", "own_low", 1))
            if "edge" in ties:
                # level with it, where the tape prints a next open on its bar's own high or low
                if r["up"] and (sizes.gaps_up or (sizes.gap0 and sizes.wick0)):
                    owed.add(("next_rel", "own_high", 0))
                if r["dn"] and (sizes.gaps_dn or (sizes.gap0 and sizes.wick0)):
                    owed.add(("next_rel", "own_low", 0))
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
        if rb["inside"]:
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
    "wick": ("wick", "bare"),
    "volume": ("volume", "zero", "vol_eq"),
    "next_open": ("next_gap", "next_past"),
    "next_step": ("next_step",),
}
_UNSET = {"move": 0, "wick": 0, "volume": 0, "high": 0, "low": 0, "inside": False, "zero": False,
          "hi_vs": (), "lo_vs": (), "next_gap": None, "next_step": None, "close_at": None,
          "vol_eq": False, "next_past": None, "bare": 0}


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
    sizes = _at_bar(sizes, k)
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
        if ref in ("own_high", "own_low"):
            # the close toward it and the gap sized from the bar as built: first with a short wick that
            # side, of the tape's own; with none (where the tape prints bars with none) only after. A
            # repair that only ever built the wickless bar was a shape a strategy could stand down on
            # (a twenty-second red team).
            toward = 1 if ref == "own_high" else -1
            clear = side == toward
            if side == 0:
                return Plan(move=toward, band="random", bare=toward if attempt % 2 else 0, next_past=(ref, 0))
            return Plan(move=side, band="random", wick=-toward if clear and not attempt % 2 else 0,
                        bare=side if clear and attempt % 2 else 0, next_past=(ref, side))
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
    sizes = _sizes(tape)
    return _rethread(tape, boundary, rng, _Donors(tape, boundary, rng, sizes.calendar, sizes.local_rms, sizes.donors),
                     grid=(_prices_by_bar(sizes), sizes.vol_grid), no_zero=not sizes.zero, widths=sizes.widths,
                     caps=_caps(sizes), rms=sizes.local_rms, z=sizes.local_z)


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
    # every argument, and the caller's sigma against the tape's sizes, before the strategy runs once:
    # a sigma refused after the truncation phase cost a run per bar first (a twentieth red team)
    args, sizes = _check_arguments(tape, boundaries=boundaries, draws=draws, sigma=sigma, probes=probes, seed=seed)
    n = len(tape)
    boundaries, draws, sigma, seed, tape = args["boundaries"], args["draws"], args["sigma"], args["seed"], args["tape"]
    sizes = sizes if sizes is not None else _sizes(tape)
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

    sg = realized_sigma(tape) if sigma is None else sigma
    mode = sizes.step_mode
    gapped = bool(sizes.gaps_up or sizes.gaps_dn)
    # Reach is measured from the same extremes the probed bar is built from: the tape's largest
    # move on its grid, or on a tape with no moves the floor push, exactly.
    far = tuple(k for k in pert_bounds if draws and beyond_reach(tape, k, sizes=sizes, sigma=sg))
    gridded = tuple(k for k in far if _bar_grid(_at_bar(sizes, k), tape[k].open, tape[k]) is not None)
    traded = bool(sizes.volumes)
    floors = tuple(name for name, lack in (("moves", not sizes.moves), ("volume changes", traded and not sizes.ratios),
                                           ("no trades", not traded))
                   if draws and pert_bounds and lack)
    short: list[int] = []
    repairs = 0
    tie_kinds: set = set()

    def probe(k: int, plan: Plan, salt: int, avoid: frozenset) -> tuple[list[Any], bool]:
        nonlocal runs
        notes: dict = {}
        varied = _perturbed(tape, k, seed=nonce ^ (k * 1_000_003 + salt), sigma=sigma, plan=plan, sizes=sizes,
                            avoid=avoid, notes=notes)
        variant = list(strategy(varied))
        runs += 1
        idx = _first_disagreement(full, variant, k + 1)
        if idx is not None:
            d = Divergence(index=idx, boundary=k, baseline=full[idx], variant=variant[idx],
                           probe="perturbation", detail=_detail(sigma, floors, bool(notes.get("off_scale")),
                                                                bool(sizes.moves), notes.get("past", ())))
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
        if gapped and "next_close" in avoid:
            # The real next bar gapped, so an ungapped next open is a tie it did not print: a planned
            # draw forcing one would be credited with nothing else. Its next open is left free; the
            # ungapped open gets a draw of its own, where it is owed.
            plans = [dataclasses.replace(pl, next_gap=None) if pl.next_gap == 0 else pl for pl in plans]

        def credit(varied: list[Any]) -> set:
            return _credited(_delivered(varied, k, tape[k - 1], _at_bar(sizes, k).step_mode),
                             _tie_fields(varied, k, sizes) - real, gapped)

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
                beyond_reach=far, beyond_gridded=gridded, calendar=sizes.timing is not None, undelivered=tuple(short), repairs=repairs, truncations=len(trunc_bounds),
                sigma=sigma,
                # A tape with no wicks gets no wick push at all (none is claimed); moves and volume
                # changes fall back to a floor size, which the note names.
                floors=floors,
                ties=tuple(t for t in ("doji", "close", "high", "low", "own", "volume", "ungapped", "next", "edge")
                           if t in tie_kinds),
                grids=tuple(g for g, found in (
                    ("tick", sizes.price_grid or sizes.price_grids), ("lot", sizes.vol_grid),
                    ("eras", sizes.eras is not None),
                    ("adjusted", any(g is not None and g.scale != 1.0 for g in _all_price_grids(sizes))),
                    ("float32", sizes.widths[0] == 32),
                    ("unspelled", any(g is not None and not g.spelled for g in _all_price_grids(sizes))),
                    ("unspelled volumes", sizes.vol_grid is not None and not sizes.vol_grid.spelled))
                    if found))

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
