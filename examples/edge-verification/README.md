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

## The probe compares against the pristine run, and the tape carries no tell

A red team's first two evasions were strategies that leak on real data and behave the moment
they recognise a probe — one keyed on a seam (opens redrawn independently, so open no longer
equalled the previous close), one on a fixed one-percent nudge that pushed intrabar moves past
anything the tape ever showed. Both got `PROVABLE` and a clean report.

Two changes. The perturbed walk is now **re-threaded** — each bar keeps its gap, wick ratios
and volume; only the move is nudged, on the tape's own realized scale — so it preserves every
invariant the pristine tape has and there is no seam to find. And every perturbed run is
compared against the **pristine** run: for `i <= k` a causal strategy must reproduce it exactly,
because nothing it may legitimately read has changed. A strategy that leaked on real data used
the boundary bar's close there, and no causal fallback can reproduce that value once the close
has moved. All three evasions are now fixtures, and all three are `PROVEN`.

Measured after this landed: detection no longer depends on the nudge size at all — every leak,
at every σ from 0.002 to 1.5. An earlier version of this README carried a table showing a
tradeoff between wide and narrow nudges. That tradeoff was an artifact of comparing perturbed
runs only to each other, and it is gone.

## Where the probe cuts decides what it can see

At a boundary *k*, a same-bar read is visible **only at index *k***: bars before it are pristine
and bars after it are not compared. So the set of perturbation boundaries is exactly the set of
bars at which a same-bar leak can be caught at all. The first version cut at four fixed
fractions of the tape; a second red team leaked on every bar *except* those four and got a clean
report — a fixed public schedule is a schedule a leak can be written around.

Two modes now, and the report states which and what it covered:

- `probes="sparse"` (default) draws the boundary set from a nonce the strategy never sees. A
  same-bar leak confined to one bar is caught with probability *B / (n − 4)* per audit — 14% on
  a 300-bar tape — and the report prints that number as coverage rather than implying more.
- `probes="every_bar"` probes every bar, one run each. Complete for a leak of any horizon at
  any index; ~n runs. This is what a paid report should use.

At a probed bar the boundary bar's move is forced up in one draw and down in the next, so a
decision on the sign of close − open flips in one of them for certain rather than on a coin
toss. Volume is varied too: a bar's traded volume is no more known at its open than its close
is, and a red team read one behind a harmless price term because no probe had ever moved it.

## Nothing is proven until it reproduces

A strategy seeded on a coarse clock passed the same-tape-twice gate — the two runs shared a
one-second bucket — and was then convicted of a 130-bar lookahead that the clock, not the data,
had produced. Now every candidate divergence is re-run on both sides, and only an exact
reproduction becomes `Proven`. Which side fails says what the strategy is: if the pristine run
does not reproduce, the strategy is nondeterministic and nothing in the report is attributed
to the data. If the pristine run reproduces and a varied one does not, the strategy is telling
real data from probe data — a second red team built exactly that, deterministic only on a
tape it recognised by checksum and random everywhere else — and the report says so instead
of calling it inconclusive. `precheck` catches the same pattern earlier by checking
determinism on a varied tape as well as the real one.

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
python3 -m pytest tests/ -q      # no network; namespace-only tests skip by name where the host cannot make one
```

## Running the customer's code

`edgecheck.sandbox.Sandbox` runs a strategy directory as a callable that plugs straight into
`check_causality`: one fresh process, in one fresh directory, with a fresh copy of the
strategy, per call. `prove` runs the gates and then the probes, never the other way round.

The threat model is stated rather than assumed. The customer is paying to audit their own
code; the sandbox is for accidents, for **owning the inputs**, and — a red team showed this is
not a theoretical category — for casual cheating. Every probe works by changing the data and
watching the output. A strategy that brought its own prices is unmoved by anything we do, and
"nothing moved" is what a clean strategy looks like. So `precheck` runs two gates first:

- **same tape twice → identical output**, else nondeterministic and no divergence could be
  attributed to the data;
- **the bars from the first probe boundary on replaced with a fresh continuation → different
  output**, else the output does not change when the bars we can vary change, and nothing can
  be proved about it. (The first version tested a wholly different tape, which a strategy
  depending on bar 0 alone — a bar no probe ever moves — passed.)

Either failing is reported `UNPROVABLE`, in those words.

Two tiers, and the report says which ran:

| | `namespace` (default where `unshare` works) | `plain` |
|---|---|---|
| fresh interpreter per run, `-s -B` | yes | yes |
| child's environment built from scratch — no inherited keys or proxies | yes | yes |
| rlimits: CPU, memory, processes, file size, open files, no core | yes | yes |
| wall-clock kill of the whole process group, always, after every run | yes | yes |
| audit hook that **records** every socket use and spawn, then refuses it | yes | yes |
| results and the record travel over parent-owned pipes; child `_exit`s on write | yes | yes |
| network blocked by the kernel — `connect()` and DNS fail, by any route | yes | no |
| a new root: only `/usr`, `/etc`, the lib and bin trees and the interpreter's prefix, read-only | yes | no |
| plus a fresh `/proc`, four device nodes, a tmpfs `/tmp` that dies with the process, the run dir | yes | no |
| the home directory, `/opt`, `/var`, `/run`, the work root, the source, the repository: absent | yes | no |
| the strategy's copy of itself read-only; the run directory deleted after the run | yes | no |

`plain` stops accidents and runaway loops. It is a correctness boundary, not a security one:
`ctypes` walks past the audit hook, the parent's environment is readable through `/proc`, and
files written anywhere outside the run directory survive between runs. `namespace` closes each
of those — and it does so with a **whitelist**, not a list of paths to hide. The first version
hid a list; a second red team cached through `/opt` and injected a module into the interpreter's
own `dist-packages`, because a list can never name every writable directory on a host. Now
everything the strategy can see is read-only except the run directory and a `/tmp` that does
not outlive the process, and a test writes to eight places outside the run directory and
asserts that every write fails and nothing persists. Neither tier is
a boundary against a determined attacker — one running inside the child process can discover
the result descriptor and forge a payload — and the docs say so rather than imply otherwise.

**What two red-team rounds got through, and what closed it.** A cache written to `/tmp` and keyed on
data the probes never move replayed a leaky answer to every probe: the default work root was
under `/tmp`, and the first version refused to hide any ancestor of it. Now `/tmp` is always
hidden and only the run directory is bound back in. A strategy rebound the child runner's
result function, another started a thread that rewrote the output after the official write,
another deleted the violation record after a swallowed network attempt: results and the
record now travel over pipes the strategy did not create and cannot unlink, and the child
exits the instant the result is written. `importlib.reload(socket)` undid a monkeypatched
ban; a PEP 578 audit hook cannot be removed. A multi-line exception message planted a
reassuring last line in the report; messages are `repr`'d. A strategy claimed a CPU-limit
death without consuming CPU; the claim is checked against the kernel's accounting. In the
second round: a cache in `/opt` (the whitelist root), a leak on every bar but the four fixed
boundaries (nonce-drawn boundaries, every-bar mode, coverage stated), a same-bar volume read
(volume varied), and a strategy deterministic only on the real tape (named as such, at the
gate and in the report).

Two things measured, not assumed. `unshare --fork` reports rc=1 for a child the kernel
killed at its CPU limit, indistinguishable from an ordinary failure, so nothing classifies
on exit codes: the child catches `SIGXCPU` and writes down why it is dying; the parent
measures the run's real CPU through `getrusage`. And the child must not coerce output —
`int(0.5)` is `0`, a valid position — so the parent insists on Python ints in `{-1, 0, 1}`.

**A limit no probe can remove.** If the customer supplied the tape, a strategy can recognise
it by hash and leak only on that exact data. Black-box probing cannot catch that. The audit
must therefore never run on data the customer chose; that is a rule of the service, not a
feature of this code.

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
