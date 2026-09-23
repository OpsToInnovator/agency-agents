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


@pytest.mark.parametrize("iso", ["namespace", "plain"])
@pytest.mark.parametrize("how,named", [
    ("mp", "fork_exec"),
    ("forkpty", "os.forkpty"),
])
def test_starting_a_process_by_any_stdlib_route_is_refused_and_named(how, named, iso, tape, tmp_path):
    """A seventh red team: multiprocessing's spawn context starts a process through
    _posixsubprocess.fork_exec, which raises no audit event, and os.forkpty raised one the
    hook did not list. Both started a process with an empty record; in the plain tier a
    forkpty child held the run open for its own lifetime."""
    if iso == "namespace" and not NAMESPACED:
        pytest.skip("namespace isolation not available on this host")
    body = {"mp": """
        import multiprocessing as mp
        def _noop():
            pass
        def signals(bars):
            try:
                p = mp.get_context("spawn").Process(target=_noop)
                p.start()
                p.join()
            except Exception:
                pass
            return [0] * len(bars)
    """, "forkpty": """
        import os, time
        def signals(bars):
            try:
                pid, fd = os.forkpty()
                if pid == 0:
                    time.sleep(8)
                    os._exit(0)
            except Exception:
                pass
            return [0] * len(bars)
    """}[how]
    p = strategy_file(tmp_path, f"spawn_{how}", body)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", isolation=iso)
    with pytest.raises(ContractViolation, match=named):
        sb(tape)
    assert sb.records[-1].duration_s < 6, "a spawned child held the run open"


def test_from_file_leaves_no_copy_of_the_source_behind(tape, tmp_path):
    """from_file copied the strategy into a temp directory of its own, then staged it again;
    close() removed the stage and never the first copy, so every audit left the customer's
    source in /tmp. The first copy is gone as soon as the stage exists."""
    import tempfile
    tmp = Path(tempfile.gettempdir())
    before = set(tmp.glob("edgecheck-strategy-*"))
    sb = fixture_sandbox("clean_lagged", tmp_path)
    assert set(tmp.glob("edgecheck-strategy-*")) == before
    assert sb(tape) == importlib.import_module("edgecheck.fixtures.strategies.clean_lagged").signals(tape)
    sb.close()
    assert set(tmp.glob("edgecheck-strategy-*")) == before


@pytest.mark.parametrize("iso", ["namespace", "plain"])
@pytest.mark.parametrize("how", ["reimport", "ctypes"])
def test_the_kernel_refuses_a_new_process_by_any_route(how, iso, tape, tmp_path):
    """An eighth red team popped _posixsubprocess from sys.modules, imported a fresh copy with
    the real fork_exec, and started a process with nothing recorded: no audit event fires for
    any step of it. A seccomp filter installed before the strategy is imported now makes the
    kernel refuse every new process -- by that route, and by ctypes, which was outside the
    model until now."""
    if iso == "namespace" and not NAMESPACED:
        pytest.skip("namespace isolation not available on this host")
    body = {"reimport": """
        import importlib, sys, multiprocessing.util
        def signals(bars):
            sys.modules.pop("_posixsubprocess", None)
            importlib.import_module("_posixsubprocess")
            try:
                multiprocessing.util.spawnv_passfds(b"/bin/sh", [b"/bin/sh", b"-c", b"echo ran > MARKER"], ())
                started = True
            except OSError:
                started = False
            return [1 if started else -1] * len(bars)
    """, "ctypes": """
        import ctypes, os
        def signals(bars):
            libc = ctypes.CDLL(None, use_errno=True)
            pid = libc.fork()
            if pid == 0:
                os._exit(0)
            return [1 if pid > 0 else -1] * len(bars)
    """}[how]
    p = strategy_file(tmp_path, f"spawn_{how}", body)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", isolation=iso)
    assert sb(tape) == [-1] * len(tape), "a process was started"
    assert sb.records[-1].spawn_lock
    assert "MARKER" not in sb.records[-1].files_written


@pytest.mark.parametrize("iso", ["namespace", "plain"])
def test_the_spawn_lock_leaves_threads_alone(iso, tape, tmp_path):
    """Threads are clones too. The filter lets a clone with CLONE_THREAD through, and answers
    clone3 with ENOSYS so libc falls back to clone -- or every threaded strategy would die."""
    if iso == "namespace" and not NAMESPACED:
        pytest.skip("namespace isolation not available on this host")
    p = strategy_file(tmp_path, "threaded", """
        import threading
        def signals(bars):
            out = [0] * len(bars)
            def work(lo, hi):
                for i in range(max(lo, 1), hi):
                    out[i] = 1 if bars[i - 1].close > bars[i - 1].open else -1
            ts = [threading.Thread(target=work, args=(j * 40, (j + 1) * 40)) for j in range(3)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            return out
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs", isolation=iso)
    expected = [0] + [1 if b.close > b.open else -1 for b in tape[:-1]]
    assert sb(tape) == expected and sb.records[-1].spawn_lock


def test_a_proof_is_not_withheld_because_the_gate_could_not_move_the_strategy(tmp_path):
    """The gate replaced bars from 4 on with a fresh continuation and, when the output did not
    move, blocked the probes with "nothing can be proved about it" -- about a strategy the
    probes convicted. A strategy the continuation does not move is now probed for a proof
    only: a proof is reported, a clean result withheld."""
    from edgecheck.causality import continuation
    from edgecheck.sandbox import GATE_BOUNDARY
    tape = bars(120, gap_prob=0.3, late_prob=0.1)
    varied = continuation(tape, GATE_BOUNDARY, seed=7)
    up = lambda b: b.close > b.open
    at = next(i for i in range(60, 110) if up(varied[i]) == up(tape[i]))
    p = strategy_file(tmp_path, "one_bar", f"""
        def signals(bars):
            out = [0] * len(bars)
            if len(bars) > {at}:
                out[{at}] = 1 if bars[{at}].close > bars[{at}].open else -1
            return out
    """)
    pc, report = prove(Sandbox.from_file(p, work_root=tmp_path / "runs"), tape, probes="every_bar", seed=1)
    assert not pc.input_dependent and pc.proof_only and not pc.provable
    assert "PROOF ONLY" in pc.describe() and "nothing can be proved" not in pc.describe()
    assert report is not None and report.leaks and report.worst_horizon == 0

    q = strategy_file(tmp_path, "constant", """
        def signals(bars):
            return [1] * len(bars)
    """)
    pc, report = prove(Sandbox.from_file(q, work_root=tmp_path / "runs2"), tape, probes="every_bar", seed=1)
    assert pc.proof_only and report is None


def test_the_error_that_ended_a_run_survives_a_long_log(tape, tmp_path):
    """A run that dies without writing a result is reported from the tail of its stderr.
    Stderr kept its FIRST 64 KiB, so a long log pushed the real final error out of the
    report. It keeps the last 64 KiB now."""
    p = strategy_file(tmp_path, "chatty", """
        import os, sys
        def signals(bars):
            for i in range(20000):
                print(f"progress line {i:06d} " + "x" * 40, file=sys.stderr)
            print("FATAL: the real cause is here", file=sys.stderr)
            sys.stderr.flush()
            os._exit(1)
    """)
    with pytest.raises(StrategyError, match="the real cause is here") as ei:
        Sandbox.from_file(p, work_root=tmp_path / "runs", isolation="plain")(tape)
    assert "the strategy's own stderr ended" in str(ei.value)


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
    assert "distinguishes real data from varied data" in pc.describe()


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


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_many_files_cannot_fill_the_host_disk(tape, tmp_path):
    """RLIMIT_FSIZE caps one file. Inside the namespace the run directory is a tmpfs of a
    fixed size, so the strategy runs out of space and the host does not."""
    p = strategy_file(tmp_path, "filler", """
        def signals(bars):
            written = 0
            try:
                for i in range(200):
                    with open(f"blob{i}", "wb") as fh:
                        fh.write(b"x" * (4 * 1024 * 1024))
                    written += 1
            except OSError:
                pass
            return [1 if written < 200 else -1] * len(bars)     # 1 = ran out of space
    """)
    sb = Sandbox.from_file(p, work_root=tmp_path / "runs",
                           limits=Limits(fsize_bytes=8 * 1024 ** 2, run_dir_bytes=32 * 1024 ** 2))
    assert sb(tape) == [1] * len(tape)
    assert not any(f.startswith("blob") for f in os.listdir(tmp_path / "runs")) 
    host_bytes = sum(f.stat().st_size for f in (tmp_path / "runs").rglob("*") if f.is_file())
    assert host_bytes < 8 * 1024 ** 2
    assert any(f.startswith("blob") for f in sb.records[-1].files_written)


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_the_hostname_is_not_the_hosts(tape, tmp_path):
    """Without --uts the child shared the host's UTS namespace, and an ordinary write to
    /proc/sys/kernel/hostname from the unmapped user changed the host's name for good."""
    import socket
    before = socket.gethostname()
    p = strategy_file(tmp_path, "hostname", """
        import os
        def signals(bars):
            try:
                with open("/proc/sys/kernel/hostname", "w") as fh:
                    fh.write("edgecheck-pwned")
                wrote = 1
            except OSError:
                wrote = 0
            return [wrote] * len(bars)
    """)
    Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)
    assert socket.gethostname() == before


def test_a_deeply_nested_result_on_the_pipe_is_a_strategy_error_not_a_crash(tape, tmp_path):
    """Out of the stated model (the child must find the result descriptor), but the fix is
    one line and the invariant is that nothing the child does crashes the parent."""
    p = strategy_file(tmp_path, "nested", """
        import os, sys
        def signals(bars):
            os.write(int(sys.argv[4]), b"[" * 4000)
            return [0] * len(bars)
    """)
    with pytest.raises(StrategyError, match="malformed"):
        Sandbox.from_file(p, work_root=tmp_path / "runs")(tape)


@pytest.mark.skipif(not NAMESPACED, reason="namespace isolation not available on this host")
def test_host_sysctls_cannot_be_written(tape, tmp_path):
    """A fresh /proc is a writable window onto host-wide sysctls. A fifth red team set
    vm.overcommit_memory for the whole host from inside; /proc/sys is read-only now."""
    before = open("/proc/sys/vm/overcommit_ratio").read().strip()
    p = strategy_file(tmp_path, "sysctl", """
        def signals(bars):
            wrote = 0
            for f, v in (("/proc/sys/vm/overcommit_ratio", "77"), ("/proc/sys/kernel/hostname", "x")):
                try:
                    open(f, "w").write(v); wrote += 1
                except OSError:
                    pass
            return [wrote] * len(bars)
    """)
    assert Sandbox.from_file(p, work_root=tmp_path / "runs")(tape) == [0] * len(tape)
    assert open("/proc/sys/vm/overcommit_ratio").read().strip() == before


def test_close_removes_the_staged_source_and_an_owned_work_root(tape):
    sb = Sandbox.from_file(FIX / "clean_lagged.py")          # default work root: created by the sandbox
    stage, root = sb.strategy_dir, sb.work_root
    sb(tape)
    assert stage.exists() and root.exists()
    sb.close()
    assert not stage.exists() and not root.exists()


def test_a_supplied_work_root_is_left_in_place_on_close(tape, tmp_path):
    root = tmp_path / "runs"
    with Sandbox.from_file(FIX / "clean_lagged.py", work_root=root) as sb:
        sb(tape)
        stage = sb.strategy_dir
    assert root.exists() and not stage.exists()


def test_the_precheck_never_promises_what_a_failed_gate_cancelled():
    """A ninth red team: a nondeterministic strategy the continuation did not move got "the
    probes still run, for a proof only" printed above UNPROVABLE, and prove() ran no probe; a
    strategy nondeterministic on both tapes got "while the real tape reproduced 3 times"."""
    from edgecheck.sandbox import Precheck
    base = dict(isolation="plain", visible=(), files_written=(), first_boundary=4)
    flaky = Precheck(deterministic=False, deterministic_on_varied=False, input_dependent=False, **base)
    text = flaky.describe()
    assert not flaky.proof_only and "UNPROVABLE" in text
    assert "for a proof only" not in text and "the probes do not run" in text
    assert "while the real tape reproduced" not in text and "DIFFERENT output, as on the real tape" in text
    twofaced = Precheck(deterministic=True, deterministic_on_varied=False, input_dependent=False, **base)
    assert "while the real tape reproduced 3 times" in twofaced.describe()
    unmoved = Precheck(deterministic=True, deterministic_on_varied=True, input_dependent=False, **base)
    assert unmoved.proof_only and "the probes still run, for a proof only" in unmoved.describe()


def test_the_hash_seed_line_does_not_promise_set_order_it_cannot_pin(tape, tmp_path):
    """PYTHONHASHSEED pins the hash of strings and numbers only; a set of plain objects
    iterates in address order, and addresses differ between runs."""
    pc = precheck(fixture_sandbox("clean_lagged", tmp_path), tape)
    line = next(l for l in pc.describe().splitlines() if l.startswith("hash seed"))
    assert "hashed by identity still follows memory addresses" in line
    assert "dict and set order cannot differ" not in line


def test_the_limits_are_reported_as_they_were_set(tape, tmp_path):
    """A 1.5s wall clock was reported as 2s and a 0.4s one as 0s; a result of exactly the cap
    was refused as larger than it; a strategy that raised SIGXCPU itself after half its CPU
    budget was told it had hit the limit."""
    nap = strategy_file(tmp_path, "nap", """
        import time
        def signals(bars):
            time.sleep(5)
            return [0] * len(bars)
    """)
    with pytest.raises(Timeout, match=r"exceeded 1\.5s wall clock"):
        Sandbox.from_file(nap, work_root=tmp_path / "r1", limits=Limits(wall_s=1.5))(tape)

    const = strategy_file(tmp_path, "const", """
        def signals(bars):
            return [0] * len(bars)
    """)
    probe = Sandbox.from_file(const, work_root=tmp_path / "r2")
    probe(tape)
    import json
    size = len(json.dumps({"ok": True, "signals": [0] * len(tape), "files_written": [],
                           "run_dir_over_limit": False, "spawn_lock": probe.records[-1].spawn_lock}).encode())
    assert Sandbox.from_file(const, work_root=tmp_path / "r3", limits=Limits(result_bytes=size))(tape) == [0] * len(tape)
    with pytest.raises(BadOutput, match="larger than"):
        Sandbox.from_file(const, work_root=tmp_path / "r4", limits=Limits(result_bytes=size - 1))(tape)

    forger = strategy_file(tmp_path, "xcpu", """
        import os, signal, time
        def signals(bars):
            t = time.process_time()
            while time.process_time() - t < 1.2:
                pass
            os.kill(os.getpid(), signal.SIGXCPU)
            return [0] * len(bars)
    """)
    # a signal the strategy sends itself is not the limit: the run goes on to its output
    assert Sandbox.from_file(forger, work_root=tmp_path / "r5", limits=Limits(cpu_s=2, wall_s=10))(tape) == [0] * len(tape)


def test_a_self_raised_cpu_signal_short_of_the_limit_is_not_the_limit(tape, tmp_path):
    """The kernel sends SIGXCPU only once the limit is reached. A strategy that raised it
    itself at 1.8s of a 2s limit was told it had hit the limit."""
    forger = strategy_file(tmp_path, "xcpu18", """
        import os, signal, time
        def signals(bars):
            t = time.process_time()
            while time.process_time() - t < 1.7:
                pass
            os.kill(os.getpid(), signal.SIGXCPU)
            return [0] * len(bars)
    """)
    assert Sandbox.from_file(forger, work_root=tmp_path / "r", limits=Limits(cpu_s=2, wall_s=10))(tape) == [0] * len(tape)


def test_limits_are_validated_and_a_fractional_cpu_limit_works(tape, tmp_path):
    """A fractional cpu_s reached setrlimit in the child's preexec hook, failed there as a bare
    SubprocessError and leaked six pipe ends per call."""
    with pytest.raises(ValueError):
        Limits(cpu_s=0)
    with pytest.raises(ValueError):
        Limits(wall_s=-1)
    before = len(os.listdir("/proc/self/fd"))
    sb = fixture_sandbox("clean_lagged", tmp_path, limits=Limits(cpu_s=1.5))
    for _ in range(3):
        sb(tape)
    assert len(os.listdir("/proc/self/fd")) <= before + 1


# -- round twelve ---------------------------------------------------------------------------

def test_limits_on_counts_and_sizes_are_whole_numbers():
    """``violation_bytes=1e6`` killed the drain thread on a float slice, and the network record it
    carried went with it: a strategy that dialled out came back with no violation on record. A
    float memory or file limit failed in the child's preexec hook; a float entry count failed in
    the child's own argument parsing. Each is refused where it is set."""
    for name, value in (("violation_bytes", 1e6), ("memory_bytes", 2.5e9), ("nofile", 256.0),
                        ("run_dir_entries", 20000.0), ("run_dir_bytes", 2.5e8), ("result_bytes", 1e6)):
        with pytest.raises(ValueError, match=f"Limits.{name} must be a positive whole number"):
            Limits(**{name: value})
    for name, value in (("wall_s", float("inf")), ("cpu_s", float("nan"))):
        with pytest.raises(ValueError, match=f"Limits.{name}"):
            Limits(**{name: value})
    assert Limits(cpu_s=1.5, wall_s=2.5).cpu_enforced == 2


def test_a_drain_hands_over_what_it_read_whatever_its_cap():
    from edgecheck.sandbox import _drain
    r, w = os.pipe()
    os.write(w, b"x" * 100)
    os.close(w)
    sink: list[bytes] = []
    _drain(r, sink, 10.0).join(timeout=5)
    assert sink == [b"x" * 10, b"1"]


def _spin_to(seconds: float, then: str) -> str:
    return f"""
        import ctypes, os, resource, signal
        def signals(bars):
            x = 0
            while True:
                for _ in range(20000):
                    x += 1
                ru = resource.getrusage(resource.RUSAGE_SELF)
                if ru.ru_utime + ru.ru_stime >= {seconds}:
                    {then}
    """


_FORGE = "os.kill(os.getpid(), signal.SIGXCPU); return [0] * len(bars)"


def _forged_is_not_the_limit(path, tape, root, limits):
    """A strategy that sent itself SIGXCPU and returned gets its output; within a scheduler tick or
    two of the limit the kernel's own signal can come first, and that one is the limit."""
    try:
        assert Sandbox.from_file(path, work_root=root, limits=limits)(tape) == [0] * len(tape)
    except ResourceExceeded as e:
        assert "the kernel's signal" in str(e)


def test_a_cpu_claim_is_checked_against_the_limit_the_kernel_enforces(tape, tmp_path):
    """The kernel counts CPU in whole seconds, so a 1.5s limit fires at 2s, and the parent's count
    includes the namespace setup. A strategy that raised SIGXCPU itself at 1.6s of a 1.5s limit,
    or at 1.95s of 2s where the setup carried the parent's count past 2, was reported as having
    hit the limit (a twelfth red team). The strategy's own CPU, reported with its claim, decides."""
    for cpu_s, at in ((1.5, 1.6), (2, 1.95)):
        p = strategy_file(tmp_path, f"xcpu{int(at * 100)}", _spin_to(at, _FORGE))
        _forged_is_not_the_limit(p, tape, tmp_path / f"r{int(at * 100)}", Limits(cpu_s=cpu_s, wall_s=15))
    spin = strategy_file(tmp_path, "spin15", """
        def signals(bars):
            while True:
                pass
    """)
    with pytest.raises(ResourceExceeded, match="enforces in whole seconds, at 2s"):
        Sandbox.from_file(spin, work_root=tmp_path / "rs", limits=Limits(cpu_s=1.5, wall_s=15))(tape)


def test_a_crash_near_the_cpu_limit_is_a_crash(tape, tmp_path):
    """A strategy that segfaulted at 1.95s of a 2s limit was reported as having used the limit:
    no output, and the parent's count -- setup included -- read past it. What killed it decides."""
    p = strategy_file(tmp_path, "segv", _spin_to(1.95, "ctypes.string_at(0)"))
    with pytest.raises(StrategyError, match="killed by SIGSEGV"):
        Sandbox.from_file(p, work_root=tmp_path / "r", limits=Limits(cpu_s=2, wall_s=15))(tape)


# -- round thirteen -------------------------------------------------------------------------

def test_limits_setrlimit_would_refuse_are_refused_by_name():
    """nofile past the kernel's nr_open, or a CPU limit past what setrlimit takes, reached the
    child's preexec hook and died there as a bare SubprocessError naming nothing."""
    with pytest.raises(ValueError, match="Limits.nofile"):
        Limits(nofile=10 ** 7)
    with pytest.raises(ValueError, match="cpu_s"):
        Limits(cpu_s=2 ** 63)
    assert "1.0000001s" in Limits(cpu_s=1.0000001).cpu_words()


def test_a_limit_is_printed_as_it_was_set(tape, tmp_path):
    nap = strategy_file(tmp_path, "nap13", """
        import time
        def signals(bars):
            time.sleep(5)
            return [0] * len(bars)
    """)
    with pytest.raises(Timeout, match=r"exceeded 1\.0000001s wall clock"):
        Sandbox.from_file(nap, work_root=tmp_path / "r", limits=Limits(wall_s=1.0000001))(tape)


def test_a_signal_raised_just_short_of_the_limit_is_not_the_limit(tape, tmp_path):
    """A SIGXCPU the strategy raised itself at 1.99s of a 2s limit was reported as the limit, with
    '1.99s used' printed beside it: the margin under the limit admitted only forgeries."""
    p = strategy_file(tmp_path, "xcpu199", _spin_to(1.99, _FORGE))
    _forged_is_not_the_limit(p, tape, tmp_path / "r", Limits(cpu_s=2, wall_s=15))


def test_an_exit_past_the_soft_limit_is_not_called_a_kill(tape, tmp_path):
    """In the namespace tier a SIGKILL and an exit with status 1 both come back as status 1. A
    strategy that ignored SIGXCPU and exited with status 1 just short of the hard limit was told
    it had been killed there."""
    p = strategy_file(tmp_path, "exit1", _spin_to(4.97, "os._exit(1)").replace(
        "def signals(bars):", "signal.signal(signal.SIGXCPU, signal.SIG_IGN)\n        def signals(bars):"))
    with pytest.raises((ResourceExceeded, StrategyError)) as e:
        Sandbox.from_file(p, work_root=tmp_path / "r", limits=Limits(cpu_s=2, wall_s=20))(tape)
    assert "killed at the hard CPU limit" not in str(e.value)


# -- round fourteen -------------------------------------------------------------------------

def test_the_kernels_cpu_signal_is_told_from_a_forged_one_by_its_sender(tape, tmp_path):
    """Timing could not tell them apart: a genuine SIGXCPU came 15ms before the process's own clock
    reached the limit and was reported as a forged claim. The child now reads who sent it."""
    spin = strategy_file(tmp_path, "spin14", """
        def signals(bars):
            while True:
                pass
    """)
    for i in range(3):
        with pytest.raises(ResourceExceeded, match="the kernel's signal"):
            Sandbox.from_file(spin, work_root=tmp_path / f"r{i}", limits=Limits(cpu_s=1, wall_s=15))(tape)
    with pytest.raises(ValueError, match="cpu_s"):
        Limits(cpu_s=18446744074)
    assert "1.0000000000000002" in Limits(cpu_s=1 + 2 ** -52).cpu_words()


def test_limits_bind_the_strategy_not_the_sandboxs_setup(tape, tmp_path):
    """A file-size cap smaller than the tape killed the namespace setup's copy of it, and a memory
    cap too small for the launcher killed the launcher; both were reported as the strategy's own
    failure, quoting the launcher's stderr as the strategy's."""
    ok = strategy_file(tmp_path, "honest14", """
        def signals(bars):
            return [0] * len(bars)
    """)
    assert Sandbox.from_file(ok, work_root=tmp_path / "f", limits=Limits(fsize_bytes=512))(tape) == [0] * len(tape)
    # a memory cap below what the interpreter already holds binds what the strategy allocates next
    try:
        assert Sandbox.from_file(ok, work_root=tmp_path / "m", limits=Limits(memory_bytes=1024 ** 2))(tape) == [0] * len(tape)
    except ResourceExceeded as e:
        assert "MemoryError" in str(e)
    hog = strategy_file(tmp_path, "hog14", """
        def signals(bars):
            block = bytearray(64 * 1024 ** 2)
            return [0] * len(bars)
    """)
    with pytest.raises(ResourceExceeded, match="MemoryError"):
        Sandbox.from_file(hog, work_root=tmp_path / "h", limits=Limits(memory_bytes=1024 ** 2))(tape)


def test_a_runs_cpu_is_its_own_not_a_neighbours(tape, tmp_path):
    """The run's CPU was a difference of this process's children's totals, so a run was billed for
    whatever another Sandbox reaped meanwhile."""
    import threading
    burn = strategy_file(tmp_path, "burn14", """
        import time
        def signals(bars):
            t = time.process_time()
            while time.process_time() - t < 2.5:
                pass
            return [0] * len(bars)
    """)
    nap = strategy_file(tmp_path, "nap14", """
        import time
        def signals(bars):
            time.sleep(3.5)
            return [0] * len(bars)
    """)
    a = Sandbox.from_file(burn, work_root=tmp_path / "a", limits=Limits(cpu_s=10, wall_s=20))
    b = Sandbox.from_file(nap, work_root=tmp_path / "b", limits=Limits(cpu_s=10, wall_s=20))
    th = threading.Thread(target=lambda: a(tape))
    th.start()
    b(tape)
    th.join()
    assert b.records[-1].cpu_s < 1.0, b.records[-1].cpu_s


def test_the_network_record_names_the_host_and_a_dotted_file_name_runs(tape, tmp_path):
    p = strategy_file(tmp_path, "dns14", """
        import socket
        def signals(bars):
            try:
                socket.getaddrinfo("example.invalid", 443)
            except OSError:
                pass
            return [0] * len(bars)
    """)
    with pytest.raises(NetworkAttempt, match="example.invalid"):
        Sandbox.from_file(p, work_root=tmp_path / "n")(tape)
    dotted = tmp_path / "strategy_v1.2.py"
    dotted.write_text("def signals(bars):\n    return [0] * len(bars)\n")
    assert Sandbox.from_file(dotted, work_root=tmp_path / "d")(tape) == [0] * len(tape)


def test_a_signal_the_strategy_sends_itself_gives_the_same_answer_every_run(tape, tmp_path):
    """Ending the run on a SIGXCPU the strategy sent itself raced its own return: one run got the
    output, the next an error, and with both writing, a malformed result. Only the kernel's signal
    ends a run now, and a strategy that keeps sending itself the signal still meets the limit."""
    forger = strategy_file(tmp_path, "xcpu15", """
        import os, signal
        def signals(bars):
            os.kill(os.getpid(), signal.SIGXCPU)
            return [0] * len(bars)
    """)
    for i in range(6):
        assert Sandbox.from_file(forger, work_root=tmp_path / f"r{i}", limits=Limits(cpu_s=2, wall_s=10))(tape) == [0] * len(tape)
    spam = strategy_file(tmp_path, "spam15", """
        import os, signal
        def signals(bars):
            while True:
                os.kill(os.getpid(), signal.SIGXCPU)
    """)
    with pytest.raises(ResourceExceeded):
        Sandbox.from_file(spam, work_root=tmp_path / "s", limits=Limits(cpu_s=1, wall_s=15))(tape)
