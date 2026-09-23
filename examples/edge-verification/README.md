# edgecheck

Does this strategy read the future?

Not "does it contain a suspicious pattern" — whether its output actually moves when you
change data it could not have seen. That distinction is the entire point. A pattern match
is a conversation; a changed output is a fact.

```
PROVEN: this strategy reads something from its own bar on that had not happened at its
open -- its own close, high, low or volume, or a later bar; the evidence does not say which.

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
difference never landed on a crossing. That is luck, not safety. And a global transform mixes
every value into every output at the 1e-14 level, so a perturbed *future* bar can flip a
*past* cell on a knife-edge too: under this contract that is a real, if useless, dependence
on the future, and it is reported as one. Perturbation removes the *length* artifact; it does
not make a global transform causal.

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

Reported *horizon* is a lower bound — "at least this far" — never an exact depth, and it is
claimed only when the **perturbation** probe demonstrated it. Truncation also shortens the
tape, and a strategy that sizes something from `len(bars)` diverges under it without reading
any future value; a third red team's count-dependent strategy was reported as reading 172
bars ahead when it read none. A truncation-only conviction now says exactly what it can:
the output changed when the tape was shortened, a dependence on data after that bar or on
how much data there is, reach not bounded.

## The probe compares against the pristine run, and the tape carries no tell

A red team's first two evasions were strategies that leak on real data and behave the moment
they recognise a probe — one keyed on a seam (opens redrawn independently, so open no longer
equalled the previous close), one on a fixed one-percent nudge that pushed intrabar moves past
anything the tape ever showed. Both got `PROVABLE` and a clean report.

Two changes. The perturbed walk is now **re-threaded** so it preserves every invariant the
pristine tape has — **in distribution, never per bar**. The first re-threading copied each
pristine bar's own wick ratios and opening gap onto the rebuilt bar and left every timestamp
alone, so there would be no seam; a third red team read exactly those properties — a bar's
own wick, the next bar's gap, the next bar's timestamp — and none of them ever moved under a
probe. Now the rebuilt bar takes its wick ratios, its gap and its time step from random donor
bars of the pristine tape, and its volume is nudged. On a continuous, regular tape that is
exactly continuous and regular; on a gappy, irregular one it is as gappy and irregular; and
everything unknowable at a bar's open varies at that bar. And every perturbed run is
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
- `probes="every_bar"` probes every bar from bar 4 on (bars 0–3 never are, and the report says
  a leak confined to them would not show up) and truncates at every bar too — a dependence on *how much data there is*
  shows up only under a cut between the index it moves and the length it keys on, and the
  four fixed cuts stopped at 0.9n. ~5n runs with the default four draws. This is what a paid
  report should use.

At a probed bar the boundary bar's move, its wick skew and its volume are each forced up or
down, and the sign triples an audit tries come from a **covering design under a mask drawn
from the audit nonce**: every field flips, every pair is pushed apart, the parity of all
three flips, and eight draws cover every combination. A fifth red team read whether the move
and the wick *agreed*, which two lockstep draws never changed, and a fixed design is a design
a leak can be written against. The wick's magnitudes are a donor bar's, with only the skew
forced — the previous fix had made the size a constant, so a wick-size threshold just below it
was never crossed.

Forced *against what* matters as much as forced. A sixth red team read `close > previous
close` at one bar and walked at every bar whose opening gap outweighed a typical move: the
move was forced against the bar's own open, so the close never crossed the previous close.
`volume > previous volume` walked at a fifth of bars for the same reason, and a breakout read
(`high > previous high`, `low < previous low`) at a fifth for the coin toss of whether a
donor's wick reached the previous extreme. Everything a strategy can compare an unknown
field against is the bar's own open and the previous bar's open, close, high and low, so on
the first two draws the close is pushed past as many of them as it can reach, one way and
then the other, the volume to both sides of the previous bar's, and the range to a higher
high and higher low, then a lower high and lower low.

*How far* it may be pushed is what a seventh red team found. Round six measured the push
from the far level, so at a bar that gapped 3% the probe's move was six times the largest
the tape had made — and a strategy that read its own close on real-sized moves and followed
the gap on huge ones waited every probe out, where round five's module had convicted it.
Now every size forced into the probed bar is one the tape has printed: its move is drawn
from the tape's own moves, its wicks from its own wicks, its volume ratio from its own
bar-to-bar ratios, each jittered off its exact value (a verbatim copy is a duplicate no
real tape prints) and never past the largest. A level further from the open than any move
the tape has made is left alone, and the report counts the bars where that happened. The
same round showed that all-up-then-all-down leaves every read of how two relations
*combine* untouched — an inside bar, a failed breakout, a close between the open and the
previous high — so the next two draws put the close in a band between the levels chosen at
random, and the range once inside and once outside the previous bar's. And the range pin
had landed exactly *on* the previous extreme, so `low <= previous low` never flipped; every
target is now met strictly. Where the open already sits beyond the previous extreme, that
side is decided by a value the strategy may read, and nothing is claimed.

Every-bar mode uses four draws by default. The report says what was pushed where at the
bars that did not diverge, that probing of a bar stopped at its first divergence, how many
bars had a level out of reach, and states the residual: a read of a magnitude rather than a
direction, against a level further back than the previous bar, or of how two of these
relations combine at a single bar, is tried only on the draws that happen to produce it and
can go unseen. Made at every bar, as real code makes it, each such combination was convicted
in every audit measured. With one draw the report says each bar was pushed one way only;
with none, that nothing a bar had not yet printed was varied. A
fourth red team's one-bar wick read was being missed in one audit out of eight while the
report said "every bar was probed"; the wick was a random donor's, and only the move had
been mirrored. Volume is varied too: a bar's traded volume is no more known at its open than
its close is, and a red team read one behind a harmless price term because no probe had
ever moved it.

## Nothing is proven until it reproduces

A strategy seeded on a coarse clock passed the same-tape-twice gate — the two runs shared a
one-second bucket — and was then convicted of a 130-bar lookahead that the clock, not the data,
had produced. Now every candidate divergence is re-run on both sides, and only an exact
reproduction becomes `Proven`. Which side fails says something, and the report says only what
it can: if the pristine run does not reproduce (it is replayed three times), the strategy is
nondeterministic and nothing is attributed to the data. If the pristine run reproduces and a
varied one does not, then *either* the strategy distinguishes real data from varied data — a
second red team built exactly that — *or* it is intermittently nondeterministic and the real
tape got lucky; the report names both, because a fifth red team showed a plain p = 0.2 flake
being accused of the first. `precheck` catches the same pattern earlier by checking
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
| audit hook that **records** every socket use and spawn, then refuses it — `multiprocessing`'s and `os.forkpty`'s included | yes | yes |
| results and the record travel over parent-owned pipes; child `_exit`s on write | yes | yes |
| network blocked by the kernel — `connect()` and DNS fail, by any route | yes | no |
| a new root: only `/usr`, `/etc`, the lib and bin trees and the interpreter's prefix, read-only | yes | no |
| plus a fresh `/proc`, four device nodes, and a tmpfs `/tmp` that dies with the process | yes | no |
| the run directory is a **size-capped tmpfs** too: many files cannot fill the host disk | yes | no |
| the home directory, `/opt`, `/var`, `/run`, the work root, the source, the repository: absent | yes | no |
| the strategy runs in one more, unmapped user namespace: no capabilities, every mount locked | yes | no |
| its own UTS and IPC namespaces: the host's hostname is not the child's to change | yes | no |
| `/proc/sys` read-only, `sysrq-trigger`, `irq` and `bus` masked: host sysctls are not the child's either | yes | no |
| `close()` / `with` removes the staged source and an owned work root | yes | yes |
| the parent bounds what it takes: result and record sizes, run-dir entries and depth; walks and teardown never recurse | yes | yes |
| the strategy's copy of itself read-only; the run directory deleted after the run | yes | no |

`plain` stops accidents and runaway loops. It is a correctness boundary, not a security one:
`ctypes` walks past the audit hook, the parent's environment is readable through `/proc`, and
files written anywhere outside the run directory survive between runs. `namespace` closes each
of those — and it does so with a **whitelist**, not a list of paths to hide. The first version
hid a list; a second red team cached through `/opt` and injected a module into the interpreter's
own `dist-packages`, because a list can never name every writable directory on a host. Now
everything the strategy can see is read-only except the run directory and a `/tmp` that does
not outlive the process, and a test writes to eight places outside the run directory and
asserts that every write fails and nothing persists. Setting that up needs the mapped-root
capabilities, and a child that kept them could undo it — it would be root in that namespace,
and a mount the namespace created is a mount it may remount read-write; a third red team did
exactly that through a `ctypes` `mount()` call. So the strategy runs inside one more user
namespace, unmapped: no capabilities over anything that exists, every inherited mount locked,
the remount refused with `EPERM`. The interpreter does not care what uid it is.

**Nothing the child does may crash the parent.** A strategy built a run directory two
thousand levels deep with nothing but `os.mkdir` and `os.chdir` — single components, so no
`PATH_MAX` — and the auditor died of a `RecursionError` in its own `Path.rglob` and
`shutil.rmtree`, producing no verdict on a strategy that was `PROVEN` on its own. The walk
is now iterative with a ceiling on entries and depth (over it is `ResourceExceeded`, named),
teardown flattens the tree by renaming subdirectories up to the root with two descriptors
and no path, and the result and violation pipes are capped: a result larger than the cap is
`BadOutput`, a flood of violations is recorded up to the cap and then says "and more".
`RLIMIT_FSIZE` caps one file, and many files at that size through a run directory bound from
the host would have filled the host disk, so inside the namespace the run directory is a
tmpfs of a fixed size: the strategy gets `ENOSPC`, the host gets nothing. One consequence,
stated plainly: in that tier the files-written listing comes from the child, because the
directory dies with it. That listing was always a hint about caches, never evidence.

Neither tier is
a boundary against a determined attacker — one running inside the child process can discover
the result descriptor and forge a payload — and the docs say so rather than imply otherwise.

**What seven red-team rounds got through, and what closed it.** A cache written to `/tmp` and keyed on
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
gate and in the report). In the third: leaks on a bar's own wick ratio, the next bar's
gap and the next bar's timestamp (the rebuilt tape now draws those from donor bars), a
count-dependent strategy convicted with a fabricated reach (reach is claimed from the
perturbation probe only), a run directory deep enough to crash the auditor (bounded,
iterative walks; flatten-by-rename teardown), and a `ctypes` remount of the read-only
system trees (the nested unmapped namespace). In the fourth — the first with nothing of
verdict-poisoning severity — a one-bar wick read missed in one every-bar audit in eight
(the boundary bar's wick and volume are now mirrored across the draws like its move), a
count dependence above 0.9n (every-bar mode truncates at every bar), a `RecursionError` from
a deeply nested result escaping the parent (caught, reported as malformed), and a child
that could change the host's hostname through the shared UTS namespace (`--uts --ipc`). In
the fifth: the agreement of move and wick invariant under two lockstep draws (random sign
triples after the first two), a constant forced wick size (donor magnitudes, forced skew),
host sysctls written through the fresh `/proc` (`/proc/sys` read-only), a rare flake accused
of telling real from varied data (three pristine replays, and the report names both
possibilities), truncation evidence lines printing a horizon under a headline that claimed
none, "every bar was probed" while bars 0–3 cannot be, and staged source lingering in `/tmp`
(`close()`). In the sixth — none of it created by the fifth's fixes; the fourth's code misses
them identically — one-bar reads of the close, the volume, the high and the low *against the
previous bar* surviving at gap bars and at a fifth of bars respectively (every unknown field
is now pushed to both sides of the previous bar's level), a horizon-0 proof described as
"read the close" whatever was read (the report names the four fields it might have been and
says the evidence does not distinguish them), and a coverage note claiming four sign
combinations at every bar while probing of a bar stops at its first divergence (it says so).
In the seventh — two of the sixth's fixes had opened holes — a close forced past a far level
by a move six times the tape's largest, which a strategy recognised and waited out (every
forced size is now one the tape has printed), every draw all-up or all-down so an inside
bar, a failed breakout or a close between two levels was never flipped (random bands; inside
and outside ranges), a range pin landing exactly on the previous extreme (strict), a
horizon-0 headline naming the bar's own fields for a strategy that read only the next bar's
timestamp ("or a later bar"), two-draw text printed for one draw and for none, spawns through
`multiprocessing` and `os.forkpty` that raised no audited event (refused and recorded),
`from_file`'s first copy of the source left in `/tmp`, and a long stderr log pushing the
error that ended a run out of the report (the tail is kept).

Two things measured, not assumed. `unshare --fork` reports rc=1 for a child the kernel
killed at its CPU limit, indistinguishable from an ordinary failure, so nothing classifies
on exit codes: the child catches `SIGXCPU` and writes down why it is dying; the parent
measures the run's real CPU through `getrusage`. And the child must not coerce output —
`int(0.5)` is `0`, a valid position — so the parent insists on Python ints in `{-1, 0, 1}`.

**What is not cleaned up.** An auditor that is killed or terminated mid-run — `SIGKILL`, or
the `SIGTERM` a `timeout` sends — runs no finalizer, so its work root stays behind with the
staged source and the tape; the library installs no signal handlers of its own. A write to
`/dev/random` inside the namespace mixes into the host's entropy pool; it cannot lower it.

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
