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


@pytest.mark.parametrize("sigma", [0.002, 0.05, 0.5])      # 0.5: the widest sigma taken (SIGMA_MAX)
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
        if calls[0] > 2 and len(out) > 10:    # the "clock" moves after the first two runs
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
            assert "at least as far from the open as the largest move the tape has made" in r.coverage_note()
            assert "the close was not pushed past that level" in r.coverage_note()
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
    undelivered, and the repair draws that make that so are a modest share of the audit. At
    two draws the share is larger by construction: a gappy tape's next bar can gap up, gap
    down or not gap, and two draws cannot hold three states, so one is repaired at nearly
    every bar. Since round twelve an ungapped next open is a tie at a bar whose real next bar
    gapped, and gets a draw of its own there -- about one repair at three bars in ten on the
    gappy tape, which is what moved its four-draw share from 7% to 12%. Since round twenty-one the
    next open is owed past the bar's own high and low as well, which the planned draws reach only
    where a gap happens to clear a wick: that moved it to 17% (37% at two draws, 11% at eight)."""
    tape = bars(200, gap_prob=gap_prob, late_prob=0.1)
    for draws, share in ((2, 0.40), (4, 0.20), (8, 0.13)):
        r = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", draws=draws, seed=3)
        assert not r.leaks and r.undelivered == (), (draws, r.undelivered)
        assert r.repairs < share * r.probes_run, (draws, r.repairs, r.probes_run)
        assert "checked on the bar as built" in r.coverage_note()


def test_an_audit_that_probed_no_bar_says_so(tape):
    """Explicit boundaries=[] used to print the full list of pushes over a single run."""
    r = check_causality(strat("clean_lagged").signals, tape, boundaries=[], draws=4, seed=1)
    note = r.coverage_note()
    assert r.coverage == 0.0 and note.startswith("no perturbation ran")
    assert "truncation did not run either" in note and "both ways" not in note


def test_a_proof_line_does_not_claim_the_variation_stayed_in_the_tapes_range(tape):
    """"varied within the tape's own range" was on every perturbation line, and most varied
    tapes leave the tape's price range: moves are drawn at the tape's scale, not inside its
    range. A ninth red team read it as the claim it is."""
    r = check_causality(strat("leak_same_bar_close").signals, tape, probes="every_bar", seed=1)
    text = r.describe()
    assert "within the tape's own range" not in text and "at the tape's own scale" in text


def test_a_tape_with_no_sizes_of_a_kind_says_its_pushes_used_a_floor():
    """On a flat tape no move, no wick and no volume change exists to draw from; the pushes
    fall back to a floor, and the note used to say nothing about it."""
    flat = [dataclasses.replace(b, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000.0)
            for b in bars(60)]

    def reads_its_close(bs):
        return [1 if b.close > b.open else -1 if b.close < b.open else 0 for b in bs]

    r = check_causality(reads_its_close, flat, probes="every_bar", seed=1)
    assert r.leaks, "the floor-sized push still has to catch a same-bar read"
    clean = check_causality(strat("clean_lagged").signals, flat, probes="every_bar", seed=1)
    assert set(clean.floors) == {"moves", "volume changes"}
    note = clean.coverage_note()
    assert "so the close was pushed by a floor size, not by a move of its own" in note
    assert "so the volume was pushed by a floor ratio" in note
    real = check_causality(strat("clean_lagged").signals, bars(200), probes="every_bar", seed=1)
    assert real.floors == () and "floor size" not in real.coverage_note()


def test_a_zero_volume_push_comes_from_the_planned_draws(tape):
    """Zero volume at the probed bar used to come only from repair draws, which made a repair
    draw recognisable in principle. On a tape that prints zeros the planned draws deliver it."""
    from edgecheck.causality import draw_plans
    plans = draw_plans(5, 60, 4, zero=True)
    assert sum(p.zero for p in plans) == 1 and all(p.volume < 0 for p in plans if p.zero)
    assert not any(p.zero for p in draw_plans(5, 60, 4))
    zt = _zero_volume_tape(0.3)
    with_zero = check_causality(strat("clean_lagged").signals, zt, probes="every_bar", seed=2)
    assert with_zero.undelivered == ()
    # the same ceiling the gappy fixture tape is held to at four draws; the owed set grew in rounds
    # ten to twelve and twenty-one, and what this test guards is that zero comes from a planned draw
    assert with_zero.repairs < 0.20 * with_zero.probes_run


def test_the_one_draw_note_mentions_its_repairs_and_runs_are_named_as_runs(tape):
    one = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", draws=1, seed=0)
    assert "plus a repair draw where that draw fell short of its own plan" in one.coverage_note()
    none = check_causality(strat("clean_lagged").signals, tape, boundaries=[], seed=0)
    assert "runs of the strategy" in none.describe() and "probe runs" not in none.describe()


def _tick_tape(tick: float = 0.01):
    """bars() rounded to a tick, as an exchange prints them: ties, zero wicks, ungapped opens."""
    r = lambda x: round(round(x / tick) * tick, 10)
    out = []
    for b in bars(200, seed=7, gap_prob=0.3, late_prob=0.1):
        o, c = r(b.open), r(b.close)
        out.append(dataclasses.replace(b, open=o, close=c, high=max(r(b.high), o, c),
                                       low=min(r(b.low), o, c), volume=float(round(b.volume))))
    return out


def _one_bar(at: int, read):
    """A causal lagged signal everywhere, and ``read(bars)`` at bar ``at`` alone."""
    def signals(bs):
        out = [0] + [1 if bs[i - 1].close > bs[i - 1].open else -1 for i in range(1, len(bs))]
        if len(bs) > at + 1:
            out[at] = read(bs)
        return out
    return signals


@pytest.mark.parametrize("what", ["gap", "late"])
def test_a_read_of_the_next_bars_open_or_time_at_one_bar_is_caught(what):
    """A tenth red team read, at one bar, whether the NEXT bar gaps up or opens late. At the
    boundary those came only from a random donor bar, so the read flipped about half the time
    -- the same fields a third red team read at every bar. The next bar's gap is now pushed
    both ways (and to none, where the tape prints ungapped bars) and its time both on time
    and late, and checked like everything else."""
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    read = ((lambda bs: 1 if bs[11].open > bs[10].close else -1) if what == "gap"
            else (lambda bs: 0 if bs[10].ts - bs[9].ts > 60 else 1))
    at = 10 if what == "gap" else 9
    for seed in range(1, 11):
        r = check_causality(_one_bar(at, read), tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == at for p in r.proven), f"missed at seed {seed}"


def test_a_gap_fill_read_at_one_bar_is_caught():
    """The close was pushed past every previous-bar level, the high and the low only past the
    previous high and low; a tenth red team read "did the low fill the gap to the previous
    close" and walked. Every unknown of the bar is now pushed past every previous level."""
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    for at in (36, 38):
        read = lambda bs, at=at: 1 if bs[at].low < bs[at - 1].close else -1
        for seed in range(1, 21):
            r = check_causality(_one_bar(at, read), tape, probes="every_bar", seed=seed)
            assert r.leaks and any(p.evidence.index == at for p in r.proven), (at, seed)


def test_a_breakout_read_at_a_bar_that_opened_on_the_extreme_is_caught_by_a_tie():
    """On a tick tape a bar can open exactly at the previous high. Then "high below the
    previous high" is ruled out by the open, and the only other state is a tie -- which was
    never pushed, so the breakout read walked in 18 audits of 20."""
    tape = _tick_tape()
    at = 68
    assert tape[at].open == tape[at - 1].high
    read = lambda bs: 1 if bs[at].high > bs[at - 1].high else -1
    for seed in range(1, 11):
        r = check_causality(_one_bar(at, read), tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == at for p in r.proven), f"missed at seed {seed}"


def test_every_relation_to_the_previous_bar_is_owed_where_it_can_be_made():
    """Enumerated, not collected: for every unknown of the bar and every level of the previous
    bar, both sides are owed wherever the open leaves them open and the tape's sizes reach,
    and checked delivered on the fixture tapes."""
    from edgecheck.causality import LEVELS, _owed, _sizes, draw_plans, realized_sigma
    tape = bars(200, gap_prob=0.3, late_prob=0.1)
    sz, sg = _sizes(tape), realized_sigma(tape)
    fields_seen = set()
    for k in range(4, 200, 3):
        owed = _owed(tape, k, sz, draw_plans(1, k, 4), sg)
        fields_seen |= {(x[1], x[2]) for x in owed if x[0] == "rel"}
        nxt = {("next_gap", 1), ("next_gap", -1), ("next_gap", 0), ("next_step", 0), ("next_step", 1)}
        if k + 1 < len(tape):
            assert nxt <= owed
        else:
            assert not (nxt & owed), "the last bar has no next bar to push"
    assert fields_seen == {(f, n) for f in ("close", "high", "low") for n in LEVELS} | {("high", "own"), ("low", "own")}
    for seed in (1, 2):
        r = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=seed)
        assert not r.leaks and r.undelivered == ()


def test_the_reach_sentences_are_true_on_a_tape_with_no_moves():
    """On a doji tape every close equals its open, so the close is pushed by the floor; the
    beyond-reach sentence named "the largest move the tape has made", which it never made,
    and the inside range was owed where a floor-sized move could not stay inside."""
    doji = [dataclasses.replace(b, close=b.open, high=max(b.high, b.open), low=min(b.low, b.open))
            for b in bars(200, gap_prob=0.3, late_prob=0.1)]
    r = check_causality(strat("clean_lagged").signals, doji, probes="every_bar", seed=1)
    note = r.coverage_note()
    assert not r.leaks and r.undelivered == () and "moves" in r.floors
    assert "the floor-sized move the close was given" in note
    assert "the largest move the tape has made" not in note


def test_a_level_a_hair_short_of_the_largest_move_is_counted_and_never_crossed():
    """Reach used one threshold in the note and another in the builder, so a level 5e-10 short
    of the largest move was counted as not pushed past while the close was pushed past it."""
    from edgecheck.causality import _sizes, draw_plans
    tape = bars(200, gap_prob=0.0)
    top = _sizes(tape).moves[-1]
    o = tape[100].open
    tape[99] = dataclasses.replace(tape[99], high=o * math.exp(top * (1 - 5e-10)))
    for seed in (1, 2, 3):
        r = check_causality(strat("clean_lagged").signals, tape, boundaries=[100], draws=4, seed=seed)
        assert 100 in r.beyond_reach
        for i, plan in enumerate(draw_plans(seed, 100, 4)):
            b = _perturbed(tape, 100, seed=seed ^ (100 * 1_000_003 + i), sigma=None, plan=plan)[100]
            assert not b.close > tape[99].high


def test_a_proof_line_names_the_scale_it_actually_used(tape):
    flat = [dataclasses.replace(b, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000.0)
            for b in bars(40)]
    read = lambda bs: [1 if b.close > b.open else -1 if b.close < b.open else 0 for b in bs]
    floor = check_causality(read, flat, probes="every_bar", seed=1).describe()
    assert "with a floor size where the tape has made none" in floor and "tape's own scale" not in floor
    wide = check_causality(strat("leak_same_bar_close").signals, tape, probes="every_bar", sigma=0.5, seed=1)
    assert "with moves of sigma 0.5" in wide.describe() and "tape's own scale" not in wide.describe()
    own = check_causality(strat("leak_same_bar_close").signals, tape, probes="every_bar", seed=1)
    assert "at the tape's own scale" in own.describe()


def test_a_truncation_proof_without_perturbation_says_none_ran():
    r = check_causality(strat("count_dependent").signals, bars(200), draws=0, seed=1)
    head = r.describe().splitlines()[0]
    assert r.leaks and "No perturbation ran to corroborate it" in head and "did not corroborate" not in head


def test_a_small_coverage_is_not_printed_as_zero():
    r = check_causality(strat("clean_lagged").signals, bars(1004), boundaries=[500], draws=2, seed=1)
    assert "(0.1%)" in r.coverage_note() and "(0%)" not in r.coverage_note()


def _lot_tape():
    """bars() with volumes printed in lots of 100, as many venues print them: volumes repeat."""
    return [dataclasses.replace(b, volume=float(round(b.volume / 100) * 100))
            for b in bars(200, seed=7, gap_prob=0.3, late_prob=0.1)]


@pytest.mark.parametrize("what", ["unchanged_close", "doji", "double_top", "same_volume"])
def test_a_read_of_a_tie_is_caught_where_the_tape_prints_ties(what):
    """An eleventh red team: ties were never owed and never made, so on a tick tape "close
    unchanged", "doji", "high equal to the previous high" and on a lot tape "volume unchanged"
    walked at every seed. Ties of each kind the tape prints are now owed and set exactly."""
    tape = _lot_tape() if what == "same_volume" else _tick_tape()
    reads = {
        "unchanged_close": (lambda b, p: b.close != p.close, lambda b, p: b.close != p.close and b.open != p.close),
        "doji": (lambda b, p: b.close != b.open, lambda b, p: b.close != b.open),
        "double_top": (lambda b, p: b.high != p.high, lambda b, p: b.high != p.high and b.open < p.high),
        "same_volume": (lambda b, p: b.volume != p.volume, lambda b, p: b.volume != p.volume),
    }
    read, pick = reads[what]
    at = next(k for k in range(12, 190) if pick(tape[k], tape[k - 1]))
    strategy = _one_bar(at, lambda bs: 1 if read(bs[at], bs[at - 1]) else -1)
    for seed in range(1, 9):
        r = check_causality(strategy, tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == at for p in r.proven), f"{what} missed at seed {seed}"


def test_on_time_is_the_tapes_commonest_step_not_its_shortest():
    """On a 60s clock with some 30s and some 120s bars, "on time" was the SHORTEST step, so a
    regular bar was counted late and the late read walked half the time. The clock has three
    states now: early, on time (the commonest step) and late."""
    import random
    base, rng = bars(200, seed=3, gap_prob=0.3), random.Random(5)
    ts, tape = base[0].ts, [base[0]]
    for b in base[1:]:
        u = rng.random()
        ts += 30.0 if u < 0.08 else 120.0 if u < 0.18 else 60.0
        tape.append(dataclasses.replace(b, ts=ts))
    at = next(k for k in range(10, 190) if tape[k + 1].ts - tape[k].ts == 60.0)
    strategy = _one_bar(at, lambda bs: 1 if bs[at + 1].ts - bs[at].ts > 60 else -1)
    for seed in range(1, 9):
        r = check_causality(strategy, tape, probes="every_bar", seed=seed)
        assert r.leaks and any(p.evidence.index == at for p in r.proven), f"missed at seed {seed}"
    assert "early, on time and late" in r.coverage_note()


def test_the_next_open_against_the_previous_bar_is_caught():
    """The next bar's open was owed only against this bar's close. Against the previous bar's
    high, a one-bar read walked in three to four audits of ten."""
    tape = bars(200, seed=1, gap_prob=0.3, late_prob=0.1)
    for at in (10, 17, 18, 19):
        strategy = _one_bar(at, lambda bs, at=at: 1 if bs[at + 1].open > bs[at - 1].high else -1)
        for seed in range(1, 11):
            r = check_causality(strategy, tape, probes="every_bar", seed=seed)
            assert r.leaks and any(p.evidence.index == at for p in r.proven), (at, seed)


def test_a_probed_bar_on_a_grid_tape_stays_on_the_grid():
    """Rebuilt prices off the tape's tick were a tell: an evader that answered as on real data
    whenever a price left the grid walked at every seed. The tape is rebuilt on its own tick
    and lot, and the evader is caught."""
    from edgecheck.causality import _sizes, draw_plans
    tape = _tick_tape()
    sz = _sizes(tape)
    assert sz.tick == 0.01 and sz.lot == 1.0
    on = lambda x: abs(x * 100 - round(x * 100)) < 1e-6
    for k in (30, 90, 150):
        for i, plan in enumerate(draw_plans(3, k, 4, zero=sz.zero)):
            v = _perturbed(tape, k, seed=3 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            assert all(on(x) for b in v for x in (b.open, b.high, b.low, b.close)), (k, i)
            assert all(float(b.volume).is_integer() for b in v), (k, i)
    at = next(k for k in range(15, 190) if tape[k].close > tape[k].open and tape[k - 1].close > tape[k - 1].open)

    def evader(bs):
        b = bs[at]
        if not all(on(x) for x in (b.close, b.high, b.low)):
            return 1
        return 1 if b.close > b.open else -1

    strategy = _one_bar(at, evader)
    for seed in range(1, 9):
        assert check_causality(strategy, tape, probes="every_bar", seed=seed).leaks, f"missed at seed {seed}"


def test_a_tape_with_a_zero_price_does_not_crash_the_auditor():
    """Round ten's builder divided by the previous low and by the open; a tape with a zero
    price crashed check_causality with ZeroDivisionError -- no report at all."""
    tape = bars(60, seed=7, gap_prob=0.3)
    reads_close = lambda bs: [1 if b.close > b.open else -1 for b in bs]
    zero_low = list(tape)
    zero_low[30] = dataclasses.replace(tape[30], low=0.0)
    assert check_causality(reads_close, zero_low, probes="every_bar", seed=1).leaks
    zero_open = list(tape)
    zero_open[30] = dataclasses.replace(tape[30], open=0.0, low=0.0)
    assert check_causality(reads_close, zero_open, probes="every_bar", seed=1).leaks
    zeros = [dataclasses.replace(b, open=0.0, high=0.0, low=0.0, close=0.0) for b in tape]
    check_causality(reads_close, zeros, probes="every_bar", seed=1)


def test_the_wick_skew_is_delivered_in_price_units_too():
    """The skew was checked as a fraction of each body end; a strategy reading it in price
    units saw it unflipped at about one bar-audit in two thousand, with the note claiming it
    both ways. It counts only where it holds both ways of measuring."""
    tape = bars(200, seed=3, gap_prob=0.3, late_prob=0.1)

    def skew(bs):
        out = [0] * len(bs)
        for i in range(len(bs)):
            b = bs[i]
            top, bot = max(b.open, b.close), min(b.open, b.close)
            out[i] = 1 if (b.high - top) > (bot - b.low) else -1
        return out

    for seed in (1, 3):
        r = check_causality(skew, tape, probes="every_bar", seed=seed)
        caught = {p.evidence.index for p in r.proven}
        assert set(range(4, 200)) - caught - set(r.undelivered) == set(), seed


def test_a_quiet_tape_is_varied_at_its_own_scale_and_says_so():
    """A tape quieter than the old floor was rebuilt at the floor, seven times its largest
    move, under proof lines saying "at the tape's own scale", and unforced closes crossed
    levels the note said were out of reach. The scale is the tape's own unless it has none."""
    from edgecheck.causality import _sizes, realized_sigma
    quiet = bars(120, seed=7, vol=0.0001)
    assert realized_sigma(quiet) < 0.001 and realized_sigma(quiet) < _sizes(quiet).moves[-1] * 2
    r = check_causality(strat("leak_same_bar_close").signals, quiet, probes="every_bar", draws=2, seed=1)
    assert r.leaks and "at the tape's own scale" in r.describe()


def test_a_given_sigma_on_a_tape_with_no_moves_is_named():
    flat = [dataclasses.replace(b, close=b.open, high=b.open * 1.001, low=b.open * 0.999)
            for b in bars(80, seed=7, gap_prob=0.3)]
    r = check_causality(lambda bs: [0] * len(bs), flat, probes="every_bar", seed=1, sigma=0.05)
    note = r.coverage_note()
    assert "pushed by the given sigma 0.05" in note and "a floor size" not in note
    assert r.undelivered == ()


# -- round twelve ---------------------------------------------------------------------------

def _any_tie(b, p) -> bool:
    return b.close == b.open or any(getattr(b, f) in (p.open, p.close, p.high, p.low) for f in ("close", "high", "low"))


def test_a_probe_adds_no_tie_the_real_bar_did_not_print():
    """A twelfth red team: snapping to the tick and merged repairs put ties on probed bars at
    three times the rate real bars carry them, and an evader that answered as on real data
    whenever the probed bar showed a tie -- reading its own close otherwise -- walked at six
    seeds of ten. A draw now counts only where it carries no tie the real bar did not print."""
    tape = _tick_tape()
    at = 10
    assert not _any_tie(tape[at], tape[at - 1])

    def evader(bs):
        out = [0] + [1 if bs[i - 1].close > bs[i - 1].open else -1 for i in range(1, len(bs))]
        if len(bs) > at and not _any_tie(bs[at], bs[at - 1]):
            out[at] = 1 if bs[at].close > bs[at].open else -1
        return out

    for seed in range(1, 11):
        r = check_causality(evader, tape, boundaries=[at], draws=4, seed=seed)
        assert any(p.evidence.index == at for p in r.proven), f"missed at seed {seed}"


def test_a_repair_draw_sets_one_tie_at_a_time():
    """The same red team's evader hid behind merged repairs carrying a doji, a high tie and a low
    tie at once. A draw that sets a tie now merges with nothing."""
    from edgecheck.causality import Plan, _merge
    ties = [Plan(close_at="own"), Plan(vol_eq=True), Plan(move=1, hi_vs=(("high", 0),)), Plan(next_gap=0),
            Plan(next_past=("high", 0))]
    strict = [Plan(move=1, band="far"), Plan(volume=-1), Plan(next_step=1)]
    for a in ties:
        for b in ties + strict:
            assert _merge(a, b) is None and _merge(b, a) is None
    assert _merge(strict[0], strict[1]) is not None


def test_the_high_or_low_on_the_bars_own_open_is_owed_and_caught():
    """``low == open`` at a bar whose low was below its open walked at every seed: the high or
    low level with the bar's own open was never owed. It is, where the tape prints it."""
    tape = _tick_tape()
    levels = lambda p: (p.open, p.close, p.high, p.low)
    picks = [k for k in range(10, 190) if tape[k].low != tape[k].open and tape[k].open not in levels(tape[k - 1])][:3]
    assert picks
    for at in picks:
        s = _one_bar(at, lambda bs, at=at: 1 if bs[at].low == bs[at].open else -1)
        for seed in range(1, 7):
            r = check_causality(s, tape, boundaries=[at], draws=4, seed=seed)
            assert any(p.evidence.index == at for p in r.proven), (at, seed)


def _gappy_tick_tape(n: int = 200, seed: int = 11, tick: float = 0.05):
    """A tick tape on which every bar gaps by a tick or two, and next opens land on the previous
    bar's levels by themselves. A twelfth red team's."""
    import random
    from edgecheck.fixtures import Bar
    rng, out, ts, c = random.Random(seed), [], 1.7e9, 100.0
    for i in range(n):
        o = round(c + tick * rng.choice((1, 2, -1, -2)), 2) if i else 100.0
        c = round(o + tick * rng.choice((-3, -2, -1, 1, 2, 3)), 2)
        h = round(max(o, c) + tick * rng.choice((0, 1, 2)), 2)
        lo = round(min(o, c) - tick * rng.choice((0, 1, 2)), 2)
        out.append(Bar(ts, o, h, lo, c, float(rng.randint(5, 20) * 100)))
        ts += 60
    return out


def test_the_next_open_on_a_previous_level_by_a_gap_is_caught():
    """The next open level with a previous-bar level was owed only through an ungapped bar, so on
    a tape that always gaps ``next open == previous high`` walked. It is now set exactly, by a gap
    of the tape's own size from a close near the level."""
    from edgecheck.causality import _sizes
    tape = _gappy_tick_tape()
    sz = _sizes(tape)
    assert not sz.gap0
    tried = 0
    for at in range(10, 60):
        b, p, nb = tape[at], tape[at - 1], tape[at + 1]
        g = math.log(p.high / b.close)
        if nb.open == p.high or not ((g > 0 and g <= sz.gaps_up[-1]) or (g < 0 and -g <= sz.gaps_dn[-1])):
            continue
        s = _one_bar(at, lambda bs, at=at: 1 if bs[at + 1].open == bs[at - 1].high else -1)
        for seed in range(1, 6):
            r = check_causality(s, tape, boundaries=[at], draws=4, seed=seed)
            assert any(q.evidence.index == at for q in r.proven), (at, seed)
        tried += 1
        if tried == 3:
            break
    assert tried == 3 and "next" in sz.ties


def _cents_times_float():
    """Whole ticks times 0.01, as a store of integer ticks converts them: 100.19000000000001."""
    ticks = lambda x: round(x / 0.01)
    out = []
    for b in bars(200, seed=7, gap_prob=0.3, late_prob=0.1):
        o, c = ticks(b.open), ticks(b.close)
        h, lo = max(ticks(b.high), o, c), min(ticks(b.low), o, c)
        out.append(dataclasses.replace(b, open=o * 0.01, high=h * 0.01, low=lo * 0.01, close=c * 0.01,
                                       volume=float(round(b.volume))))
    return out


def test_a_grid_is_found_under_float_noise_and_off_zero():
    """A tape stored as whole ticks times 0.01 was taken for no grid at all, and a mid-price tape
    on x.xx5 was snapped to whole cents; on both, an evader that answered as on real data
    whenever a price left the tape's lattice walked at every seed."""
    from edgecheck.causality import _sizes, draw_plans
    noisy = _cents_times_float()
    mid = [dataclasses.replace(b, open=round(b.open + 0.005, 3), high=round(b.high + 0.005, 3),
                               low=round(b.low + 0.005, 3), close=round(b.close + 0.005, 3)) for b in _tick_tape()]
    for tape, off in ((noisy, 0.0), (mid, 0.005)):
        sz = _sizes(tape)
        assert sz.price_grid is not None and abs(sz.price_grid.step - 0.01) < 1e-12
        assert abs(sz.price_grid.off - off) < 1e-9
        lat = lambda x: abs((x - off) * 100 - round((x - off) * 100)) < 1e-6
        for k in (40, 120):
            for i, plan in enumerate(draw_plans(5, k, 4, zero=sz.zero)):
                v = _perturbed(tape, k, seed=5 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
                assert all(lat(x) for b in v[k:] for x in (b.open, b.high, b.low, b.close)), (off, k, i)
        at = next(k for k in range(15, 190) if tape[k].close > tape[k].open and tape[k - 1].close > tape[k - 1].open)

        def evader(bs, at=at, lat=lat):
            b, nb = bs[at], bs[at + 1]
            if not all(lat(x) for x in (b.close, b.high, b.low, nb.open, nb.close)):
                return 1
            return 1 if b.close > b.open else -1

        for seed in range(1, 5):
            r = check_causality(_one_bar(at, evader), tape, boundaries=[at], draws=4, seed=seed)
            assert any(q.evidence.index == at for q in r.proven), (off, seed)


def _penny_tape(price: float = 0.03, tick: float = 0.01, n: int = 200, seed: int = 7, vol: float = 0.3):
    import random
    from edgecheck.fixtures import Bar
    rng, out, ts, p = random.Random(seed), [], 1.7e9, price
    for _ in range(n):
        o = p
        c = max(tick, round(round(o * math.exp(rng.gauss(0, vol)) / tick) * tick, 10))
        top, bot = max(o, c), min(o, c)
        h = round(top + (tick if top >= 0.03 and rng.random() < 0.5 else 0), 10)
        lo = round(bot - (tick if bot >= 0.03 and rng.random() < 0.5 else 0), 10)
        out.append(Bar(ts, o, h, lo, c, float(rng.randint(1, 9))))
        ts += 60
        p = c
    return out


def test_honest_tapes_that_used_to_crash_the_auditor_get_a_report():
    """A penny stock on a one-cent tick snapped a low to zero; a single bad print with a wick past
    the body end made a lower wick of more than 100%; volumes in whole contracts snapped to zero
    on a tape that never prints one. Each crashed the auditor on an honest strategy (a twelfth red
    team). None of them may."""
    from edgecheck.causality import _sizes, draw_plans
    honest_log = lambda bs: [0] + [1 if math.log(bs[i - 1].close / bs[i - 1].open) > 0 else -1 for i in range(1, len(bs))]
    r = check_causality(honest_log, _penny_tape(), probes="every_bar", seed=1)
    assert not r.leaks
    spike = bars(200, seed=7, gap_prob=0.3)
    spike[100] = dataclasses.replace(spike[100], high=max(spike[100].open, spike[100].close) * 2.2)
    assert not check_causality(strat("clean_lagged").signals, spike, probes="every_bar", seed=1).leaks
    import random
    rng = random.Random(3)
    thin = [dataclasses.replace(b, volume=float(rng.randint(1, 12))) for b in _tick_tape()]
    sz = _sizes(thin)
    assert not sz.zero
    for k in range(4, 200, 9):
        for i, plan in enumerate(draw_plans(2, k, 4)):
            v = _perturbed(thin, k, seed=2 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            assert all(b.volume > 0 for b in v), (k, i)
            assert all(b.low > 0 for b in v), (k, i)
    honest_vol = lambda bs: [0, 0] + [1 if bs[i - 1].close > bs[i - 1].open and math.log(bs[i - 1].volume / bs[i - 2].volume) > 0
                                      else -1 for i in range(2, len(bs))]
    assert not check_causality(honest_vol, thin, probes="every_bar", seed=1).leaks


@pytest.mark.parametrize("field", ["ts", "open", "high", "low", "close", "volume"])
def test_a_value_that_is_not_finite_is_refused_by_name(field):
    tape = bars(40)
    for bad in (math.inf, math.nan):
        t = list(tape)
        t[10] = dataclasses.replace(t[10], **{field: bad})
        with pytest.raises(ValueError, match=f"bar 10 has a {field}"):
            check_causality(strat("clean_lagged").signals, t, seed=1)


def test_a_one_draw_audit_pushes_the_close_strictly_past_the_farthest_level():
    """On a tick tape a far close snapped to the nearest tick landed exactly ON the farthest level
    it was to pass, and a one-draw audit counted it delivered with no repair: a read of the close
    past the previous low walked. The close now lands on the grid strictly inside its band, and
    the one-draw audit owes, and checks, the far close and the range it claims."""
    from edgecheck.causality import _owed, _sizes, draw_plans, realized_sigma
    tape = _tick_tape(0.05)
    sz, sg = _sizes(tape), realized_sigma(tape)
    for k in range(10, 190, 7):
        p, o = tape[k - 1], tape[k].open
        for nonce in (1, 2, 3):
            plan = draw_plans(nonce, k, 1)[0]
            b = _perturbed(tape, k, seed=nonce ^ (k * 1_000_003), sigma=None, plan=plan, sizes=sz)[k]
            owed = _owed(tape, k, sz, [plan], sg)
            for name in ("open", "close", "high", "low"):
                level = getattr(p, name)
                if ("rel", "close", name, plan.move) in owed:
                    assert (b.close - level) * plan.move >= 0
                if (level - o) * plan.move > 0:
                    assert b.close != level, (k, nonce, name, "a far close landed on a level it was to pass")
    at = 5
    s = _one_bar(at, lambda bs: 1 if bs[at].close < bs[at - 1].low else -1)
    for seed in range(1, 9):
        r = check_causality(s, tape, probes="every_bar", draws=1, seed=seed)
        down = draw_plans(r.seed, at, 1)[0].move < 0
        if down and at not in r.undelivered:
            assert any(q.evidence.index == at for q in r.proven), seed


def test_a_floor_push_on_a_grid_never_crosses_a_level_it_calls_out_of_reach():
    """On a doji tape on a half-point tick the floor push was nudged a tick and crossed levels the
    note counted as out of its reach; on a float doji tape a draw forcing no move drew a size
    wider than the floor. Reach and the push now come from the same function."""
    from edgecheck.causality import _sizes, draw_plans
    r_ = lambda x: round(round(x / 0.5) * 0.5, 10)
    tape = []
    for b in bars(120, seed=7, gap_prob=0.3, late_prob=0.1, vol=0.01):
        o = r_(b.open)
        tape.append(dataclasses.replace(b, open=o, close=o, high=o + 0.5, low=o - 0.5, volume=float(round(b.volume))))
    report = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=1)
    sz = _sizes(tape)
    for k in report.beyond_reach[:25]:
        p, o = tape[k - 1], tape[k].open
        far = [x for x in (p.open, p.close, p.high, p.low) if x != o]
        for i, plan in enumerate(draw_plans(1, k, 4)):
            b = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)[k]
            from edgecheck.causality import _reach, realized_sigma
            reach = _reach(o, p, sz, realized_sigma(tape))
            for level in far:
                if not reach["past"](level):
                    assert not ((b.close - level) * (level - o) > 0), (k, i, level)
    float_doji = [dataclasses.replace(b, close=b.open, high=b.open * 1.0024, low=b.open * 0.9976)
                  for b in bars(120, seed=7, gap_prob=0.3, late_prob=0.1)]
    from edgecheck.causality import Plan
    for k in (20, 60, 100):
        for seed in range(6):
            b = _perturbed(float_doji, k, seed=seed, sigma=None, plan=Plan(volume=1))[k]
            assert abs(abs(math.log(b.close / b.open)) - 0.002) < 1e-12, "an unforced draw used a wider size"


def test_a_push_off_the_tapes_scale_is_named_in_the_proof():
    """Where one tick is larger than any move the tape has made at that price, a push of one tick
    was reported as varied "at the tape's own scale" -- a hundred times the largest move."""
    from edgecheck.fixtures import Bar
    tape = []
    for i in range(120):
        price = 1000.0 if i < 60 else 10.0
        o = price
        c = price + (0.01 if i % 2 and i < 60 else 0.0)
        tape.append(Bar(1.7e9 + 60 * i, o, max(o, c), min(o, c), c, 100.0 + 10 * (i % 3)))
    read = _one_bar(80, lambda bs: 1 if bs[80].close > bs[80].open else 0)
    r = check_causality(read, tape, boundaries=[80], draws=4, seed=1)
    assert r.leaks
    text = r.describe()
    assert "tape's own scale" not in text and "farther than any move the tape has made" in text


def test_volumes_on_a_lot_stay_on_it():
    """A volume pushed below a previous volume of one lot fell back to the float just under it:
    99.99999999999999 on a lot of 100."""
    from edgecheck.causality import _sizes, draw_plans
    tape = [dataclasses.replace(b, volume=100.0 * (1 + (i % 7 == 0) + 2 * (i % 11 == 0)))
            for i, b in enumerate(_tick_tape())]
    sz = _sizes(tape)
    assert sz.vol_grid is not None and sz.vol_grid.step == 100.0
    for k in range(4, 200, 3):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            assert all(b.volume % 100.0 == 0.0 and b.volume > 0 for b in v), (k, i)


def test_the_note_names_only_the_ties_and_grids_it_used():
    """The tie list named a doji, a close on a previous level, a repeated volume and an ungapped
    open on every tape, and on a doji tape no doji was ever built; the grid clause named a tick
    and a lot on tapes that had neither. Both now say what was set."""
    float_tape = bars(200, seed=7, gap_prob=0.0)
    r = check_causality(strat("clean_lagged").signals, float_tape, probes="every_bar", seed=1)
    note = r.coverage_note()
    assert "set level with its reference" not in note and "own tick" not in note and "own lot" not in note
    assert r.ties == () and r.grids == ()
    doji = [dataclasses.replace(b, close=b.open, high=max(b.high, b.open), low=min(b.low, b.open))
            for b in bars(120, seed=7, gap_prob=0.3)]
    rd = check_causality(strat("clean_lagged").signals, doji, probes="every_bar", seed=1)
    assert "doji" in rd.ties and "a doji" in rd.coverage_note()
    rt = check_causality(strat("clean_lagged").signals, _tick_tape(), probes="every_bar", seed=1)
    assert rt.grids == ("tick", "lot") and "prices on the tape's own tick and volumes on the lot found in it" in rt.coverage_note()


def test_the_volume_floor_says_what_it_did():
    """"The tape has made no volume changes" on a tape whose volume went between zero and a lot.
    (Its round-twelve tape, traded and untraded bars alternating at three levels, is no floor case
    at all since round fourteen counts changes between successive traded bars.)"""
    tape = [dataclasses.replace(b, volume=0.0 if i % 2 else 100.0) for i, b in enumerate(_tick_tape())]
    r = check_causality(strat("clean_lagged").signals, tape, probes="every_bar", seed=1)
    note = r.coverage_note()
    assert "volume changes" in r.floors
    assert "no volume changes" not in note
    assert "never changed from one traded bar to the next" in note


# -- round thirteen -------------------------------------------------------------------------

def test_a_tape_of_tiny_values_is_not_taken_for_a_coarse_grid():
    """Every value under about 0.005 sat within an absolute tolerance of the first point of a 5000
    grid, so fractional coin volumes were rebuilt at 5000 and nothing else, and a plain read of
    ``volume > previous volume`` walked with the note claiming the volume pushed both ways. Two
    distinct values may never share a grid point, and the tolerance is relative."""
    from edgecheck.causality import _sizes
    base = bars(200, seed=7, gap_prob=0.3, late_prob=0.1)
    coins = [dataclasses.replace(b, volume=round(b.volume * 2e-6, 8)) for b in base]
    assert _sizes(coins).vol_grid.step == 1e-8
    sc = 0.003 / 100
    sub = [dataclasses.replace(b, open=round(b.open * sc, 7), high=round(b.high * sc, 7), low=round(b.low * sc, 7),
                               close=round(b.close * sc, 7)) for b in base]
    assert abs(_sizes(sub).price_grid.step - 1e-7) < 1e-20
    lag = lambda bs, i: 1 if bs[i - 1].close > bs[i - 1].open else -1
    at = next(k for k in range(20, 190) if coins[k].volume > coins[k - 1].volume and lag(coins, k) == 1)
    s = _one_bar(at, lambda bs: 1 if bs[at].volume > bs[at - 1].volume else -1)
    for seed in range(1, 5):
        r = check_causality(s, coins, boundaries=[at], draws=4, seed=seed)
        assert any(q.evidence.index == at for q in r.proven), seed
    at = next(k for k in range(20, 190) if sub[k].close > sub[k].open)
    s = _one_bar(at, lambda bs: 1 if bs[at].close > bs[at].open else -1)
    for seed in range(1, 5):
        r = check_causality(s, sub, boundaries=[at], draws=4, seed=seed)
        assert any(q.evidence.index == at for q in r.proven), seed


def test_binary_ticks_big_lots_and_the_tapes_own_rounding_are_kept():
    """Three grids the rebuild left: a tape on 1/32 was taken for a 0.00025 grid; whole-share
    volumes above a hundred million were given no lot (a guard stopped short of it); and grid
    points the tape never printed came out as 100.19000000000001 on a tape that writes round(x, 2).
    Each let an evader that answered as on real data off the tape's own lattice walk."""
    import random
    from edgecheck.causality import _sizes, draw_plans
    base = bars(200, seed=7, gap_prob=0.3, late_prob=0.1)
    r32 = lambda x: round(x * 32) / 32
    thirty2 = [dataclasses.replace(b, open=r32(b.open), high=max(r32(b.high), r32(b.open), r32(b.close)),
                                   low=min(r32(b.low), r32(b.open), r32(b.close)), close=r32(b.close)) for b in base]
    rng = random.Random(1)
    big = [dataclasses.replace(b, volume=float(rng.randint(60_000_000, 250_000_000))) for b in _tick_tape()]
    cents = _tick_tape()
    assert _sizes(thirty2).price_grid.step == 0.03125
    assert _sizes(big).vol_grid.step == 1.0
    for tape, on in ((thirty2, lambda x: x * 32 == round(x * 32)), (cents, lambda x: x == round(x, 2)), (big, None)):
        sz = _sizes(tape)
        for k in (40, 120):
            for i, plan in enumerate(draw_plans(7, k, 4)):
                v = _perturbed(tape, k, seed=7 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
                if on is not None:
                    assert all(on(x) for b in v[k:] for x in (b.open, b.high, b.low, b.close)), (k, i)
                else:
                    assert all(float(b.volume).is_integer() for b in v), (k, i)
    at = next(k for k in range(60, 150) if cents[k].close != cents[k].open)
    real = 1 if cents[at].close > cents[at].open else -1

    def evader(bs):
        if not all(x == round(x, 2) for b in bs[at:at + 30] for x in (b.open, b.high, b.low, b.close)):
            return real                      # varied data, written off the tape's own rounding
        return 1 if bs[at].close > bs[at].open else -1

    s = _one_bar(at, evader)
    for seed in range(1, 4):
        r = check_causality(s, cents, boundaries=[at], draws=4, seed=seed)
        assert any(q.evidence.index == at for q in r.proven), seed


def _disp(bs, a, b):
    import statistics
    xs = [math.log(bs[i].volume / bs[i - 1].volume) for i in range(a + 1, b) if bs[i].volume > 0 and bs[i - 1].volume > 0]
    return statistics.pstdev(xs) if len(xs) > 5 else None


def test_the_rebuilt_tail_changes_volume_as_the_tape_does():
    """Each rebuilt bar's volume was its own times fresh noise, which widened the bar-to-bar
    changes after the boundary by two thirds; an evader that read its own close only when the
    tail's changes were no wider than the head's walked at every seed. The tail now carries a
    slow level offset, and its changes are as wide as the tape's own."""
    import statistics
    from edgecheck.causality import _sizes, draw_plans
    lag = lambda bs, i: 1 if bs[i - 1].close > bs[i - 1].open else -1
    tape = bars(200, seed=7, gap_prob=0.3, late_prob=0.1)
    sz = _sizes(tape)
    ratios = []
    for k in (30, 60, 90, 120):
        for seed in range(6):
            for i, plan in enumerate(draw_plans(seed, k, 4)):
                v = _perturbed(tape, k, seed=seed * 101 + i, sigma=None, plan=plan, sizes=sz)
                ratios.append(_disp(v, k + 1, len(v)) / _disp(tape, k + 1, len(tape)))
    assert statistics.mean(ratios) < 1.1 and max(ratios) < 1.25, (statistics.mean(ratios), max(ratios))

    def evader(at):
        def s(bs):
            out = [0] + [lag(bs, i) for i in range(1, len(bs))]
            if len(bs) <= at + 12:
                return out
            before, after = _disp(bs, 1, at), _disp(bs, at + 1, len(bs))
            if before and after and after < 1.25 * before and bs[at].close != bs[at].open:
                out[at] = 1 if bs[at].close > bs[at].open else -1
            return out
        return s

    ats = [k for k in range(20, 150) if tape[k].close != tape[k].open
           and (1 if tape[k].close > tape[k].open else -1) == lag(tape, k)][::9][:4]
    for at in ats:
        before, after = _disp(tape, 1, at), _disp(tape, at + 1, len(tape))
        if not after < 1.25 * before:
            continue                     # on this tape the evader never reads the future at ``at``
        for seed in (1, 2, 3):
            assert check_causality(evader(at), tape, boundaries=[at], draws=4, seed=seed).leaks, (at, seed)


def test_a_close_is_never_pushed_past_a_level_the_note_calls_out_of_reach():
    """Where every point a move reaches is a level, the close was put on the farthest of them,
    past the nearer ones -- under a note saying those were out of reach and not pushed past. And
    an off-scale push stepped over levels while its proof line said "one step"."""
    import random
    from edgecheck.fixtures import Bar
    rng, out, ts, c = random.Random(3), [], 1.7e9, 10000
    for i in range(120):
        o = c + (rng.choice((1, -1)) if (i and rng.random() < 0.3) else 0)
        cc = o + rng.randint(-2, 2)
        h, lo = max(o, cc) + rng.randint(0, 2), min(o, cc) - rng.randint(0, 2)
        if i == 49:
            o, h, lo, cc = c, c, c - 1, c - 1
        if i == 50:
            o, h, lo, cc = c - 1, c - 1, c - 3, c - 2
        out.append(Bar(ts, round(o * 0.01, 10), round(h * 0.01, 10), round(lo * 0.01, 10), round(cc * 0.01, 10),
                       abs(rng.gauss(1000, 200))))
        ts += 60
        c = cc
    K, p = 50, out[49]
    seen = []

    def strat(bs):
        if len(bs) > K:
            seen.append(bs[K])
        return [0] + [1 if bs[i - 1].close > bs[i - 1].open else -1 for i in range(1, len(bs))]

    for seed in range(1, 7):
        seen.clear()
        r = check_causality(strat, out, boundaries=[K], draws=4, seed=seed)
        if K in r.beyond_reach:
            from edgecheck.causality import _reach, _sizes, _tie_fields, ALL_TIES, realized_sigma
            sz = _sizes(out)
            reach = _reach(out[K].open, p, sz, realized_sigma(out), ALL_TIES - _tie_fields(out, K, sz))
            o = out[K].open
            for level in (p.open, p.close, p.high, p.low):
                if level != o and not reach["past"](level):
                    assert not any((b.close - level) * (level - o) > 0 for b in seen[1:]), (seed, level)
    t = []
    for i in range(120):
        if i < 60:
            b = (1000.0, 1000.01, 1000.0, 1000.01) if i % 2 else (1000.0, 1000.0, 1000.0, 1000.0)
        elif i == 79:
            b = (10.0, 10.0, 9.99, 10.0)
        elif i == 80:
            b = (10.0, 10.01, 10.0, 10.01)
        elif i > 80:
            b = (10.01, 10.01, 10.01, 10.01)
        else:
            b = (10.0, 10.0, 10.0, 10.0)
        t.append(Bar(1.7e9 + 60 * i, b[0], b[1], b[2], b[3], 100.0 + 10 * (i % 3)))
    seen.clear()

    def reads(bs):
        if len(bs) > 80:
            seen.append(bs[80])
        o = [0] * len(bs)
        if len(bs) > 80:
            o[80] = 1 if bs[80].close >= bs[80].open else -1
        return o

    for seed in range(1, 5):
        seen.clear()
        r = check_causality(reads, t, boundaries=[80], draws=4, seed=seed)
        assert r.leaks and "one step of the tape's price grid off its open" in r.describe()
        assert all(abs(round((b.close - b.open) / 0.01)) <= 1 for b in seen[1:]), seed


def test_the_floor_push_lands_where_the_note_says():
    """The note said the floor push went to the grid point nearest the floor size that was on no
    level; it rounded, landed on a level, and stepped outward past the nearer free point."""
    import random
    from edgecheck.fixtures import Bar
    rng, t = random.Random(5), []
    for i in range(120):
        up, dn = rng.randint(0, 1), rng.randint(0, 1)
        if i == 59:
            up, dn = 2, 1
        t.append(Bar(1.7e9 + 60 * i, 8.0, round(8.0 + 0.01 * up, 2), round(8.0 - 0.01 * dn, 2), 8.0,
                     float(rng.randint(5, 20) * 100)))
    seen = []

    def strat(bs):
        if len(bs) > 60:
            seen.append(bs[60])
        return [0] + [1 if bs[i - 1].high > bs[i - 1].low + 0.015 else -1 for i in range(1, len(bs))]

    r = check_causality(strat, t, boundaries=[60], draws=4, seed=1)
    note = r.coverage_note()
    assert "the first point of its price grid at least that far from the open" in note
    ups = {b.close for b in seen[1:] if b.close > 8.0}
    assert ups == {8.03}, ups          # 8.016 is the floor size: 8.02 is the previous high, 8.03 the first free point


# -- round fourteen -------------------------------------------------------------------------

def _coupled_tape(n: int = 300, seed: int = 7, price: float = 100.0):
    """Volatility that clusters, and volume that rises with the size of the move -- as markets
    have them. A fourteenth red team's."""
    import random
    from edgecheck.fixtures import Bar
    rng, rows, p, s2, lv, prev_r, ts = random.Random(seed), [], price, 0.002 ** 2, 0.0, 0.0, 1.7e9
    for _ in range(n):
        s2 = 0.03 * 0.002 ** 2 + 0.12 * prev_r ** 2 + 0.85 * s2
        s = math.sqrt(s2)
        r = rng.gauss(0, s)
        o, c = p, p * math.exp(r)
        h, lo = max(o, c) * (1 + abs(rng.gauss(0, s / 2))), min(o, c) * (1 - abs(rng.gauss(0, s / 2)))
        lv = 0.7 * lv + rng.gauss(0, 0.15)
        rows.append(Bar(ts, o, h, lo, c, float(round(1000 * math.exp(lv + 250 * abs(r))))))
        p, prev_r, ts = c, r, ts + 60
    return rows


def _ac1(xs):
    import statistics
    m = statistics.fmean(xs)
    d = [x - m for x in xs]
    den = sum(x * x for x in d)
    return sum(a * b for a, b in zip(d, d[1:])) / den if den else 0.0


def _mvcorr(seg):
    import statistics
    xs = [(abs(math.log(b.close / b.open)), math.log(b.volume)) for b in seg if b.volume > 0 and b.open > 0]
    return statistics.correlation([a for a, _ in xs], [b for _, b in xs])


def test_the_rebuilt_tail_keeps_what_goes_with_what_on_the_tape():
    """Round thirteen laid a slow offset on each bar's own volume and drew moves from other bars:
    the tail's log volume came out autocorrelated where the tape's was not, and its volume no
    longer rose with its moves. An evader keyed on either statistic walked. The tail is now runs of
    the tape's own bars, each bar's move, wicks, gap, step and volume together."""
    import statistics
    from edgecheck.causality import _sizes, draw_plans
    for tape in (bars(300, seed=7, gap_prob=0.3, late_prob=0.1), _coupled_tape()):
        sz = _sizes(tape)
        k = 100
        lv = lambda bs: [math.log(b.volume) for b in bs if b.volume > 0]
        real_ac, real_mv = _ac1(lv(tape[k + 1:])), _mvcorr(tape[k + 1:])
        acs, mvs = [], []
        for seed in range(16):
            plan = draw_plans(seed, k, 4)[seed % 4]
            v = _perturbed(tape, k, seed=seed, sigma=None, plan=plan, sizes=sz)
            acs.append(_ac1(lv(v[k + 1:])))
            mvs.append(_mvcorr(v[k + 1:]))
        assert abs(statistics.fmean(acs) - real_ac) < 0.15, (statistics.fmean(acs), real_ac)
        assert abs(statistics.fmean(mvs) - real_mv) < 0.2, (statistics.fmean(mvs), real_mv)

    tape = _coupled_tape()
    K = next(k for k in range(100, 240) if tape[k].close > tape[k].open)

    def evader(bs):
        out = [0] * len(bs)
        for i in range(3, len(bs)):
            out[i] = 1 if bs[i - 1].close > bs[i - 3].close else -1
        if len(bs) > K:
            real_looking = _mvcorr(bs[K + 1:]) > 0.5 * _mvcorr(bs[:K]) and \
                _ac1([math.log(b.volume) for b in bs[K + 1:]]) - _ac1([math.log(b.volume) for b in bs[:K]]) < 0.2
            out[K] = (1 if bs[K].close > bs[K].open else -1) if real_looking else 1
        return out

    assert evader(tape)[K] == 1 and _mvcorr(tape[K + 1:]) > 0.5 * _mvcorr(tape[:K])
    for seed in (1, 2, 3):
        r = check_causality(evader, tape, boundaries=[K], draws=4, seed=seed)
        assert any(q.evidence.index == K for q in r.proven), seed


def test_grids_with_float_twins_huge_integers_and_a_tick_by_price_level():
    """A mid-price tape prints one half-cent point as 49.974999999999994 and as 49.975, and the
    grid was refused altogether; whole volumes of nine trillion got no lot; a tape whose tick is
    1/100 of a cent below a dollar and a cent above had its sub-penny tick put on every price."""
    import random
    from edgecheck.causality import _sizes, _grid, draw_plans
    rng = random.Random(2)
    mid = []
    for b in _tick_tape():
        f = lambda x: (round(x, 2) + round(x + 0.01 * rng.randint(0, 1), 2)) / 2
        o, c = f(b.open), f(b.close)
        mid.append(dataclasses.replace(b, open=o, close=c, high=max(o, c, f(b.high)), low=min(o, c, f(b.low))))
    g = _sizes(mid).price_grid
    assert g is not None and g.step == 0.005
    r = check_causality(strat("clean_lagged").signals, mid, boundaries=[60, 100], draws=4, seed=1)
    assert "unspelled" in r.grids and "spell differently in the last bits" in r.coverage_note()
    assert _grid([9e12 + i * 7919.0 for i in range(50)]).step >= 1.0
    base, p, sub = bars(200, seed=7, gap_prob=0.3, late_prob=0.1), 1.2, []
    for i, b in enumerate(base):
        p = p * (b.close / b.open) ** 8 if i else 1.2
        q = lambda x: round(x, 4) if x < 1 else round(x, 2)
        o, c = q(p), q(p * 0.999)
        sub.append(dataclasses.replace(b, open=o, close=c, high=max(o, c), low=min(o, c)))
    sz = _sizes(sub)
    above = [k for k in range(4, 199) if sub[k].open >= 1.05]
    assert above
    for k in above[:6]:
        for i, plan in enumerate(draw_plans(3, k, 4)):
            v = _perturbed(sub, k, seed=3 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            for b in v[k:k + 20]:
                for x in (b.open, b.high, b.low, b.close):
                    if x >= 1.05:
                        assert abs(x * 100 - round(x * 100)) < 1e-6, (k, i, x)


def test_a_rare_finer_print_is_rebuilt_at_its_own_rate():
    """Three half-cent prints among eight hundred cent prices put the half-cent grid under the
    whole rebuild, where half the rebuilt prices then landed on half-cents."""
    from edgecheck.causality import _sizes, draw_plans
    tape = _tick_tape()
    for i in (5, 9, 14):
        tape[i] = dataclasses.replace(tape[i], high=round(tape[i].high + 0.005, 3))
    sz = _sizes(tape)
    half, total = 0, 0
    for k in (40, 90, 140):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            for b in v[k + 1:]:
                for x in (b.open, b.high, b.low, b.close):
                    total += 1
                    half += abs(x * 100 - round(x * 100)) > 1e-6
    assert half / total < 0.05, half / total


def test_the_volume_floor_and_the_reach_and_sigma_lines_say_what_happened():
    """Volume changes were counted only between adjacent traded bars, so a tape alternating
    traded and untraded bars was told its traded volume never changed; the beyond-reach line
    said 'at least as far as the largest move' of a level a part in a billion inside it; and a
    proof under a caller's sigma said the bar moved by that sigma when it moved by the tape's size."""
    alt = [dataclasses.replace(b, volume=0.0 if i % 2 else b.volume) for i, b in enumerate(bars(120, seed=7))]
    r = check_causality(strat("clean_lagged").signals, alt, probes="every_bar", seed=1)
    assert "volume changes" not in r.floors
    tape = bars(200, gap_prob=0.0)
    from edgecheck.causality import _sizes
    top = _sizes(tape).moves[-1]
    o = tape[100].open
    tape[99] = dataclasses.replace(tape[99], high=o * math.exp(top * (1 - 5e-10)))
    note = check_causality(strat("clean_lagged").signals, tape, boundaries=[100], draws=4, seed=1).coverage_note()
    assert "(or within a part in a billion of it)" in note
    wide = check_causality(strat("leak_same_bar_close").signals, bars(200), probes="every_bar", sigma=0.5, seed=1)
    assert "the bar itself by sizes the tape has made and later bars with moves of sigma 0.5" in wide.describe()


def _banded_tape(fp, price, seed, lot=1.0):
    """bars() printed by ``fp``, a writer whose tick depends on the price."""
    out = []
    for b in bars(200, seed=seed, price=price, vol=0.004 if price > 5 else 0.006, gap_prob=0.3, late_prob=0.1):
        o, c = fp(b.open), fp(b.close)
        out.append(dataclasses.replace(b, open=o, close=c, high=max(fp(b.high), o, c), low=min(fp(b.low), o, c),
                                       volume=float(round(b.volume * lot))))
    return out


def _rebuilt_prices(tape, ks):
    from edgecheck.causality import _sizes, draw_plans
    sz = _sizes(tape)
    for k in ks:
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            for b in v[k:]:
                yield from (b.open, b.high, b.low, b.close)


def test_a_price_is_rebuilt_on_the_tick_of_its_own_band():
    """A stock printing four places below a dollar and cents above it, whose last few prices were
    its only ones above a dollar: those were given the four-place tick, the window above them
    being empty. And a spread table's band under 20 on 0.02, over it on 0.05: the finer prints over
    20 were rebuilt under it, a bar opening under 20 crossed it on 0.02, and a price between the
    two bands' prints was set on either. Each was a price no such tape prints, at every bar."""
    penny = _banded_tape(lambda x: round(x, 4) if x < 1 else round(x, 2), 1.0, 11, lot=100)
    assert max(b.high for b in penny) < 1.1 and any(b.high > 1.05 for b in penny)
    off_cent = [x for x in _rebuilt_prices(penny, (29, 31, 37, 90)) if x >= 1 and x != round(x, 2)]
    assert not off_cent, off_cent[:5]

    def hk(x):
        return round(round(x / 0.02) * 0.02, 2) if x < 20 else round(round(x / 0.05) * 0.05, 2)
    table = _banded_tape(hk, 20.0, 3, lot=100)

    def printable(x):
        return round(x * 50, 6) == round(x * 50) if x < 20 else round(x * 20, 6) == round(x * 20)
    assert all(printable(x) for b in table for x in (b.open, b.high, b.low, b.close))
    near = [k for k in range(10, 190) if 19.8 < table[k].open < 20.0][:4] + [60, 120]
    off_band = [x for x in _rebuilt_prices(table, near) if not printable(x)]
    assert not off_band, off_band[:5]


# -- round fifteen --------------------------------------------------------------------------

def _f32(x):
    import struct
    return struct.unpack("f", struct.pack("f", x))[0]


def _era_tape(kind):
    """300 bars near 20 whose print rule changes at bar 150: a tick from 0.05 to 0.01 at the same
    prices, or a 3-for-2 split with the history before it divided by 1.5 and written to 4 places."""
    base = bars(300, seed=5 if kind == "tick" else 8, price=20.0, vol=0.004, gap_prob=0.3, late_prob=0.1)
    cut = base[150].ts
    if kind == "tick":
        def w(x, ts):
            return round(round(x / 0.05) * 0.05, 2) if ts < cut else round(x, 2)

        def ok(x, ts):
            return abs(x * 20 - round(x * 20)) < 1e-6 if ts < cut else abs(x * 100 - round(x * 100)) < 1e-6
    else:
        def w(x, ts):
            return round(round(x * 1.5, 2) / 1.5, 4) if ts < cut else round(x, 2)

        def ok(x, ts):
            return round(round(x * 1.5, 2) / 1.5, 4) == x if ts < cut else abs(x * 100 - round(x * 100)) < 1e-6
    tape = []
    for b in base:
        o, c = w(b.open, b.ts), w(b.close, b.ts)
        tape.append(dataclasses.replace(b, open=o, close=c, high=max(w(b.high, b.ts), o, c),
                                        low=min(w(b.low, b.ts), o, c), volume=float(round(b.volume))))
    return tape, ok


def _rebuilt_bars(tape, ks):
    from edgecheck.causality import _sizes, draw_plans
    sz = _sizes(tape)
    for k in ks:
        for i, plan in enumerate(draw_plans(1, k, 4)):
            yield from _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)[k:]


def _keyed_on(tape, ok, at):
    """Reads its own close at ``at``, and answers as on the real tape wherever a price from there
    on breaks the tape's print rule."""
    real = [1 if b.close > b.open else -1 if b.close < b.open else 0 for b in tape]

    def signals(bs):
        out = [0] * len(bs)
        if len(bs) > at:
            tell = any(not ok(x, b.ts) for b in bs[at:] for x in (b.open, b.high, b.low, b.close))
            b = bs[at]
            out[at] = real[at] if tell else (1 if b.close > b.open else -1 if b.close < b.open else 0)
        return out
    return signals


def test_a_tape_is_rebuilt_by_the_rule_it_prints_by_where_and_when():
    """A cent tape stored as float32 sat on no grid at a double's precision and was rebuilt in
    arbitrary doubles; a tick that changed in time, and a split whose history was adjusted to four
    places, each put one era's tick under the other's bars; the sparse top of a spread table just
    over its 0.50 band took the finer tick below it. A same-bar reader keyed on each walked."""
    from edgecheck.causality import _sizes
    base = bars(200, seed=4, price=20.0, vol=0.004, gap_prob=0.3, late_prob=0.1)
    f32 = [dataclasses.replace(b, open=_f32(round(b.open, 2)), close=_f32(round(b.close, 2)),
                               high=_f32(max(round(b.high, 2), round(b.open, 2), round(b.close, 2))),
                               low=_f32(min(round(b.low, 2), round(b.open, 2), round(b.close, 2))),
                               volume=float(round(b.volume))) for b in base]

    def f32_cent(x, ts=None):
        return _f32(x) == x and abs(x * 100 - round(x * 100)) < 1e-3
    bad = [x for b in _rebuilt_bars(f32, (30, 90, 150)) for x in (b.open, b.high, b.low, b.close) if not f32_cent(x)]
    assert not bad, bad[:5]
    assert all(check_causality(_keyed_on(f32, f32_cent, 100), f32, boundaries=[100], draws=4, seed=s).proven
               for s in (1, 2))

    for kind in ("tick", "split"):
        tape, ok = _era_tape(kind)
        assert _sizes(tape).eras.starts == (150,)
        bad = [(b.ts, x) for b in _rebuilt_bars(tape, (60, 140, 149, 150, 151, 220))
               for x in (b.open, b.high, b.low, b.close) if not ok(x, b.ts)]
        assert not bad, (kind, bad[:5])
        at = 80 if kind == "tick" else 40
        assert tape[at].close != tape[at].open
        assert all(check_causality(_keyed_on(tape, ok, at), tape, boundaries=[at], draws=4, seed=s).proven
                   for s in (1, 2))

    def tick(x):
        return 0.001 if x < 0.25 else 0.005 if x < 0.5 else 0.01

    def hk(x):
        t = tick(x)
        y = round(round(x / t) * t, 3)
        return y if abs(y / tick(y) - round(y / tick(y))) < 1e-6 else round(round(x / tick(y)) * tick(y), 3)
    table = []
    for b in bars(300, seed=19, price=0.34, vol=0.03, gap_prob=0.3, late_prob=0.1):
        o, c = hk(b.open), hk(b.close)
        table.append(dataclasses.replace(b, open=o, close=c, high=max(hk(b.high), o, c), low=min(hk(b.low), o, c),
                                         volume=float(round(b.volume) * 1000)))
    assert max(b.high for b in table) > 0.5
    bad = [x for b in _rebuilt_bars(table, (100, 157, 158, 200, 280)) for x in (b.open, b.high, b.low, b.close)
           if abs(x / tick(x) - round(x / tick(x))) > 1e-6]
    assert not bad, bad[:5]


def test_round_fifteen_sentences_say_what_happened():
    """An off-grid beyond-reach bar in on-grid words; a volume-only spelling caveat that blamed the
    prices; a caller's sigma called a floor; a volume floor on a tape where no bar traded; a high
    carried past the tape's largest wick under 'the tape's own scale'; and a crash near 2**53."""
    base = bars(240, gap_prob=0.0, price=100.0)
    mixed = [dataclasses.replace(b, open=round(b.open, 2), high=round(b.high, 2), low=round(b.low, 2),
                                 close=round(b.close, 2)) if i < 235 else b for i, b in enumerate(base)]
    for i in range(1, 235):
        p = mixed[i - 1].close
        mixed[i] = dataclasses.replace(mixed[i], open=p, high=max(mixed[i].high, p), low=min(mixed[i].low, p))
    from edgecheck.causality import _sizes
    top = _sizes(mixed).moves[-1]
    o = mixed[237].open
    mixed[236] = dataclasses.replace(mixed[236], high=max(mixed[236].high, o * math.exp(top * (1 - 5e-10))))
    note = check_causality(strat("clean_lagged").signals, mixed, boundaries=[237], draws=4, seed=1).coverage_note()
    assert "(or within a part in a billion of it)" in note and "on the tape's price grid" not in note

    import random as _random
    rng = _random.Random(5)
    feeds = []
    for i, b in enumerate(bars(150, gap_prob=0.2, price=50.0)):
        o, c, h, lo = (round(x, 2) for x in (b.open, b.close, b.high, b.low))
        n = rng.randint(1, 400)
        feeds.append(dataclasses.replace(b, open=o, close=c, high=max(o, c, h), low=min(o, c, lo),
                                         volume=n / 10 if i % 2 else n * 0.1))
    r = check_causality(strat("clean_lagged").signals, feeds, boundaries=[40], draws=4, seed=1)
    assert "unspelled volumes" in r.grids and "unspelled" not in r.grids
    assert "the tape's volumes are not written as plain roundings" in r.coverage_note()

    flat = [dataclasses.replace(b, open=100.0, high=100.0, low=100.0, close=100.0) for b in bars(120, gap_prob=0.0)]
    lines = check_causality(strat("leak_same_bar_close").signals, flat, boundaries=[60], draws=4, seed=1,
                            sigma=0.01).describe()
    assert "with moves of sigma 0.01" in lines and "floor size" not in lines.split("\n", 3)[3]

    idle = [dataclasses.replace(b, volume=0.0) for b in bars(120, seed=2)]
    r = check_causality(strat("clean_lagged").signals, idle, probes="every_bar", seed=1)
    assert "floor ratio" not in r.coverage_note() and "no bar of the tape traded, so no volume was pushed" in r.coverage_note()

    import random as _r
    from edgecheck.fixtures import Bar
    rng = _r.Random(4)

    def band(x):
        return 0.05 if x >= 20.0 else 0.01

    def snap(x):
        return round(round(x / band(x)) * band(x), 2)
    edge, p, ts = [], 19.95, 1.7e9
    big = math.log(20.05 / 20.0) + 1e-12
    for _ in range(200):
        o = p
        for _ in range(20):
            c = snap(o + rng.choice((-2, -1, 1, 2)) * band(o))
            if c != o and abs(math.log(c / o)) <= big and 19.80 <= c <= 20.30:
                break
        else:
            c = o
        h = snap(max(o, c) + rng.choice((0, 0, 1)) * band(max(o, c)))
        lo = snap(min(o, c) - rng.choice((0, 0, 1)) * band(min(o, c)))
        h = max(o, c) if abs(math.log(h / max(o, c))) > big else h
        lo = min(o, c) if abs(math.log(min(o, c) / lo)) > big else lo
        edge.append(Bar(ts, o, h, lo, c, float(rng.randint(5, 50) * 100)))
        p, ts = c, ts + 60
    wick = _sizes(edge).wicks[-1]
    seen = {}

    def skew(bs):
        out = [0] * len(bs)
        if len(bs) > 10:
            b = bs[10]
            out[10] = 1 if b.high - max(b.open, b.close) > min(b.open, b.close) - b.low else -1
            if bs is not edge and len(bs) == len(edge):
                seen.setdefault(out[10], b)
        return out
    r = check_causality(skew, edge, boundaries=[10], draws=4, seed=2)
    for p in r.proven:
        b = seen[p.evidence.variant]
        assert b.high / max(b.open, b.close) - 1 <= wick + 1e-12 or "past the largest" in p.evidence.detail, (b, p)

    huge = [dataclasses.replace(b, **{f: float(2 ** 53 - 2 ** 20 + round((getattr(b, f) - 100) * 1000) * 2)
                                      for f in ("open", "high", "low", "close")}) for b in bars(120, seed=3)]
    huge = [dataclasses.replace(b, high=max(b.high, b.open, b.close), low=min(b.low, b.open, b.close)) for b in huge]
    check_causality(strat("clean_lagged").signals, huge, probes="every_bar", seed=1)


# -- round sixteen --------------------------------------------------------------------------

def test_eras_are_found_in_the_middle_past_a_stray_print_and_on_rounded_ticks():
    """A tick that went to 0.05 and back got no eras; one stray cent print in a nickel era moved its
    cut to the print; an 11-for-10 history was fit to a tick of 1/5600; and a 1/32 tick written to four
    places was taken for an adjusted 1/96. Each rebuilt prices the tape never prints then."""
    from edgecheck.causality import _sizes
    base = bars(300, seed=5, price=20.0, vol=0.004, gap_prob=0.3, late_prob=0.1)
    t150, t225 = base[150].ts, base[225].ts

    def build(src, w):
        out = []
        for b in src:
            o, c = w(b.open, b.ts), w(b.close, b.ts)
            out.append(dataclasses.replace(b, open=o, close=c, high=max(w(b.high, b.ts), o, c),
                                           low=min(w(b.low, b.ts), o, c), volume=float(round(b.volume))))
        return out

    def on(x, st):
        return abs(x / st - round(x / st)) < 1e-6

    def nick(x):
        return round(round(x / 0.05) * 0.05, 2)
    twice = build(base, lambda x, ts: nick(x) if t150 <= ts < t225 else round(x, 2))
    assert _sizes(twice).eras.starts == (150, 225)
    bad = [x for b in _rebuilt_bars(twice, (60, 160, 200, 240)) for x in (b.open, b.high, b.low, b.close)
           if not (on(x, 0.05) if t150 <= b.ts < t225 else on(x, 0.01))]
    assert not bad, bad[:5]

    stray = build(base, lambda x, ts: nick(x) if ts < t150 else round(x, 2))
    b = stray[40]
    stray[40] = dataclasses.replace(b, close=min(max(round(b.close + (0.02 if b.close < b.high else -0.02), 2), b.low), b.high))
    assert _sizes(stray).eras.starts == (150,)

    s11 = build(base, lambda x, ts: round(round(x * 1.1, 2) / 1.1, 4) if ts < t150 else round(x, 2))
    bad = [x for b in _rebuilt_bars(s11, (60, 140, 200)) for x in (b.open, b.high, b.low, b.close)
           if not (round(round(x * 1.1, 2) / 1.1, 4) == x if b.ts < t150 else on(x, 0.01))]
    assert not bad, bad[:5]

    t32 = build(bars(300, seed=9, price=110.0, vol=0.001, gap_prob=0.3, late_prob=0.1),
                lambda x, ts: round(round(x * 32) / 32, 4))
    sz = _sizes(t32)
    assert (sz.price_grid.step, sz.price_grid.scale) == (0.03125, 1.0)
    bad = [x for b in _rebuilt_bars(t32, (60, 200)) for x in (b.open, b.high, b.low, b.close)
           if round(round(x * 32) / 32, 4) != x]
    assert not bad, bad[:5]
    r = check_causality(strat("clean_lagged").signals, t32, boundaries=[60], draws=4, seed=1)
    assert "adjusted" not in r.grids


def test_round_sixteen_sentences_say_what_happened():
    """A high inside the largest wick before its close was set down on its level's tick, and past it
    after, was left unnamed under 'the tape's own scale'; and a tape of signed volumes was probed
    upward only and told no bar traded."""
    from edgecheck.causality import _sizes

    def fp(x):
        return round(x, 4) if x < 1 else round(x, 2)
    sub = []
    for b in bars(200, seed=11, price=1.0, vol=0.01, gap_prob=0.3, late_prob=0.1):
        o, c = fp(b.open), fp(b.close)
        sub.append(dataclasses.replace(b, open=o, close=c, high=max(fp(b.high), o, c), low=min(fp(b.low), o, c),
                                       volume=float(round(b.volume * 100))))
    wick = _sizes(sub).wicks[-1]
    seen = {}

    def high_reader(bs):
        out = [0] * len(bs)
        if len(bs) > 44:
            b = bs[44]
            out[44] = 1 if b.high / max(b.open, b.close) - 1 > 0.019 else -1
            if bs is not sub and len(bs) == len(sub):
                seen.setdefault(out[44], b)
        return out
    for p in check_causality(high_reader, sub, boundaries=[44], draws=4, seed=1).proven:
        b = seen[p.evidence.variant]
        assert b.high / max(b.open, b.close) - 1 <= wick + 1e-12 or "past the largest" in p.evidence.detail

    signed = [dataclasses.replace(b, volume=-b.volume) for b in bars(120, seed=5, gap_prob=0.2)]
    with pytest.raises(ValueError, match="traded size"):
        check_causality(strat("clean_lagged").signals, signed, boundaries=[60], draws=4, seed=1)


def _session_tape(days=6, seed=4, per=78):
    """5-minute bars 09:30-15:55 over six days, overnight gaps, a U-shaped day of volume."""
    import random
    from edgecheck.fixtures import Bar
    r, p, out = random.Random(seed), 50.0, []
    day0 = 1_700_006_400.0 - (1_700_006_400.0 % 86400)
    for d in range(days):
        for j in range(per):
            ts = day0 + d * 86400 + 9.5 * 3600 + j * 300
            o = round(p * math.exp(r.gauss(0, 0.01)), 2) if j == 0 and d else p
            c = round(o * math.exp(r.gauss(0, 0.0015)), 2)
            h = round(max(o, c) * (1 + abs(r.gauss(0, 0.0008))), 2)
            lo = round(min(o, c) * (1 - abs(r.gauss(0, 0.0008))), 2)
            u = 1 + 3 * ((j - per / 2) / (per / 2)) ** 2
            out.append(Bar(ts, o, max(h, o, c), min(lo, o, c), c, float(max(100, round(2000 * u * math.exp(r.gauss(0, 0.3)) / 100) * 100))))
            p = c
    return out


def test_the_rebuilt_tail_keeps_the_tapes_clock_level_and_coupling():
    """Donor runs started anywhere on the tape put more than half a rebuilt tail's bars outside a
    09:30-15:55 session; gave a tail on a tape whose volume rose twentyfold the whole tape's level,
    with a jump at every join; and under a caller's sigma drew moves that had lost their coupling
    with volume. A sixteenth red team measured each on the rebuilt tail against the real one."""
    import random
    import statistics
    from edgecheck.causality import _sizes, draw_plans
    tape = _session_tape()
    sz = _sizes(tape)
    printed = {b.ts % 86400 for b in tape}
    days = {int(b.ts // 86400 + 3) % 7 for b in tape}
    off = 0
    for k in (100, 249, 380):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            for b in _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)[k + 2:]:
                off += (b.ts % 86400) not in printed or int(b.ts // 86400 + 3) % 7 not in days
    assert off == 0, off

    r, rows, ts = random.Random(1), [], 1.7e9
    p = 100.0
    from edgecheck.fixtures import Bar
    for i in range(400):
        o, c = p, p * math.exp(r.gauss(0, 0.002))
        rows.append(Bar(ts, o, max(o, c) * 1.001, min(o, c) * 0.999, c, float(round(1000 * math.exp(3.0 * i / 400 + r.gauss(0, 0.3))))))
        p, ts = c, ts + 60
    sz = _sizes(rows)
    gaps = []
    for k in (150, 250):
        real = statistics.fmean(math.log(b.volume) for b in rows[k + 2:k + 40])
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(rows, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            gaps.append(abs(statistics.fmean(math.log(b.volume) for b in v[k + 2:k + 40]) - real))
    assert statistics.fmean(gaps) < 0.35, gaps

    coupled = _coupled_tape(400)
    sz = _sizes(coupled)
    sg = realized_sigma(coupled)
    cors = []
    for k in (100, 200, 300):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(coupled, k, seed=1 ^ (k * 1_000_003 + i), sigma=sg, plan=plan, sizes=sz)
            cors.append(_mvcorr(v[k + 2:]))
    assert statistics.fmean(cors) > 0.5 * _mvcorr(coupled), (statistics.fmean(cors), _mvcorr(coupled))


def test_a_tape_with_no_trades_names_no_floor_in_its_proof_line():
    """No bar traded, so no volume was pushed and no floor size used; the proof line said one was."""
    idle = [dataclasses.replace(b, volume=0.0) for b in bars(120, seed=2)]
    r = check_causality(strat("leak_same_bar_close").signals, idle, boundaries=[60], draws=4, seed=1)
    assert r.proven and all("floor size" not in p.evidence.detail for p in r.proven), [p.evidence.detail for p in r.proven]


# -- round seventeen ------------------------------------------------------------------------

_MON0 = 1_700_438_400.0          # 2023-11-20 00:00 UTC, a Monday


def _walk_bar(r, p, s, tick=None, gap=0.0):
    o = p * math.exp(r.gauss(0, gap)) if gap else p
    c = o * math.exp(r.gauss(0, s))
    h, lo = max(o, c) * (1 + abs(r.gauss(0, s / 2))), min(o, c) * (1 - abs(r.gauss(0, s / 2)))
    if tick:
        o, c = round(o / tick) * tick, round(c / tick) * tick
        h, lo = max(math.ceil(h / tick - 1e-9) * tick, o, c), min(math.floor(lo / tick + 1e-9) * tick, o, c)
        o, c, h, lo = (round(x, 6) for x in (o, c, h, lo))
    return o, h, lo, c


def _weekday_sessions(days, seed, opens):
    """Weekday sessions of 78 five-minute bars, opening at ``opens(session)`` seconds of the UTC day."""
    import random
    from edgecheck.fixtures import Bar
    r, out, t, d, p = random.Random(seed), [], _MON0, 0, 50.0
    while d < days:
        if int(t // 86400 + 3) % 7 < 5:
            for j in range(78):
                o, h, lo, c = _walk_bar(r, p * math.exp(r.gauss(0, 0.01)) if j == 0 and out else p, 0.0015, tick=0.01)
                u = 1 + 3 * ((j - 39) / 39) ** 2
                out.append(Bar(t + opens(d) + j * 300, o, h, lo, c, float(max(100, round(2000 * u * math.exp(r.gauss(0, 0.3)) / 100) * 100))))
                p = c
            d += 1
        t += 86400
    return out


def _weekday_daily(n=500, seed=21):
    """Daily bars on weekdays, a few holidays, and Mondays gapping three times wider."""
    import random
    from edgecheck.fixtures import Bar
    r, out, t, p = random.Random(seed), [], _MON0, 100.0
    hol, wd_i = set(r.sample(range(550), 20)), 0
    while len(out) < n:
        if int(t // 86400 + 3) % 7 < 5:
            wd_i += 1
            if wd_i not in hol:
                mon = bool(out) and t - out[-1].ts > 1.5 * 86400
                o, h, lo, c = _walk_bar(r, p, 0.012, gap=0.012 if mon else 0.004)
                out.append(Bar(t, o, h, lo, c, float(round(1e6 * math.exp(r.gauss(0, 0.3) + (0.4 if mon else 0.0))))))
                p = c
        t += 86400
    return out


def test_the_rebuilt_tail_uses_each_bar_once_keeps_each_days_hours_and_what_follows_a_weekend():
    """A seventeenth red team: donors drawn again and again from sixty-odd bars made the tail repeat
    its own runs; one set of hours merged across a change of clock gave every rebuilt day 90 bars
    where every real day had 78; a late next bar mid-session was an overnight gap long; Mondays were
    rebuilt from ordinary days and lost their gaps; and tail wicks set on a coarse tick came out
    twice the largest the tape made."""
    import collections
    import random
    import statistics
    from edgecheck.causality import _Donors, _sizes, draw_plans
    from edgecheck.fixtures import Bar
    coupled = _coupled_tape(400)
    don = _Donors(coupled, 200, random.Random(1))
    used = [don.at(i, coupled[i - 1].ts) for i in range(200, 400)]
    pairs = list(zip(used, used[1:]))
    assert len(set(pairs)) == len(pairs), len(pairs) - len(set(pairs))      # no run repeats

    dst = _weekday_sessions(10, 24, lambda d: (14.5 if d < 5 else 13.5) * 3600)
    sz = _sizes(dst)
    long_days = []
    for k in (120, 300, 500, 700):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(dst, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            per = collections.Counter(math.floor(b.ts / 86400) for b in v)
            long_days += [c for day, c in per.items() if day != max(per) and c != 78]
    assert not long_days, long_days[:5]

    daily = _weekday_daily()
    sz = _sizes(daily)
    ratios = []
    for k in (200, 300, 400):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(daily, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            after = [abs(math.log(v[j].open / v[j - 1].close)) for j in range(k + 2, len(v)) if v[j].ts - v[j - 1].ts > 1.5 * 86400]
            other = [abs(math.log(v[j].open / v[j - 1].close)) for j in range(k + 2, len(v)) if v[j].ts - v[j - 1].ts <= 1.5 * 86400]
            if after and other:
                ratios.append(statistics.fmean(after) / statistics.fmean(other))
    assert statistics.fmean(ratios) > 2.0, ratios

    rng, tape, p, ts = random.Random(3), [], 10.0, 1.7e9
    for _ in range(200):
        o = round(p, 2)
        c = round(o + rng.choice((-2, -1, 0, 1, 2)) * 0.01, 2)
        tape.append(Bar(ts, o, round(max(o, c) + rng.choice((0, 0, 1)) * 0.01, 2),
                        round(min(o, c) - rng.choice((0, 0, 1)) * 0.01, 2), c, float(rng.randint(1, 50) * 100)))
        p, ts = c, ts + 60
    sz = _sizes(tape)
    wick = sz.wicks[-1]
    over = 0
    for i, plan in enumerate(draw_plans(1, 60, 4)):
        for b in _perturbed(tape, 60, seed=1 ^ (60 * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)[62:]:
            top, bot = max(b.open, b.close), min(b.open, b.close)
            over += b.high / top - 1 > wick + 1e-9 or 1 - b.low / bot > wick + 1e-9
    assert over == 0, over


def test_the_tail_clause_says_what_each_tape_got():
    """The note said later bars kept the tape's times of day on a tape with no calendar, and that the
    tail's volatility stayed near the tape's own under a caller's sigma ten times it."""
    flat = check_causality(strat("clean_lagged").signals, bars(200), boundaries=[100], draws=4, seed=1)
    assert "their times, after the next bar's, stepping as those bars' own do" in flat.coverage_note()
    assert "at the times of day" not in flat.coverage_note()
    wide = check_causality(strat("clean_lagged").signals, bars(200), boundaries=[100], draws=4, seed=1, sigma=0.05)
    assert ", of volatility, which is the given sigma's" in wide.coverage_note()
    assert "of where the price is far ahead, which the rebuild takes from the tape's own moves near it, rescaled" in wide.coverage_note()
    assert "of where the price is far ahead, which strays from the tape's own less than a walk of its own would" in flat.coverage_note()
    sess = check_causality(strat("clean_lagged").signals, _session_tape(), boundaries=[100], draws=4, seed=1)
    assert "at the times of day and on the days it prints" in sess.coverage_note()


# ---------------------------------------------------------------- an eighteenth red team

def _suspended_daily(n=140, seed=8):
    """Weekday bars at 07:00 UTC, with trading suspended for 45 days in the middle."""
    import random
    from edgecheck.fixtures import Bar
    r, out, p, d = random.Random(seed), [], 12.0, 0
    while len(out) < n:
        t = _MON0 + d * 86400
        if int(t // 86400 + 3) % 7 < 5 and not 100 <= d < 145:
            o, h, lo, c = _walk_bar(r, p, 0.015, tick=0.01, gap=0.004)
            out.append(Bar(t + 7 * 3600, o, h, lo, c, float(r.randint(10, 90) * 100)))
            p = c
        d += 1
    return out


def _midnight_sessions(sessions=10, seed=31):
    """5-minute bars 22:00-03:55 UTC, Sunday night to Friday morning: a session across midnight."""
    import random
    from edgecheck.fixtures import Bar
    r, out, p, t, d = random.Random(seed), [], 1.1, _MON0 - 86400, 0
    while d < sessions:
        if int(t // 86400 + 3) % 7 in (6, 0, 1, 2, 3):
            for j in range(72):
                o, h, lo, c = _walk_bar(r, p, 0.0006, tick=0.00001, gap=0.0015 if j == 0 and out else 0.0)
                u = 1 + 3 * ((j - 36) / 36) ** 2
                out.append(Bar(t + 22 * 3600 + j * 300, o, h, lo, c, float(max(1, round(50 * u * math.exp(r.gauss(0, 0.3)))))))
                p = c
            d += 1
        t += 86400
    return out


def test_a_tail_crosses_a_long_halt_keeps_the_tapes_steps_and_is_as_wide_as_sigma():
    """An eighteenth red team: past a 45-day suspension the tail stepped blindly through weekends; a
    session across midnight UTC took its donors' breaks where the tape has none; on a tape loud and
    then quiet, a tail under a caller's sigma of 0.01 moved 0.0007 a bar."""
    import random
    import statistics
    from edgecheck.causality import _calendar, _sizes, draw_plans
    from edgecheck.fixtures import Bar
    halted = _suspended_daily()
    sz = _sizes(halted)
    off = []
    for k in (40, 55, 60, 66):
        for i, plan in enumerate(draw_plans(1, k, 4)[:3]):
            v = _perturbed(halted, k, seed=31 * i + k, sigma=None, plan=plan, sizes=sz)
            off += [b.ts for b in v[k + 1:] if int(b.ts // 86400 + 3) % 7 >= 5 or b.ts % 86400 != 7 * 3600]
    assert not off, off[:5]

    night = _midnight_sessions()
    cal = _calendar(night)
    sz = _sizes(night)
    strange = []
    for k in (560, 600, 640, 680):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(night, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            for j in range(k + 2, len(v)):
                made = cal.steps_after.get(cal.key(v[j - 1].ts))
                if made is not None and v[j].ts - v[j - 1].ts not in made:
                    strange.append((k, j, v[j].ts - v[j - 1].ts))
    assert not strange, strange[:5]

    r, loud, p = random.Random(11), [], 100.0
    for i in range(400):
        sd = 0.02 if i < 200 else 0.001
        o = round(p, 2)
        c = round(o * math.exp(r.gauss(0, sd)), 2)
        loud.append(Bar(1.7e9 + 60 * i, o, max(round(max(o, c) * (1 + abs(r.gauss(0, sd / 2))), 2), o, c),
                        min(round(min(o, c) * (1 - abs(r.gauss(0, sd / 2))), 2), o, c), c, float(r.randint(1, 60) * 100)))
        p = c
    sz = _sizes(loud)
    widths = []
    for i in range(6):
        v = _perturbed(loud, 300, seed=100 + i, sigma=0.01, plan=draw_plans(i, 300, 4)[i % 4], sizes=sz)
        widths.append(math.sqrt(statistics.fmean(math.log(b.close / b.open) ** 2 for b in v[301:])))
    assert all(0.006 < w < 0.016 for w in widths), widths


def test_a_tails_end_is_not_the_tapes_and_a_short_tail_copies_no_run():
    """An eighteenth red team: a tail made of every later bar once ended where the real tape did, so
    a strategy reading the direction to the last bar was never caught; a tail too short for runs was
    rebuilt from runs of the bars before the probed one, copied as they stood."""
    import random
    import statistics
    from edgecheck.causality import _Donors, _sizes, draw_plans
    tape = bars(400, seed=6, gap_prob=0.3, late_prob=0.1)
    n, sz = len(tape), _sizes(tape)
    sd = statistics.pstdev(math.log(tape[i].close / tape[i - 1].close) for i in range(1, n))
    shifts = []
    for k in (40, 114, 188, 262):
        real = math.log(tape[-1].close / tape[k].open)
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            shifts.append((math.log(v[-1].close / v[k].open) - real) / (sd * math.sqrt(n - 1 - k)))
    assert statistics.pstdev(shifts) > 0.35, statistics.pstdev(shifts)

    runs = 0
    for seed in range(12):
        don = _Donors(tape, n - 6, random.Random(seed))
        got = [don.at(i, tape[i - 1].ts) for i in range(n - 6, n)]
        runs += sum(b == a + 1 for a, b in zip(got, got[1:]))
    assert runs <= 3, runs


def test_rebuilt_gaps_and_volumes_stay_within_the_tapes_largest():
    """An eighteenth red team: jittered off a donor at the tape's largest, a rebuilt gap or volume
    went up to 1.16 times the largest the tape had printed -- under a note that said the tape's own."""
    from edgecheck.causality import _sizes, draw_plans
    tape = bars(300, seed=3, gap_prob=0.5)
    sz = _sizes(tape)
    over = []
    for k in (60, 120, 180):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=plan, sizes=sz)
            for j in range(k + 2, len(v)):
                g = math.log(v[j].open / v[j - 1].close)
                if g > sz.gaps_up[-1] * (1 + 1e-9) or -g > sz.gaps_dn[-1] * (1 + 1e-9) or v[j].volume > sz.volumes[-1]:
                    over.append((k, j, g, v[j].volume))
    assert not over, over[:5]


def test_the_calendar_is_found_once_per_audit_and_not_for_event_bars(monkeypatch):
    """An eighteenth red team: found again for every rebuild, the calendar made an audit's cost grow
    with the square of the tape. And a tape whose times keep no step (event bars) has none."""
    import random
    import edgecheck.causality as C
    from edgecheck.fixtures import Bar
    calls = []
    real = C._calendar
    monkeypatch.setattr(C, "_calendar", lambda t: calls.append(1) or real(t))
    check_causality(strat("clean_lagged").signals, _session_tape(), boundaries=[100, 200], draws=4, seed=1)
    assert len(calls) <= 2, len(calls)

    r, ts, events = random.Random(1), 1.7e9, []
    for b in bars(2500, seed=1):
        ts += r.uniform(20, 400)
        events.append(dataclasses.replace(b, ts=ts))
    assert real(events) is None


# ---------------------------------------------------------------- a nineteenth red team

def test_a_still_stretch_stays_still_under_a_sigma_and_a_sigma_below_the_tick_is_refused():
    """A nineteenth red team: scaled by the volatility around it, an untraded bar of a still stretch
    had nothing to scale and was given a normal move of the caller's sigma -- an untraded bar that
    moved, which the tape never prints; and a sigma below a $2 stock's one-cent tick rounded every
    rebuilt move to nothing under a proof line that said 'moves of sigma 0.0005'."""
    import random
    from edgecheck.causality import _sizes, draw_plans
    from edgecheck.fixtures import Bar
    r, p, t, still = random.Random(71), 50.0, [], []
    for i in range(400):
        if i < 270:
            o, h, lo, c = _walk_bar(r, p, 0.003)
            t.append(Bar(1.7e9 + 60 * i, o, h, lo, c, float(r.randint(10, 90))))
            p = c
        else:
            t.append(Bar(1.7e9 + 60 * i, p, p, p, p, 0.0))
    sz = _sizes(t)
    for k in (150, 200, 260):
        for i, plan in enumerate(draw_plans(1, k, 4)):
            v = _perturbed(t, k, seed=1 ^ (k * 1_000_003 + i), sigma=0.004, plan=plan, sizes=sz)
            still += [j for j in range(k + 1, len(v)) if v[j].volume == 0 and (v[j].close != v[j].open or v[j].high != v[j].low)]
    assert not still, still[:5]

    r, p, two = random.Random(11), 2.0, []
    for i in range(300):
        o = round(p, 2)
        c = round(o * math.exp(r.gauss(0, 0.004)), 2)
        two.append(Bar(1.7e9 + 60 * i, o, round(max(o, c) + r.choice((0, 0.01)), 2),
                       round(min(o, c) - r.choice((0, 0.01)), 2), c, float(r.randint(1, 50) * 100)))
        p = c
    with pytest.raises(ValueError, match="below the tape's tick"):
        check_causality(strat("clean_lagged").signals, two, boundaries=[100], draws=4, seed=1, sigma=0.0005)
    for bad in (0.0, -0.01, float("nan")):
        with pytest.raises(ValueError, match="sigma"):
            check_causality(strat("clean_lagged").signals, two, boundaries=[100], draws=4, seed=1, sigma=bad)
    ok = check_causality(strat("leak_same_bar_close").signals, two, boundaries=[100], draws=4, seed=1, sigma=0.02)
    assert ok.leaks


def test_the_next_open_stays_within_the_tapes_largest_gap():
    """A nineteenth red team: the next bar's planned open was not held to the tape's largest gap, as
    later bars' were, and went up to 1.19 times past it under 'the tape's own scale'."""
    from edgecheck.causality import _past_clause, _sizes, draw_plans
    for tape, where in ((_weekday_daily(300), [(171, 2), (179, 3), (247, 2), (284, 3), (285, 2)]),
                        (bars(300, seed=3, gap_prob=0.5), [(167, 3), (173, 3), (189, 3)])):
        sz = _sizes(tape)
        for k, i in where:
            notes: dict = {}
            v = _perturbed(tape, k, seed=1 ^ (k * 1_000_003 + i), sigma=None, plan=draw_plans(1, k, 4)[i],
                           sizes=sz, notes=notes)
            g = math.log(v[k + 1].open / v[k].close)
            top = (sz.gaps_up if g > 0 else sz.gaps_dn)[-1]
            assert abs(g) <= top * (1 + 1e-9) or "next open" in notes.get("past", ()), (k, i, abs(g) / top)
    assert "the next bar's open set on the tick of its price level, past the largest gap" in _past_clause(("next open",))


def test_two_changes_of_tick_are_not_joined_into_a_grid_that_snaps_backwards():
    """A nineteenth red team: two changes of tick, each with only its two printed prices, joined by the
    lcm of their float-noise widths into a step of 8e13, whose one reachable point a snap up returned
    below its input -- a wick 1.03 times the largest the tape made."""
    from edgecheck.causality import Grid, Grids, _joined, _snap
    a = Grids([0.997, 1.005], [Grid(0.001, 0.0, {}), Grid(0.005, 0.0, {})]).at(0.9989, snap=True)
    b = Grids([0.997, 1.0], [Grid(0.001, 0.0, {}), Grid(0.01, 0.0, {})]).at(0.9989, snap=True)
    assert a.pair and b.pair
    assert _joined(a, b) is a
    assert _snap(0.9989, _joined(a, b), "up") >= 0.9989
    wide = Grid(79999999999999.03, 0.997, {})
    assert _snap(0.9989, wide, "up") >= 0.9989 and _snap(0.9989, wide, "down") <= 0.9989


# ---------------------------------------------------------------- a twentieth red team

def test_coarse_lots_and_sub_nano_ticks_are_found_and_a_tape_with_no_grid_says_so():
    """A twentieth red team: the candidate steps stopped at 5000 and 1e-9, so a volume lot of 100,000
    and a token's 1e-10 tick were never found; rebuilt values went off the tape's rule under a note
    that said 'volumes on its own lot', or, with no grid found, said nothing of the prices at all."""
    import random
    from edgecheck.causality import _grid, _sizes
    from edgecheck.fixtures import Bar
    r, p, lots, nano = random.Random(3), 50.0, [], []
    for i in range(200):
        o, h, lo, c = _walk_bar(r, p, 0.01, tick=0.01)
        lots.append(Bar(1.7e9 + 60 * i, o, h, lo, c, float(r.randint(100, 900) * 100_000)))
        p = c
    assert _sizes(lots).vol_grid.step == 100_000
    p = 8e-6
    for i in range(200):
        o = round(p, 10)
        c = round(o * math.exp(r.gauss(0, 0.01)), 10)
        h, lo = round(max(o, c) * (1 + abs(r.gauss(0, 0.004))), 10), round(min(o, c) * (1 - abs(r.gauss(0, 0.004))), 10)
        nano.append(Bar(1.7e9 + 60 * i, o, max(h, o, c), min(lo, o, c), c, float(r.randint(1, 90))))
        p = c
    assert abs(_sizes(nano).price_grid.step - 1e-10) < 1e-22
    # floats near their last place are no grid at all: any of them sits within a unit of such a step
    assert _grid([100 + r.random() for _ in range(300)]) is None
    floats = bars(120, seed=5)
    note = check_causality(strat("clean_lagged").signals, floats, boundaries=[60], draws=4, seed=1).coverage_note()
    assert "no price grid was found in the tape, so a rebuilt price is any float" in note
    note = check_causality(strat("clean_lagged").signals, lots, boundaries=[60], draws=4, seed=1).coverage_note()
    assert "volumes on the lot found in it" in note and "own lot" not in note


def test_a_sigma_is_checked_before_any_run_and_only_one_that_can_be_honoured_is_taken():
    """A twentieth red team: the least sigma the refusal named, rounded down, was refused again; a float
    tape took a sigma of 1e-17; a sigma of five hung the snap and one of a million overflowed; a tape
    with no moves at all got a tail of dojis under 'moves of sigma'; and a bad sigma was refused only
    after the truncation phase had run the strategy once a bar."""
    import random
    import re
    from edgecheck.causality import _sizes, draw_plans
    from edgecheck.fixtures import Bar
    from edgecheck.sandbox import prove
    r, p, cents = random.Random(8), 8.13, []
    for i in range(120):
        o, h, lo, c = _walk_bar(r, p, 0.004, tick=0.01)
        cents.append(Bar(1.7e9 + 60 * i, o, h, lo, c, float(r.randint(1, 50) * 100)))
        p = c
    with pytest.raises(ValueError) as e:
        check_causality(strat("clean_lagged").signals, cents, boundaries=[60], draws=4, seed=1, sigma=1e-6)
    least = float(re.search(r"at least ([0-9.e+-]+)", str(e.value)).group(1))
    check_causality(strat("clean_lagged").signals, cents, boundaries=[60], draws=4, seed=1, sigma=least)
    for big in (5.0, 1e6, 1e300):
        with pytest.raises(ValueError, match="largest sigma taken"):
            check_causality(strat("clean_lagged").signals, cents, boundaries=[60], draws=4, seed=1, sigma=big)
    with pytest.raises(ValueError, match="what a float can move a price by"):
        check_causality(strat("clean_lagged").signals, bars(120, seed=5), boundaries=[60], draws=4, seed=1, sigma=1e-17)
    runs = []
    with pytest.raises(ValueError):
        check_causality(lambda bs: runs.append(1) or [0] * len(bs), cents, probes="every_bar", seed=1, sigma=0.0)
    assert not runs
    with pytest.raises(ValueError, match="sigma"):
        prove(None, cents, sigma=float("nan"))          # before the gates run anything

    flat = [dataclasses.replace(b, close=b.open, high=b.open * 1.001, low=b.open * 0.999) for b in bars(120, seed=7)]
    sz = _sizes(flat)
    v = _perturbed(flat, 60, seed=5, sigma=0.01, plan=draw_plans(1, 60, 4)[0], sizes=sz)
    assert sum(b.close != b.open for b in v[61:]) > 40


# ---------------------------------------------------------------- a twenty-first red team

def _intraday_sessions(days=20, per=13, seed=4):
    """Half-hourly bars from 09:30 UTC on weekdays: intraday gaps of a tick or none, overnight gaps of
    a normal, and whole-tick wicks that are often zero."""
    import random
    from edgecheck.fixtures import Bar
    r, p, out, d, made = random.Random(seed), 50.0, [], 0, 0
    day0 = 1_700_006_400.0 - (1_700_006_400.0 % 86400)
    while made < days:
        t_day = day0 + d * 86400
        d += 1
        if int((t_day // 86400 + 3) % 7) >= 5:
            continue
        made += 1
        for j in range(per):
            if j == 0 and out:
                o = round(p * math.exp(r.gauss(0, 0.008)) / 0.01) * 0.01
                o = o if round(o, 2) != round(p, 2) else p + 0.01 * r.choice((1, -1))
            elif out and r.random() < 0.12:
                o = p + 0.01 * r.choice((1, -1))
            else:
                o = p
            o = round(o, 2)
            c = round(round(o * math.exp(r.gauss(0, 0.002)) / 0.01) * 0.01, 2)
            h = round(max(o, c) + 0.01 * int(abs(r.gauss(0, 3))), 2)
            lo = round(min(o, c) - 0.01 * int(abs(r.gauss(0, 3))), 2)
            out.append(Bar(t_day + 9.5 * 3600 + j * 1800, o, h, lo, c,
                           float(max(100, round(3000 * math.exp(r.gauss(0, 0.4)) / 100) * 100))))
            p = c
    return out


def test_the_next_open_is_pushed_past_the_bars_own_high_and_low():
    """A twenty-first red team: the next open was pushed past this bar's open and close and the
    previous bar's levels, never past this bar's own high or low; on a session tape whose intraday
    gaps are a tick, a strategy reading 'the next bar opens above this one's high' walked every_bar."""
    from edgecheck.causality import _at_bar, _owed, _sizes, draw_plans, realized_sigma
    tape = _intraday_sessions()
    sz = _sizes(tape)
    owed = _owed(tape, 85, _at_bar(sz, 85), draw_plans(1, 85, 4), realized_sigma(tape))
    assert {("next_rel", "own_high", 1), ("next_rel", "own_low", -1)} <= owed

    def above_high(bs):
        out = [0] * len(bs)
        for k in (40, 85, 121):
            if k + 1 < len(bs):
                out[k] = int(bs[k + 1].open > bs[k].high)
        return out
    for seed in (0, 1, 2):
        r = check_causality(above_high, tape, boundaries=[40, 85, 121], draws=4, seed=seed)
        assert {p.evidence.index for p in r.proven} >= {40, 85, 121}, (seed, r.describe()[:200])
    assert "above this bar's own high and below its own low" in r.coverage_note()


def test_round_twenty_one_sentences_and_arguments():
    """A twenty-first red team: the least sigma a refusal named was refused again (0.00625); 'sigma 0.5
    is more than ... 0.5'; one bad print was 'the tape's tick, 2 of its price'; a tape of no trades was
    told its rebuilt volumes were any float, a tape of no moves that its tail was still; a trending
    tape under a sigma drifted to a cent of dojis, or to infinity; a Decimal sigma, a numpy seed and a
    bad tape through prove() failed only after the strategy had run."""
    import decimal
    import random
    import re
    from edgecheck.causality import _ceil_sig, _perturbed, _sizes, draw_plans
    from edgecheck.fixtures import Bar
    from edgecheck.sandbox import prove
    for x in (0.07 / 11.2, 0.05 / 3906.25, 0.0020000000000005105):
        assert _ceil_sig(x) >= x, x

    r, p, cents = random.Random(8), 50.0, []
    for i in range(200):
        o, h, lo, c = _walk_bar(r, p, 0.004, tick=0.01)
        cents.append(Bar(1.7e9 + 60 * i, o, h, lo, c, float(r.randint(1, 50) * 100)))
        p = c
    with pytest.raises(ValueError, match=re.escape("sigma 0.5000000000000001 is more than")):
        check_causality(strat("clean_lagged").signals, cents, boundaries=[60], draws=4, seed=1, sigma=0.5000000000000001)
    bad = list(cents)
    bad[150] = dataclasses.replace(bad[150], low=0.01, close=0.01)
    with pytest.raises(ValueError, match="the tick at bar 150, 1 of its price"):
        check_causality(strat("clean_lagged").signals, bad, boundaries=[60], draws=4, seed=1, sigma=0.01)

    runs = []
    with pytest.raises(ValueError, match="sigma must be"):
        check_causality(lambda bs: runs.append(1) or [0] * len(bs), cents, boundaries=[60], draws=4, seed=1,
                        sigma=decimal.Decimal("0.01"))
    assert not runs

    class Index:                                   # a numpy integer, as far as the audit cares
        def __index__(self):
            return 5
    check_causality(strat("clean_lagged").signals, cents, boundaries=[Index()], draws=4, seed=Index())
    for tape in (cents[:5], cents[:30] + [dataclasses.replace(cents[30], close=math.inf)] + cents[31:]):
        with pytest.raises(ValueError, match="need at least 8 bars|finite number"):
            prove(None, tape)                       # before the gates run anything

    untraded = [dataclasses.replace(b, volume=0.0) for b in cents]
    note = check_causality(strat("clean_lagged").signals, untraded, boundaries=[60], draws=4, seed=1).coverage_note()
    assert "no bar of the tape traded, and no rebuilt bar does" in note and "any float" not in note
    flat = [dataclasses.replace(b, close=b.open, high=b.open * 1.001, low=b.open * 0.999) for b in bars(120, seed=7)]
    note = check_causality(lambda bs: [0] * len(bs), flat, boundaries=[60], draws=4, seed=1, sigma=0.01).coverage_note()
    assert "the tape having made no moves" in note and "still where it is still" not in note

    r, p, down = random.Random(5), 3.0, []
    for i in range(240):
        o = round(p, 2)
        c = round(max(0.01, o * math.exp(r.gauss(-0.004, 0.012))), 2)
        down.append(Bar(1.7e9 + 60 * i, o, max(o, c), min(o, c), c, float(r.randint(1, 50) * 100)))
        p = c
    sz = _sizes(down)
    for i in range(4):
        v = _perturbed(down, 60, seed=10 + i, sigma=0.2, plan=draw_plans(1, 60, 4)[i], sizes=sz)
        tail = v[-60:]
        assert all(math.isfinite(b.close) and b.close > 0 for b in v)
        assert sum(b.close == b.open for b in tail) < 30, sum(b.close == b.open for b in tail)
