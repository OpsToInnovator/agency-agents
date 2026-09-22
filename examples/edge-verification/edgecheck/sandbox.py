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

    namespace   (default when ``unshare`` can create user, mount, pid and network namespaces
                and pivot into a new root)
                Fresh interpreter per run. No network at the kernel level: connect() and
                DNS both fail, by any route, ctypes and child processes included. A NEW
                ROOT: an empty tmpfs into which only what the interpreter needs is bound,
                read-only -- /usr, /etc, the lib and bin trees, the interpreter's prefix --
                plus a fresh /proc, four device nodes, a tmpfs /tmp that dies with the
                process, and the run directory. Nothing else exists. The home directory,
                /opt, /var, /run, the working root, the source tree, the repository: not
                hidden, absent. A second red team had cached through /opt and injected a
                module into the interpreter's own dist-packages, because the first version
                hid a LIST of paths and a list can never name every writable directory on
                a host. Scrubbed environment. rlimits on CPU, memory, processes, file size,
                open files. Wall-clock kill. Nothing written anywhere but the run directory
                survives the process, and the run directory is deleted after it.

    plain       Everything above except the namespaces. The audit hook still records every
                in-process socket use and every attempt to spawn a process, and refuses
                them; ``ctypes`` walks past it, and so would a process it managed to start.
                The parent's environment is readable through /proc. Files written anywhere
                outside the run directory survive, so state CAN carry between runs. This
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

from edgecheck.causality import Report, check_causality, continuation

__all__ = ["Limits", "Sandbox", "SandboxError", "StrategyError", "Timeout", "ResourceExceeded",
           "NetworkAttempt", "ContractViolation", "BadOutput", "Precheck", "precheck", "prove",
           "detect_isolation", "Isolation", "RunRecord"]

Isolation = Literal["namespace", "plain"]
CHILD = Path(__file__).with_name("_child.py")
SYSTEM_ROOTS = ("/usr", "/etc", "/lib", "/lib64", "/bin", "/sbin")   # bound read-only into the new root
MASKED = ("/etc/arbbot",)                                             # exists on the host; not in the new root
SHELVES = ("/dev/shm", "/mnt", "/media")   # the new root is a fresh tmpfs mounted here, then pivoted to


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
    visible: tuple[str, ...]
    files_written: tuple[str, ...]
    violations: tuple[str, ...]


_ISOLATION_CACHE: dict[str, Isolation] = {}


def detect_isolation() -> Isolation:
    """Can this host build the new root? Probed once, by running the REAL namespace script
    against a stub child that exits 0. An earlier probe used its own shorter sequence,
    which bound fewer trees than the real one and failed where the real one succeeded --
    a probe that diverges from what it probes measures nothing.
    """
    if "v" in _ISOLATION_CACHE:
        return _ISOLATION_CACHE["v"]
    ok = False
    unshare = shutil.which("unshare")
    if unshare:
        run = Path(tempfile.mkdtemp(prefix="edgecheck-probe-"))
        try:
            (run / "strategy").mkdir()
            (run / "_child.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
            cmd = [unshare, "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--",
                   "sh", "-c", _NS_SCRIPT, "sh", sys.executable, str(run), "x", "x", "1", "2",
                   Sandbox._shelf(), *Sandbox._bound_roots()]
            r = subprocess.run(cmd, cwd=run, env=_scrubbed_env(), capture_output=True, timeout=20)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        finally:
            shutil.rmtree(run, ignore_errors=True)
    _ISOLATION_CACHE["v"] = "namespace" if ok else "plain"
    return _ISOLATION_CACHE["v"]


def _scrubbed_env() -> dict[str, str]:
    """Built from nothing. Whatever the parent had -- keys, tokens, proxies -- stays with the parent."""
    return {
        # sbin too: pivot_root and umount live there, and the namespace script runs under this
        # environment. A manual test passed on a shell whose PATH had them; the probe did not.
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",
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
# into this string, so nothing a caller passes can become shell. The new root is built in a
# fresh tmpfs: each system root is bound in read-only (or recreated as the same symlink where
# the host has one), the run directory is bound in writable with the strategy's own copy made
# read-only, then pivot_root makes it the root and the old root is detached.
_NS_SCRIPT = r'''
set -e
py="$1"; run="$2"; entry="$3"; func="$4"; ofd="$5"; vfd="$6"; new="$7"; shift 7
mount -t tmpfs -o nodev,nosuid,size=64m tmpfs "$new"
cd "$new"
mkdir -p proc dev tmp work oldroot
for p in "$@"; do
  if [ -L "$p" ]; then
    mkdir -p "$(dirname "$new$p")"; ln -s "$(readlink "$p")" "$new$p"
  elif [ -d "$p" ]; then
    mkdir -p "$new$p"; mount --rbind "$p" "$new$p"; mount -o remount,bind,ro,nosuid,nodev "$new$p"
  fi
done
for m in /etc/arbbot; do [ -d "$new$m" ] && mount -t tmpfs -o size=1m,nodev,nosuid tmpfs "$new$m" || true; done
for f in null zero urandom random; do touch "dev/$f"; mount --bind "/dev/$f" "dev/$f"; done
mount -t tmpfs -o nodev,nosuid,size=64m tmpfs tmp
mount -t proc proc proc
mount --bind "$run" work
mount --bind work/strategy work/strategy
mount -o remount,bind,ro,nodev,nosuid work/strategy
pivot_root . oldroot
umount -l /oldroot
cd /work
exec "$py" -s -B /work/_child.py /work "$entry" "$func" "$ofd" "$vfd"
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

    # -- what exists inside, and nothing else -------------------------------------------

    @staticmethod
    def _bound_roots() -> tuple[str, ...]:
        """The read-only whitelist: the system trees plus wherever this interpreter lives.

        A virtualenv under a home directory is bound at its own path and nothing around it.
        """
        roots = [r for r in SYSTEM_ROOTS if Path(r).exists()]
        for extra in {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                      Path(sys.executable).resolve().parent.parent}:
            if not any(extra == Path(r) or Path(r) in extra.parents for r in roots):
                roots.append(str(extra))
        return tuple(roots)

    @staticmethod
    def _shelf() -> str:
        for sh in SHELVES:
            if Path(sh).is_dir():
                return sh
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
        shutil.copy2(CHILD, run / "_child.py")      # so the repository need not exist inside
        with (run / "tape.jsonl").open("w", encoding="utf-8") as fh:
            for b in tape:
                fh.write(json.dumps({"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
                                     "close": b.close, "volume": b.volume}) + "\n")
        before = self._snapshot(run) - {"_child.py"}

        out_r, out_w = os.pipe()
        viol_r, viol_w = os.pipe()
        err_r, err_w = os.pipe()
        visible: tuple[str, ...] = ()
        if self.isolation == "namespace":
            visible = self._bound_roots()
            cmd = [shutil.which("unshare") or "unshare", "--user", "--map-root-user", "--mount",
                   "--net", "--pid", "--fork", "--", "sh", "-c", _NS_SCRIPT, "sh",
                   sys.executable, str(run), self.entry, self.func,
                   str(out_w), str(viol_w), self._shelf(), *visible]
        else:
            cmd = [sys.executable, "-s", "-B", str(run / "_child.py"), str(run), self.entry, self.func,
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
                           visible, before, viols[0] if viols else b"")
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

    def _record(self, run: Path, t0: float, cpu_used: float, rc: int, visible: tuple[str, ...],
                before: set[str], viol_bytes: bytes) -> RunRecord:
        written = sorted(self._snapshot(run) - before - {"_child.py"})
        violations: list[str] = []
        for line in viol_bytes.decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(line)
                violations.append(f"{d['kind']}: {d['detail']}")
            except (ValueError, KeyError, TypeError):
                violations.append("unparsed: " + line[:200])
        rec = RunRecord(time.monotonic() - t0, cpu_used, rc, self.isolation, visible,
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
    deterministic_on_varied: bool
    input_dependent: bool
    isolation: str
    visible: tuple[str, ...]
    files_written: tuple[str, ...]
    first_boundary: int
    hash_seed_pinned: bool = True

    @property
    def provable(self) -> bool:
        return self.deterministic and self.deterministic_on_varied and self.input_dependent

    def describe(self) -> str:
        lines = [f"isolation: {self.isolation}" +
                 (f" (inside: {', '.join(self.visible)}, read-only; plus /proc, /dev, /tmp and the run directory)"
                  if self.visible else "")]
        if self.isolation == "plain":
            lines.append("  plain tier: files written outside the run directory survive between runs, "
                         "so state CAN carry; the namespace tier is what prevents it")
        lines.append("same tape twice            -> " + ("identical output" if self.deterministic else
                     "DIFFERENT output: the strategy is nondeterministic, so no divergence could be attributed to the data"))
        lines.append("same varied tape twice     -> " + ("identical output" if self.deterministic_on_varied else
                     "DIFFERENT output: deterministic on the real tape but not on a varied one -- the strategy "
                     "is telling the two apart, and nothing it does under a probe can be trusted"))
        lines.append(f"bars from {self.first_boundary} on replaced -> " + ("different output" if self.input_dependent else
                     "IDENTICAL output: the output does not change when the bars we can vary change, so nothing can be proved about it"))
        if self.files_written:
            lines.append(f"files written by the strategy during a run: {', '.join(self.files_written)} "
                         f"-- this is what a feature cache looks like")
        lines.append("hash seed pinned to 0 for every run, so dict and set order cannot differ between them")
        lines.append("PROVABLE" if self.provable else "UNPROVABLE: no probe result would mean anything; fix the above first")
        return "\n".join(lines)


GATE_BOUNDARY = 4   # the earliest bar any probe can vary; the gate varies everything from here


def precheck(sandbox: Sandbox, tape: Sequence[Any]) -> Precheck:
    """The conditions under which a probe result carries meaning. Run before any probe.

    Determinism is checked on the pristine tape AND on a varied one. A second red team
    built a strategy that leaks on the real tape, recognises it by a checksum, and returns
    random output on anything else: deterministic exactly where the old gate looked, and
    "nondeterministic" everywhere the probes looked, which downgraded PROVEN to inconclusive.
    Now that pattern fails the gate, and the report says what it is.

    The input-dependence gate replaces every bar from the earliest probe boundary on with a
    fresh continuation and requires the output to change; a strategy unmoved by that is a
    strategy no probe can move.
    """
    varied = continuation(tape, GATE_BOUNDARY, seed=7)
    a = sandbox(tape)
    c1 = sandbox(varied)
    b = sandbox(tape)          # the pairs are interleaved on purpose, to space them in time
    c2 = sandbox(varied)
    written = tuple(sorted({f for r in sandbox.records[-4:] for f in r.files_written}))
    return Precheck(deterministic=(a == b), deterministic_on_varied=(c1 == c2), input_dependent=(a != c1),
                    isolation=sandbox.isolation, visible=sandbox.records[-1].visible,
                    files_written=written, first_boundary=GATE_BOUNDARY)


def prove(sandbox: Sandbox, tape: Sequence[Any], **kw: Any) -> tuple[Precheck, Report | None]:
    """Gates first, probes second, never the other way round."""
    pc = precheck(sandbox, tape)
    if not pc.provable:
        return pc, None
    return pc, check_causality(sandbox, tape, **kw)
