# edgecheck

Does this strategy read the future?

Not "does it contain a suspicious pattern" — whether its output actually moves when you
change data it could not have seen. That distinction is the entire point. A pattern match
is a conversation; a changed output is a fact.

```
PROVEN: this strategy reads its own bar (decided at the open, read the close).

  signals[45] = 1 normally, -1 once bar 45 onward was varied within a percent of its
  true value (perturbation probe, horizon 0)
```

## Why this needs the code

Every tool that accepts only a trade list or an equity curve is permanently locked out of
this check, and of three others besides — real achievable fee tier, depth-aware slippage,
and quote staleness. You cannot recover any of them from a list of fills, at any price, by
any amount of cleverness. The information is not in the file. Taking the strategy itself is
more to ask of a customer, and it is the only way these four questions get answered.

## Two probes, because neither alone is enough

**Truncation** cuts the tape at bar *k* and re-runs. Anything reading past the end of the
data moves: full-sample statistics, centred windows, back-fills.

**Perturbation** nudges the fields that had not happened when the position was chosen. At
bar *i*'s open you know bars 0..*i*-1 entirely, plus bar *i*'s timestamp and open. You do
not know bar *i*'s own high, low, close or volume.

Truncation is structurally blind to that last case — cut the tape at bar *k* and bar *k*'s
close is still sitting in it — and *deciding at the open using the close* is the most common
real lookahead there is. Both probes ship because the gap between them is where the common
bug lives. `tests/test_causality.py` pins this: with perturbation disabled, the same-bar leak
goes undetected.

## What it proves, and what it does not

A divergence proves a causal dependency. The evidence is the pair of runs, and it is in the
report: index, the two conflicting outputs, and which probe produced them. A `Proven` cannot
be constructed without that evidence — the split between proof and suspicion is enforced by
the type, not by a flag someone remembers to set.

The absence of a divergence proves nothing. A leak on a branch this tape never took will not
show up, and the report says so in those words rather than issuing a clean bill of health.

Reported *horizon* is a lower bound — "at least this far" — never an exact depth.

## The nudge is one percent, and that is a tradeoff

An early version replaced prices uniformly across 50–200 against a series trading near 100.
Detection collapsed: every comparison inside the strategy saturated identically on every
draw, so a strategy plainly reading the future reported nothing. But a very small nudge is
not free either. Perturbation findings by sigma, out of four probe boundaries:

| leak | 0.002 | 0.01 | 0.05 | 0.3 | 1.5 |
|---|---|---|---|---|---|
| reads its own bar | 4 | 4 | 4 | 4 | 4 |
| reads the next bar | 4 | 4 | 4 | 4 | 4 |
| centred window | 4 | 4 | 4 | 4 | 4 |
| full-sample z-score | 4 | 2 | **0** | **0** | **0** |
| back-filled level | 2 | 4 | 4 | 4 | 4 |
| full-sample quantile | 3 | 1 | 1 | 1 | 1 |

A leak reading a specific cell is caught at any sigma. A leak working through a statistic of
the whole sample is caught only while the nudge stays too small to dominate that statistic.
A back-filled level wants the opposite. One percent sits between them, and it is survivable
because truncation convicts the full-sample family regardless.

## The contract

```python
signals(bars) -> list[int]
```

One position per bar — `-1`, `0` or `+1` — held during that bar, entered at its open. So
`signals(bars)[i]` may read `bars[0..i-1]`, plus bar *i*'s `ts` and `open`, and nothing else.

```python
from edgecheck import check_causality

report = check_causality(my_strategy.signals, tape)
print(report.describe())
```

## Ground truth

`edgecheck/fixtures/strategies/` holds six strategies with known leaks and two known-clean
controls, each carrying a `LEAKS` constant stating what it reads forward. The suite asserts
both directions. The acquittals matter more than the convictions: one false accusation costs
the credibility of every true finding.

Reported reach matches each fixture's real mechanism exactly — the centred window reports its
own `K`, the back-fill reports its gap minus one.

```
python3 -m pytest tests/ -q      # 22 tests, no network
```

## Not yet built

Sandboxing of untrusted customer code, the static pre-filter that would narrow where to
probe, adapters for pandas and event-driven strategies, and the other four checks. This
module is the one that most justifies asking for the code.

## Bars handed to the strategy are always possible bars

The perturbation nudges each field and then repairs the envelope — the high is lifted to
cover whatever the open and close became, the low dropped likewise. An earlier version drew
each field independently and broke `high >= low` on 42% of perturbed bars, putting the close
outside its own range on 78%. The obvious cost is that a strategy validating its input dies
mid-audit. The real cost is subtler and worse: a strategy that merely *behaves differently*
on an impossible bar would have that recorded as evidence of lookahead. A probe that
manufactures its own findings is not a probe. `test_every_perturbed_bar_is_a_possible_bar`
pins it.
