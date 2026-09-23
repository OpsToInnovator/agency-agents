"""The detector is judged on its acquittals as much as its convictions.

One false accusation costs the credibility of every true finding, so the clean fixtures
matter more here than the dirty ones.
"""
from __future__ import annotations

import dataclasses
import importlib
import math

import pytest

from edgecheck.causality import (Divergence, Proven, Report, _perturbed, check_causality,
                                 continuation, default_boundaries, realized_sigma)
from edgecheck.fixtures import bars
from edgecheck.fixtures.strategies import leak_backfill, leak_centered_window

CLEAN = ["clean_lagged", "clean_but_costly"]
LEAKY = ["leak_same_bar_close", "leak_future_close", "leak_centered_window",
         "leak_full_sample_zscore", "leak_backfill", "leak_peak_threshold"]
EVASIVE = ["evade_continuity", "evade_envelope", "evade_intrabar"]
TRUE_REACH = {"leak_same_bar_close": 0, "leak_future_close": 1,
              "leak_centered_window": leak_centered_window.K,
              "leak_backfill": leak_backfill.EVERY - 1}


def strat(name: str):
    return importlib.import_module(f"edgecheck.fixtures.strategies.{name}")


@pytest.fixture(scope="module")
def tape():
    return bars(300)


@pytest.mark.parametrize("name", CLEAN)
def test_a_clean_strategy_is_never_accused(name, tape):
    report = check_causality(strat(name).signals, tape)
    assert not report.leaks, f"{name} was accused: {report.describe()}"
    assert report.proven == ()


@pytest.mark.parametrize("name", LEAKY)
def test_every_known_leak_is_caught(name, tape):
    report = check_causality(strat(name).signals, tape)
    assert report.leaks, f"{name} leaks ({strat(name).LEAKS}) and went undetected"
    assert report.worst_horizon is not None


@pytest.mark.parametrize("name,reach", TRUE_REACH.items())
def test_the_reported_reach_never_overclaims(name, reach, tape):
    """Horizon is a lower bound. Reporting more than the fixture actually reads is a lie."""
    report = check_causality(strat(name).signals, tape)
    assert report.worst_horizon <= reach, (
        f"{name} truly reads {reach} bar(s) ahead but the report claims {report.worst_horizon}")


def test_truncation_alone_is_blind_to_a_same_bar_leak(tape):
    """The finding that shaped the design: cut the tape at bar k and bar k's close is still in it.

    With draws=0 no perturbation run happens, leaving truncation on its own. It
    catches the leak that runs off the end of the data and misses the one sitting inside
    the current bar -- which is the most common lookahead in real code. If this test ever
    starts passing with the perturbation probe removed, the probe has been broken.
    """
    truncation_only = check_causality(strat("leak_same_bar_close").signals, tape, draws=0)
    assert not truncation_only.leaks

    both = check_causality(strat("leak_same_bar_close").signals, tape)
    assert both.leaks
    assert both.worst_horizon == 0
    assert all(p.evidence.probe == "perturbation" for p in both.proven)


def test_truncation_still_carries_the_leaks_that_run_off_the_end(tape):
    """The converse: the probe that is blind to same-bar leaks is the one that finds these."""
    report = check_causality(strat("leak_full_sample_zscore").signals, tape, draws=0)
    assert report.leaks
    assert all(p.evidence.probe == "truncation" for p in report.proven)


@pytest.mark.parametrize("sigma", [0.002, 0.05, 1.5])
def test_detection_does_not_depend_on_the_size_of_the_nudge(sigma, tape):
    """Measured after the pristine comparison landed, and it overturned an earlier table.

    When perturbed runs were compared only to each other, a wide nudge blinded the probe to
    full-sample leaks and a narrow one weakened it against back-fills, and the docs carried
    a sigma table explaining the tradeoff. Comparing every perturbed run against the
    PRISTINE run instead, every leak is caught at every sigma. The nudge size is now chosen
    for one reason only -- so a perturbed bar has no tell -- and this test keeps anyone from
    reintroducing the tradeoff by weakening the comparison.
    """
    for name in LEAKY:
        report = check_causality(strat(name).signals, tape, draws=4, sigma=sigma)
        hits = [p for p in report.proven if p.evidence.probe == "perturbation"]
        assert hits, f"{name} not caught by perturbation at sigma {sigma}"


def test_a_perturbed_tape_keeps_every_invariant_the_pristine_tape_has(tape):
    """The seam is what the first evasion keyed on. There must not be one.

    Where the pristine tape has open == previous close, so must the perturbed one; the
    prefix up to the boundary is untouched; the boundary bar keeps the open the strategy
    was entitled to; every bar is a possible bar; and the nudge is on the tape's own scale.
    """
    k = 120
    p = _perturbed(tape, k, seed=1000, sigma=None)
    assert p[:k] == list(tape[:k])
    assert p[k].open == tape[k].open and p[k].close != tape[k].close
    for i in range(1, len(p)):
        assert abs(p[i].open - p[i - 1].close) <= 1e-9 * abs(p[i - 1].close), f"seam at bar {i}"
    for b in p:
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high and b.low > 0
    assert 0.001 <= realized_sigma(tape) <= 0.01


def test_the_probe_aware_leaks_are_convicted(tape):
    """Three strategies that leak on real data and behave when they smell a probe.

    Each got PROVABLE and a clean report from the first version. The pristine comparison
    convicts them all: on the real tape their output at the boundary used that bar's own
    fields, and the causal fallback they switch to cannot reproduce it.
    """
    for name in EVASIVE:
        report = check_causality(strat(name).signals, tape)
        assert report.leaks, f"{name} ({strat(name).LEAKS}) walked out clean"
        assert any(p.evidence.probe == "perturbation" for p in report.proven)


def test_a_divergence_that_does_not_reproduce_is_not_proven(tape):
    """A clock-bucket seed slipped the same-tape-twice gate and was then convicted of a
    130-bar lookahead the clock had produced. Nothing is Proven until both runs reproduce."""
    calls = [0]

    def drifting(bs):
        calls[0] += 1
        out = [0] * len(bs)
        for i in range(3, len(bs)):
            out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
        if calls[0] > 2:                      # the "clock" moves after the first two runs
            out[10] = -out[10]
        return out

    report = check_causality(drifting, tape)
    assert report.nondeterministic
    assert report.proven == ()
    assert "nondeterministic" in report.describe()
    assert "PROVEN" not in report.describe()


def test_the_gate_and_the_probes_share_their_boundaries(tape):
    """The input-dependence gate must test the region the probes can reach, and only that.
    One red-team strategy depended on bar 0 alone, which no probe moves, and was
    certified provable."""
    bounds = default_boundaries(len(tape))
    assert bounds == [45, 120, 195, 270]
    other = continuation(tape, bounds[0], seed=7)
    assert other[:bounds[0]] == list(tape[:bounds[0]])
    assert other[bounds[0]].open == tape[bounds[0]].open
    assert [b.close for b in other[bounds[0]:]] != [b.close for b in tape[bounds[0]:]]


def test_a_proof_cannot_be_filed_without_its_evidence():
    """The PROVEN/SUSPECTED split is structural, not a flag someone remembers to set."""
    with pytest.raises(TypeError):
        Proven(summary="reads the future")  # type: ignore[call-arg]

    d = Divergence(index=5, boundary=9, baseline=1, variant=-1, probe="perturbation", detail="varied")
    assert Proven(d, "x").horizon == 4
    t = Divergence(index=5, boundary=9, baseline=1, variant=-1, probe="truncation", detail="removed")
    assert Proven(t, "x").horizon is None          # truncation carries no reach


def test_a_clean_run_never_claims_innocence(tape):
    """Absence of evidence is reported as absence of evidence."""
    text = check_causality(strat("clean_lagged").signals, tape).describe()
    assert "not a clean bill of health" in text
    assert "PROVEN" not in text


def test_the_same_input_gives_the_same_answer(tape):
    """A probe that is itself random would report a leak on a coin flip."""
    a = check_causality(strat("leak_peak_threshold").signals, tape, seed=1)
    b = check_causality(strat("leak_peak_threshold").signals, tape, seed=1)
    assert a.proven == b.proven
    assert a.boundaries == b.boundaries
    assert (a.probes_run, a.worst_horizon) == (b.probes_run, b.worst_horizon)


def test_a_tape_too_short_to_probe_is_refused():
    with pytest.raises(ValueError, match="at least 8 bars"):
        check_causality(strat("clean_lagged").signals, bars(5))


def test_findings_are_ordered_worst_first(tape):
    report = check_causality(strat("leak_full_sample_zscore").signals, tape)
    horizons = [p.horizon for p in report.proven if p.horizon is not None]
    assert horizons == sorted(horizons, reverse=True)
    kinds = [p.evidence.probe for p in report.proven]
    assert kinds == sorted(kinds, key=lambda k: k != "perturbation")     # perturbation first


def test_an_empty_report_is_falsey_about_leaking():
    assert Report().leaks is False
    assert Report().worst_horizon is None


@pytest.mark.parametrize("sigma", [0.002, 0.01, 0.3, 1.5])
def test_every_perturbed_bar_is_a_possible_bar(sigma, tape):
    """The probe must never hand the strategy a bar the market could not have printed.

    Nudging each field independently broke high >= low on 42% of perturbed bars. A strategy
    that validates its input dies on that; a strategy that merely reacts to it has the
    reaction recorded as evidence of lookahead, which is the failure this whole module
    exists to avoid.
    """
    for seed in (1000, 1005, 1011):
        for bar in _perturbed(tape, 120, seed=seed, sigma=sigma)[120:]:
            assert bar.low <= bar.high, f"inverted bar at sigma {sigma}"
            assert bar.low <= bar.open <= bar.high, f"open outside its range at sigma {sigma}"
            assert bar.low <= bar.close <= bar.high, f"close outside its range at sigma {sigma}"
            assert bar.low > 0, f"non-positive price at sigma {sigma}"
            assert bar.volume >= 0, f"negative volume at sigma {sigma}"


def test_the_boundary_bar_keeps_the_open_the_strategy_was_entitled_to(tape):
    """Bar k's open is knowable at bar k's decision -- perturbing it would be unfair."""
    for seed in (1000, 1007):
        p = _perturbed(tape, 120, seed=seed, sigma=0.01)
        assert p[120].open == tape[120].open
        assert p[120].close != tape[120].close
        assert p[121].open != tape[121].open


def _length_artifact_strategy(flip_at_length: int):
    """A causally clean strategy that wobbles at exactly one array length.

    This is what a floating-point shape artifact looks like from outside. Measured on a
    real FFT-based causal filter, values differ by 4e-14 between a 400-bar run and a
    100-bar run because the transform pads to a power of two derived from the total
    length. Mathematically the filter is past-only; arithmetically it is not identical,
    and a difference that small still flips a threshold outright, reproducibly, whenever
    it happens to land on one. No strategy here reads the future.
    """
    def signals(bs):
        out = [0] * len(bs)
        for i in range(3, len(bs)):
            out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
        if len(bs) == flip_at_length and len(out) > 5:
            out[5] = -out[5]
        return out
    return signals


def test_a_single_truncation_hit_is_suspected_not_proven(tape):
    """One boundary is a coincidence. A real leak diverges wherever you cut."""
    boundaries = [45, 120, 200, 270]
    report = check_causality(_length_artifact_strategy(45), tape, boundaries=boundaries)

    assert not report.leaks, "a one-boundary wobble must not be sold as proof"
    assert report.proven == ()
    assert len(report.suspected) == 1
    assert "not corroborated" in report.suspected[0].reason

    text = report.describe()
    assert "SUSPECTED, not proven" in text
    assert "PROVEN" not in text


def test_two_truncation_hits_are_proven(tape):
    """Corroboration across boundaries is what promotes a truncation finding."""
    class Twice:
        def __call__(self, bs):
            out = [0] * len(bs)
            for i in range(3, len(bs)):
                out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
            if len(bs) in (45, 120) and len(out) > 5:
                out[5] = -out[5]
            return out

    report = check_causality(Twice(), tape, boundaries=[45, 120, 200, 270])
    assert report.leaks
    assert len(report.proven) == 2
    assert report.suspected == ()


def test_a_leak_on_one_bar_is_caught_by_every_bar_and_reported_as_coverage_by_sparse(tape):
    """A same-bar read is visible only at the boundary bar itself. A fixed schedule of
    four boundaries is a schedule a leak can be written around -- a red team leaked on
    every bar except those four and walked. Random boundaries make that a gamble the
    report quantifies; every_bar makes it impossible."""
    single = strat("leak_single_bar").signals
    complete = check_causality(single, tape, probes="every_bar", seed=1)
    assert complete.leaks and complete.worst_horizon == 0 and complete.coverage == 1.0
    assert "every bar from 4 on was probed" in complete.describe()

    sparse = check_causality(single, tape, probes="sparse", seed=1)
    assert 0 < sparse.coverage < 1.0
    assert "possible boundaries" in sparse.describe()
    # caught exactly when its bar is on the schedule, never otherwise
    for seed in range(12):
        r = check_causality(single, tape, probes="sparse", seed=seed)
        assert r.leaks == (57 in r.boundaries)


def test_a_same_bar_volume_read_is_a_leak(tape):
    """No probe moved a volume until a red team read one."""
    report = check_causality(strat("leak_same_bar_volume").signals, tape, probes="every_bar", seed=1)
    assert report.leaks and report.worst_horizon == 0


def test_boundaries_come_from_a_nonce_the_strategy_never_sees(tape):
    a = check_causality(strat("clean_lagged").signals, tape, seed=1).boundaries
    b = check_causality(strat("clean_lagged").signals, tape, seed=2).boundaries
    c = check_causality(strat("clean_lagged").signals, tape, seed=1).boundaries
    assert a == c and a != b
    assert all(4 <= k < len(tape) for k in a)


def test_a_strategy_that_reproduces_only_on_the_real_tape_is_named_for_it(tape):
    """Deterministic on the pristine tape, random on anything else. The old guard called
    that 'nondeterministic' and stopped; now it is reported as what it is."""
    import random
    real_sum = sum(b.close for b in tape)

    def two_faced(bs):
        if len(bs) == len(tape) and abs(sum(b.close for b in bs) - real_sum) < 1e-9:
            return [1 if bs[i].close > bs[i].open else -1 for i in range(len(bs))]     # leaks
        rng = random.Random()                                                          # os.urandom-seeded
        return [rng.choice((-1, 1)) for _ in bs]

    report = check_causality(two_faced, tape, seed=1)
    assert report.recognises_input and not report.nondeterministic
    assert report.proven == ()
    assert "distinguishes real data from varied data" in report.describe()
    assert "intermittently nondeterministic" in report.describe()     # the report never asserts intent


@pytest.mark.parametrize("name,kw", [("leak_same_bar_wick", {}),
                                     ("leak_next_bar_gap", {"gap_prob": 0.15}),
                                     ("leak_next_bar_ts", {"late_prob": 0.12})])
def test_a_read_of_a_shape_property_is_a_leak(name, kw):
    """Wick ratios, gaps and time steps used to be copied per bar onto the rebuilt tape,
    so a strategy reading exactly those never moved under any probe. They are now drawn
    from donor bars: same distribution, different values."""
    tape = bars(200, **kw)
    report = check_causality(strat(name).signals, tape, probes="every_bar", seed=1)
    assert report.leaks, f"{name} ({strat(name).LEAKS}) walked out clean"
    assert any(p.evidence.probe == "perturbation" for p in report.proven)


def test_a_rebuilt_tape_keeps_the_shape_of_the_tape_not_of_each_bar():
    """Distributional invariants: as gappy, as late, as wicky as the pristine tape -- and
    on a continuous, regular tape, exactly continuous and regular."""
    import statistics as st
    tape = bars(300, gap_prob=0.2, late_prob=0.1)
    p = _perturbed(tape, 100, seed=5, sigma=None)
    gaps_t = [abs(tape[i].open / tape[i - 1].close - 1) for i in range(101, 300)]
    gaps_p = [abs(p[i].open / p[i - 1].close - 1) for i in range(101, 300)]
    assert abs(st.fmean(gaps_p) - st.fmean(gaps_t)) < 0.5 * st.fmean(gaps_t) + 1e-9
    steps_t = sorted({round(tape[i].ts - tape[i - 1].ts) for i in range(101, 300)})
    steps_p = sorted({round(p[i].ts - p[i - 1].ts) for i in range(101, 300)})
    assert set(steps_p) <= set(steps_t)
    assert [b.ts for b in p[:101]] == [b.ts for b in tape[:101]]
    assert all(b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in p)
    # per-bar copying is what let the leaks through: the wick ratio must NOT be preserved per bar
    same = sum(1 for i in range(100, 300)
               if abs(p[i].high / max(p[i].open, p[i].close) - tape[i].high / max(tape[i].open, tape[i].close)) < 1e-12)
    assert same < 20

    regular = bars(200)
    q = _perturbed(regular, 50, seed=3, sigma=None)
    assert all(abs(q[i].open - q[i - 1].close) <= 1e-9 * q[i - 1].close for i in range(1, 200))
    assert all(abs((q[i].ts - q[i - 1].ts) - 60.0) < 1e-6 for i in range(1, 200))


def test_a_count_dependent_strategy_is_convicted_without_a_fabricated_reach(tape):
    """Truncation cannot tell 'reads bar k+172' from 'looked at len(bars)'. Convict, but
    do not print a reach the evidence cannot support."""
    report = check_causality(strat("count_dependent").signals, tape, probes="every_bar", seed=1)
    assert report.leaks
    assert report.worst_horizon is None
    text = report.describe()
    assert "into the future" not in text
    assert "how much data there is" in text


def test_a_single_same_bar_wick_read_is_caught_at_a_probed_bar_every_time(tape):
    """A fourth red team's one-bar wick read was missed in one every_bar audit out of eight,
    because the boundary bar's wick came from a random donor and only the move was pushed
    both ways. The wick and the volume are now pushed both ways too."""
    AT = 137

    def one_wick(bs):
        out = [0] * len(bs)
        for i in range(3, len(bs)):
            out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
        if len(bs) > AT:
            b = bs[AT]
            top, bot = max(b.open, b.close), min(b.open, b.close)
            out[AT] = -1 if (b.high / top - 1.0) > (1.0 - b.low / bot) else 1
        return out

    irregular = bars(200, gap_prob=0.1, late_prob=0.1)
    for seed in range(5011, 5031):
        r = check_causality(one_wick, irregular, probes="every_bar", seed=seed)
        assert r.leaks and r.worst_horizon == 0, f"missed at seed {seed}"
        assert any(p.evidence.index == AT for p in r.proven)


def test_a_count_dependence_near_the_tail_is_convicted_in_every_bar(tape):
    """Truncation's fixed cuts stopped at 0.9n; a flip at index 185 keyed on len >= 190 was
    never compared. every_bar now truncates at every bar."""
    def tail_count(bs):
        out = [0] * len(bs)
        for i in range(3, len(bs)):
            out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
        if len(bs) > 185 and len(bs) >= 190:
            out[185] = -out[185]
        return out

    r = check_causality(tail_count, bars(200), probes="every_bar", seed=7)
    assert r.leaks
    assert r.worst_horizon is None                      # truncation-only: no reach claimed
    assert "how much data there is" in r.describe()


def _momentum(bs):
    out = [0] * len(bs)
    for i in range(3, len(bs)):
        out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
    return out


def test_a_read_of_move_and_wick_agreement_is_caught():
    """A fifth red team read whether the bar's move and its wick skew AGREE. Two lockstep
    draws (all up, then all down) never changed that. The draws beyond the first two now
    take a random sign triple from the nonce."""
    AT = 57

    def agree(bs):
        out = _momentum(bs)
        if len(bs) > AT:
            b = bs[AT]
            top, bot = max(b.open, b.close), min(b.open, b.close)
            skew = 1 if (b.high / top - 1.0) > (1.0 - b.low / bot) else -1
            move = 1 if b.close > b.open else -1
            out[AT] = skew * move
        return out

    tape = bars(200, gap_prob=0.1, late_prob=0.1)
    for seed in range(8):
        r = check_causality(agree, tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == AT for p in r.proven), f"missed at seed {seed}"


def test_a_wick_size_threshold_is_crossed_by_donor_magnitudes():
    """The fix that forced the wick's skew made its size a constant, so a threshold just
    below it was never crossed. The size is a donor's now."""
    AT, T = 52, 0.0015

    def maxwick(bs):
        out = _momentum(bs)
        if len(bs) > AT:
            b = bs[AT]
            top, bot = max(b.open, b.close), min(b.open, b.close)
            out[AT] = -1 if max(b.high / top - 1.0, 1.0 - b.low / bot) > T else 1
        return out

    tape = bars(200, gap_prob=0.1, late_prob=0.1)
    caught = sum(1 for seed in range(8) if check_causality(maxwick, tape, probes="every_bar", seed=seed).leaks)
    assert caught >= 6, f"caught in only {caught} of 8 audits"


def test_a_rare_flake_is_not_accused_of_telling_the_two_apart_without_hedging():
    """Flips one bar with p = 0.2 on any tape, inspecting nothing. Whatever the nonce, the
    report must never assert that it distinguishes real from varied data."""
    import random as _r

    def flaky(bs):
        out = _momentum(bs)
        if len(out) > 10 and _r.Random().random() < 0.2:
            out[10] = -out[10]
        return out

    tape = bars(200)
    for seed in range(6):
        r = check_causality(flaky, tape, probes="every_bar", seed=seed)
        assert r.proven == ()
        text = r.describe()
        assert "telling the two apart" not in text
        if r.recognises_input:
            assert "intermittently nondeterministic" in text


def test_mixed_mechanisms_are_both_named_and_truncation_lines_claim_no_reach(tape):
    def mixed(bs):
        out = _momentum(bs)
        if len(bs) > 100:
            out[100] = 1 if bs[100].close > bs[100].open else -1      # same-bar read
        if len(bs) >= 190 and len(bs) > 5:
            out[5] = -out[5]                                           # count dependence
        return out

    r = check_causality(mixed, tape, probes="every_bar", seed=1)
    assert r.leaks and r.worst_horizon == 0
    text = r.describe()
    assert "from its own bar on" in text and "how much data there is" in text
    assert "truncation probe, horizon" not in text and "reach not bounded" in text
    assert r.proven[0].evidence.probe == "perturbation"


def test_the_coverage_note_does_not_claim_bars_it_cannot_probe(tape):
    r = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=1)
    note = r.coverage_note()
    assert "from 4 on" in note and "bars 0-3 never are" in note
    assert "every pair of move, wick and volume pushed apart" in note and "4 of 8" in note
    assert "stopped at the first divergence" not in note, "nothing diverged, so nothing stopped"


def test_the_coverage_note_says_where_probing_stopped(tape):
    """A sixth red team: the note claimed four combinations at every bar, while the draw loop
    at a bar stops at its first divergence. It now says so, and only when that happened."""
    r = check_causality(strat("leak_same_bar_close").signals, tape, probes="every_bar", seed=1)
    assert r.leaks
    note = r.coverage_note()
    assert "that did not diverge" in note and "stopped at the first divergence" in note


def test_a_same_bar_proof_does_not_name_a_field_the_evidence_does_not_name(tape):
    """The same red team: a horizon-0 proof was described as "read the close" whatever was
    read. A volume-only reader is not accused of reading the close."""
    r = check_causality(strat("leak_same_bar_volume").signals, tape, probes="every_bar", seed=1)
    assert r.leaks and r.worst_horizon == 0
    text = r.describe()
    assert "read the close" not in text
    assert "close, high, low or volume" in text and "does not say which" in text


def _prev_bar_reader(field: str, at: int):
    """Decides bar ``at`` on one of its own unknown fields compared with the previous bar's:
    the close-to-close return, the volume change, a breakout above the previous high or
    below the previous low. The four most common one-bar reads in real code."""
    def signals(bs):
        out = _momentum(bs)
        if len(bs) > at:
            b, p = bs[at], bs[at - 1]
            if field == "close":
                out[at] = -1 if b.close > p.close else 1
            elif field == "volume":
                out[at] = -1 if b.volume > p.volume else 1
            elif field == "high":
                out[at] = -1 if b.high > p.high else 1
            else:
                out[at] = -1 if b.low < p.low else 1
        return out
    return signals


@pytest.mark.parametrize("field", ["close", "volume", "high", "low"])
def test_a_one_bar_read_against_the_previous_bar_is_caught_at_gap_bars(field):
    """A sixth red team read ``close > previous close`` at one bar and walked at any bar
    whose opening gap outweighed a typical move: the forced move was applied to the bar's
    own open, so the close never crossed the previous close. ``volume > previous volume``
    walked at a fifth of bars for the same reason (the push was relative to the bar's own
    pristine volume, and clamped), and the breakout reads at a fifth for the coin toss of
    whether a donor's wick reached the previous extreme. Every unknown field is now pushed
    to both sides of the previous bar's level. A breakout read survives only where the OPEN
    already sits beyond the previous extreme -- decided by a value the strategy may see --
    or where the level lies further than a move (and, for the high and low, a wick) of any
    size the tape has made; a seventh red team showed that forcing the close there anyway
    is a probe a strategy can recognise. Such bars must be disclosed in the report."""
    from edgecheck.causality import _sizes, beyond_reach
    gappy = bars(200, gap_prob=0.3, late_prob=0.1)
    sz = _sizes(gappy)
    moves, wicks = sz.moves, sz.wicks
    top_move, top_wick = moves[-1], wicks[-1]
    missed, out_of_reach = [], []
    for at in range(4, 200, 7):
        b, p = gappy[at], gappy[at - 1]
        o = b.open
        if field == "high" and o > p.high or field == "low" and o < p.low:
            continue
        far = {"close": abs(math.log(p.close / o)) >= top_move,
               "volume": False,
               "high": math.log(p.high / o) >= top_move + math.log1p(top_wick),
               "low": math.log(o / p.low) >= top_move - math.log1p(-top_wick)}[field]
        if far:
            out_of_reach.append(at)
            continue
        r = check_causality(_prev_bar_reader(field, at), gappy, probes="every_bar", seed=9001 + at)
        if not (r.leaks and any(q.evidence.index == at for q in r.proven)):
            missed.append(at)
    assert not missed, f"{field} read against the previous bar survived at bars {missed}"
    assert all(beyond_reach(gappy, at) for at in out_of_reach), "a bar left alone was not disclosed"
    assert len(out_of_reach) <= 3, f"too many bars out of reach to mean anything: {out_of_reach}"


def test_a_breakout_read_is_never_charged_where_the_open_decides_it():
    """The other side of the same fix: where the open is already past the previous high,
    ``high > previous high`` is a function of the open. No probe can move it, and the
    detector must not claim it did."""
    gappy = bars(200, gap_prob=0.3, late_prob=0.1)
    decided = [at for at in range(4, 200) if gappy[at].open > gappy[at - 1].high]
    assert decided, "the fixture should contain bars that gap above the previous high"
    at = decided[len(decided) // 2]
    r = check_causality(_prev_bar_reader("high", at), gappy, probes="every_bar", seed=77)
    assert not any(p.evidence.index == at for p in r.proven)


def test_the_boundary_bar_is_pushed_past_every_level_it_can_reach():
    """Direct measurement of the design, not of a strategy. Over the four draws an every-bar
    audit makes at a bar whose open is inside the previous range: the close lands on both
    sides of the open, and past the previous high and low wherever a move of the tape's own
    size reaches them; the volume on both sides of the previous bar's; the range makes a
    higher high and higher low, a lower high and lower low, and an INSIDE range; no range
    target lands exactly on the previous extreme; and no forced size is larger than the
    largest the tape has made."""
    from edgecheck.causality import _sizes, draw_plans
    gappy = bars(200, gap_prob=0.3, late_prob=0.1)
    sz = _sizes(gappy)
    moves, wicks = sz.moves, sz.wicks
    top_move, top_wick = moves[-1], wicks[-1]
    inside = [k for k in range(4, 200) if gappy[k - 1].low < gappy[k].open < gappy[k - 1].high]
    assert len(inside) > 40
    for k in inside[::4]:
        p, o = gappy[k - 1], gappy[k].open
        up, dn = math.log(p.high / o), math.log(o / p.low)
        for nonce in (1, 2, 3):
            got = [_perturbed(gappy, k, seed=nonce ^ (k * 1_000_003 + i), sigma=None, plan=plan)[k]
                   for i, plan in enumerate(draw_plans(nonce, k, 4))]
            assert any(b.close > o for b in got) and any(b.close < o for b in got), k
            assert any(b.volume > p.volume for b in got) and any(b.volume < p.volume for b in got), k
            if up < top_move:
                assert any(b.close > p.high for b in got), (k, "close past the previous high")
            if dn < top_move:
                assert any(b.close < p.low for b in got), (k, "close past the previous low")
            if up < top_move + math.log1p(top_wick):
                assert any(b.high > p.high and b.low > p.low for b in got), (k, "higher high, higher low")
            if dn < top_move - math.log1p(-top_wick):
                assert any(b.high < p.high and b.low < p.low for b in got), (k, "lower high, lower low")
            assert any(b.high < p.high and b.low > p.low for b in got), (k, "inside range")
            for b in got:
                assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
                assert b.open == gappy[k].open and b.ts == gappy[k].ts
                assert b.high != p.high and b.low != p.low, (k, "pinned exactly on the previous extreme")
                assert abs(math.log(b.close / b.open)) <= top_move, (k, "a move larger than the tape's")
                assert b.high / max(b.open, b.close) - 1.0 <= top_wick * (1 + 1e-9), (k, "a wick larger")
                assert 1.0 - b.low / min(b.open, b.close) <= top_wick * (1 + 1e-9), (k, "a wick larger")


def test_the_sign_design_covers_what_it_claims():
    """Every field both ways, every pair apart at least once, the triple parity both ways --
    for any mask -- and all eight combinations by the eighth draw."""
    from edgecheck.causality import sign_design
    for nonce in range(40):
        d = sign_design(nonce, 57)
        first4 = d[:4]
        for f in range(3):
            assert {c[f] for c in first4} == {1, -1}
        for i, j in ((0, 1), (0, 2), (1, 2)):
            assert any(c[i] != c[j] for c in first4)
        assert {c[0] * c[1] * c[2] for c in first4} == {1, -1}
        assert d[0] == tuple(-x for x in d[1])
        assert len(set(d)) == 8


def _gapped(seed: int, at: int = 100, by: float = 0.97):
    """A tape that gaps ``by`` at bar ``at`` and carries on from there: six times the largest
    move the walk otherwise makes."""
    t = bars(200, seed=seed, late_prob=0.1)
    return t[:at] + [dataclasses.replace(b, open=b.open * by, high=b.high * by, low=b.low * by,
                                         close=b.close * by) for b in t[at:]]


def _gap_evader(bs):
    """Reads its own close at any bar that gaps more than 1%, unless the bar's move is too
    big to be real -- then it follows the gap. A seventh red team's strategy, verbatim."""
    out = _momentum(bs)
    for i in range(1, len(bs)):
        b, p = bs[i], bs[i - 1]
        if abs(b.open / p.close - 1.0) > 0.01:
            move = b.close / b.open - 1.0
            out[i] = (1 if move > 0 else -1) if abs(move) < 0.015 else (1 if b.open > p.close else -1)
    return out


def test_a_probe_never_forces_a_move_larger_than_the_tape_has_made():
    """Round six forced the close past the previous high from wherever the open was, so at a
    bar that gapped 3% the probe's move was six times the tape's largest. A strategy that
    trusted only real-sized moves waited every probe out, and a clean report came back --
    where round five's module convicted it. Forced sizes now come from the tape's own, and a
    level beyond them is left alone and disclosed."""
    from edgecheck.causality import _sizes, draw_plans
    for tape_seed in (7, 1, 10):
        t = _gapped(tape_seed)
        for seed in (1, 42, 777):
            r = check_causality(_gap_evader, t, probes="every_bar", seed=seed)
            assert r.leaks and any(p.evidence.index == 100 for p in r.proven), (tape_seed, seed)
            assert 100 in r.beyond_reach
            assert "further from the open than any move the tape has made" in r.coverage_note()
        top = _sizes(t).moves[-1]
        for nonce in range(6):
            for i, plan in enumerate(draw_plans(nonce, 100, 4)):
                b = _perturbed(t, 100, seed=nonce ^ (100 * 1_000_003 + i), sigma=None, plan=plan)[100]
                assert abs(math.log(b.close / b.open)) <= top


@pytest.mark.parametrize("gap_prob", [0.3, 0.0])
@pytest.mark.parametrize("kind", ["inside", "outside", "failed_breakout", "band"])
def test_a_read_of_how_two_relations_combine_is_caught_wherever_it_is_made(kind, gap_prob):
    """A seventh red team: every draw was all-up or all-down, so a read of how two relations
    to the previous bar COMBINE -- an inside bar, an outside bar, a failed breakout, a close
    between the open and the previous high -- never changed at a bar whose pristine state
    was one of those two. The later draws now place the close in a random band and the range
    once inside and once outside the previous bar's. At a single bar such a read is tried
    only when a draw happens to produce the other state, and the report says so; made at
    every bar, as real code makes it, it is convicted in every audit."""
    def reader(bs):
        out = [0] * len(bs)
        for i in range(1, len(bs)):
            b, p = bs[i], bs[i - 1]
            v = {"inside": b.high < p.high and b.low > p.low,
                 "outside": b.high > p.high and b.low < p.low,
                 "failed_breakout": (b.high > p.high) != (b.close > p.close),
                 "band": b.open < b.close < p.high}[kind]
            out[i] = -1 if v else 1
        return out

    tape = bars(200, gap_prob=gap_prob, late_prob=0.1)
    for seed in range(4):
        r = check_causality(reader, tape, probes="every_bar", seed=seed)
        assert r.leaks and r.worst_horizon == 0, f"missed at seed {seed}"
    note = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=1).coverage_note()
    assert "a failed breakout" in note and "inside or outside range" in note
    assert "tried only on the draws that happen to produce it" in note


@pytest.mark.parametrize("field", ["low", "high"])
def test_a_breakout_read_with_or_equal_is_caught(field):
    """The range pin used to land EXACTLY on the previous extreme, so ``low <= previous low``
    held on the up draws as well as the down ones and was never flipped, in 20 audits of 20.
    Every range target is now met strictly."""
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    AT = 178
    assert tape[AT - 1].low < tape[AT].open < tape[AT - 1].high

    def reader(bs):
        out = _momentum(bs)
        if len(bs) > AT:
            b, p = bs[AT], bs[AT - 1]
            hit = b.low <= p.low if field == "low" else b.high >= p.high
            out[AT] = -1 if hit else 1
        return out

    for seed in range(10):
        r = check_causality(reader, tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == AT for p in r.proven), f"missed at seed {seed}"


def test_a_next_bar_reader_is_not_said_to_read_its_own_bar():
    """Horizon 0 is a lower bound: the boundary probe varies the boundary bar and every bar
    after it. A reader of only the NEXT bar's timestamp was headlined as reading its own
    close, high, low or volume."""
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    r = check_causality(strat("leak_next_bar_ts").signals, tape, probes="every_bar", seed=1)
    assert r.leaks and r.worst_horizon == 0
    head = r.describe().splitlines()[0]
    assert "or a later bar" in head and "does not say which" in head
    assert "reads its own bar:" not in head


def test_the_note_does_not_claim_pushes_that_were_never_made(tape):
    """At draws=0 no perturbation runs at all, and at draws=1 each bar is pushed one way
    only; both used to get the two-draw text claiming pushes both ways."""
    def close_at(bs):
        out = _momentum(bs)
        if len(bs) > 57:
            out[57] = 1 if bs[57].close > bs[57].open else -1
        return out

    none = check_causality(close_at, tape, probes="every_bar", draws=0, seed=0)
    assert not none.leaks and none.coverage == 0.0
    assert none.coverage_note().startswith("no perturbation ran")
    assert "both ways" not in none.coverage_note() and "no perturbation ran" in none.describe()
    one = check_causality(close_at, tape, probes="every_bar", draws=1, seed=0)
    note = one.coverage_note()
    assert "one draw at each bar" in note and "only the other way would flip" in note
    assert "both ways" not in note


def test_a_rebuilt_tape_repeats_none_of_the_values_it_was_built_from():
    """Donor wick sizes, gaps and volume ratios were copied verbatim, so a rebuilt bar carried
    a value that already existed elsewhere on the tape -- a duplicate no real tape prints. They
    are jittered off their exact values; zero stays zero, so a gapless tape stays gapless."""
    from edgecheck.causality import draw_plans
    tape = bars(300, gap_prob=0.3, late_prob=0.1)

    def shape(b, before):
        top, bot = max(b.open, b.close), min(b.open, b.close)
        return {round(b.high / top - 1.0, 12), round(1.0 - b.low / bot, 12),
                round(b.open / before.close - 1.0, 12), round(math.log(b.volume / before.volume), 12)}

    k = 120
    seen = set().union(*(shape(tape[i], tape[i - 1]) for i in range(1, k))) - {0.0}
    for i, plan in enumerate(draw_plans(5, k, 4)):
        p = _perturbed(tape, k, seed=5 ^ (k * 1_000_003 + i), sigma=None, plan=plan)
        made = set().union(*(shape(p[j], p[j - 1]) for j in range(k, 300))) - {0.0}
        assert not (made & seen), f"draw {i} repeated {sorted(made & seen)[:3]}"


def _zero_volume_tape(gap_prob: float, n: int = 200, seed: int = 7):
    """A tape with illiquid stretches: runs of bars that did not trade. An eighth red team's."""
    import random
    t = bars(n, gap_prob=gap_prob, late_prob=0.1, seed=seed)
    r, out, zero = random.Random(seed + 1), [], False
    for i, b in enumerate(t):
        if r.random() < 0.08:
            zero = True
        elif zero and r.random() < 0.4:
            zero = False
        out.append(dataclasses.replace(b, volume=0.0) if zero and i else b)
    return out


def _trades_after_a_quiet_bar(bs):
    """After an untraded bar, takes a position only if THIS bar trades -- its own volume."""
    out = [0, 0]
    for i in range(2, len(bs)):
        if bs[i - 1].volume == 0:
            out.append(1 if bs[i].volume > 0 else 0)
        else:
            out.append(1 if bs[i - 1].close > bs[i - 1].open else -1)
    return out


@pytest.mark.parametrize("gap_prob", [0.3, 0.0])
def test_whether_a_bar_traded_is_varied(gap_prob):
    """The volume push was a multiple of the previous bar's volume, so after an untraded bar
    every draw was zero and "does this bar trade" never moved -- at every seed -- while the note
    claimed the volume was pushed both ways. Zero is now a level like any other: pushed to
    and away from, where the tape prints zeros."""
    tape = _zero_volume_tape(gap_prob)
    assert sum(1 for i in range(1, len(tape)) if tape[i - 1].volume == 0) > 10
    for seed in (1, 2, 3):
        r = check_causality(_trades_after_a_quiet_bar, tape, probes="every_bar", seed=seed)
        assert r.leaks and r.worst_horizon == 0, f"missed at seed {seed}"
    clean = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=1)
    assert not clean.leaks and clean.undelivered == ()
    assert "to zero and away from it" in clean.coverage_note()


def test_the_planned_wick_skew_is_delivered_even_against_a_range_target():
    """Round seven's range targets won over the planned wick skew, so at a bar whose open sat
    just above the previous low the skew was never pushed the other way, and a one-bar skew
    read walked in 49 audits of 200. What each built bar delivered is now checked against what
    the note claims, and a repair draw makes up any shortfall."""
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    AT, ts = 32, tape[32].ts

    def skew(bs):
        out = []
        for b in bs:
            top, bot = max(b.open, b.close), min(b.open, b.close)
            out.append((1 if b.high / top - 1.0 > 1.0 - b.low / bot else -1) if b.ts == ts else 0)
        return out

    for seed in (6, 21, 22, *range(40)):
        r = check_causality(skew, tape, boundaries=[AT], draws=4, seed=seed)
        assert r.leaks, f"missed at seed {seed}"


@pytest.mark.parametrize("gap_prob", [0.3, 0.0])
def test_eight_draws_deliver_every_sign_combination(gap_prob):
    """At draws=8 the note claims every sign combination of move, wick and volume; range
    targets used to override the skew, and four of the eight were never produced at some
    seeds. Each is now checked on the bar as built and repaired if missing."""
    import itertools
    tape = bars(200, gap_prob=gap_prob, late_prob=0.1)
    AT = 120

    def signs(b, p):
        top, bot = max(b.open, b.close), min(b.open, b.close)
        return (1 if b.close > b.open else -1, 1 if b.high / top - 1.0 > 1.0 - b.low / bot else -1,
                1 if b.volume > p.volume else -1)

    pristine = signs(tape[AT], tape[AT - 1])
    for target in itertools.product((1, -1), repeat=3):
        if target == pristine:
            continue

        def reader(bs, target=target):
            out = [0] * len(bs)
            if len(bs) > AT:
                out[AT] = 1 if signs(bs[AT], bs[AT - 1]) == target else 0
            return out

        for seed in range(5):
            assert check_causality(reader, tape, boundaries=[AT], draws=8, seed=seed).leaks, (target, seed)


@pytest.mark.parametrize("gap_prob", [0.3, 0.0])
def test_every_claimed_push_is_delivered_on_the_fixture_tapes(gap_prob):
    """The note's claims are checked facts: on the fixture tapes nothing the note lists goes
    undelivered, and the repair draws that make that so are a small share of the audit."""
    tape = bars(200, gap_prob=gap_prob, late_prob=0.1)
    for draws in (2, 4, 8):
        r = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", draws=draws, seed=3)
        assert not r.leaks and r.undelivered == (), (draws, r.undelivered)
        assert r.repairs < 0.1 * r.probes_run, (draws, r.repairs, r.probes_run)
        assert "checked on the bar as built" in r.coverage_note()


def test_an_audit_that_probed_no_bar_says_so(tape):
    """Explicit boundaries=[] used to print the full list of pushes over a single run."""
    r = check_causality(strat("clean_lagged").signals, tape, boundaries=[], draws=4, seed=1)
    note = r.coverage_note()
    assert r.coverage == 0.0 and note.startswith("no perturbation ran")
    assert "truncation did not run either" in note and "both ways" not in note
