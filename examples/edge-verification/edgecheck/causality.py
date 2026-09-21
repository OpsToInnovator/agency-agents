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

The perturbation is deliberately GENTLE, and the size of it is a real tradeoff rather than
a detail. An early version drew replacement prices uniformly from 50 to 200 against a series
trading near 100, and detection collapsed: every comparison inside the strategy saturated the
same way on every draw, so a strategy that was plainly reading the future sat there reporting
nothing. But a very small nudge is not free either. Measured across the fixtures, findings by
sigma (out of four probe boundaries):

    sigma                     0.002   0.01   0.05    0.3    1.5
    reads its own bar             4      4      4      4      4
    reads the next bar            4      4      4      4      4
    centred window                4      4      4      4      4
    full-sample z-score           4      2      0      0      0
    back-filled level             2      4      4      4      4
    full-sample quantile          3      1      1      1      1

A leak that reads a specific cell is caught at any sigma. A leak that works through a
statistic of the whole sample is caught only while the nudge stays small enough not to
dominate that statistic -- widen it and the z-score's denominator inflates until every real
signal collapses toward zero on every draw alike. A back-filled level wants the opposite: a
nudge large enough to flip a comparison. The default of one percent sits between them, and
the reason that is survivable is that truncation convicts the full-sample family regardless.
Neither probe is the safety net for the other by accident.

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
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

__all__ = ["Bar", "Strategy", "Divergence", "Proven", "Suspected", "Report",
           "check_causality", "DEFAULT_DRAWS", "DEFAULT_SIGMA"]

DEFAULT_DRAWS = 12
DEFAULT_SIGMA = 0.01
UNKNOWABLE_AT_OPEN = ("high", "low", "close", "volume")
UNKNOWABLE_ENTIRELY = ("open", "high", "low", "close", "volume")


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

    @property
    def leaks(self) -> bool:
        return bool(self.proven)

    @property
    def worst_horizon(self) -> int | None:
        return max((p.horizon for p in self.proven), default=None)

    def describe(self) -> str:
        if not self.proven:
            base = (f"No causal dependency on future data was demonstrated over {self.bars_tested} bars "
                    f"and {self.probes_run} probe runs. This is not a clean bill of health: a leak on a "
                    f"branch this data never took would not show up here.")
            if self.suspected:
                base += "\n\nSUSPECTED, not proven:\n" + "\n".join(
                    f"  {s.summary}\n    {s.reason}" for s in self.suspected)
            return base
        h = self.worst_horizon
        reach = "its own bar (decided at the open, read the close)" if h == 0 else f"at least {h} bar(s) into the future"
        lines = [f"PROVEN: this strategy reads {reach}.", ""]
        for p in self.proven:
            e = p.evidence
            lines.append(f"  signals[{e.index}] = {e.baseline} normally, {e.variant} once bar {e.boundary} "
                         f"onward was {e.detail} ({e.probe} probe, horizon {e.horizon})")
        return "\n".join(lines)


def _replace(bar: Any, **kw: float) -> Any:
    if dataclasses.is_dataclass(bar):
        return dataclasses.replace(bar, **kw)
    raise TypeError(f"cannot vary a {type(bar).__name__}; bars must be a dataclass")


def _perturbed(tape: Sequence[Any], boundary: int, seed: int, sigma: float) -> list[Any]:
    """Nudge everything unknowable at the moment bar ``boundary``'s position was chosen.

    The replacement bar must still be a POSSIBLE bar. An earlier version drew each field
    independently, which broke ``high >= low`` on 42% of perturbed bars and put the close
    outside its own range on 78% of them. Two things go wrong with that, and the second is
    worse than the first: a strategy that validates its input dies mid-audit, and a strategy
    that merely behaves differently on an impossible bar has that difference recorded as
    evidence of lookahead. A probe that manufactures its own findings is not a probe.

    So each field is nudged, and then the envelope is repaired -- the high is lifted to
    cover whatever the open and close became, the low dropped likewise. Intra-bar
    relationships still move, which is what keeps the probe sensitive, but every bar it
    hands the strategy is one the market could have printed.

    The multiplier is lognormal rather than ``1 + gauss``. At the default sigma the two are
    indistinguishable, but a wide sigma sends ``1 + gauss`` negative, and a negative price
    is the same class of mistake as an inverted bar.
    """
    rng = random.Random(seed)
    out = list(tape)
    for i in range(boundary, len(tape)):
        bar = tape[i]
        nudge = lambda v: v * math.exp(rng.gauss(0.0, sigma))
        # Bar ``boundary``'s open is knowable -- it is the moment the position is chosen.
        opened = bar.open if i == boundary else nudge(bar.open)
        closed, high, low, volume = nudge(bar.close), nudge(bar.high), nudge(bar.low), nudge(bar.volume)
        out[i] = _replace(bar, open=opened, close=closed, volume=volume,
                          high=max(high, opened, closed), low=min(low, opened, closed))
    return out


def _first_disagreement(a: Sequence[int], b: Sequence[int], upto: int) -> int | None:
    for i in range(min(upto, len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None


def check_causality(strategy: Strategy, tape: Sequence[Any], *, boundaries: Sequence[int] | None = None,
                    draws: int = DEFAULT_DRAWS, sigma: float = DEFAULT_SIGMA) -> Report:
    """Run both probes and return what could be demonstrated.

    ``strategy`` takes the tape and returns one position per bar: the position held during
    that bar, entered at its open. So ``signals(tape)[i]`` may read ``tape[0..i-1]`` and
    bar i's own ts and open, and nothing else.
    """
    n = len(tape)
    if n < 8:
        raise ValueError(f"need at least 8 bars to probe, got {n}")
    if boundaries is None:
        boundaries = [max(4, int(n * f)) for f in (0.15, 0.4, 0.65, 0.9)]
    boundaries = sorted({b for b in boundaries if 4 <= b < n})

    proven: list[Proven] = []
    suspected: list[Suspected] = []
    runs = 0
    full = list(strategy(tape))
    runs += 1

    truncation_hits: list[Divergence] = []
    for k in boundaries:
        truncated = list(strategy(tape[:k]))
        runs += 1
        idx = _first_disagreement(truncated, full, k)
        if idx is not None:
            truncation_hits.append(Divergence(index=idx, boundary=k, baseline=full[idx],
                                              variant=truncated[idx], probe="truncation",
                                              detail="removed"))

    for k in boundaries:
        base = list(strategy(_perturbed(tape, k, seed=1000, sigma=sigma)))
        runs += 1
        for d_i in range(1, draws):
            variant = list(strategy(_perturbed(tape, k, seed=1000 + d_i, sigma=sigma)))
            runs += 1
            idx = _first_disagreement(base, variant, k + 1)
            if idx is not None:
                d = Divergence(index=idx, boundary=k, baseline=base[idx], variant=variant[idx],
                               probe="perturbation", detail="varied within a percent of its true value")
                proven.append(Proven(d, f"signals[{idx}] depends on fields of bar {k} onward that were not knowable"))
                break

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
    return Report(proven=tuple(proven), suspected=tuple(suspected), probes_run=runs, bars_tested=n)
