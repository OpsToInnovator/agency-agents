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

import dataclasses
import math
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

__all__ = ["Bar", "Strategy", "Divergence", "Proven", "Suspected", "Report",
           "check_causality", "default_boundaries", "sparse_boundaries", "continuation",
           "realized_sigma", "DEFAULT_DRAWS", "SIGMA_FLOOR", "MIN_BOUNDARY"]

DEFAULT_DRAWS = 2
SIGMA_FLOOR = 0.002
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
    def horizon(self) -> int:
        """How many bars past the decision point the output reached, as a LOWER BOUND.

        The probe shows that signals[index] depends on some bar at or after ``boundary``,
        so the reach is at least ``boundary - index``. It may be further: the strategy
        might read bar boundary + 40 as well, and one divergence cannot tell them apart.
        Report it as "at least", never as the exact depth.

        Zero means the strategy read its own bar -- deciding at the open using the close.
        Only the perturbation probe can produce a zero; truncation removes whole bars, so
        the shallowest thing it can demonstrate is a reach of one bar past the data.
        """
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
    def horizon(self) -> int:
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
        """Share of bars at which a same-bar read would have been caught: one per boundary."""
        probeable = max(1, self.bars_tested - MIN_BOUNDARY)
        return min(1.0, len(self.boundaries) / probeable)

    @property
    def leaks(self) -> bool:
        return bool(self.proven)

    @property
    def worst_horizon(self) -> int | None:
        return max((p.horizon for p in self.proven), default=None)

    def coverage_note(self) -> str:
        if self.coverage >= 1.0:
            return "every bar was probed"
        return (f"probed at {len(self.boundaries)} of {self.bars_tested - MIN_BOUNDARY} possible boundaries "
                f"({self.coverage:.0%}); a leak confined to bars that were not probed would not show up here")

    def describe(self) -> str:
        if self.nondeterministic:
            return ("NOTHING PROVED: the strategy gave different output on identical input, so no "
                    "divergence can be attributed to the data.\n" +
                    "\n".join(f"  {s.summary}\n    {s.reason}" for s in self.suspected))
        if self.recognises_input:
            return ("NOTHING PROVED, AND THAT IS ITSELF THE FINDING: the strategy reproduces its output on the "
                    "real tape and fails to reproduce it on a varied one. It is telling the two apart. A "
                    "strategy that behaves differently under a probe cannot be audited by probing.\n" +
                    "\n".join(f"  {s.summary}\n    {s.reason}" for s in self.suspected))
        if not self.proven:
            base = (f"No causal dependency on future data was demonstrated over {self.bars_tested} bars "
                    f"and {self.probes_run} probe runs; {self.coverage_note()}. This is not a clean bill of "
                    f"health: a leak on a branch this data never took would not show up here.")
            if self.suspected:
                base += "\n\nSUSPECTED, not proven:\n" + "\n".join(
                    f"  {s.summary}\n    {s.reason}" for s in self.suspected)
            return base
        h = self.worst_horizon
        reach = "its own bar (decided at the open, read the close)" if h == 0 else f"at least {h} bar(s) into the future"
        lines = [f"PROVEN: this strategy reads {reach}.", f"  ({self.coverage_note()})", ""]
        for p in self.proven:
            e = p.evidence
            lines.append(f"  signals[{e.index}] = {e.baseline} normally, {e.variant} once bar {e.boundary} "
                         f"onward was {e.detail} ({e.probe} probe, horizon {e.horizon})")
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


def _rethread(tape: Sequence[Any], boundary: int, move_of, volume_of) -> list[Any]:
    """Rebuild the walk from ``boundary`` on, keeping every invariant the pristine tape has.

    Each new bar keeps the pristine bar's gap to its predecessor and its wick ratios; the
    open-to-close move and the volume are supplied by the callers. So where the pristine
    tape has open == previous close, the rebuilt one does too; where it has gaps, the gaps
    are preserved; high still covers open and close, low still sits under them. A strategy
    looking for the seam between real data and probe data has nothing to find, which is the
    point: a red team's first successful evasion keyed on exactly that seam.

    Volume is varied as well. A bar's traded volume is not known at that bar's open any
    more than its close is, and a second red team read it: a same-bar volume leak fronted by
    a harmless price term passed every probe because no probe ever moved a volume.
    """
    out = list(tape)
    prev_close = tape[boundary - 1].close
    for i in range(boundary, len(tape)):
        b = tape[i]
        top, bot = max(b.open, b.close), min(b.open, b.close)
        wick_up = b.high / top if top > 0 else 1.0
        wick_dn = b.low / bot if bot > 0 else 1.0
        gap = (b.open / tape[i - 1].close) if i > 0 and tape[i - 1].close > 0 else 1.0
        opened = b.open if i == boundary else prev_close * gap
        closed = opened * move_of(i, b)
        hi, lo = max(opened, closed), min(opened, closed)
        out[i] = _replace(b, open=opened, close=closed, volume=volume_of(i, b),
                          high=max(hi * wick_up, hi), low=min(lo * wick_dn, lo))
        prev_close = closed
    return out


def _perturbed(tape: Sequence[Any], boundary: int, seed: int, sigma: float | None,
               boundary_sign: int = 0) -> list[Any]:
    """Nudge everything unknowable at the moment bar ``boundary``'s position was chosen.

    The nudge is a small multiplicative change to each bar's own move, applied to the pristine
    move and then re-threaded, so the result is a plausible walk that shares its prefix with
    the pristine tape and differs only after the boundary -- in a way the strategy cannot tell
    from ordinary market variation. The multiplier is lognormal, so it can never send a price
    negative however wide sigma is set.
    """
    rng = random.Random(seed)
    sg = realized_sigma(tape) if sigma is None else sigma

    # Fresh moves at the tape's own scale, not a nudge on top of the pristine move: adding a
    # nudge inflated the variance of the varied region by half again, and a red team
    # measured the difference. Fresh moves have the pristine distribution.
    #
    # The boundary bar's move can be forced up or down. A same-bar read that decides on the
    # sign of close - open only flips when the varied sign differs from the pristine one --
    # a coin toss per draw, measured at a third of single-bar leaks missed at a probed bar.
    # Mirroring the first two draws makes one of them certain, at the same magnitude.
    def move(i, b):
        m = rng.gauss(0.0, sg)
        if i == boundary and boundary_sign:
            m = math.copysign(abs(m) or sg, boundary_sign)
        return math.exp(m)

    return _rethread(tape, boundary, move,
                     lambda i, b: b.volume * math.exp(rng.gauss(0.0, 0.25)))


def continuation(tape: Sequence[Any], boundary: int, *, seed: int) -> list[Any]:
    """A fresh continuation from ``boundary`` on: same prefix, same shape, different moves.

    This is what the input-dependence gate feeds the strategy. It differs from the pristine
    tape exactly where the probes can reach and nowhere else, so a strategy unmoved by it is
    a strategy no probe can move.
    """
    rng = random.Random(seed)
    sg = realized_sigma(tape)
    return _rethread(tape, boundary,
                     lambda i, b: math.exp(rng.gauss(0.0, sg)),
                     lambda i, b: b.volume * math.exp(rng.gauss(0.0, 0.25)))


def _first_disagreement(a: Sequence[int], b: Sequence[int], upto: int) -> int | None:
    for i in range(min(upto, len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None


def check_causality(strategy: Strategy, tape: Sequence[Any], *, boundaries: Sequence[int] | None = None,
                    draws: int = DEFAULT_DRAWS, sigma: float | None = None,
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
    if boundaries is not None:
        trunc_bounds = pert_bounds = sorted({b for b in boundaries if MIN_BOUNDARY <= b < n})
    elif probes == "every_bar":
        trunc_bounds = default_boundaries(n)
        pert_bounds = list(range(MIN_BOUNDARY, n))
    elif probes == "sparse":
        trunc_bounds = default_boundaries(n)
        pert_bounds = sorted(set(sparse_boundaries(n, nonce)) | set(trunc_bounds))
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

    for k in pert_bounds:
        for d_i in range(draws):
            sign = (1, -1)[d_i] if d_i < 2 else 0
            varied = _perturbed(tape, k, seed=nonce ^ (k * 1_000_003 + d_i), sigma=sigma, boundary_sign=sign)
            variant = list(strategy(varied))
            runs += 1
            idx = _first_disagreement(full, variant, k + 1)
            if idx is not None:
                d = Divergence(index=idx, boundary=k, baseline=full[idx], variant=variant[idx],
                               probe="perturbation", detail="varied within the tape's own range")
                candidates.append((d, (lambda v=varied: list(strategy(v))), variant))
                break

    proven: list[Proven] = []
    suspected: list[Suspected] = []
    truncation_hits: list[Divergence] = []
    meta = dict(probes_run=runs, bars_tested=n, boundaries=tuple(pert_bounds), seed=nonce)

    if candidates:
        # Reproduction before promotion. One extra pristine run, one extra variant run per
        # candidate. Which side fails to reproduce says what kind of strategy this is.
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
                            f"while the pristine tape reproduced exactly",
                    reason="deterministic on the real data and not on varied data: the strategy "
                           "distinguishes the two, so no probe result about it can be trusted"),),
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
    # The perturbation probe holds row count, column set and index fixed and changes only
    # values, so it cannot produce this artifact. Its corroboration promotes.
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

    proven.sort(key=lambda p: (-p.horizon, p.evidence.index))
    return Report(proven=tuple(proven), suspected=tuple(suspected), **meta)
