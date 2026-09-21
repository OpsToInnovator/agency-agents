"""The detector is judged on its acquittals as much as its convictions.

One false accusation costs the credibility of every true finding, so the clean fixtures
matter more here than the dirty ones.
"""
from __future__ import annotations

import importlib

import pytest

from edgecheck.causality import Divergence, Proven, Report, _perturbed, check_causality
from edgecheck.fixtures import bars
from edgecheck.fixtures.strategies import leak_backfill, leak_centered_window

CLEAN = ["clean_lagged", "clean_but_costly"]
LEAKY = ["leak_same_bar_close", "leak_future_close", "leak_centered_window",
         "leak_full_sample_zscore", "leak_backfill", "leak_peak_threshold"]
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

    With draws=1 no perturbation comparison happens, leaving truncation on its own. It
    catches the leak that runs off the end of the data and misses the one sitting inside
    the current bar -- which is the most common lookahead in real code. If this test ever
    starts passing with the perturbation probe removed, the probe has been broken.
    """
    truncation_only = check_causality(strat("leak_same_bar_close").signals, tape, draws=1)
    assert not truncation_only.leaks

    both = check_causality(strat("leak_same_bar_close").signals, tape)
    assert both.leaks
    assert both.worst_horizon == 0
    assert all(p.evidence.probe == "perturbation" for p in both.proven)


def test_truncation_still_carries_the_leaks_that_run_off_the_end(tape):
    """The converse: the probe that is blind to same-bar leaks is the one that finds these."""
    report = check_causality(strat("leak_full_sample_zscore").signals, tape, draws=1)
    assert report.leaks
    assert all(p.evidence.probe == "truncation" for p in report.proven)


def test_a_wide_nudge_blinds_the_probe_to_full_sample_leaks(tape):
    """Why the nudge is one percent and not fifty, measured rather than assumed.

    A leak that works through a statistic of the whole sample -- a mean, a standard
    deviation, a quantile -- is detected by perturbation only while the nudge stays small.
    Widen it and the nudge itself dominates the statistic: the z-score denominator inflates,
    every real signal collapses toward zero on every draw alike, and the output stops moving
    even though the leak is still there. Measured on this fixture, findings go 4 at sigma
    0.002, 2 at 0.01, and 0 at 0.05 and above.

    The leaks that survive a wide nudge (same-bar, next-bar, centred window) are the ones
    that read a specific cell rather than a distribution. So sigma is a real tradeoff, and
    the reason it is survivable is that truncation catches the full-sample family regardless.
    """
    def hits(sigma):
        r = check_causality(strat("leak_full_sample_zscore").signals, tape, draws=8, sigma=sigma)
        return len([p for p in r.proven if p.evidence.probe == "perturbation"])

    assert hits(0.002) > hits(0.01) > hits(0.05)
    assert hits(0.05) == 0, "a wide nudge should lose this leak entirely"

    # And the safety net: the probe that does not care about sigma still convicts.
    fallback = check_causality(strat("leak_full_sample_zscore").signals, tape, draws=8, sigma=0.05)
    assert fallback.leaks
    assert all(p.evidence.probe == "truncation" for p in fallback.proven)


def test_a_narrow_nudge_is_not_free_either(tape):
    """The tradeoff runs both ways, which is why the default sits in the middle.

    A nudge small enough to keep full-sample statistics sensitive is too small to reliably
    flip a comparison against a back-filled level, so neither extreme dominates.
    """
    def hits(sigma, name):
        r = check_causality(strat(name).signals, tape, draws=8, sigma=sigma)
        return len([p for p in r.proven if p.evidence.probe == "perturbation"])

    assert hits(0.002, "leak_backfill") < hits(0.01, "leak_backfill")


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
    a = check_causality(strat("leak_peak_threshold").signals, tape)
    b = check_causality(strat("leak_peak_threshold").signals, tape)
    assert a.proven == b.proven
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
