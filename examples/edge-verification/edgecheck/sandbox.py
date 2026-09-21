"""Run a customer's strategy where it cannot hurt anything, and where we own its inputs.

The second half of that sentence is the one that matters for the product. Every probe in
``causality`` works by changing the data and watching the output. A strategy that reads its
prices from a file it brought with it, or fetches them, is unmoved by anything we do -- and
"nothing moved" is what a clean strategy looks like. Without control of the inputs, the
detector would hand a clean report to exactly the code it cannot see into. So before any
probe runs, ``precheck`` establishes two things: the same tape twice gives the same answer
(else the strategy is nondeterministic and no divergence means anything), and a different
tape gives a different answer (else the output is not a function of the data we control,
and the strategy is unprovable, whatever the reason).

Threat model, stated rather than assumed: the customer is paying to have their own code
audited. The sandbox is for accidents -- a vendor SDK phoning home, a runaway allocation,
an infinite loop, a feature cache written somewhere it will be read back on the next run --
not for a determined attacker. What is actually enforced, in two tiers:

    namespace   (default when ``unshare`` can create user, mount and network namespaces)
                Fresh interpreter per run. No network at the kernel level: connect() and
                DNS both fail. tmpfs mounted over the home directory, /root, /tmp, /var/tmp
                and /dev/shm, so nothing under them is visible and nothing written there
                survives the process. Scrubbed environment. rlimits on CPU, memory,
                processes, file size, open files. Wall-clock kill.

    plain       Everything above except the namespaces: no kernel network block, no hidden
                paths. The socket ban in the child still records and refuses attempts, and
                ``ctypes`` walks straight past it. This tier stops accidents and runaway
                loops. It is a correctness boundary, not a security boundary, and the
                report says which tier ran.

In both tiers the environment is built from scratch, never inherited. That is the layer
that keeps API keys out of the strategy's reach, and it does not depend on namespaces.

Every run is one fresh process in one fresh directory with a fresh copy of the strategy.
That costs a few tens of milliseconds and buys something the probes need: no state can
carry from one run to the next. A module-level cache keyed on tape length, a pickle of
computed features, a lookup table built on the first call -- any of those would let a
poisoned run read what the baseline run computed, and launder the future through it.
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

__all__ = ["Limits", "Sandbox", "SandboxError", "StrategyError", "Timeout", "ResourceExceeded",
           "NetworkAttempt", "BadOutput", "Precheck", "precheck", "detect_isolation", "Isolation"]

Isolation = Literal["namespace", "plain"]
CHILD = Path(__file__).with_name("_child.py")
HIDDEN_CANDIDATES = ("/root", "/tmp", "/var/tmp", "/dev/shm", "/etc/arbbot")


@dataclass(frozen=True, slots=True)
class Limits:
    cpu_s: int = 60
    wall_s: float = 120.0
    memory_bytes: int = 2 * 1024 ** 3
    nproc: int = 64
    fsize_bytes: int = 256 * 1024 ** 2
    nofile: int = 256


class SandboxError(Exception):
    """Base for everything the sandbox can refuse."""


class StrategyError(SandboxError):
    """The strategy raised. The traceback tail is the message."""


class Timeout(SandboxError):
    pass


class ResourceExceeded(SandboxError):
    pass


class NetworkAttempt(SandboxError):
    pass


class BadOutput(SandboxError):
    """The strategy returned something that is not one position per bar."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What one run looked like from outside. Kept on the sandbox for the report."""

    duration_s: float
    returncode: int
    cpu_s: float
    isolation: str
    hidden: tuple[str, ...]
    files_written: tuple[str, ...]
    violations: tuple[str, ...]


_ISOLATION_CACHE: dict[str, Isolation] = {}


def detect_isolation() -> Isolation:
    """Can this host create the namespaces? Probed once, with a real command, not a guess."""
    if "v" in _ISOLATION_CACHE:
        return _ISOLATION_CACHE["v"]
    unshare = shutil.which("unshare")
    ok = False
    if unshare:
        try:
            r = subprocess.run([unshare, "--user", "--map-root-user", "--mount", "--net", "--",
                                "sh", "-c", "mount -t tmpfs -o size=1m tmpfs /dev/shm 2>/dev/null; true"],
                               capture_output=True, timeout=10)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _ISOLATION_CACHE["v"] = "namespace" if ok else "plain"
    return _ISOLATION_CACHE["v"]


def _scrubbed_env() -> dict[str, str]:
    """Built from nothing. Whatever the parent had -- keys, tokens, proxies -- stays with the parent."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "LANG": "C.UTF-8",
    }


def _rlimit_installer(limits: Limits):
    def install() -> None:
        # Soft limit first: SIGXCPU, which the child catches to record the cause. Hard
        # limit a few seconds on: SIGKILL, for a strategy that ignores the first.
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s + 3))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.nproc, limits.nproc))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.fsize_bytes, limits.fsize_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limits.nofile, limits.nofile))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return install


# Executed by sh inside the namespaces. Positional arguments only -- nothing is interpolated
# into this string, so nothing a caller passes can become shell.
_NS_SCRIPT = r'''
set -e
py="$1"; child="$2"; run="$3"; entry="$4"; func="$5"; shift 5
for p in "$@"; do mount -t tmpfs -o nodev,nosuid,size=64m tmpfs "$p"; done
cd "$run"
exec "$py" -s -B "$child" "$run" "$entry" "$func"
'''


class Sandbox:
    """A callable strategy: ``sandbox(tape) -> list[int]``, each call one fresh process.

    Plugs straight into ``check_causality`` in place of an in-process function.
    """

    def __init__(self, strategy_dir: Path | str, *, entry: str = "strategy", func: str = "signals",
                 limits: Limits = Limits(), isolation: Isolation | None = None,
                 work_root: Path | str | None = None) -> None:
        self.strategy_dir = Path(strategy_dir).resolve()
        if not self.strategy_dir.is_dir():
            raise FileNotFoundError(self.strategy_dir)
        self.entry, self.func, self.limits = entry, func, limits
        self.isolation: Isolation = isolation or detect_isolation()
        self.work_root = Path(work_root).resolve() if work_root else Path(tempfile.mkdtemp(prefix="edgecheck-"))
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.records: list[RunRecord] = []

    @classmethod
    def from_file(cls, path: Path | str, **kw: Any) -> "Sandbox":
        """A single-file strategy: copied into its own directory, imported by its stem."""
        src = Path(path).resolve()
        d = Path(tempfile.mkdtemp(prefix="edgecheck-strategy-"))
        shutil.copy2(src, d / src.name)
        return cls(d, entry=src.stem, **kw)

    # -- what gets hidden, and what must not be ------------------------------------------

    def _hidden_paths(self) -> tuple[str, ...]:
        """Paths to cover with tmpfs. Never one that holds the interpreter, the child, or the run."""
        keep_visible = [Path(sys.executable).resolve(), CHILD.resolve(), self.work_root]
        candidates = list(HIDDEN_CANDIDATES)
        home = os.environ.get("HOME")
        if home:
            candidates.append(home)
        out: list[str] = []
        for c in candidates:
            p = Path(c)
            if not p.is_dir():
                continue
            if any(k == p or p in k.parents for k in keep_visible):
                continue
            if any(o == str(p) or Path(o) in p.parents for o in out):
                continue
            out.append(str(p))
        return tuple(out)

    # -- one run ------------------------------------------------------------------------------

    def __call__(self, tape: Sequence[Any]) -> list[int]:
        run = Path(tempfile.mkdtemp(prefix="run-", dir=self.work_root))
        shutil.copytree(self.strategy_dir, run / "strategy",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with (run / "tape.jsonl").open("w", encoding="utf-8") as fh:
            for b in tape:
                fh.write(json.dumps({"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
                                     "close": b.close, "volume": b.volume}) + "\n")
        before = self._snapshot(run)

        hidden: tuple[str, ...] = ()
        if self.isolation == "namespace":
            hidden = self._hidden_paths()
            cmd = [shutil.which("unshare") or "unshare", "--user", "--map-root-user", "--mount",
                   "--net", "--pid", "--fork", "--", "sh", "-c", _NS_SCRIPT, "sh",
                   sys.executable, str(CHILD), str(run), self.entry, self.func, *hidden]
        else:
            cmd = [sys.executable, "-s", "-B", str(CHILD), str(run), self.entry, self.func]

        t0 = time.monotonic()
        cpu0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        proc = subprocess.Popen(cmd, cwd=run, env=_scrubbed_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=_rlimit_installer(self.limits))
        try:
            _, err = proc.communicate(timeout=self.limits.wall_s)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            self._record(run, t0, -9, hidden, before, 0.0)
            raise Timeout(f"strategy exceeded {self.limits.wall_s:.0f}s wall clock") from None
        cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_used = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)
        rec = self._record(run, t0, proc.returncode, hidden, before, cpu_used)

        if rec.violations:
            raise NetworkAttempt("; ".join(rec.violations))
        out_path = run / "out.json"
        if not out_path.exists():
            # The exit code is not evidence here: `unshare --fork` reports 0 for a child the
            # kernel killed. The CPU the run actually consumed, as accounted by the kernel
            # to us on reaping, is.
            tail = " | ".join(err.decode("utf-8", "replace").strip().splitlines()[-3:])
            if cpu_used >= self.limits.cpu_s:
                raise ResourceExceeded(f"used {cpu_used:.1f}s CPU against a {self.limits.cpu_s}s limit")
            raise StrategyError(f"no output (rc={proc.returncode}, {cpu_used:.1f}s CPU): {tail}")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        if not payload.get("ok"):
            if payload.get("reason") == "cpu_limit":
                raise ResourceExceeded(f"hit the {self.limits.cpu_s}s CPU limit")
            tb = payload.get("traceback", "")
            if "MemoryError" in tb:
                raise ResourceExceeded("MemoryError under the sandbox memory limit")
            raise StrategyError(tb.strip().splitlines()[-1] if tb else "strategy failed")
        sig = payload["signals"]
        if not isinstance(sig, list) or len(sig) != len(tape) or \
                any(type(s) is not int or s not in (-1, 0, 1) for s in sig):
            shown = sorted({repr(s) for s in sig})[:6] if isinstance(sig, list) else type(sig).__name__
            raise BadOutput(f"expected {len(tape)} integer positions in {{-1, 0, 1}}, got "
                            f"{len(sig) if isinstance(sig, list) else '?'} with values {shown}")
        return sig

    # -- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _snapshot(run: Path) -> set[str]:
        return {str(p.relative_to(run)) for p in run.rglob("*") if p.is_file()}

    def _record(self, run: Path, t0: float, rc: int, hidden: tuple[str, ...], before: set[str],
                cpu_used: float) -> RunRecord:
        written = sorted(self._snapshot(run) - before - {"out.json", "violations.jsonl"})
        violations: list[str] = []
        v = run / "violations.jsonl"
        if v.exists():
            for line in v.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    violations.append(f"{d['kind']}: {d['detail']}")
                except (ValueError, KeyError):
                    violations.append(line[:200])
        rec = RunRecord(time.monotonic() - t0, rc, cpu_used, self.isolation, hidden, tuple(written), tuple(violations))
        self.records.append(rec)
        return rec

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# -- the gates that must pass before a probe means anything -------------------------------------

@dataclass(frozen=True, slots=True)
class Precheck:
    deterministic: bool
    input_dependent: bool
    isolation: str
    hidden: tuple[str, ...]
    files_written: tuple[str, ...]
    hash_seed_pinned: bool = True

    @property
    def provable(self) -> bool:
        return self.deterministic and self.input_dependent

    def describe(self) -> str:
        lines = [f"isolation: {self.isolation}" + (f" (hidden: {', '.join(self.hidden)})" if self.hidden else "")]
        lines.append("same tape twice -> " + ("identical output" if self.deterministic else
                     "DIFFERENT output: the strategy is nondeterministic, so no divergence could be attributed to the data"))
        lines.append("different tape  -> " + ("different output" if self.input_dependent else
                     "IDENTICAL output: the strategy is not a function of the data we control, so nothing can be proved about it"))
        if self.files_written:
            lines.append(f"files written by the strategy during a run: {', '.join(self.files_written)} "
                         f"-- a run cannot read another run's files here, but this is what a feature cache looks like")
        lines.append("hash seed pinned to 0 for every run, so dict and set order cannot differ between them")
        lines.append("PROVABLE" if self.provable else "UNPROVABLE: no probe result would mean anything; fix the above first")
        return "\n".join(lines)


def precheck(sandbox: Sandbox, tape: Sequence[Any], other_tape: Sequence[Any]) -> Precheck:
    """The two conditions under which a probe result carries meaning. Run before any probe."""
    a = sandbox(tape)
    b = sandbox(tape)
    c = sandbox(other_tape)
    written = tuple(sorted({f for r in sandbox.records[-3:] for f in r.files_written}))
    return Precheck(deterministic=(a == b), input_dependent=(a != c), isolation=sandbox.isolation,
                    hidden=sandbox.records[-1].hidden, files_written=written)
