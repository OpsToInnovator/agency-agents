"""Run a customer's strategy where it cannot hurt anything, and where we own its inputs.

The second half of that sentence is the one that matters for the product. Every probe in
``causality`` works by changing the data and watching the output. A strategy that reads its
prices from a file it brought with it, or fetches them, is unmoved by anything we do -- and
"nothing moved" is what a clean strategy looks like. Without control of the inputs, the
detector would hand a clean report to exactly the code it cannot see into. So before any
probe runs, ``precheck`` establishes two things: the same tape twice gives the same answer
(else the strategy is nondeterministic and no divergence means anything), and changing the
bars the probes can actually reach changes the answer (else the output is not a function of
the data we control, and the strategy is unprovable, whatever the reason). ``prove`` runs
both, and refuses to probe when either fails. Run the probes without the gates and a
length-seeded coin flip will be convicted of reading the future; the conviction is even
literally true, and it is not what anyone means.

Threat model, stated rather than assumed: the customer is paying to have their own code
audited. The sandbox is for accidents -- a vendor SDK phoning home, a runaway allocation,
an infinite loop, a feature cache written somewhere it will be read back on the next run --
and for casual cheating, which a red team showed is not a theoretical category: a strategy
that leaks on the real tape and behaves when it recognises a probe, a cache keyed on data
the probes never move, a thread that rewrites the result after it was written. Each of
those is closed here. A determined author running inside the same process as the child
runner can still discover the result descriptor and forge a payload; that is outside the
model and is written down rather than implied away.

What is actually enforced, in two tiers:

    namespace   (default when ``unshare`` can create user, mount, pid and network namespaces)
                Fresh interpreter per run. No network at the kernel level: connect() and
                DNS both fail, by any route, ctypes and child processes included. tmpfs
                over the home directory, /root, /tmp, /var/tmp, /dev/shm, the working root
                and the strategy's source directory; only the run directory is bound back
                in, and the strategy's copy of itself is read-only. /proc remounted so the
                host's process table is not there to read. Scrubbed environment. rlimits on
                CPU, memory, processes, file size, open files. Wall-clock kill. Nothing
                written outside the run directory survives the process.

    plain       Everything above except the namespaces. The audit hook still records every
                in-process socket use and every attempt to spawn a process, and refuses
                them; ``ctypes`` walks past it, and so would a process it managed to start.
                The parent's environment is readable through /proc. Files written to /tmp
                or to the source directory survive, so state CAN carry between runs. This
                tier stops accidents and runaway loops. It is a correctness boundary, not a
                security boundary, and the report says which tier ran.

In both tiers the child's own environment is built from scratch, never inherited, so an
SDK that reads credentials from the environment finds none.

Nothing the child reports comes back through a file it could have written. Results and
the violation record travel over two pipes the parent owns, and the child exits the
instant the result is on the wire. Where the report classifies a failure it does so on
evidence the child cannot forge: the CPU time the kernel accounted to the run, not the
exit code (``unshare --fork`` returns 1 for a child the kernel killed, indistinguishable
from an ordinary failure) and not the child's own claim (a claimed CPU-limit death with
no CPU consumed is reported as exactly that).
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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from edgecheck.causality import Report, check_causality, continuation, default_boundaries

__all__ = ["Limits", "Sandbox", "SandboxError", "StrategyError", "Timeout", "ResourceExceeded",
           "NetworkAttempt", "ContractViolation", "BadOutput", "Precheck", "precheck", "prove",
           "detect_isolation", "Isolation", "RunRecord"]

Isolation = Literal["namespace", "plain"]
CHILD = Path(__file__).with_name("_child.py")
HIDDEN_CANDIDATES = ("/root", "/tmp", "/var/tmp", "/dev/shm", "/etc/arbbot")
SHELVES = ("/dev/shm", "/mnt", "/media")   # a fresh tmpfs goes here; the run dir is bound under it


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
    """The strategy raised. The exception type and its repr'd message are the text."""


class Timeout(SandboxError):
    pass


class ResourceExceeded(SandboxError):
    pass


class NetworkAttempt(SandboxError):
    pass


class ContractViolation(SandboxError):
    """The strategy tried to start a process. Whatever that process read, we could not see."""


class BadOutput(SandboxError):
    """The strategy returned something that is not one position per bar."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What one run looked like from outside. Kept on the sandbox for the report."""

    duration_s: float
    cpu_s: float
    returncode: int
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
            r = subprocess.run([unshare, "--user", "--map-root-user", "--mount", "--net", "--pid",
                                "--fork", "--", "sh", "-c",
                                "mount -t proc proc /proc && mount -t tmpfs -o size=1m tmpfs /dev/shm"],
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
# into this string, so nothing a caller passes can become shell. Order matters: the run
# directory is bound onto a fresh tmpfs shelf BEFORE its ancestors are covered, and the bind
# outlives the covering because it references the directory itself, not its path.
_NS_SCRIPT = r'''
set -e
py="$1"; child="$2"; run="$3"; entry="$4"; func="$5"; ofd="$6"; vfd="$7"; shelf="$8"; shift 8
mount -t proc proc /proc
mount -t tmpfs -o nodev,nosuid,size=64m tmpfs "$shelf"
mkdir "$shelf/run"
mount --bind "$run" "$shelf/run"
mount --bind "$shelf/run/strategy" "$shelf/run/strategy"
mount -o remount,bind,ro,nodev,nosuid "$shelf/run/strategy"
for p in "$@"; do
  [ "$p" = "$shelf" ] && continue
  mount -t tmpfs -o nodev,nosuid,size=64m tmpfs "$p"
done
cd "$shelf/run"
exec "$py" -s -B "$child" "$shelf/run" "$entry" "$func" "$ofd" "$vfd"
'''


def _drain(fd: int, sink: list[bytes]) -> threading.Thread:
    def go() -> None:
        chunks = []
        try:
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                chunks.append(b)
        except OSError:
            pass
        finally:
            os.close(fd)
        sink.append(b"".join(chunks))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


class Sandbox:
    """A callable strategy: ``sandbox(tape) -> list[int]``, each call one fresh process.

    Plugs straight into ``check_causality`` in place of an in-process function.
    """

    def __init__(self, strategy_dir: Path | str, *, entry: str = "strategy", func: str = "signals",
                 limits: Limits = Limits(), isolation: Isolation | None = None,
                 work_root: Path | str | None = None) -> None:
        src = Path(strategy_dir).resolve()
        if not src.is_dir():
            raise FileNotFoundError(src)
        self.source_dir = src
        self.entry, self.func, self.limits = entry, func, limits
        self.isolation: Isolation = isolation or detect_isolation()
        self.work_root = Path(work_root).resolve() if work_root else Path(tempfile.mkdtemp(prefix="edgecheck-"))
        self.work_root.mkdir(parents=True, exist_ok=True)
        # Staged once. Every run copies from here, never from the source, so a run that
        # wrote into the source directory would still not be feeding the next run.
        self.strategy_dir = Path(tempfile.mkdtemp(prefix="stage-", dir=self.work_root))
        shutil.copytree(src, self.strategy_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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
        """Paths to cover with tmpfs. Never one that holds the interpreter or the child runner.

        The working root and the source directory are candidates like any other: the run
        directory reaches the child through a bind, not through its path.
        """
        keep_visible = [Path(sys.executable).resolve(), CHILD.resolve()]
        candidates = [*HIDDEN_CANDIDATES, str(self.work_root), str(self.source_dir)]
        home = os.environ.get("HOME")
        if home:
            candidates.append(home)
        out: list[str] = []
        for c in sorted(set(candidates), key=lambda s: (len(Path(s).parts), s)):
            p = Path(c)
            if not p.is_dir():
                continue
            if any(k == p or p in k.parents for k in keep_visible):
                continue
            if any(Path(o) == p or Path(o) in p.parents for o in out):
                continue
            out.append(str(p))
        return tuple(out)

    @staticmethod
    def _shelf() -> str:
        for s in SHELVES:
            if Path(s).is_dir():
                return s
        return "/tmp"

    # -- one run ------------------------------------------------------------------------------

    def __call__(self, tape: Sequence[Any]) -> list[int]:
        run = Path(tempfile.mkdtemp(prefix="run-", dir=self.work_root))
        try:
            return self._run(run, tape)
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def _run(self, run: Path, tape: Sequence[Any]) -> list[int]:
        shutil.copytree(self.strategy_dir, run / "strategy",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with (run / "tape.jsonl").open("w", encoding="utf-8") as fh:
            for b in tape:
                fh.write(json.dumps({"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
                                     "close": b.close, "volume": b.volume}) + "\n")
        before = self._snapshot(run)

        out_r, out_w = os.pipe()
        viol_r, viol_w = os.pipe()
        err_r, err_w = os.pipe()
        hidden: tuple[str, ...] = ()
        if self.isolation == "namespace":
            hidden = self._hidden_paths()
            shelf = self._shelf()
            cmd = [shutil.which("unshare") or "unshare", "--user", "--map-root-user", "--mount",
                   "--net", "--pid", "--fork", "--", "sh", "-c", _NS_SCRIPT, "sh",
                   sys.executable, str(CHILD), str(run), self.entry, self.func,
                   str(out_w), str(viol_w), shelf, *hidden]
        else:
            cmd = [sys.executable, "-s", "-B", str(CHILD), str(run), self.entry, self.func,
                   str(out_w), str(viol_w)]

        t0 = time.monotonic()
        cpu0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        proc = subprocess.Popen(cmd, cwd=run, env=_scrubbed_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=err_w, pass_fds=(out_w, viol_w),
                                start_new_session=True, preexec_fn=_rlimit_installer(self.limits))
        for fd in (out_w, viol_w, err_w):
            os.close(fd)
        outs: list[bytes] = []
        viols: list[bytes] = []
        errs: list[bytes] = []
        drains = [_drain(out_r, outs), _drain(viol_r, viols), _drain(err_r, errs)]

        # Wait on the PROCESS, not on the pipes: a helper the strategy started could hold a
        # pipe open long after the strategy returned. Then kill the whole group regardless,
        # so nothing outlives the run in either tier.
        try:
            proc.wait(timeout=self.limits.wall_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
        self._kill(proc)
        for d in drains:
            d.join(timeout=5)
        cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_used = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)
        rec = self._record(run, t0, cpu_used, proc.returncode if proc.returncode is not None else -9,
                           hidden, before, viols[0] if viols else b"")
        if timed_out:
            raise Timeout(f"strategy exceeded {self.limits.wall_s:.0f}s wall clock")

        network = [v for v in rec.violations if v.startswith("network:")]
        spawns = [v for v in rec.violations if v.startswith("spawn:")]
        if network:
            raise NetworkAttempt("; ".join(network))
        if spawns:
            raise ContractViolation("; ".join(spawns))

        raw = outs[0] if outs else b""
        if not raw:
            # The exit code is not evidence here: `unshare --fork` reports 1 for a child the
            # kernel killed. The CPU the run actually consumed, as accounted to us by the
            # kernel on reaping, is.
            tail = " | ".join((errs[0] if errs else b"").decode("utf-8", "replace").strip().splitlines()[-3:])
            if cpu_used >= self.limits.cpu_s:
                raise ResourceExceeded(f"used {cpu_used:.1f}s CPU against a {self.limits.cpu_s}s limit")
            raise StrategyError(f"no output (rc={proc.returncode}, {cpu_used:.1f}s CPU): {tail}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError as e:
            raise StrategyError(f"malformed result from the strategy process: {e}") from None

        if not payload.get("ok"):
            if payload.get("reason") == "cpu_limit":
                # The child's word, checked against the kernel's: a claimed CPU-limit death
                # with no CPU consumed is a forged claim, and is reported as one.
                if cpu_used >= 0.5 * self.limits.cpu_s:
                    raise ResourceExceeded(f"hit the {self.limits.cpu_s}s CPU limit ({cpu_used:.1f}s used)")
                raise StrategyError(f"claimed the CPU limit after only {cpu_used:.2f}s of CPU")
            err = payload.get("error") or {}
            etype, emsg = str(err.get("type", "Error")), str(err.get("message", ""))
            if etype == "MemoryError":
                raise ResourceExceeded("MemoryError under the sandbox memory limit")
            if "can't start new thread" in emsg or "File too large" in emsg or "EFBIG" in emsg:
                raise ResourceExceeded(f"{etype}: {emsg}")
            raise StrategyError(f"{etype}: {emsg}")

        sig = payload.get("signals")
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

    def _record(self, run: Path, t0: float, cpu_used: float, rc: int, hidden: tuple[str, ...],
                before: set[str], viol_bytes: bytes) -> RunRecord:
        written = sorted(self._snapshot(run) - before)
        violations: list[str] = []
        for line in viol_bytes.decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(line)
                violations.append(f"{d['kind']}: {d['detail']}")
            except (ValueError, KeyError, TypeError):
                violations.append("unparsed: " + line[:200])
        rec = RunRecord(time.monotonic() - t0, cpu_used, rc, self.isolation, hidden,
                        tuple(written), tuple(violations))
        self.records.append(rec)
        return rec

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
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
    first_boundary: int
    hash_seed_pinned: bool = True

    @property
    def provable(self) -> bool:
        return self.deterministic and self.input_dependent

    def describe(self) -> str:
        lines = [f"isolation: {self.isolation}" + (f" (hidden: {', '.join(self.hidden)})" if self.hidden else "")]
        if self.isolation == "plain":
            lines.append("  plain tier: files written to /tmp or the source directory survive between runs, "
                         "so state CAN carry; the namespace tier is what prevents it")
        lines.append("same tape twice           -> " + ("identical output" if self.deterministic else
                     "DIFFERENT output: the strategy is nondeterministic, so no divergence could be attributed to the data"))
        lines.append(f"bars from {self.first_boundary} on replaced -> " + ("different output" if self.input_dependent else
                     "IDENTICAL output: the output does not change when the bars we can vary change, so nothing can be proved about it"))
        if self.files_written:
            lines.append(f"files written by the strategy during a run: {', '.join(self.files_written)} "
                         f"-- this is what a feature cache looks like")
        lines.append("hash seed pinned to 0 for every run, so dict and set order cannot differ between them")
        lines.append("PROVABLE" if self.provable else "UNPROVABLE: no probe result would mean anything; fix the above first")
        return "\n".join(lines)


def precheck(sandbox: Sandbox, tape: Sequence[Any], *, boundaries: Sequence[int] | None = None) -> Precheck:
    """The two conditions under which a probe result carries meaning. Run before any probe.

    The input-dependence gate replaces the bars from the FIRST PROBE BOUNDARY onward with a
    fresh continuation and requires the output to change. A strategy whose output depends
    only on bars before that point -- bar 0 alone, in one red-team case -- is unmoved by
    every probe, and would otherwise be certified provable and then found clean.
    """
    b0 = min(boundaries) if boundaries else min(default_boundaries(len(tape)))
    other = continuation(tape, b0, seed=7)
    a = sandbox(tape)
    c = sandbox(other)
    b = sandbox(tape)          # the A/A pair is spaced by the other run on purpose
    written = tuple(sorted({f for r in sandbox.records[-3:] for f in r.files_written}))
    return Precheck(deterministic=(a == b), input_dependent=(a != c), isolation=sandbox.isolation,
                    hidden=sandbox.records[-1].hidden, files_written=written, first_boundary=b0)


def prove(sandbox: Sandbox, tape: Sequence[Any], **kw: Any) -> tuple[Precheck, Report | None]:
    """Gates first, probes second, never the other way round."""
    pc = precheck(sandbox, tape, boundaries=kw.get("boundaries"))
    if not pc.provable:
        return pc, None
    return pc, check_causality(sandbox, tape, **kw)
