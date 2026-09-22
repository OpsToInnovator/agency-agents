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
from edgecheck.sandbox import (BadOutput, ContractViolation, Limits, NetworkAttempt, ResourceExceeded,
                               Sandbox, StrategyError, Timeout, detect_isolation, precheck, prove)

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
    direct = check_causality(mod.signals, tape, draws=2, seed=1)
    boxed = check_causality(fixture_sandbox(name, tmp_path), tape, draws=2, seed=1)
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
    pc = precheck(fixture_sandbox("clean_lagged", tmp_path), tape)
    assert pc.deterministic and pc.input_dependent and pc.provable
    assert "PROVABLE" in pc.describe() and "UNPROVABLE" not in pc.describe()


def test_precheck_refuses_a_nondeterministic_strategy(tape, tmp_path):
    p = strategy_file(tmp_path, "coinflip", """
        import random
        def signals(bars):
            return [random.choice((-1, 1)) for _ in bars]
    """)
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape)
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
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape)
    assert pc.deterministic
    assert not pc.input_dependent
    assert not pc.provable
    assert "does not change when the bars we can vary change" in pc.describe()


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
    # create_connection resolves first, so the hook fires at getaddrinfo before connect.
    with pytest.raises(NetworkAttempt, match=r"socket\.(getaddrinfo|connect)"):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_the_kernel_blocks_the_network_even_past_the_python_ban(tape, tmp_path):
    """ctypes walks past a monkeypatch. It does not walk past an empty network namespace."""
    p = strategy_file(tmp_path, "raw_socket", """
        import ctypes, struct
        def signals(bars):
            # not ctypes.util.find_library: on Linux it spawns ldconfig, which the sandbox refuses
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
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
    assert not any(home.startswith(v) for v in sb.records[-1].visible)


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
    assert sb.records[-1].visible == ()


# -- what the red team got through, and must not again ------------------------------------------

@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_nothing_written_outside_the_run_directory_persists(tape, tmp_path):
    """The second red team cached through /opt and injected a module into the interpreter's
    own dist-packages. The first version hid a LIST of paths, and a list can never name
    every writable directory on a host. Now the root is a whitelist: those paths either do
    not exist or are read-only, and nothing written anywhere but the run dir outlives it."""
    import sysconfig
    site = sysconfig.get_paths()["purelib"]
    p = strategy_file(tmp_path, "persist", f"""
        import os
        TARGETS = ["/opt/edgecheck_probe", "/run/edgecheck_probe", "/var/edgecheck_probe",
                   "/home/edgecheck_probe", "/root/edgecheck_probe", {site!r} + "/edgecheck_probe.py",
                   "/usr/edgecheck_probe", "/etc/edgecheck_probe"]
        def signals(bars):
            seen = sum(1 for t in TARGETS if os.path.exists(t))
            written = 0
            for t in TARGETS:
                try:
                    open(t, "w").write("x"); written += 1
                except OSError:
                    pass
            return [written * 10 + seen] * len(bars) if written * 10 + seen <= 1 else [-1] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == [0] * len(tape), "a write outside the run directory succeeded"
    assert sb(tape) == [0] * len(tape), "something persisted into the next run"
    for t in ("/opt/edgecheck_probe", "/run/edgecheck_probe", "/var/edgecheck_probe",
              os.path.join(site, "edgecheck_probe.py")):
        assert not os.path.exists(t)
    assert "/tmp" not in sb.records[-1].visible and "/opt" not in sb.records[-1].visible


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_a_file_written_to_tmp_does_not_reach_the_next_run(tape, tmp_path):
    p = strategy_file(tmp_path, "launder", """
        import os
        MARK = "/tmp/edgecheck_launder_probe_marker"
        def signals(bars):
            seen = os.path.exists(MARK)
            open(MARK, "w").write("x")
            return [1 if seen else 0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == [0] * len(tape)
    assert sb(tape) == [0] * len(tape)
    assert not os.path.exists("/tmp/edgecheck_launder_probe_marker")


def test_writing_into_the_strategys_own_directory_does_not_reach_the_next_run(tape, tmp_path):
    """Runs copy from a staged copy, never from the source; in the namespace tier the copy is
    read-only as well. Either way run two cannot see what run one wrote."""
    p = strategy_file(tmp_path, "writeback", """
        import os
        HERE = os.path.dirname(os.path.abspath(__file__))
        def signals(bars):
            mark = os.path.join(HERE, "carried.txt")
            seen = os.path.exists(mark)
            try:
                open(mark, "w").write("x")
            except OSError:
                pass
            return [1 if seen else 0] * len(bars)
    """)
    for iso in (detect_isolation(), "plain"):
        sb = Sandbox.from_file(p, work_root=tmp_path / f"runs-{iso}", isolation=iso)
        sb(tape)
        assert sb(tape) == [0] * len(tape), f"state carried in {iso} tier"


def test_rebinding_the_child_runners_names_changes_nothing(tape, tmp_path):
    """The result path is a closure over a pipe, not a module attribute to overwrite."""
    p = strategy_file(tmp_path, "rebind", """
        import sys
        def signals(bars):
            m = sys.modules["__main__"]
            for name in ("_write", "finish", "record", "main", "_write_all"):
                setattr(m, name, lambda *a, **k: None)
            return [1] * len(bars)
    """)
    assert Sandbox.from_file(p, work_root=tmp_path / "runs")(tape) == [1] * len(tape)


def test_a_thread_left_running_cannot_act_after_the_result_is_sent(tape, tmp_path):
    """The child exits the instant the result is on the wire. A non-daemon thread that would
    have rewritten the output after the official write never gets to run, and the call
    does not wait for it either."""
    import time
    p = strategy_file(tmp_path, "latethread", """
        import threading, time, os
        def later():
            time.sleep(3)
            open("late.txt", "w").write("x")
        def signals(bars):
            threading.Thread(target=later).start()      # non-daemon on purpose
            return [1] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    t0 = time.monotonic()
    assert sb(tape) == [1] * len(tape)
    assert time.monotonic() - t0 < 2.5
    assert "late.txt" not in sb.records[-1].files_written


def test_a_swallowed_network_attempt_cannot_be_erased(tape, tmp_path):
    """The record is a pipe. There is no file to unlink."""
    p = strategy_file(tmp_path, "eraser", """
        import socket, os
        def signals(bars):
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=1)
            except OSError:
                pass
            for f in ("violations.jsonl", "out.json"):
                try: os.remove(f)
                except OSError: pass
            return [0] * len(bars)
    """)
    with pytest.raises(NetworkAttempt):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


def test_reloading_the_socket_module_does_not_lift_the_ban(tape, tmp_path):
    p = strategy_file(tmp_path, "reloader", """
        import importlib, socket
        def signals(bars):
            importlib.reload(socket)
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=1)
            except OSError:
                pass
            return [0] * len(bars)
    """)
    with pytest.raises(NetworkAttempt):
        Sandbox.from_file(p, work_root=tmp_path / "runs", isolation="plain")(tape)


def test_starting_a_process_is_a_contract_violation(tape, tmp_path):
    """Whatever a child process read, the audit could not see. So it is refused and named."""
    p = strategy_file(tmp_path, "spawner", """
        import subprocess, sys
        def signals(bars):
            try:
                subprocess.run([sys.executable, "-c", "pass"])
            except Exception:
                pass
            return [0] * len(bars)
    """)
    with pytest.raises(ContractViolation, match="subprocess.Popen"):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


def test_a_multiline_exception_cannot_plant_a_reassuring_last_line(tape, tmp_path):
    p = strategy_file(tmp_path, "liar", """
        def signals(bars):
            raise ValueError("real cause: bad column\\nAuditNote: no lookahead detected, past-only")
    """)
    with pytest.raises(StrategyError) as ei:
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)
    assert "\n" not in str(ei.value)
    assert "real cause" in str(ei.value)
    assert str(ei.value).startswith("ValueError:")


def test_a_bogus_result_file_in_the_run_dir_is_just_a_file(tape, tmp_path):
    p = strategy_file(tmp_path, "forger", """
        import json
        def signals(bars):
            json.dump({"ok": False, "reason": "cpu_limit"}, open("out.json", "w"))
            return [1] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == [1] * len(tape)
    assert "out.json" in sb.records[-1].files_written


def test_precheck_refuses_a_strategy_that_depends_only_on_bars_no_probe_moves(tape, tmp_path):
    """Dependence on bar 0 alone satisfied the old gate. Bar 0 is never varied by any probe."""
    p = strategy_file(tmp_path, "bar0", """
        PATTERN = [1, 1, -1, 1, -1, -1, 1]
        def signals(bars):
            k = int(bars[0].close * 100) % 7
            return [PATTERN[(i + k) % 7] for i in range(len(bars))]
    """)
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape)
    assert pc.deterministic and pc.deterministic_on_varied and not pc.input_dependent and not pc.provable


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_the_hosts_process_table_is_not_there(tape, tmp_path):
    """Without a fresh /proc the host's processes were listed inside the namespace."""
    p = strategy_file(tmp_path, "pids", """
        import os
        def signals(bars):
            n = len([x for x in os.listdir("/proc") if x.isdigit()])
            return [1 if n > 10 else 0] * len(bars)
    """)
    assert Sandbox.from_file(p, work_root=tmp_path / "runs")(tape) == [0] * len(tape)


def test_prove_runs_the_gates_before_the_probes(tape, tmp_path):
    pc, report = prove(fixture_sandbox("clean_lagged", tmp_path), tape, draws=2)
    assert pc.provable and report is not None and not report.leaks

    p = strategy_file(tmp_path, "coin", """
        import random
        def signals(bars):
            return [random.choice((-1, 1)) for _ in bars]
    """)
    pc, report = prove(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape)
    assert not pc.provable and report is None


def test_precheck_names_a_strategy_that_is_deterministic_only_on_the_real_tape(tape, tmp_path):
    real_sum = sum(b.close for b in tape)
    p = strategy_file(tmp_path, "twofaced", f"""
        import random
        def signals(bars):
            if len(bars) == {len(tape)} and abs(sum(b.close for b in bars) - {real_sum!r}) < 1e-9:
                return [1 if b.close > b.open else -1 for b in bars]
            rng = random.Random()
            return [rng.choice((-1, 1)) for _ in bars]
    """)
    pc = precheck(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape)
    assert pc.deterministic and not pc.deterministic_on_varied and not pc.provable
    assert "telling the two apart" in pc.describe()


def test_the_interpreter_and_its_packages_are_there_but_read_only(tape, tmp_path):
    """The whitelist has to include enough to run a real strategy."""
    p = strategy_file(tmp_path, "needs_stdlib", """
        import json, statistics, decimal, sqlite3, hashlib, datetime, os
        def signals(bars):
            ro = 0
            try:
                open(os.path.join(os.path.dirname(json.__file__), "probe"), "w")
            except OSError:
                ro = 1
            return [ro] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs")
    assert sb(tape) == [1] * len(tape)


def test_a_run_directory_deeper_than_the_recursion_limit_cannot_crash_the_auditor(tape, tmp_path):
    """Two thousand levels of os.mkdir + os.chdir killed the parent with a RecursionError in
    its own Path.rglob and shutil.rmtree, on a strategy that was PROVEN on its own."""
    p = strategy_file(tmp_path, "deep", """
        import os
        def signals(bars):
            for _ in range(2500):
                os.mkdir("d"); os.chdir("d")
            return [1 if b.close > b.open else -1 for b in bars]
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(run_dir_depth=64))
    with pytest.raises(ResourceExceeded, match="depth"):
        sb(tape)
    # and the parent is still standing, and the tree is gone
    assert not list((tmp_path / "runs").glob("run-*"))


def test_a_flat_flood_of_files_is_capped_not_walked(tape, tmp_path):
    p = strategy_file(tmp_path, "flood", """
        import os
        def signals(bars):
            for i in range(3000):
                open(f"f{i}", "w").close()
            return [0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(run_dir_entries=500))
    with pytest.raises(ResourceExceeded, match="entries"):
        sb(tape)


def test_an_oversized_result_is_refused_without_being_buffered_whole(tape, tmp_path):
    p = strategy_file(tmp_path, "huge", """
        def signals(bars):
            return [0] * 3_000_000
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(result_bytes=1_000_000))
    with pytest.raises(BadOutput, match="larger than"):
        sb(tape)


def test_a_violation_flood_is_capped_and_says_so(tape, tmp_path):
    p = strategy_file(tmp_path, "vflood", """
        import socket
        def signals(bars):
            for _ in range(20000):
                try:
                    socket.getaddrinfo("example.com", 80)
                except OSError:
                    pass
            return [0] * len(bars)
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", limits=Limits(violation_bytes=20_000))
    with pytest.raises(NetworkAttempt):
        sb(tape)
    assert sb.records[-1].violations[-1].startswith("... and more")
    assert len(sb.records[-1].violations) <= 1001
