# edgecheck

Does this strategy read the future?

Not "does it contain a suspicious pattern" — whether its output actually moves when you
change data it could not have seen. That distinction is the entire point. A pattern match
is a conversation; a changed output is a fact.

```
PROVEN: this strategy reads something from its own bar on that had not happened at its
open -- its own close, high, low or volume, or a later bar; the evidence does not say which.

  signals[45] = 1 normally, -1 once bar 45 onward was varied at the tape's own scale
  (perturbation probe, horizon 0)
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
Now every size forced into the probed bar is drawn from the tape's own: its move from the
tape's moves, its wicks from its wicks, its volume ratio from its bar-to-bar ratios, each
jittered off its exact value (a verbatim copy is a duplicate no real tape prints) and never
past the largest; where no size lands in the interval a target needs, a point inside it
short of the largest is used, and a tape that has made no size of some kind — flat prices,
or a volume that never changes — gets a floor for it, which the report names.
A level at least as far from the open as the largest move
the tape has made is left alone, and the report counts the bars where that happened. The
same round showed that all-up-then-all-down leaves every read of how two relations
*combine* untouched — an inside bar, a failed breakout, a close between the open and the
previous high — so the next two draws put the close in a band between the levels chosen at
random, and the range once inside and once outside the previous bar's. And the range pin
had landed exactly *on* the previous extreme, so `low <= previous low` never flipped; every
target is now met strictly. Where the open already sits beyond the previous extreme, that
side is decided by a value the strategy may read, and nothing is claimed.

*What the note claims is checked, not planned.* An eighth red team showed round seven's
range targets quietly overriding the planned wick skew — at a bar whose open sat just above
the previous low, a higher low, a higher high and a longer lower wick cannot all hold — so
"move, wick and volume each both ways" was false there, and a one-bar skew read walked in a
quarter of audits. Each probed bar is now checked, as built, against every relation the note
lists for it (limited to what the open leaves open and the tape's own sizes can reach); any
the planned draws missed gets a repair draw of its own with nothing else forced, and a bar
where even that fails is counted in the report.

*The relations are enumerated, not collected.* Ten red teams found them one at a time — the
close against the previous close, the volume against the previous volume, the high against
the previous high, then a tenth found three more: the low against the previous close (a gap
fill), a tie at an extreme the bar opened on, and the next bar's gap and lateness read at a
single bar. They are all members of one set, and the set is now what is owed: the close
both ways past its open; the close, the high and the low each both ways past each of the
previous bar's open, close, high and low, and the high and the low level with one the bar
opened on where the tape prints a bar with no wick; the volume both ways past the previous
bar's, and to zero and away from it; the next bar's open both ways past this bar's open and
close and each of the previous bar's levels, and the next bar early, on time and late
against the tape's commonest step — each where the open leaves it open and the tape's own
sizes can reach, each checked on the bar as built. An eleventh red team added the last of
it: *ties*. On a tape printed on a tick, a close unchanged from the previous close, a doji,
a high equal to the previous high, a volume repeated in lots, walked at every seed, because
no draw ever landed exactly on a level. Each kind of tie the tape prints is now owed and
set exactly; the probed bar and everything after it are rebuilt on the tape's own tick and
lot (a price off the grid was itself a tell); and a repair draw pursues several targets at
once where they force different parts of the bar. Nothing is ever accused on a tape it
could not deliver: no clean strategy was convicted in any sweep.

A twelfth red team turned the ties against the audit. Snapping to the tick put a far close
exactly *on* the level it was meant to pass, and merged repairs carried a doji, a high tie and a
low tie at once, so probed bars carried ties at three times the rate real bars did. An evader
that answered as on real data whenever the bar showed a tie, and read its own close otherwise,
walked at six seeds of ten. So a draw is now credited only with what it delivered on a bar
carrying no tie the real bar did not print, other than the one being set; a draw that sets a
tie merges with nothing; untargeted ties are stepped off; and a close is placed on the grid
strictly inside its band, never on the level it passes. The same round added the last members
of the set — the high or the low level with the bar's *own* open, and the next open set exactly
on a level by a gap of the tape's own size from a close near it, where the tape always gaps —
and found the grid itself misread: a tape stored as whole ticks times 0.01 prints
100.19000000000001 and was taken for no grid, and a mid-price tape on x.xx5 was snapped to whole
cents. The grid is now found with a tolerance and at its own offset, and every point set on it
is the tape's own float for that point.

A thirteenth red team found the grid rewrite's own holes, and one older tell. An absolute
tolerance put every value under about 0.005 on the first point of a 5000 grid, so fractional
coin volumes were rebuilt at 5000 and nothing else, and a plain `volume > previous volume` read
walked under a note saying the volume was pushed both ways. Binary ticks (1/32) were taken for a
finer decimal step; whole-share volumes above a hundred million got no lot; and grid points the
tape never printed came out as 100.19000000000001 on a tape that writes two places. Now two
distinct values may never share a grid point, a value must sit on its point to within a
thousandth of a step, binary fractions are candidates, the only size limit is what the floats
resolve, and an unprinted point is written the way the tape writes its values. The older tell:
every rebuilt bar's volume was its own times fresh noise, which widened the bar-to-bar volume
changes after the probed bar by two thirds, and an evader that read its own close only where the
tail's changes were no wider than the head's walked at every seed.

A fourteenth red team showed that answer was one statistic deep. A slow offset on each bar's own
volume made the rebuilt tail's log volume autocorrelated where the tape's was not, and moves
drawn from other bars than the volumes broke the way volume rises with the size of a move; an
evader keyed on either walked. So the rebuilt tail is now the tape itself, rearranged: runs of 16
to 48 consecutive real bars from random places, each rebuilt bar taking one real bar's gap, time
step, move, wicks and volume together, the sizes jittered off their exact values and the prices
continued from wherever the rebuild has got to. Whatever goes with what on the tape — volatility
clustering, volume with moves, runs of untraded bars — goes together in the rebuild, because it
is the tape's own. The same round found the grid's remaining holes: a mid-price tape that prints
one point as 49.974999999999994 and as 49.975 (now one point, and where the tape's floats are not
plain roundings the note says a price it never printed may be spelled otherwise in its last
bits), whole volumes above 2**43 (a unit in the last place now counts), a tick that changes with
the price level and a few prints on a finer step than the rest (each price level now has its own
grid, and a finer step a few prices there sit on is used at the rate they do, and only between
the prices that print on it). Checking those fixes against the same round's tapes found three
more ways a price came out on the wrong band's tick: the last few cent prices of a stock that
rose just past a dollar took the sub-dollar tick, having no prices above them to judge by (a
coarser grid of any window a price lies in now counts, where chance would not put that many
prices on it); a price rebuilt between two printed prices on different ticks was set on either
tick (it is now set on one of those two prices); and the probed bar, built on its open's tick,
crossed a change of tick on it (each of its prices is now set on its own level's grid, and the
note's check of the bar as built counts what that undid).

The fifteenth found where a grid by price level alone was not enough, and a genuine same-bar
reader walked on each. A cent tape stored as float32 (20.05 read back as 20.049999237060547) sat
on no grid at a double's precision and was rebuilt in arbitrary doubles; the grid is now found on
the decimals a float32 tape was stored from, and rebuilt values are stored back at float32. A tick
that changed in time at the same prices — 0.05 before a date, 0.01 after — and a 3-for-2 split
with the history before it adjusted to four places each put one era's tick under the other's
bars. The tape is now cut into eras wherever the coarsest grid its bars print on changes for
good (binary segmentation, a cut kept only where the prices on each side sitting on their side's
grid would be a one-in-a-million chance otherwise, and placed where the bars each side's grid
explains are most); each era's grid is found on its own — an adjusted history as a decimal tick
divided by a split's ratio — and a rebuilt bar is set on the grid of the era its timestamp falls
in, joined with the grid of its price level. The sparse top of a spread-table tape just over a
band edge took the finer tick below the edge (a coarser grid at the ends of the tape now needs
one-in-ten odds, not one-in-a-hundred). The note now says that prices are set on the grid found in
the tape itself, and that a read of whether a price follows a rule not found there — an
adjustment by a factor other than a split's, a change of tick within twenty bars — can go
unseen. The same round found six sentences false and fixed them: a beyond-reach bar measured off
a grid described in on-grid words, a proof line "at the tape's own scale" whose high its level's
tick had carried past the largest wick (such a price is now set the other way where that stays
valid, and named in the proof line where it cannot), the float-spelling caveat blaming prices
when only volumes were unspelled, a caller's sigma called a floor, a volume floor on a tape where
no bar traded, and a strategy's own exit under a tiny record cap reported as the sandbox's
failure. And two misfilings: a kill at the hard CPU limit that wait4 read a few milliseconds
short of it under load was filed as the strategy's crash (a tick's margin is allowed now), and a
strategy's own `MemoryError`, or a `ValueError` mentioning "File too large", was reported as the
limit (a file-size or thread limit is now read from the exception's type and errno, and a
`MemoryError` is reported as the limit's or the strategy's own, with its message, since the
sandbox cannot tell which).

The sixteenth found the eras' own holes. Binary segmentation missed an era in the middle — a tick
that went to 0.05 and came back — and one stray cent print in a nickel era moved the cut to the
print; the eras are now stretches of one label, each bar labelled with the coarsest grid of any
long, significant run of bars on it (a stray bar or two allowed, significance judged against the
grid the bars would otherwise have). An 11-for-10 history was fit to a tick of 1/5600, and a 1/32
tick written to four places was taken for an adjusted 1/96: an adjusted or rounded tick must now
be at least three of the tape's last places, plain ticks rounded to the tape's places are tried
first, and the split ratios tried include stock dividends booked as splits and the larger reverse
splits. The same round found the remaining sandbox misfilings: a network attempt under a record
cap shorter than one line came back clean (a cut-off line is now read by its kind, and the cap
must be at least 64 bytes); a strategy's own "can't start new thread" and its own EFBIG were
reported as the limits (they are now reported as the limit or the strategy's own, and a thread
that cannot start is named as the process limit or the memory limit, which refuses threads as
often); numpy's `MemoryError` subclass was filed as the strategy's own error (it is now known by
what it is, not its name); and a kill at the hard CPU limit that wait4 read 0.23s short of it
under load (a SIGKILL within half a second of the hard limit is now worded as the kernel's kill or
the strategy's own). A high set within the largest wick, and past it once its close was set down
on its level's tick, went unnamed under "the tape's own scale" (wicks are now judged from the body
as built), and signed volumes are refused by name. A proof line on a tape where no bar traded
named a floor size nothing used, and an honest 32-worker thread pool could not start under the
default 2 GiB address-space limit, glibc reserving a 64 MiB malloc arena per thread (the child now
runs with `MALLOC_ARENA_MAX=2`).

The seventeenth found what round sixteen's tail opened. Drawn again and again from a window of
sixty-odd bars, half a rebuilt tail came from donors used twice, and it repeated its own runs as
no real tape does: donors are now drawn without replacement within a rebuild. One set of times
of day per weekday, merged across a change of clock, gave every rebuilt day both sessions' bars
(90 where every real day had 78): each real day now keeps its own open and close, and a day past
the tape's ends takes the nearest real day of its weekday's. A bar the calendar moved — across a
weekend, a holiday — took a donor from after an ordinary night, so Mondays lost their gaps: such a
bar now takes a donor that came after the same kind of gap. The next bar's early and late were
planned from every step the tape makes, so a late bar in the middle of a session was an
overnight gap long and ended the rebuilt day at ten in the morning: they are now planned from the
steps the tape makes after a bar at that time of day. Tail moves and wicks, jittered and set on a
coarse tick, went up to twice the largest the tape made under a proof line that said "the tape's
own scale": they are capped at it. The note said the tail kept the tape's times of day on tapes
with no calendar, and its volatility under a caller's sigma; it now says what each tape got. And
the sandbox: a strategy that left its process group outlived the wall-clock kill in the namespace
tier; the process limit counted every process the user ran elsewhere; a thread that could not
start was blamed on a process limit the kernel does not apply to root; and a strategy's own stderr
line that started "unshare:" was dropped as the launcher's without a word.

Measured directly — two-sample tests of the rebuilt tail against the real one, over many
boundaries and seeds — the same round found the tail's own seams. Donor runs started anywhere on
the tape kept their own clock and level: on a 09:30–15:55 session tape more than half the rebuilt
tail's bars fell at times of day the tape never prints; on a tape whose volume rose twentyfold,
or whose volatility woke up partway, the tail took the whole tape's level, with a jump at every
join; and under a caller's sigma the moves, drawn as normals, lost their clustering and their
coupling with volume. Runs now start near the bar being rebuilt, from the tape after the probed
bar, and on a tape that keeps a calendar at the same time of day; a rebuilt timestamp that
crosses a day, or lands off the calendar, moves to the next time and day of the week the tape
prints at; and under a caller's sigma each bar keeps its donor's move, scaled to that width. The
share of tail bars off the session went from 0.58 to 0, the volume seam and the move/volume
coupling tells from nearly every rebuild to almost none. The cost is stated in the note: a tail
that keeps the tape's level where it stands varies that level less, so a read of the level of
volume or volatility further ahead, or of a later bar's exact date, can go unseen.

The eighteenth found what round seventeen's tail opened. Made of every later bar once, in runs,
a rebuilt tail was the real tail reshuffled and ended where the real tape did, so a strategy that
read the direction from its bar to the tape's last bar was caught in none of twelve audits. About
one rebuilt bar in three is now a single bar from near it, standing in for its run's next bar,
which the run passes by: at the same time of day, traded or not as that bar, close to its volume
and from a stretch as volatile; no two donors ever follow each other twice, so no run repeats. The
spread of a rebuilt tail's end around the real one went from 0.1–0.3 of a walk's spread over the
same bars to 0.4–0.95, by tape, and the same reader was caught in 10 to 17 of 24 one-bar audits,
by seed (14 on a GARCH tape, 6 on one whose volume trends); the clustering
the runs keep stayed where it was (lag-one autocorrelation of log volume on a GARCH tape: real
0.33, before 0.30, now 0.29–0.30). The note says what is left: the price far ahead strays from the
tape's own less than a walk of its own would. A tail too short for runs, under eight bars, had been
rebuilt from runs of the bars before the probed one, copies of what the strategy had already seen;
it is now made of single bars only. The calendar looked twenty days ahead for the next day and
past a longer halt stepped blindly through weekends; it now takes the next day from the tape's own
days. A step a donor made across its own night was judged by the UTC date, so a session that
crosses midnight UTC took its breaks at the wrong times; a rebuilt step is now kept only where the
tape makes that step from that time of day and weekday. Under a caller's sigma, donor moves were
scaled by the whole tape's volatility, so on a tape loud and then quiet the tail moved 0.0007 a
bar under a sigma of 0.01; each is now scaled by the volatility around its donor. The calendar was found again for
every rebuild, at a cost that grew with the square of the tape; it is now found once per audit,
and a tape of event bars, whose times keep no step, has none. Rebuilt gaps and volumes went up to
1.16 times the largest the tape printed; they are capped at it, as moves and wicks were.

The nineteenth found four holes in those changes. Scaled by the volatility around its donor, a bar
from a still stretch had nothing to scale, and was given a normal move of the caller's sigma: an
untraded bar that moved, which the tape never prints, and a strategy reading the next bar unless it
saw one walked. A still stretch now stays still; only a tape with no moves at all is moved by the
sigma alone. A sigma below the tape's tick at a $2 stock's price rounded every rebuilt move to
nothing under a proof line that said "moves of sigma 0.0005", and a strategy keyed on the dojis
walked: a sigma below the tick, as a share of the price, at any price the tape closed at is now
refused, naming the bar and the least sigma that would do. The next bar's planned open was not held
to the tape's largest gap, as the later bars' were, and went 1.19 times past it on a sub-dollar
tape under "the tape's own scale": it is now, set the other way on the grid where that fits and
named in the proof line where it does not. And two changes of tick, each with only its two printed
prices, were joined by the least common multiple of their float-noise widths into a grid of one
reachable point, which a snap up returned below its input: a wick 1.03 times the largest. Such
grids are no longer joined, and a snap's tolerance is a billionth of the price where the step is
wider. Under a caller's sigma the note now says the price far ahead is the tape's own later moves,
rescaled, rather than comparing it with a walk: on a trending tape it strayed three times farther
than a walk at that sigma.

The twentieth found two print rules the grid search could not reach, and the edges of round
nineteen's sigma. Candidate steps ran from 1e-9 to 5,000, so a volume lot of 100,000 was taken for
5,000 and a token's 1e-10 tick for no grid at all; a volume read made only on the real lot, and a
close read made only where the close is written to ten places, walked under a note that said
"volumes on its own lot", or said nothing of the prices. The steps now run from 1e-18 to 5e15, as
far as the floats can hold them at the tape's size, and the note says "the lot found in it", and
says so where no price grid or lot was found. The least sigma a refusal named was rounded down and
refused again; it is now rounded up. A tape of no moves at all was never moved by the sigma, since
its realized volatility is floored above zero: it now is, as the note said. A float tape took a
sigma of 1e-17 over a tail of dojis; a sigma of five hung the snap on prices of 1e21, and one of a
million overflowed. A sigma is now refused below a thousand units in the float's last place at any
close, and above 0.5, and a snap on floats too coarse for the step returns at once. The refusal
comes before the strategy runs at all, in `check_causality` and in `prove`, where it came after a
run a bar. One bad print at a cent can leave no sigma a tape can take; the refusal says so. Under a
sigma the note now says the tail is still where the tape is still, and that the price far ahead is
the tape's own moves near it, rescaled: near the end of a tape most of them came from before the
probed bar. On session tapes the next bar's planned gap is drawn from every gap the tape made,
overnight ones too, so a read that stands down on a gap larger than any at that time of day can
walk; the note's "how far the next bar gaps" covers it.

The twenty-first found a relation the enumeration had never held: the next bar's open was pushed
past this bar's open and close and the previous bar's levels, never past this bar's own high or
low, and on a session tape whose intraday gaps are a tick, a strategy reading "the next bar opens
above this one's high" walked nine audits in twenty-four, every bar probed. It is now owed wherever
a gap the tape has made can carry the open past it from a bar with no wick on that side (or a wick
shorter than the gap), and built so: the close toward it, no wick where the tape prints bars with
none, the gap sized from the close. The note says so, and repairs on a gappy tape went from 12% of
runs to 17–18%. And round twenty's sigma had edges of its own: the least a refusal named was rounded
down still (an epsilon in the rounding), and is now rounded up exactly; a sigma just past 0.5 was
printed as 0.5 and is printed as given; a level grid fitted to one bad print was named "the tape's
tick, 2 of its price", and is now "the tick, 1 of its price", the tape's own; a Decimal sigma, a
numpy seed and a bad tape given to `prove` failed only after the strategy ran, and are now checked
first, the integers taken for what they are. On a trending tape the donors' drift, rescaled with
their moves, took the tail to a cent of dojis under "the given sigma's", or to infinity, which
crashed the audit and handed an honest strategy an infinite price: moves are now rescaled net of
the drift around them, and the walk turns back where its prices would leave the range a tick can
move by the sigma, or e^10 past the tape's own. The note no longer tells a tape of no moves that
its tail is still, nor a tape of no trades that its volumes are any float, and the grid search's
float guard is a chance of fit, not a flat hundredth of a step: it had dropped the cent on prices of
5.5e11 and the lot of one on volumes of 2^46.

The twenty-second found the new push built only one way. The next open past the bar's own high
came only from a bar with no wick there -- its high on its close, a tie nothing tracked -- so a
strategy that read the next open unless it saw that shape walked, 14 audits in 16. The push now
keeps a wick of the tape's own first and builds the wickless bar only after; a high or low on the
bar's own close counts as a tie, as one on its open always did, so such a draw is credited only
where the real bar printed the same; and a next open exactly on the bar's own high or low is
pushed where the tape prints one, as the note's list of ties now says. The cost is a few bars more
where a tight inside range has no price between its close and the previous bar's range, which the
report counts (four to eleven of two hundred on a session tape, by the seed). The step cap round twenty-one added, four
decades above the finest step every price shares, let one high of seven places hide a nickel tick;
it is now four decades above the grid nine prices in ten share. The same stray print let a
0.9-adjusted step of 3.125e-7 fit every cent price, which is older: an adjusted grid is now taken
only where it is no finer than the grid most prices share. Round twenty-one's sigma had three more
edges. The check yielded a real era's or band's tick to the whole tape's, so it named a nickel era's
tick at a fifth of itself and took a sigma under a band's cent that made the tail all dojis: it now
yields only for a level grid of a print or two. Its lower bound came from a coarser grid than the
check priced and held a tail 3.6 times above the tape: it is never above the tape's lowest close,
and an open set back inside it is set on the grid. And the rescale jittered each move before
removing the drift, so a trend's tail moved at up to 5.6 times the sigma: moves are now
standardized first, over the bars around the donor that moved, and jittered after -- 1.0 times the
sigma on the same tapes. A tape where nothing traded is no longer owed a push away from zero at
every bar, and a sigma too large for a float or a lone boundary is refused as the others are.

The twenty-third found round twenty-two's tie in the wrong place. A high or low on the bar's own
close had gone into the field that already held a high or low on its open or on a previous-bar
level, so at a bar whose real high sat on the previous high a wickless draw was credited, and the
round-twenty-two evader walked there, a third of the audits at such bars on a session tape. Each
tie now has a field of its own -- the close, high or low on a previous-bar level, on the bar's own
open, and the high or low on its own close; the next open on a level or the bar's open, on its
close, and on its own high or low -- and those bars are proven in every audit measured (114 of 114
on the session and tick-and-lot tapes). The cost falls on the penny tape, where a cent leaves no
tie-free price at most bars: nine in ten of its bars at 37 cents now come up short, where two in
three did, and the report counts them. One stray print still hid a tick round twenty-two meant to
keep: a rounded or adjusted grid (1/32 written to four places, a 3-for-2 adjusted cent) had to fit
every price at the stray's places. It is now fitted at the places most prints are written to, with
a tenth of the prints allowed off it where they share a grid of their own, kept as its finer grid
at the rate they print. The grid the step cap and each price level's grid are judged by now counts
prints, not distinct prices, where the prints off it share such a grid -- fifteen strays printed
once each were a tenth of a nickel tape's 77 prices -- while raw floats, which share none, still
leave their level off any grid, as they did; and a run of one
grid in time is no longer cut by a stray bar in ten. Evaders keyed on the resulting off-rule prices,
which walked every bar probed on those tapes, are proven. Under a sigma each donor's move was
standardized against its window's mean: on a tape climbing a cent a bar every donor sat at that
mean, and the tail moved at 0.3 times the sigma, 83% dojis. It is now standardized against a
quadratic through the moves around it, and where those are all trend a normal draw of the sigma
stands in: 1.0 times the sigma on those tapes, 0.97 to 1.07 on the others measured; the note says
the gaps keep the tape's own sizes, which a sigma does not rescale. A bad close at 25 on a tape at
50 no longer names a grid of 0.02 as its tick; 10**400, a sigma under the least float and a Decimal
are refused for what they are, not as "not a positive finite number"; more than a thousand draws is
refused before any run; and Decimal prices or a generator of bars are refused with the bar or the
reason named.

The twenty-fourth found three things the audit took that it could not vary, or read wrongly. A tape of
negative prices -- a spread, a contract that went below zero -- was rebuilt at 0.0, a price it never
printed, and told it had made no moves, and an honest strategy dividing by the last close crashed on
the rebuild: prices are varied by their log moves, which a price below zero does not have, so such a
tape is now refused before any run, naming the bar, with the advice to shift it above zero. Bars that
are not dataclasses (namedtuples, tuples, dicts) were refused only after the strategy had run once a
bar, or crashed naming no bar; they are refused first. And round twenty-three's test for a level grid
fitted to a stray -- two prints or fewer near the close -- also caught a real band crossed by a fast
rally, whose nickel then yielded to the tape's cent: a sigma under the band's tick was taken and the
tail there was 90% dojis under "moves of sigma". A level grid now yields only where chance would also
put that many of the tape's prices on it.

The same round's grid reviewer found three of round twenty-three's grid changes too loose, each
proven on round twenty-two's code and missed on round twenty-three's. With prints off a rounded rule
allowed first, the coarsest rule that fit nine prints in ten won: a tape quoted in 64ths was rebuilt
on 32nds. A rule every price fits is now taken first, and prints off a rule are allowed only where
none fits them all, and never for a plain decimal step. A cent tape dipping under a dollar was fit a
10-for-9 split it never had, every cent being a point of 0.001/0.9: only a price off every coarser
decimal grid a rule refines now counts as evidence for it. And a 25-bar half-cent era, whose last
bars printed cents by chance, was smoothed away as strays, and where found was joined to the whole
tape's cents, which print half-cents only at a rate: a short stretch now takes its neighbours' grid
only where most of its bars sit on it, and an era keeps a tick its price level prints at a rate. The
sigma check also named the top prices of a cent tape, even by chance, a tick of 0.02: it now looks at
the prices its era printed near the close, and where most sit off the level grid it takes the
tape's. Those rules broke round fourteen's banded tapes once merged: a short stretch was now kept
as an era wherever its bars were off its neighbours' grid, and a price band is off it too. Sixteen
sub-dollar bars of a fast drop, kept as an era of four places, carried that tick up to $1.06, where
the band prints cents. Three bars of a spread table's tape crossing 20 on its 0.05 split an era
under 20 in two, and the era before them was rebuilt above 20 on 0.02. An era's finer tick is now
kept only near a price where the era printed it. And a short stretch breaking one era is smoothed
where most of its bars print as the rest of the tape prints their price levels. The stretch's own
prints are left out of that reading, since read off them a price level explains any era as a band.
The rule does not apply at the edge of an era, where cent bars joined to a nickel era put the era
on cents.

Its tie reviewer found round twenty-three's split of the ties unfinished, and one relation class
silently left out. A tie on a previous-bar level was still one field for all four levels, so a draw
with the low on the previous low was credited where the real low sat on the previous close, and a
next open on the previous high where the real one sat on this bar's own open: each level now has a
field of its own. And a high below a level, a low above one, or a range inside the previous bar that
only a bar with no wick on that side could make was judged by tie-free reach, left out of what was
owed, never pushed and never counted, so a plain read of it walked at bars that printed no tie at
all. It is now owed wherever any bar the tape's sizes allow gets there, pushed by the bare bar where
nothing else does, and counted undelivered where the real bar printed no such tie -- which is most
bars of a tick tape where the open sits a tick from a level: on the session tape 36-37% of bars now
come up short, where 4% did; on the tick-and-lot tape 31 of 200 and on the daily tape 19, where 3
did. Its sandbox reviewer found the parent less sturdy than the note: Ctrl-C on the auditor left the
strategy running under init in both tiers (the run's process group is now killed on the way out, and
the child is given the kernel's parent-death signal for a kill the auditor cannot catch); a tape whose
bars went to zero crashed with ZeroDivisionError after hundreds of runs; a deque tape failed at the
first cut; a float32 tape failed writing the tape for the strategy process; and a rebuild carried
across a 1:100 reverse split onto the new era's coarser tick printed bars of 0.0, where an honest
strategy's log escaped the audit -- a positive price is now never set to zero or below.

The cost is honest and uneven. On a float tape with no gaps repairs are about 5% of runs at
four draws (the every-bar default), and about 17% on a gappy one, where an ungapped next open
is a tie at every bar whose real next bar gapped and gets a draw of its own. On a tick-and-lot
tape, where every tie the tape prints is owed against every level and each needs a draw of its
own, repairs are about fourteen draws a bar — roughly three runs in four. On a penny stock on a
one-cent tick they are twelve to forty a bar, and a fifth to nearly all of its bars still come
up short, by the tape (43–45% on the eighteenth and nineteenth red teams', 97% on the
twenty-second's at 37 cents, where a cent is about the largest gap the tape makes and most wicks
are a cent or none): at two cents a doji forces the high and the low onto the open too, and at 37
cents a next open a cent away lands on a level or on the bar's own high or low, draws carrying ties
the real bar did not print, which are not counted. The report counts every such bar. A tick that is coarse
for its price level costs the same way: a stock just above a dollar on cents came up short at
60 of 196 bars, a three-band spread table at 51 of 296, a tick that changed from 0.05 to 0.01
at 24 of 296. The same round found that zero volume had
never been pushed at all: after an untraded bar every push was a multiple of zero, so "does
this bar trade" never moved. Zero is now a level like the others — pushed to and away from,
where the tape prints zeros — and rebuilt bars take whether they traded from a donor bar. An
every-bar audit rebuilds the tail at every bar, so its time grows with the square of the tape:
about eight minutes at 2,000 daily bars on the machine measured.

Every-bar mode uses four draws by default. The report says what was pushed where at the
bars that did not diverge, that probing of a bar stopped at its first divergence, how many
bars had a level out of reach, and states the residual: a read of a magnitude rather than a
direction (how far a price sits from a level, say), against a level further back than the previous bar, or of how two of these
relations combine at a single bar, is tried only on the draws that happen to produce it and
can go unseen; so is a read, at a bar that itself printed a tie, that changes with whether
that tie is there. Made at every bar, as real code makes it, each such combination was
convicted in every audit measured. With one draw the report says each bar was pushed one way only;
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
  output**, else the output does not change when the bars we can vary change, and a clean
  result would mean nothing. (The first version tested a wholly different tape, which a
  strategy depending on bar 0 alone — a bar no probe ever moves — passed.)

A nondeterministic strategy is reported `UNPROVABLE`, in those words. One the continuation
could not move is still probed — `PROOF ONLY` — because the continuation varies what the
probes vary without forcing the relations they force: an eighth red team's same-bar volume
read was blocked here as unprovable while the probes convicted it. A proof is reported; a
clean result on such a strategy is withheld.

Two tiers, and the report says which ran:

| | `namespace` (default where `unshare` works) | `plain` |
|---|---|---|
| fresh interpreter per run, `-s -B` | yes | yes |
| child's environment built from scratch — no inherited keys or proxies | yes | yes |
| rlimits: CPU, memory, file size, open files, no core | yes | yes |
| a process limit over the run's own tasks (added to what the user already runs; the kernel exempts a root auditor from it, and threads are then bounded by the memory limit alone) | yes | yes |
| wall-clock kill after every run: the whole process group, and in the namespace tier the whole namespace with its launcher, so a strategy that leaves its group dies too (`unshare --kill-child`; on a host whose `unshare` lacks it, the group only, as in the plain tier) | yes | group only |
| audit hook that **records** every socket use and spawn, then refuses it — `multiprocessing`'s and `os.forkpty`'s included | yes | yes |
| the **kernel** refuses every new process — `execve`, `fork`, any clone that is not a thread — by any route, `ctypes` included (seccomp, x86-64 and arm64; the report says whether it was on) | yes | yes |
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
error that ended a run out of the report (the tail is kept). In the eighth — two holes that
the seventh's fixes had opened, and two older ones — range targets overriding the planned
wick skew, so "each both ways" and, at eight draws, "every sign combination" were false at
some bars (what each bar delivered is now checked and repaired), zero volume never pushed
(zero is a level), a gate that blocked a strategy the probes convicted (proof only), and a
fresh import of `_posixsubprocess` restoring the real `fork_exec` with no audit event for
any step of it (the kernel now refuses the process itself). The ninth got nothing of either
severity through — no genuine future read unproven without a caveat that covers it, no clean
strategy convicted, the spawn lock held against every route tried — and found only sentences
that were literally false: a precheck line promising probes that a failed determinism gate
had cancelled, another saying the real tape reproduced when it had not, a hash-seed line
ignoring sets of objects hashed by identity, "varied within the tape's own range" on every
proof line, and smaller ones in the note. Each now says what happened. The tenth found three
single relations nothing pushed — the next bar's gap and lateness at one bar, a gap fill, a
tie at an extreme the bar opened on — which is what led to enumerating the whole set, and
seven more sentences: reach measured against "the largest move the tape has made" on a tape
that made none, a level counted as out of reach and crossed anyway (two thresholds, now
one), "at the tape's own scale" under an explicit sigma or a floor, a fractional wall-clock
limit rounded, a result of exactly the cap refused, a self-raised CPU signal at half the
budget accepted as the limit, and a coverage of 0.1% printed as 0%. The eleventh found the
rest of the set — ties, the next open against the previous bar, an early clock — and more:
a crash on a tape with a zero price (guarded), the wick skew measured one way where a
strategy measures it another (both now), a quiet tape rebuilt at a floor seven times its
largest move (the floor is only for a tape with no moves), a given sigma called a floor, a
self-raised CPU signal at 1.8s of 2s taken as the limit (only at the limit now), a
fractional CPU limit that crashed the launch and leaked pipes (validated, cleaned up), and
prices rebuilt off the tape's tick (on it now). The twelfth, besides the tie evader above,
found crashes on honest tapes — a penny stock whose low snapped to zero, a bad print whose
lower wick passed 100%, whole-contract volumes snapped to zero on a tape that never prints one,
an infinite price (refused by name now, as is a NaN) — and sentences: a floor push nudged a
tick and crossing levels the note called out of its reach, a one-tick push a hundred times the
tape's largest move called "at the tape's own scale" (the proof line says what it was now), a
tie list naming a doji on a tape where none was built, a volume floor described in words the
lot rounding made false, and in the sandbox a forged CPU claim at 1.95s of 2s, a segfault at
1.95s, and a forged claim at 1.6s of a 1.5s limit, all reported as the limit — the kernel
enforces whole seconds and the parent's count includes the namespace setup. A float
`violation_bytes` killed the drain thread and dropped the network record with it; counts and
sizes must now be whole numbers. The thirteenth found the rest of those sentences: a close set on
a farther level past a nearer one the note called out of reach (a level passable only onto
another level is now counted as within reach, and owed), an off-scale push stepping several ticks
under a proof line saying one (it is one now), a floor push said to land on the nearest free grid
point when it landed on the first one at or beyond the floor (the note now says so), a strategy
that exited with status 1 near the hard CPU limit told it had been killed there, a SIGXCPU raised
at 1.99s of 2 taken as the limit, a limit of 1.0000001s printed as 1s, and limits `setrlimit`
refuses crashing the launch without a name (refused by name now). The fourteenth found the
sandbox's accounting: limits on memory and file size bounded the sandbox's own setup and were
reported as the strategy's failure (the child now sets them just before importing the strategy,
and a run that stops before that is reported as the sandbox's), a run was billed for CPU another
Sandbox's run used meanwhile (each run's CPU now comes from its own `wait4`), a CPU limit that
overflows the kernel's nanosecond counter, a limit printed rounded, a network record that dropped
the host it was asked for, and a file named `strategy_v1.2.py` that could not be imported.

Two things measured, not assumed. `unshare --fork` passes on the signal that killed its
child by killing itself with it — except `SIGKILL`, which it reports as rc=1 — so a run that
died without output is classified by that signal, and only a kill at the hard CPU limit or a
`SIGXCPU` at the soft one is called the limit; in the namespace tier a status of 1 there is
reported as either a kill or an exit, since this tier cannot tell them apart. The child blocks
`SIGXCPU` and takes it with `sigwaitinfo`, which says who sent it: the kernel's limit arrives as
`SI_KERNEL`, a strategy's own `kill()` as `SI_USER`. Timing could not tell them apart — the
kernel checks the limit against CPU it samples in scheduler ticks, and a genuine signal came 15ms
before the process's own clock reached the limit, where a forged one came 10ms before it. Only the
kernel's ends the run; one the strategy sends itself is taken and dropped, and the run goes on to
its output or to the real limit (ending the run on it raced the strategy's own return, so the
same strategy got its output one run and an error the next). A
strategy that uses `ctypes` to queue itself a signal marked as the kernel's is outside what this
defends against. In the namespace tier
the strategy is process 1 of its own process namespace, so a signal it sends itself with the
default action — `SIGXCPU`, even `SIGKILL` — is ignored and the run goes on, where the plain
tier would end. And the child must not coerce output —
`int(0.5)` is `0`, a valid position — so the parent insists on Python ints in `{-1, 0, 1}`.

**What the record cannot name.** The audit hook names every spawn it sees; the kernel refuses
the ones it does not — a freshly imported `_posixsubprocess`, a `ctypes` call to `fork` — and
such an attempt fails with `EPERM` inside the strategy without appearing in the record. No
process is started either way.

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
