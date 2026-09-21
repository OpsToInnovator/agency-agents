"""The sandbox is judged on two things: it must be invisible to an honest strategy, and it
must own the strategy's inputs. Containment of accidents is third."""
from __future__ import annotations

import importlib
import os
import textwrap
from pathlib import Path

import pytest

from edgecheck.causality import check_causality
from edgecheck.fixtures import bars
from edgecheck.sandbox import (BadOutput, Limits, NetworkAttempt, ResourceExceeded, Sandbox,
                               StrategyError, Timeout, detect_isolation, precheck)

FIX = Path(__file__).resolve().parents[1] / "edgecheck" / "fixtures" / "strategies"
NAMESPACED = detect_isolation() == "namespace"


def strategy_file(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def fixture_sandbox(name: str, tmp_path: Path, **kw) -> Sandbox:
    return Sandbox.from_file(FIX / f"{name}.py", work_root=tmp_path / "runs", **kw)


@pytest.fixture(scope="module")
def tape():
    return bars(120)


# -- invisible to an honest strategy ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["clean_lagged", "leak_same_bar_close", "leak_full_sample_zscore"])
def test_the_sandbox_returns_exactly_what_the_strategy_returns(name, tape, tmp_path):
    direct = importlib.import_module(f"edgecheck.fixtures.strategies.{name}").signals(tape)
    assert fixture_sandbox(name, tmp_path)(tape) == direct


@pytest.mark.parametrize("name", ["clean_lagged", "leak_same_bar_close"])
def test_the_probes_reach_the_same_verdict_through_the_sandbox(name, tape, tmp_path):
    """The whole point: check_causality does not know or care that the strategy is a process."""
    mod = importlib.import_module(f"edgecheck.fixtures.strategies.{name}")
    direct = check_causality(mod.signals, tape, draws=4)
    boxed = check_causality(fixture_sandbox(name, tmp_path), tape, draws=4)
    assert boxed.leaks == direct.leaks
    assert boxed.worst_horizon == direct.worst_horizon
    assert [p.evidence for p in boxed.proven] == [p.evidence for p in direct.proven]


def test_each_run_is_a_fresh_process_so_no_state_carries(tape, tmp_path):
    """A module-level cache would let a poisoned run read the baseline run's answer."""
    p = strategy_file(tmp_path, "cachey", """
        CALLS = []
        def signals(bars):
            CALLS.append(len(bars))
            # If state carried between runs, the second call would see the first's entry.
            return [1 if len(CALLS) == 1 else -1] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == sb(tape) == [1] * len(tape)


# -- owning the inputs -------------------------------------------------------------------------------

def test_precheck_passes_a_deterministic_input_dependent_strategy(tape, tmp_path):
    pc = precheck(fixture_sandbox("clean_lagged", tmp_path), tape, bars(120, seed=99))
    assert pc.deterministic and pc.input_dependent and pc.provable
    assert "PROVABLE" in pc.describe() and "UNPROVABLE" not in pc.describe()


def test_precheck_refuses_a_nondeterministic_strategy(tape, tmp_path):
    p = strategy_file(tmp_path, "coinflip", """
        import random
        def signals(bars):
            return [random.choice((-1, 1)) for _ in bars]
    """)
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape, bars(120, seed=99))
    assert not pc.deterministic
    assert not pc.provable
    assert "nondeterministic" in pc.describe()


def test_precheck_refuses_a_strategy_that_ignores_our_data(tape, tmp_path):
    """Bundled data, or a constant -- either way no probe can move it, so nothing is provable."""
    p = strategy_file(tmp_path, "bundled", """
        PRICES = [100 + (i % 7) for i in range(1000)]     # brought its own data
        def signals(bars):
            return [1 if PRICES[i + 1] > PRICES[i] else -1 for i in range(len(bars))]
    """)
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape, bars(120, seed=99))
    assert pc.deterministic
    assert not pc.input_dependent
    assert not pc.provable
    assert "not a function of the data we control" in pc.describe()


def test_files_the_strategy_writes_are_reported(tape, tmp_path):
    p = strategy_file(tmp_path, "cacher", """
        import json
        def signals(bars):
            json.dump([b.close for b in bars], open("features.json", "w"))
            return [0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    sb(tape)
    assert sb.records[-1].files_written == ("features.json",)


# -- the environment is built, not inherited ------------------------------------------------------

def test_secrets_in_the_parent_environment_do_not_reach_the_strategy(tape, tmp_path, monkeypatch):
    monkeypatch.setenv("EDGECHECK_TEST_SECRET", "hunter2")
    monkeypatch.setenv("BINANCE_API_KEY", "should-never-be-seen")
    p = strategy_file(tmp_path, "peek", """
        import os
        def signals(bars):
            leaked = [k for k in os.environ if k in ("EDGECHECK_TEST_SECRET", "BINANCE_API_KEY")]
            return [len(leaked)] * len(bars)     # 0 means nothing leaked
    """)
    assert Sandbox.from_file(p, work_root=tmp_path / "runs")(tape) == [0] * len(tape)


# -- containment of accidents ----------------------------------------------------------------------

def test_a_network_attempt_is_refused_and_named(tape, tmp_path):
    p = strategy_file(tmp_path, "phone_home", """
        import socket
        def signals(bars):
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=1)
            except OSError:
                pass                      # swallowed -- must still be on record
            return [0] * len(bars)
    """)
    with pytest.raises(NetworkAttempt, match="create_connection"):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_the_kernel_blocks_the_network_even_past_the_python_ban(tape, tmp_path):
    """ctypes walks past a monkeypatch. It does not walk past an empty network namespace."""
    p = strategy_file(tmp_path, "raw_socket", """
        import ctypes, ctypes.util, struct
        def signals(bars):
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fd = libc.socket(2, 1, 0)                       # AF_INET, SOCK_STREAM
            # sin_family is host byte order; the port is network byte order.
            addr = struct.pack("=H", 2) + struct.pack("!H", 53) + bytes([1, 1, 1, 1]) + b"\\0" * 8
            rc = libc.connect(fd, addr, len(addr))
            err = ctypes.get_errno()
            return [1 if rc == 0 else (-1 if err == 101 else 0)] * len(bars)   # 101 = ENETUNREACH
    """)
    assert Sandbox.from_file(p, work_root=tmp_path / "runs")(tape) == [-1] * len(tape)


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_the_home_directory_is_not_there(tape, tmp_path):
    home = os.environ.get("HOME", "/root")
    p = strategy_file(tmp_path, "snoop", f"""
        import os
        def signals(bars):
            try:
                n = len(os.listdir({home!r}))
            except OSError:
                n = 0
            return [1 if n else 0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == [0] * len(tape)
    assert home in sb.records[-1].hidden or "/root" in sb.records[-1].hidden


def test_a_runaway_allocation_is_stopped(tape, tmp_path):
    p = strategy_file(tmp_path, "hog", """
        def signals(bars):
            x = bytearray(3 * 1024 * 1024 * 1024)
            return [0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(memory_bytes=512 * 1024 ** 2))
    with pytest.raises(ResourceExceeded):
        sb(tape)


def test_an_infinite_loop_is_killed(tape, tmp_path):
    p = strategy_file(tmp_path, "spin", """
        def signals(bars):
            while True:
                pass
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(cpu_s=2, wall_s=8))
    with pytest.raises((ResourceExceeded, Timeout)):
        sb(tape)


def test_a_sleeping_strategy_hits_the_wall_clock(tape, tmp_path):
    p = strategy_file(tmp_path, "nap", """
        import time
        def signals(bars):
            time.sleep(60)
            return [0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(wall_s=3))
    with pytest.raises(Timeout):
        sb(tape)


def test_a_strategy_that_raises_reports_its_last_line(tape, tmp_path):
    p = strategy_file(tmp_path, "boom", """
        def signals(bars):
            raise ValueError("bad column: recv_ts")
    """)
    with pytest.raises(StrategyError, match="bad column: recv_ts"):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


@pytest.mark.parametrize("body", [
    "def signals(bars): return [0] * (len(bars) - 1)",      # wrong length
    "def signals(bars): return [0.5] * len(bars)",          # not a position
    "def signals(bars): return [2] * len(bars)",            # out of range
])
def test_output_that_is_not_one_position_per_bar_is_refused(body, tape, tmp_path):
    p = strategy_file(tmp_path, "shape", body)
    with pytest.raises(BadOutput):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


def test_the_report_says_which_tier_ran(tape, tmp_path):
    sb = fixture_sandbox("clean_lagged", tmp_path)
    sb(tape)
    assert sb.records[-1].isolation in ("namespace", "plain")
    assert sb.records[-1].isolation == detect_isolation()


def test_plain_tier_is_available_on_request(tape, tmp_path):
    """Whatever the host supports, the weaker tier must still run and still return."""
    direct = importlib.import_module("edgecheck.fixtures.strategies.clean_lagged").signals(tape)
    sb = fixture_sandbox("clean_lagged", tmp_path, isolation="plain")
    assert sb(tape) == direct
    assert sb.records[-1].hidden == ()
