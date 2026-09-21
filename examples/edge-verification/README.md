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

## Truncation needs corroboration; perturbation does not

Truncation changes the length of the array the strategy is handed, and length changes the
arithmetic. Measured directly: an FFT-based causal filter — mathematically past-only —
returns values differing by **4–6e-14** between a 400-bar run and a 100-bar run, because the
transform pads to a power of two derived from the total length. The outputs compared here
are categorical, so there is no tolerance to apply: a 1e-14 wobble either flips a threshold
or it does not, and when it does it is perfectly reproducible, which is exactly what a real
finding looks like.

A clean numpy strategy built on that filter was **not** falsely accused on this tape — the
difference never landed on a crossing. That is luck, not safety.

So a truncation finding standing alone at a single boundary is filed as `Suspected`, not
`Proven`. A real dependence on the future shows up wherever you cut; a float artifact is a
knife-edge coincidence at one particular boundary. Perturbation holds row count and index
fixed and changes only values, so it cannot produce this artifact at all — its corroboration
promotes. Checked: `np.cumsum` is bit-identical under truncation, so the ordinary vectorised
idiom is unaffected.

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

## Running the customer's code

`edgecheck.sandbox.Sandbox` runs a strategy directory as a callable that plugs straight into
`check_causality`: one fresh process, in one fresh directory, with a fresh copy of the
strategy, per call. That costs tens of milliseconds and buys something the probes need — no
state can carry from one run to the next, so a feature cache written during the baseline run
cannot launder the future into a poisoned one.

The threat model is stated rather than assumed. The customer is paying to audit their own
code; the sandbox is for accidents — a vendor SDK phoning home, a runaway allocation, an
infinite loop — and above all for **owning the inputs**. Every probe works by changing the
data and watching the output. A strategy that brought its own prices, or fetches them, is
unmoved by anything we do, and "nothing moved" is what a clean strategy looks like. So
`precheck` runs two gates before any probe:

- **same tape twice → identical output**, else the strategy is nondeterministic and no
  divergence could be attributed to the data;
- **a different tape → different output**, else the output is not a function of the data we
  control and nothing can be proved about it, whatever the reason.

Either failing is reported as `UNPROVABLE`, in those words.

Two tiers, and the report says which ran:

| | `namespace` (default where `unshare` works) | `plain` |
|---|---|---|
| fresh interpreter per run, `-s -B` | yes | yes |
| environment built from scratch — no inherited keys or proxies | yes | yes |
| rlimits: CPU, memory, processes, file size, open files, no core | yes | yes |
| wall-clock kill of the whole process group | yes | yes |
| socket ban that **records** the call before refusing it | yes | yes |
| network blocked by the kernel — `connect()` and DNS both fail | yes | no |
| tmpfs over the home directory, `/root`, `/tmp`, `/var/tmp`, `/dev/shm` | yes | no |

`plain` stops accidents and runaway loops. It is a correctness boundary, not a security one:
the socket ban is a monkeypatch and `ctypes` walks past it. `namespace` closes that — a raw
`connect()` through `libc` returns `ENETUNREACH` — and hides the paths where secrets and
other runs' files live. Neither tier is a boundary against a determined attacker, and the
docs say so rather than imply otherwise.

Two things learned building it. `unshare --fork` reports **rc=1** for a child the kernel
killed at its CPU limit, indistinguishable from an ordinary failure, so the sandbox does not
classify on exit codes: the child catches `SIGXCPU` at the soft limit and writes down why
it is dying, and the parent measures the run's real CPU consumption through `getrusage` as
the backstop. And the child must not coerce the strategy's output — `int(0.5)` is `0`, a
valid position, and a strategy emitting probabilities would have been audited as though it
emitted decisions. The parent now insists on Python ints in `{-1, 0, 1}` and refuses
everything else.

The hash seed is pinned to 0 for every run so dict and set order cannot differ between them;
the report says so. Default memory limit is 2 GiB; a numpy strategy runs under 512 MiB.

## Not yet built

The static pre-filter that would narrow where to probe and name the line, adapters for
pandas and event-driven strategies, containment against a hostile author, and the other
four checks. This
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
