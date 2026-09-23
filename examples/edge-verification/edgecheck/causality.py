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
                      f"{levels} it could reach, the high and the low past the previous bar's, and the "
                      f"volume past the previous bar's, each one way chosen at random ({caveat}), plus a "
                      f"repair draw where that draw fell short of its own plan")
            residual = ("a read that only the other way would flip, a read of a magnitude rather than a "
                        "direction (how far the close is from the open, where it sits within its own range), "
                        "or of how two relations combine, can still go unseen")
        else:
            combos = (f"the close pushed both ways past its open and past the farthest of {levels} it could "
                      f"reach, the high and the low both ways past the previous bar's, the volume both ways "
                      f"past the previous bar's (and to zero and away from it, where the tape prints zeros), "
                      f"and move, wick and volume each both ways")
            if self.draws >= 4:
                pairs = ("every sign combination of move, wick and volume" if self.draws >= 8 else
                         f"every pair of move, wick and volume pushed apart ({min(self.draws, 8)} of 8 sign "
                         f"combinations)")
                combos += (f"; on the further draws the close in a band between those levels chosen at random, "
                           f"the range once inside and once outside the previous bar's, and {pairs}")
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range), against a level further back than "
                            "the previous bar, or of how two of these relations combine at one bar -- where the "
                            "close sits between two of the previous bar's levels, a failed breakout, an inside "
                            "or outside range together with where the close sits"
                            + ("" if self.draws >= 8 else ", a pattern across move, wick and volume at once")
                            + " -- is tried only on the draws that happen to produce it, and can go unseen")
            else:
                residual = ("a read of a magnitude rather than a direction (how far the close is from the "
                            "open, where it sits within its own range), against a level further back than "
                            "the previous bar, of where the close sits between two of the previous bar's levels, "
                            "of an inside or outside range, or of how two directions relate, can still go unseen")
            combos += (f" ({caveat}), each checked on the bar as built, with a repair draw wherever the "
                       f"planned draws fell short")
        stopped = ("; at a bar that diverged, probing stopped at the first divergence"
                   if any(p.evidence.probe == "perturbation" for p in self.proven) else "")
        far = (f"; at {len(self.beyond_reach)} of the probed bars one of {levels} lay at least as far from "
               f"the open as the largest move the tape has made, and the close was not pushed past it"
               if self.beyond_reach else "")
        if self.floors:
            far += (f"; the tape has made no {' and no '.join(self.floors)}, so pushes of that kind used a "
                    f"floor size, not one of its own")
        if self.undelivered:
            far += (f"; at {len(self.undelivered)} of the probed bars a push listed here could not be made "
                    f"with sizes the tape has made, even on a repair draw, and was not")
        if self.coverage >= 1.0:
            return (f"every bar from {MIN_BOUNDARY} on was probed (bars 0-{MIN_BOUNDARY - 1} never are, so a "
                    f"leak confined to them would not show up here), {combos}, at the bars that did not "
                    f"diverge{stopped}{far}; {residual}")
        return (f"probed at {len(self.boundaries)} of {self.bars_tested - MIN_BOUNDARY} possible boundaries "
                f"({self.coverage:.0%}), {combos}, at the bars that did not diverge{stopped}{far}; a leak "
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
            head = (f"PROVEN: this strategy's output changed when the tape was shortened past bar {first - 1} -- "
                    f"a dependence on data after that bar, or on how much data there is. The perturbation "
                    f"probe did not corroborate it, so no reach into future values is claimed.")
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
    return max(SIGMA_FLOOR, math.sqrt(mu * mu + var))


def _jitter(x: float, rng: random.Random) -> float:
    """A size of the tape's own, moved off its exact value. Copying a donor's wick, gap or
    volume ratio verbatim put a value on the rebuilt tape that already existed elsewhere on
    it -- a duplicate no real tape prints, and so a tell. Zero stays zero: a tape with no
    gaps must stay a tape with no gaps."""
    return x * math.exp(rng.gauss(0.0, JITTER)) if x else x


def _rethread(tape: Sequence[Any], boundary: int, rng: random.Random, move_of, volume_of,
              first: tuple[float, float, float, float] | None = None) -> list[Any]:
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
    and volume, built by ``_forced_bar``.
    """
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
            gap = _gap_of(tape, tape[rng.choice(donors)])
            opened = prev_close * math.exp(_jitter(math.log(gap), rng)) if gap > 0 else prev_close
            ts = prev_ts + _step_of(tape, tape[rng.choice(donors)])
        closed = opened * move_of(i, b)
        hi, lo = max(opened, closed), min(opened, closed)
        d = tape[rng.choice(donors)]
        d_top, d_bot = max(d.open, d.close), min(d.open, d.close)
        up = _jitter(max(0.0, d.high / d_top - 1.0), rng) if d_top > 0 else 0.0
        dn = min(_jitter(max(0.0, 1.0 - d.low / d_bot), rng), 0.99) if d_bot > 0 else 0.0
        out[i] = _replace(b, ts=ts, open=opened, close=closed, volume=volume_of(i, b),
                          high=hi * (1.0 + up), low=lo * (1.0 - dn))
        prev_close, prev_ts = closed, ts
    return out


class Sizes(NamedTuple):
    """The sizes the tape itself has printed, each sorted: every bar's move as |log(close/open)|,
    every wick as a fraction of the body end it hangs from, every bar-to-bar volume ratio as
    |log|, every nonzero volume -- and whether it has printed a zero volume at all. The
    probed bar is built from these and nothing else."""

    moves: list[float]
    wicks: list[float]
    ratios: list[float]
    volumes: list[float]
    zero: bool


def _sizes(tape: Sequence[Any]) -> Sizes:
    moves: list[float] = []
    wicks: list[float] = []
    ratios: list[float] = []
    volumes: list[float] = []
    zero = False
    before = None
    for b in tape:
        if b.open > 0 and b.close > 0:
            m = abs(math.log(b.close / b.open))
            if m > 0:
                moves.append(m)
            top, bot = max(b.open, b.close), min(b.open, b.close)
            wicks.append(max(0.0, b.high / top - 1.0))
            wicks.append(min(max(0.0, 1.0 - b.low / bot), 0.99))
        if b.volume > 0:
            volumes.append(b.volume)
        elif b.volume == 0:
            zero = True
        if before is not None and before.volume > 0 and b.volume > 0:
            r = abs(math.log(b.volume / before.volume))
            if r > 0:
                ratios.append(r)
        before = b
    return Sizes(sorted(moves), sorted(wicks), sorted(ratios), sorted(volumes), zero)


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


def beyond_reach(tape: Sequence[Any], boundary: int, top_move: float | None = None) -> bool:
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
    return any(abs(math.log(x / o)) >= top_move * (1.0 - EDGE) for x in _levels(tape[boundary - 1]) if x != o)


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
    below (-1) the previous low. Zero leaves a field free. ``inside`` keeps the close inside
    the previous bar's range so that an inside range is possible at all. ``zero`` sends a
    downward volume all the way to zero, a volume the tape must itself have printed.
    """

    move: int = 0
    wick: int = 0
    volume: int = 0
    band: str = "far"
    high: int = 0
    low: int = 0
    inside: bool = False
    zero: bool = False

    @property
    def forced(self) -> bool:
        return bool(self.move or self.wick or self.volume or self.high or self.low)


def draw_plans(nonce: int, boundary: int, draws: int, zero: bool = False) -> list[Plan]:
    """The draws an audit makes at a bar, in order.

    Move, wick and volume signs follow ``sign_design``. The first two draws are its
    complementary pair and push everything one way and then the other: the close past every
    previous-bar level it can reach, the range to a higher high and a higher low, then a
    lower high and a lower low -- so a read of any ONE relation to the open or the previous
    bar flips in two draws. A seventh red team showed that is all two lockstep draws can do:
    a read of how two relations combine -- an inside bar, a failed breakout, a close
    between the open and the previous high -- never changes when every draw is all-up or
    all-down. So the next two draws put the close in a band chosen at random and the range
    once INSIDE the previous bar's and once OUTSIDE it, in an order drawn from the nonce;
    later draws choose the range at random too."""
    design = sign_design(nonce, boundary)
    r = random.Random(nonce ^ (boundary * 104_729 + 3))
    inside_first = r.random() < 0.5
    plans: list[Plan] = []
    for d in range(draws):
        m, w, v = design[d] if d < 8 else (r.choice((1, -1)), r.choice((1, -1)), r.choice((1, -1)))
        if d < 2:
            plans.append(Plan(m, w, v, "far", m, m))
        elif d < 4:
            inside = (d == 2) == inside_first
            plans.append(Plan(m, w, v, "random", -1 if inside else 1, 1 if inside else -1, inside))
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
    tape itself has made. Returns values that satisfy every target that is reachable, each
    strictly; a target the open already decides, or that lies beyond the tape's own sizes,
    is dropped rather than forced with a size the tape never printed."""
    b, prev = tape[boundary], tape[boundary - 1]
    o = b.open
    sizes = sizes if sizes is not None else _sizes(tape)
    moves, wicks, vols = sizes.moves, sizes.wicks, sizes.ratios
    top_move = moves[-1] if moves else 0.0

    # The close: a band on the move's side of the open, reachable by a move of the tape's size.
    if plan.move and o > 0 and top_move > 0:
        bands = [(lo, hi) for lo, hi in _bands(o, _levels(prev), plan.move) if lo < top_move]
        edge = prev.high if plan.move > 0 else prev.low
        if plan.inside:
            # the same expression _bands uses, so the band that ends at this edge compares equal
            limit = (math.log(edge / o) if plan.move > 0 else math.log(o / edge)) if edge > 0 else 0.0
            snug = [(lo, hi) for lo, hi in bands if hi <= limit]
            bands = snug or bands
        # A range target on the move's own side -- a higher high on an up move, a lower low
        # on a down one -- may be out of a wick's reach from a typical close but within
        # reach of a move and a wick together, each of a size the tape has made. Then the
        # move is drawn from the sizes that bring it within reach.
        need = -math.inf
        if (plan.high if plan.move > 0 else -plan.low) > 0 and wicks and edge > 0:
            top_wick = wicks[-1]
            need = (math.log(edge / (o * (1.0 + top_wick))) if plan.move > 0
                    else math.log(o * (1.0 - top_wick) / edge))
            reaching = [(lo, hi) for lo, hi in bands if hi > need and need < top_move]
            bands = reaching or bands
        lo, hi = bands[-1] if plan.band == "far" else rng.choice(bands)
        if lo < need < min(hi, top_move):
            lo = need
        mag = _draw_within(moves, lo, hi, rng)
        closed = o * math.exp(plan.move * (mag if mag is not None else min(top_move, sg)))
    elif plan.move and o > 0:
        closed = o * math.exp(plan.move * sg)
    else:
        closed = o * math.exp(rng.gauss(0.0, sg))

    # The range: each wick from the tape's own wick sizes, inside the interval its target needs.
    hi0, lo0 = max(o, closed), min(o, closed)
    need_h = prev.high / hi0 - 1.0 if hi0 > 0 else 0.0     # upper wick at which high == prev.high
    need_l = 1.0 - prev.low / lo0 if lo0 > 0 else 0.0      # lower wick at which low == prev.low
    # A target the open or the close already decides is not a target: the high is past the
    # previous high whenever the body is, and no wick can pull it back.
    above_h, below_h = plan.high > 0 and need_h >= 0, plan.high < 0 and need_h > 0
    above_l, below_l = plan.low > 0 and need_l > 0, plan.low < 0 and need_l >= 0
    a_lo = need_h if above_h else -math.inf
    a_hi = need_h if below_h else math.inf
    b_lo = need_l if below_l else -math.inf
    b_hi = min(0.99, need_l) if above_l else 0.99
    a = _draw_within(wicks, a_lo, a_hi, rng)
    if a is None:                        # beyond every wick the tape has made: dropped
        above_h = below_h = False
        a_lo, a_hi = -math.inf, math.inf
        a = _draw_within(wicks, a_lo, a_hi, rng) or 0.0
    bw = _draw_within(wicks, b_lo, b_hi, rng)
    if bw is None:
        above_l = below_l = False
        b_lo, b_hi = -math.inf, 0.99
        bw = _draw_within(wicks, b_lo, b_hi, rng) or 0.0
    # The skew, without giving up a range target: lengthen the wick that must be longer or
    # shorten the other -- which one first is a coin toss, so the fix-up biases wick sizes
    # neither up nor down -- each within its own interval.
    if plan.wick and (a - bw) * plan.wick <= 0:
        grow_first = rng.random() < 0.5
        for grow in ((True, False) if grow_first else (False, True)):
            if plan.wick > 0:
                c = (_draw_within(wicks, max(a_lo, bw), a_hi, rng) if grow
                     else _draw_within(wicks, b_lo, min(b_hi, a), rng))
            else:
                c = (_draw_within(wicks, max(b_lo, a), b_hi, rng) if grow
                     else _draw_within(wicks, a_lo, min(a_hi, bw), rng))
            if c is not None:
                if (plan.wick > 0) == grow:
                    a = c
                else:
                    bw = c
                break
    high, low = hi0 * (1.0 + a), lo0 * (1.0 - bw)
    # Strictness survives rounding: a target that was set is met with room, never touched.
    if above_h and high <= prev.high:
        high = math.nextafter(prev.high, math.inf)
    if below_h and high >= prev.high:
        high = max(hi0, math.nextafter(prev.high, -math.inf))
    if above_l and low <= prev.low:
        low = min(lo0, math.nextafter(prev.low, math.inf))
    if below_l and low >= prev.low:
        low = math.nextafter(prev.low, -math.inf)

    # The volume: to the chosen side of the previous bar's, by one of the tape's own ratios --
    # and against zero as well, where the tape prints zeros. An untraded previous bar used to
    # make the push a multiple of zero, so "does this bar trade" never moved.
    if plan.volume < 0 and (plan.zero or prev.volume <= 0):
        volume = 0.0                         # nothing is below an untraded bar but another one
    elif plan.volume and prev.volume > 0:
        r = _draw_within(vols, 0.0, math.inf, rng)
        volume = prev.volume * math.exp(plan.volume * (r if r is not None else 0.25))
        if (volume - prev.volume) * plan.volume <= 0:
            volume = math.nextafter(prev.volume, math.inf if plan.volume > 0 else -math.inf)
    elif plan.volume > 0:                    # above an untraded bar: a volume the tape has printed
        pool = sizes.volumes
        volume = (_jitter(rng.choice(pool), rng) if pool
                  else (b.volume if b.volume > 0 else 1.0) * math.exp(rng.gauss(0.0, 0.25)))
    else:
        volume = b.volume * math.exp(rng.gauss(0.0, 0.25))
    return closed, high, low, volume


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
    if plan is None:
        m, w, v = signs
        plan = Plan(m, w, v, "far", m, m)
    sizes = sizes if sizes is not None else _sizes(tape)
    first = _forced_bar(tape, boundary, rng, plan, sg, sizes) if plan.forced else None
    return _rethread(tape, boundary, rng,
                     lambda i, b: math.exp(rng.gauss(0.0, sg)),
                     _volume_of(tape, rng, sizes),
                     first=first)


def _delivered(bar: Any, prev: Any) -> set:
    """Every relation the probed bar, as built, actually has to its open and to the previous
    bar. The note is checked against these, not against what the plan intended: an eighth red
    team showed a range target overriding the planned wick skew with nothing recorded."""
    o, c, v, pv = bar.open, bar.close, bar.volume, prev.volume
    top, bot = max(o, c), min(o, c)
    up_w = bar.high / top - 1.0 if top > 0 else 0.0
    dn_w = 1.0 - bar.low / bot if bot > 0 else 0.0
    m = 1 if c > o else -1 if c < o else 0
    w = 1 if up_w > dn_w else -1 if dn_w > up_w else 0
    got = set()
    if m:
        got.add(("move", m))
    if w:
        got.add(("wick", w))
    if v > pv:
        got.add(("vol", 1))
    elif v < pv or v == 0:                   # below the previous volume, or untraded after untraded
        got.add(("vol", -1))
    got.add(("zero", v == 0))
    for name, level in (("open", prev.open), ("close", prev.close), ("high", prev.high), ("low", prev.low)):
        if c > level:
            got.add(("past", name, 1))
        elif c < level:
            got.add(("past", name, -1))
    got.add(("high", 1 if bar.high > prev.high else -1 if bar.high < prev.high else 0))
    got.add(("low", 1 if bar.low > prev.low else -1 if bar.low < prev.low else 0))
    if bar.high < prev.high and bar.low > prev.low:
        got.add(("range", "inside"))
    if bar.high > prev.high and bar.low < prev.low:
        got.add(("range", "outside"))
    if m and w:
        got.add(("signs", (m, w, 1 if v > pv else -1)))
    return got


def _reach(o: float, p: Any, sizes: Sizes) -> dict:
    """What the tape's own sizes can do from open ``o`` against previous bar ``p``, computed
    in the same terms the probed bar is built in, and a hair short of the tape's largest size:
    no draw gets STRICTLY past a level that sits exactly at it, and a ninth-round tie showed a
    log-space comparison calling such a level reachable."""
    tm = (sizes.moves[-1] if sizes.moves else 0.0) * (1.0 - EDGE)
    tw = (sizes.wicks[-1] if sizes.wicks else 0.0) * (1.0 - EDGE)
    up, dn = o * math.exp(tm), o * math.exp(-tm)
    return {
        "move": tm > 0,
        "wick": tw > 0,
        "past": lambda level: (math.log(level / o) if level > o else math.log(o / level)) < tm,
        "high_up": o > p.high or p.high < up * (1.0 + tw),
        "low_dn": o < p.low or p.low > dn * (1.0 - tw),
        "outside_up": (o > p.high or p.high < up * (1.0 + tw)) and (o < p.low or p.low > o * (1.0 - tw)),
        "outside_dn": (o < p.low or p.low > dn * (1.0 - tw)) and (o > p.high or p.high < o * (1.0 + tw)),
    }


def _owed(tape: Sequence[Any], k: int, sizes: Sizes, plans: Sequence[Plan]) -> set:
    """What the coverage note claims was pushed at bar ``k``, limited to what the open leaves
    open and the tape's own sizes can reach -- the note's own qualification."""
    b, p = tape[k], tape[k - 1]
    o = b.open
    if not plans or o <= 0:
        return set()
    r = _reach(o, p, sizes)
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
    if r["wick"]:
        owed |= {("wick", 1), ("wick", -1)}
    if vol_up:
        owed.add(("vol", 1))
    if p.volume > 0:
        owed.add(("vol", -1))                # nothing is below a bar that did not trade
    if sizes.zero:
        owed |= {("zero", True), ("zero", False)}
    if r["move"]:
        for name, level in (("open", p.open), ("close", p.close), ("high", p.high), ("low", p.low)):
            if level <= 0 or level == o:
                continue
            owed.add(("past", name, -1 if level > o else 1))           # the way the move gets there
            if r["past"](level):
                owed.add(("past", name, 1 if level > o else -1))       # past it, within reach
        if r["high_up"]:
            owed.add(("high", 1))
        if o < p.high:
            owed.add(("high", -1))
        if o > p.low:
            owed.add(("low", 1))
        if r["low_dn"]:
            owed.add(("low", -1))
    if len(plans) >= 4:
        if p.low < o < p.high:
            owed.add(("range", "inside"))
        if r["move"] and (r["outside_up"] or r["outside_dn"]):
            owed.add(("range", "outside"))
        if r["move"] and r["wick"]:
            for pl in plans[:8]:
                if pl.volume > 0 and not vol_up:
                    continue
                owed.add(("signs", (pl.move, pl.wick, pl.volume)))
    return owed


def _repair(item: tuple, tape: Sequence[Any], k: int, sizes: Sizes, coin: int) -> Plan:
    """A draw aimed at one relation the planned draws did not deliver, with nothing else
    forced that could get in its way."""
    kind = item[0]
    if kind == "move":
        return Plan(move=item[1], band="random")
    if kind == "wick":
        return Plan(move=coin, wick=item[1], band="random")
    if kind == "vol":
        return Plan(volume=item[1])
    if kind == "zero":
        return Plan(volume=-1, zero=True) if item[1] else Plan(volume=1)
    if kind == "past":
        return Plan(move=item[2], band="far")
    if kind == "high":
        return Plan(move=item[1], band="far", high=item[1])
    if kind == "low":
        return Plan(move=item[1], band="far", low=item[1])
    if kind == "range" and item[1] == "inside":
        return Plan(move=coin, band="random", high=-1, low=1, inside=True)
    if kind == "range":
        up = _reach(tape[k].open, tape[k - 1], sizes)["outside_up"]
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
    far = tuple(k for k in pert_bounds if draws and beyond_reach(tape, k, top_move))
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
                           probe="perturbation", detail="varied at the tape's own scale")
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
            got |= _delivered(varied[k], tape[k - 1])
        if diverged or not plans:
            continue
        # Checked, not assumed: whatever the note will claim for this bar and the planned draws
        # did not deliver gets a draw of its own, twice at most; what still fails is disclosed.
        owed = _owed(tape, k, sizes, plans)
        tries: dict = {}
        coin = random.Random(nonce ^ (k * 7_907 + 11))
        while True:
            todo = sorted((x for x in owed - got if tries.get(x, 0) < 2), key=repr)
            if not todo:
                break
            item = todo[0]
            tries[item] = tries.get(item, 0) + 1
            repairs += 1
            varied, diverged = probe(k, _repair(item, tape, k, sizes, coin.choice((1, -1))),
                                     1_000 + repairs)
            if diverged:
                break
            got |= _delivered(varied[k], tape[k - 1])
        if not diverged and owed - got:
            short.append(k)

    proven: list[Proven] = []
    suspected: list[Suspected] = []
    truncation_hits: list[Divergence] = []
    meta = dict(probes_run=runs, bars_tested=n, boundaries=tuple(pert_bounds), seed=nonce, draws=draws,
                beyond_reach=far, undelivered=tuple(short), repairs=repairs, truncations=len(trunc_bounds),
                # A tape with no wicks gets no wick push at all (none is claimed); moves and volume
                # changes fall back to a floor size, which the note names.
                floors=tuple(name for name, pool in (("moves", sizes.moves), ("volume changes", sizes.ratios))
                             if draws and pert_bounds and not pool))

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
