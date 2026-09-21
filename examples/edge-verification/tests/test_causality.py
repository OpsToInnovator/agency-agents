"""The detector is judged on its acquittals as much as its convictions.

One false accusation costs the credibility of every true finding, so the clean fixtures
matter more here than the dirty ones.
"""
from __future__ import annotations

import importlib

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

    d = Divergence(index=5, boundary=9, baseline=1, variant=-1, probe="truncation", detail="removed")
    assert Proven(d, "x").horizon == 4


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
    horizons = [p.horizon for p in report.proven]
    assert horizons == sorted(horizons, reverse=True)


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
    assert "every bar was probed" in complete.describe()

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
    assert "telling the two apart" in report.describe()
