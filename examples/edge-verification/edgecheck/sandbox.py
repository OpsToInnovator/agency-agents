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
                open files. Wall-clock kill. /proc/sys read-only, since a fresh /proc is otherwise a
                writable window onto host-wide sysctls (a fifth red team set vm.overcommit
                for the whole host from inside). Nothing written anywhere but the run
                directory survives the process, and the run directory is deleted after it. The
                strategy itself runs in one more user namespace, unmapped: no capabilities,
                every mount locked, so the read-only trees stay read-only even against a
                ctypes mount() call. And the parent bounds what it will take from the
                child -- result and record sizes, run-directory entries and depth -- with
                walks and teardown that never recurse, because a strategy that builds a
                directory two thousand levels deep must get a verdict, not crash the auditor.

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

import dataclasses
import errno
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
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
HARD_MARGIN = 0.5   # how far under the hard CPU limit wait4's figure has read at the kernel's kill, and more
SHELVES = ("/dev/shm", "/mnt", "/media")   # the new root is a fresh tmpfs mounted here, then pivoted to


@dataclass(frozen=True, slots=True)
class Limits:
    cpu_s: int = 60
    wall_s: float = 120.0
    memory_bytes: int = 2 * 1024 ** 3
    nproc: int = 64
    fsize_bytes: int = 256 * 1024 ** 2
    nofile: int = 256
    # What the PARENT will take from the child. A third red team built a run directory two
    # thousand levels deep with nothing but os.mkdir and os.chdir, and the auditor died of a
    # RecursionError in its own recursive walk and teardown -- no verdict at all, on a
    # strategy that was PROVEN on its own. Nothing the child does may crash the parent.
    result_bytes: int = 64 * 1024 ** 2
    violation_bytes: int = 1024 ** 2
    run_dir_entries: int = 20_000
    run_dir_depth: int = 64
    # RLIMIT_FSIZE caps one file. Many files at that size would fill the host disk through a
    # run directory bound from the host, so inside the namespace the run directory is a
    # tmpfs of this size: the strategy gets ENOSPC, the host gets nothing.
    run_dir_bytes: int = 256 * 1024 ** 2

    def __post_init__(self) -> None:
        # Every limit is a positive amount. A fractional cpu_s used to reach setrlimit in the
        # child's preexec hook, fail there as a bare SubprocessError, and leak six pipe ends.
        # The two times may be fractional; every count and size is a whole number. A twelfth
        # red team passed violation_bytes=1e6: the drain thread died on a float slice and the
        # network record it carried was dropped -- a strategy that dialled out came back clean.
        for name in ("cpu_s", "wall_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 < value < math.inf):
                raise ValueError(f"Limits.{name} must be a positive, finite number of seconds, not {value!r}")
        # The kernel keeps the CPU limit in nanoseconds in 64 bits: a limit of 18446744074s wrapped
        # to a third of a second (a fourteenth red team). A billion seconds is thirty years.
        if self.cpu_s > 1e9:
            raise ValueError(f"Limits.cpu_s must be at most a billion seconds, not {self.cpu_s!r}")
        for name in ("memory_bytes", "nproc", "fsize_bytes", "nofile", "result_bytes", "violation_bytes",
                     "run_dir_entries", "run_dir_depth", "run_dir_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not value > 0:
                raise ValueError(f"Limits.{name} must be a positive whole number, not {value!r}")
        # A record too short to hold the start of one line could not say a strategy dialled out.
        if self.violation_bytes < 64:
            raise ValueError(f"Limits.violation_bytes must be at least 64, enough to name a network or spawn "
                             f"attempt, not {self.violation_bytes!r}")
        # Each rlimit is set in the child's preexec hook, where setrlimit refuses a value past the
        # auditor's own hard limit -- or, for open files, past the kernel's nr_open -- and the launch
        # died as a bare SubprocessError naming nothing (a thirteenth red team, at nofile=10**7 and
        # cpu_s=2**63). Refused here instead, by name.
        try:
            with open("/proc/sys/fs/nr_open") as fh:
                nr_open = int(fh.read())
        except (OSError, ValueError):
            nr_open = None
        for name, res, value in (("cpu_s", resource.RLIMIT_CPU, math.ceil(self.cpu_s) + 3),
                                 ("memory_bytes", resource.RLIMIT_AS, self.memory_bytes),
                                 ("nproc", resource.RLIMIT_NPROC, self.nproc),
                                 ("fsize_bytes", resource.RLIMIT_FSIZE, self.fsize_bytes),
                                 ("nofile", resource.RLIMIT_NOFILE, self.nofile)):
            hard = resource.getrlimit(res)[1]
            # an unlimited hard limit reads as RLIM_INFINITY (-1 here); setrlimit takes below 2**63
            ceiling = (1 << 63) - 2 if hard == resource.RLIM_INFINITY or hard < 0 else hard
            if name == "nofile" and nr_open is not None:
                ceiling = min(ceiling, nr_open)
            if value > ceiling:
                what = "cpu_s, plus the three seconds before the hard kill," if name == "cpu_s" else f"Limits.{name}"
                raise ValueError(f"{what} is {value!r}, more than this process may set as its limit ({ceiling})")

    @property
    def cpu_enforced(self) -> int:
        """The CPU limit the kernel actually enforces: it counts whole seconds."""
        return math.ceil(self.cpu_s)

    def cpu_words(self) -> str:
        # exact, not :g -- a limit of 1.0000001s was printed as 1s (a thirteenth red team)
        return (f"the {_secs(self.cpu_s)}s CPU limit" if self.cpu_s == self.cpu_enforced else
                f"the CPU limit of {_secs(self.cpu_s)}s, which the kernel enforces in whole seconds, at "
                f"{self.cpu_enforced}s")


def _secs(x: float) -> str:
    # repr, the shortest form that reads back as the same float: .15g printed 1 + 2**-52 as 1
    return repr(x) if isinstance(x, float) else str(x)


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
    spawn_lock: bool = False      # the kernel refused new processes (seccomp), per the child


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
            cmd = [unshare, "--user", "--map-root-user", "--mount", "--net", "--uts", "--ipc", "--pid", "--fork", "--",
                   "sh", "-c", _NS_SCRIPT, "sh", sys.executable, str(run), "x", "x", "1", "2",
                   Sandbox._shelf(), "8", "100", "8", "1", "1", "1", "1", *Sandbox._bound_roots()]
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
        # glibc reserves a 64 MiB malloc arena of address space per thread, so under the default
        # 2 GiB address-space limit an honest 32-worker thread pool could not start (a sixteenth red
        # team); two arenas are shared by all threads instead
        "MALLOC_ARENA_MAX": "2",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",
    }


def _rlimit_installer(limits: Limits):
    def install() -> None:
        # Soft limit first: SIGXCPU, which the child catches to record the cause. Hard
        # limit a few seconds on: SIGKILL, for a strategy that ignores the first.
        cpu = limits.cpu_enforced                     # the kernel counts whole seconds
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 3))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # Memory, processes, file size and open files are set by the child itself, after the
        # sandbox's own setup and before the strategy is imported (see _child.py).
    return install


# Executed by sh inside the namespaces. Positional arguments only -- nothing is interpolated
# into this string, so nothing a caller passes can become shell. The new root is built in a
# fresh tmpfs: each system root is bound in read-only (or recreated as the same symlink where
# the host has one), the run directory is bound in writable with the strategy's own copy made
# read-only, then pivot_root makes it the root and the old root is detached.
#
# The last line matters as much as the rest. Setup needs the mapped-root capabilities, but a
# child that keeps them can undo the setup: it is root in this namespace, and a mount this
# namespace created is a mount it may remount read-write -- a third red team did exactly
# that through a ctypes mount() call and wrote into the host's /usr. So the strategy runs
# inside one more user namespace, unmapped: no capabilities over anything that exists, and
# every mount inherited from outside is locked. The remount is refused. The interpreter
# does not care what uid it is.
_NS_SCRIPT = r'''
set -e
py="$1"; run="$2"; entry="$3"; func="$4"; ofd="$5"; vfd="$6"; new="$7"; workmb="$8"; ents="$9"; shift 9
depth="$1"; mem="$2"; nproc="$3"; fsz="$4"; nofile="$5"; shift 5
mount -t tmpfs -o nodev,nosuid,size=64m tmpfs "$new"
cd "$new"
mkdir -p proc dev tmp work src oldroot
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
mount --bind proc/sys proc/sys
mount -o remount,bind,ro,nosuid,nodev,noexec proc/sys
for m in proc/sysrq-trigger proc/irq proc/bus; do
  if [ -d "$m" ]; then mount -t tmpfs -o size=1m,ro tmpfs "$m"; elif [ -e "$m" ]; then mount --bind dev/null "$m"; fi
done
mount --bind "$run" src
mount -o remount,bind,ro,nodev,nosuid src
mount -t tmpfs -o nodev,nosuid,size="$workmb"m tmpfs work
cp -a src/. work/
chmod -R a-w work/strategy
pivot_root . oldroot
umount -l /oldroot
cd /work
exec unshare --user -- "$py" -s -B /work/_child.py /work "$entry" "$func" "$ofd" "$vfd" "$ents" "$depth" "$mem" "$nproc" "$fsz" "$nofile"
'''


_START_LINE = b'{"kind": "start", "detail": ""}\n'     # the child's first record line, exactly


def _kill_child() -> list[str]:
    """``--kill-child`` where this unshare has it: the kernel then kills the namespace's first process,
    and with it every process in the namespace, when the launcher dies. Without it a strategy that
    left the launcher's process group (setsid) outlived the wall-clock kill, reparented to init, and
    held the run's pipes open for a quarter of a minute (a seventeenth red team)."""
    if "kc" not in _ISOLATION_CACHE:
        try:
            out = subprocess.run([shutil.which("unshare") or "unshare", "--help"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _ISOLATION_CACHE["kc"] = ["--kill-child"] if "--kill-child" in out else []
    return _ISOLATION_CACHE["kc"]


def _drain(fd: int, sink: list[bytes], cap: int, *, tail: bool = False) -> threading.Thread:
    """Read to EOF so the child never blocks on a full pipe, keep at most ``cap`` bytes, and
    say whether more than that arrived -- exactly ``cap`` is within it. A result or a record larger than the cap is not
    evidence of anything but a strategy trying to exhaust the auditor. ``tail`` keeps the
    LAST ``cap`` bytes instead of the first: for stderr, where the error that ended the run
    is at the end, and a long log before it had pushed it out of the report."""
    cap = int(cap)

    def go() -> None:
        buf = bytearray()
        total = 0
        try:
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                total += len(b)
                if tail:
                    buf += b
                    if len(buf) > cap:
                        del buf[:len(buf) - cap]
                elif len(buf) < cap:
                    buf += b[:cap - len(buf)]
        except OSError:
            pass
        finally:
            # Whatever happened above, what was read is handed over: a drain that died took the
            # network record with it once (a twelfth red team).
            try:
                os.close(fd)
            except OSError:
                pass
            sink.append(bytes(buf))
            sink.append(b"1" if total > cap else b"0")
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
        self._owns_work_root = work_root is None
        self.work_root = Path(work_root).resolve() if work_root else Path(tempfile.mkdtemp(prefix="edgecheck-"))
        self.work_root.mkdir(parents=True, exist_ok=True)
        # Staged once. Every run copies from here, never from the source, so a run that
        # wrote into the source directory would still not be feeding the next run.
        self.strategy_dir = Path(tempfile.mkdtemp(prefix="stage-", dir=self.work_root))
        shutil.copytree(src, self.strategy_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.records: list[RunRecord] = []
        # The staged copy is the customer's source, sitting in a world-readable temp dir for
        # as long as the host lives unless someone removes it; a long-lived auditor would
        # also fill the disk one stage at a time. Removed on close(), on leaving a `with`
        # block, and by the finalizer if neither happened.
        self._finalizer = weakref.finalize(self, Sandbox._cleanup, self.strategy_dir,
                                           self.work_root if self._owns_work_root else None)

    def close(self) -> None:
        """Remove the staged copy, and the work root if this sandbox created it."""
        self._finalizer()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _cleanup(stage: Path, owned_root: Path | None) -> None:
        for d in (stage, owned_root):
            if d is not None and d.exists():
                Sandbox._remove_tree(d)

    @classmethod
    def from_file(cls, path: Path | str, **kw: Any) -> "Sandbox":
        """A single-file strategy: copied into its own directory, imported by its stem."""
        src = Path(path).resolve()
        d = Path(tempfile.mkdtemp(prefix="edgecheck-strategy-"))
        # imported by its stem where that is a module name, and staged as strategy.py where it is
        # not: strategy_v1.2.py was looked up as a package strategy_v1 and failed as the strategy's error
        entry = src.stem if src.stem.isidentifier() else "strategy"
        try:
            shutil.copy2(src, d / f"{entry}.py")
            sb = cls(d, entry=entry, **kw)
        finally:
            # The sandbox stages its own copy on construction and never reads this one
            # again, and this one is the customer's source in a world-readable temp dir: it
            # outlived close() until a seventh red team counted them.
            shutil.rmtree(d, ignore_errors=True)
        sb.source_dir = src
        return sb

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

    def _child_limits(self) -> list[str]:
        return [str(self.limits.memory_bytes), str(self.limits.nproc), str(self.limits.fsize_bytes),
                str(self.limits.nofile)]

    # -- one run ------------------------------------------------------------------------------

    def __call__(self, tape: Sequence[Any]) -> list[int]:
        run = Path(tempfile.mkdtemp(prefix="run-", dir=self.work_root))
        try:
            return self._run(run, tape)
        finally:
            try:
                self._remove_tree(run)
            except Exception:  # noqa: BLE001 -- teardown must never take the verdict with it
                pass

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
                   "--net", "--uts", "--ipc", "--pid", "--fork", *_kill_child(), "--", "sh", "-c", _NS_SCRIPT, "sh",
                   sys.executable, str(run), self.entry, self.func,
                   str(out_w), str(viol_w), self._shelf(),
                   str(max(1, self.limits.run_dir_bytes // (1024 ** 2))),
                   str(self.limits.run_dir_entries), str(self.limits.run_dir_depth), *self._child_limits(), *visible]
        else:
            cmd = [sys.executable, "-s", "-B", str(run / "_child.py"), str(run), self.entry, self.func,
                   str(out_w), str(viol_w), str(self.limits.run_dir_entries), str(self.limits.run_dir_depth),
                   *self._child_limits()]

        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, cwd=run, env=_scrubbed_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=err_w, pass_fds=(out_w, viol_w),
                                    start_new_session=True, preexec_fn=_rlimit_installer(self.limits))
        except BaseException as e:
            for fd in (out_r, out_w, viol_r, viol_w, err_r, err_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            if isinstance(e, subprocess.SubprocessError):
                raise SandboxError(f"the strategy process could not be started under these Limits: {e} "
                                   f"(each is applied with setrlimit before the strategy starts)") from e
            raise
        for fd in (out_w, viol_w, err_w):
            os.close(fd)
        outs: list[bytes] = []
        viols: list[bytes] = []
        errs: list[bytes] = []
        drains = [_drain(out_r, outs, self.limits.result_bytes),
                  _drain(viol_r, viols, self.limits.violation_bytes + len(_START_LINE)),
                  _drain(err_r, errs, 64 * 1024, tail=True)]

        # Wait on the PROCESS, not on the pipes: a helper the strategy started could hold a
        # pipe open long after the strategy returned. Then kill the whole group regardless,
        # so nothing outlives the run in either tier.
        # The run's CPU is its own process tree's, from wait4 -- not a difference of this process's
        # children's totals, which billed a run for whatever another Sandbox reaped meanwhile: a
        # fourteenth red team's idle run was told it had used five seconds of a neighbour's CPU.
        timed_out, usage = False, None
        while True:
            try:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                proc.returncode = os.waitstatus_to_exitcode(status)
                break
            if time.monotonic() - t0 >= self.limits.wall_s:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _, status, usage = os.wait4(proc.pid, 0)
                    proc.returncode = os.waitstatus_to_exitcode(status)
                except ChildProcessError:
                    pass
                break
            time.sleep(0.002)
        self._kill(proc)
        for d in drains:
            d.join(timeout=5)
        cpu_used = (usage.ru_utime + usage.ru_stime) if usage is not None else 0.0
        viol = viols[0] if viols else b""
        # The start line is the sandbox's, written before anything of the strategy's, and read past
        # the cap on the strategy's record: under a cap smaller than the line, an imported strategy's
        # own exit was reported as the sandbox stopping before the import (a fifteenth red team).
        at = viol.find(_START_LINE)
        started = at >= 0
        truncated = len(viols) > 1 and viols[1] == b"1"
        if started:
            viol = viol[:at] + viol[at + len(_START_LINE):]
        if len(viol) > self.limits.violation_bytes:
            viol, truncated = viol[:self.limits.violation_bytes], True
        rec = self._record(run, t0, cpu_used, proc.returncode if proc.returncode is not None else -9,
                           visible, before, viol, truncated=truncated)
        if timed_out:
            raise Timeout(f"strategy exceeded {_secs(self.limits.wall_s)}s wall clock")
        if any(f.startswith("<run directory exceeded") for f in rec.files_written):
            raise ResourceExceeded(f"run directory exceeded {self.limits.run_dir_entries} entries "
                                   f"or depth {self.limits.run_dir_depth}")
        if len(outs) > 1 and outs[1] == b"1":
            raise BadOutput(f"result larger than {self.limits.result_bytes} bytes")

        network = [v for v in rec.violations if v.startswith("network:")]
        spawns = [v for v in rec.violations if v.startswith("spawn:")]
        if network:
            raise NetworkAttempt("; ".join(network))
        if spawns:
            raise ContractViolation("; ".join(spawns))

        raw = outs[0] if outs else b""
        if not raw:
            # No output: the process died. What killed it is in the exit status -- a signal the
            # plain tier reports as such and `unshare --fork` passes on, except SIGKILL, which it
            # reports as exit status 1. Only a kill at the hard CPU limit (three seconds past the
            # soft one, for a strategy that ignored SIGXCPU) or a SIGXCPU at the soft limit is the
            # CPU limit; a twelfth red team's segfault at 1.95s of a 2s limit was reported as
            # having used the limit, because the parent's count includes the namespace setup.
            lines = (errs[0] if errs else b"").decode("utf-8", "replace").strip().splitlines()
            launcher_tail = " | ".join(lines[-3:])
            # In the namespace tier the launcher's own 'unshare:' lines share the pipe; they are left
            # out of the strategy's, and the report says so -- a strategy's own line that starts the
            # same way was dropped without a word (a seventeenth red team)
            own = [ln for ln in lines if not ln.startswith("unshare: ")] if self.isolation == "namespace" else lines
            tail = " | ".join(own[-3:]) + (" (lines starting 'unshare: ' left out as the launcher's)"
                                           if len(own) != len(lines) else "")
            rc = proc.returncode
            sig = -rc if rc is not None and rc < 0 else None
            hard = self.limits.cpu_enforced + 3
            if not started:
                # The child never got as far as importing the strategy: the sandbox's own setup or
                # interpreter failed, and its stderr is the launcher's, not the strategy's.
                how = (f"killed by {signal.Signals(sig).name}" if sig is not None and sig in signal.valid_signals()
                       else f"rc={rc}")
                raise SandboxError(f"the sandbox stopped before the strategy was imported ({how}); these Limits "
                                   f"may be too tight for the launcher itself. The launcher's stderr ended: {launcher_tail}")
            # The kernel kills on CPU it samples in scheduler ticks, and the exact figure wait4 returns
            # can read well short of it: under load a fifteenth red team's kill read 4.997s of a 5s hard
            # limit, a sixteenth's 3.77s of 4. So a SIGKILL near the hard limit is worded as what it may
            # be -- the kernel's kill, or one the strategy sent itself -- not inferred from the figure.
            # SIGXCPU at the soft limit goes unanswered when the strategy ignores it or holds the
            # interpreter in one long call, where the handler cannot run.
            near = hard - HARD_MARGIN
            if sig == signal.SIGKILL and cpu_used >= near:
                raise ResourceExceeded(f"killed by SIGKILL at {cpu_used:.3f}s of CPU as wait4 counts it, the "
                                       f"SIGXCPU of {self.limits.cpu_words()} unanswered: the kernel's kill at "
                                       f"the hard limit of {hard}s by its own clock, or a SIGKILL the strategy "
                                       f"sent itself -- the sandbox cannot tell which")
            if self.isolation == "namespace" and rc == 1 and cpu_used >= near:
                # `unshare --fork` reports a SIGKILL as status 1, the status a strategy exiting with
                # 1 also gets: a thirteenth red team's did so at 4.93s of its own CPU, which with the
                # setup read past the 5s hard limit, and was told it had been killed there.
                raise ResourceExceeded(f"ran past {self.limits.cpu_words()}, its SIGXCPU unanswered, and ended with "
                                       f"no output at {cpu_used:.3f}s of CPU as wait4 counts it: the kernel's "
                                       f"kill at the hard limit of {hard}s by its own clock, or an exit with "
                                       f"status 1 -- this tier reports both the same way")
            if sig == signal.SIGXCPU and cpu_used >= self.limits.cpu_enforced:
                raise ResourceExceeded(f"hit {self.limits.cpu_words()} ({cpu_used:.1f}s used)")
            how = (f"killed by {signal.Signals(sig).name}" if sig is not None and sig in signal.valid_signals()
                   else f"rc={rc}")
            raise StrategyError(f"no output ({how}, {cpu_used:.1f}s CPU); "
                                f"the strategy's own stderr ended: {tail}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except (ValueError, RecursionError) as e:     # a deeply nested array recurses in the decoder
            raise StrategyError(f"malformed result from the strategy process: {type(e).__name__}") from None
        self.records[-1] = rec = dataclasses.replace(rec, spawn_lock=payload.get("spawn_lock") is True)

        if not payload.get("ok"):
            if payload.get("reason") == "cpu_limit":
                # The child's word, checked against the kernel's. A tenth red team raised the signal
                # itself at 1.3s of 2s, an eleventh at 1.8s, a twelfth at 1.95s, a thirteenth at 1.99s.
                own = payload.get("cpu")
                own = own if isinstance(own, (int, float)) and not isinstance(own, bool) and math.isfinite(own) else None
                # Who sent the signal decides: the child takes SIGXCPU with sigwaitinfo, which says
                # whether it came from the kernel's limit or from a kill() -- timing could not tell
                # them apart (a genuine signal came 15ms before the process's clock reached the limit).
                # The child drops one from a kill(), so a claim without the kernel's mark was written
                # by the strategy itself.
                shown = f"{own:.3f}s" if own is not None else "an unknown time"
                if payload.get("kernel") is True:
                    raise ResourceExceeded(f"hit {self.limits.cpu_words()} (the kernel's signal, handled at "
                                           f"{shown} by the strategy's own CPU clock)")
                raise StrategyError(f"claimed the CPU limit at {shown} of CPU without the kernel's signal: the "
                                    f"claim came from its own process, not from the kernel's enforcement of "
                                    f"{self.limits.cpu_words()}")
            err = payload.get("error") or {}
            etype, emsg = str(err.get("type", "Error")), str(err.get("message", ""))
            # Classified by the exception's type and errno, not by words in its message, which are the
            # strategy's: a fifteenth red team's ValueError mentioning 'File too large' was reported
            # as the file-size limit. A MemoryError is the limit's or the strategy's own -- the
            # sandbox cannot tell which -- and the report says so, with the strategy's message.
            # Every one of these the strategy can raise itself, and a sixteenth red team's did -- a
            # thread pool's own "can't start new thread", an OSError it gave EFBIG -- so each is worded
            # as the limit or the strategy's own. A MemoryError is known by what it is, not its name:
            # numpy's _ArrayMemoryError under the limit was filed as the strategy's own error. A
            # thread that cannot start is refused by the memory limit as often as the process limit.
            if err.get("memory") is True or etype == "MemoryError":
                raise ResourceExceeded(f"{etype} under a memory limit of {self.limits.memory_bytes} bytes: "
                                       f"the limit, or a MemoryError the strategy raised itself -- the sandbox "
                                       f"cannot tell which. Its message: {emsg}")
            if err.get("errno") == errno.EFBIG:
                raise ResourceExceeded(f"{etype}: {emsg} -- errno EFBIG, under a file-size limit of "
                                       f"{self.limits.fsize_bytes} bytes: the limit, or an error the strategy "
                                       f"raised itself -- the sandbox cannot tell which")
            if etype == "RuntimeError" and emsg.strip("'\"").startswith("can't start new thread"):
                # the kernel exempts root from the process limit, so under a root auditor it cannot
                # be the cause (a seventeenth red team)
                procs = "" if os.geteuid() == 0 else f"the limit of {self.limits.nproc} processes, "
                raise ResourceExceeded(f"{etype}: {emsg} -- {procs}the memory limit of {self.limits.memory_bytes} "
                                       f"bytes (each thread reserves address space for its stack), or an error the "
                                       f"strategy raised itself -- the sandbox cannot tell which")
            raise StrategyError(f"{etype}: {emsg}")

        if self.isolation == "namespace":
            hint = payload.get("files_written")
            over = bool(payload.get("run_dir_over_limit"))
            if isinstance(hint, list):
                self.records[-1] = dataclasses.replace(
                    rec, files_written=tuple(str(f)[:200] for f in hint[:2000] if isinstance(f, str)))
            if over:
                raise ResourceExceeded(f"run directory exceeded {self.limits.run_dir_entries} entries "
                                       f"or depth {self.limits.run_dir_depth}")
        sig = payload.get("signals")
        if not isinstance(sig, list) or len(sig) != len(tape) or \
                any(type(s) is not int or s not in (-1, 0, 1) for s in sig):
            shown = sorted({repr(s) for s in sig})[:6] if isinstance(sig, list) else type(sig).__name__
            raise BadOutput(f"expected {len(tape)} integer positions in {{-1, 0, 1}}, got "
                            f"{len(sig) if isinstance(sig, list) else '?'} with values {shown}")
        return sig

    # -- helpers ------------------------------------------------------------------------------

    def _snapshot(self, run: Path) -> set[str]:
        """Every regular file under the run directory, walked with an explicit stack and a
        ceiling on depth and count. Path.rglob and shutil.rmtree both recurse once per level
        on this interpreter; a tree deeper than the recursion limit killed the auditor."""
        out: set[str] = set()
        stack = [(run, 0)]
        seen = 0
        while stack:
            d, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        seen += 1
                        if seen > self.limits.run_dir_entries or depth > self.limits.run_dir_depth:
                            out.add("<run directory exceeded the entry or depth limit>")
                            return out
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((Path(e.path), depth + 1))
                            elif e.is_file(follow_symlinks=False):
                                out.add(str(Path(e.path).relative_to(run)))
                        except OSError:
                            continue
            except OSError:
                continue
        return out

    @staticmethod
    def _remove_tree(root: Path) -> None:
        """Remove the run directory however deep it is, with two descriptors and no path.

        A tree built one component at a time with chdir+mkdir is deeper than PATH_MAX long
        before it is deeper than the recursion limit, so a path-based walk gets
        ENAMETOOLONG and leaves it standing; and a descriptor-per-level walk runs out of
        descriptors at a thousand. So the tree is flattened instead: every directory one
        level down has its files unlinked and its subdirectories RENAMED up to the root,
        then is removed. Repeat until the root is empty. Each directory is lifted at most
        once per level it started below, visited once, and needs one descriptor.
        """
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
        lifted = 0
        try:
            while True:
                try:
                    with os.scandir(root_fd) as it:
                        entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
                except OSError:
                    break
                if not entries:
                    break
                for name, is_dir in entries:
                    try:
                        if not is_dir:
                            os.unlink(name, dir_fd=root_fd)
                            continue
                        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                        try:
                            with os.scandir(fd) as it:
                                for c in it:
                                    try:
                                        if c.is_dir(follow_symlinks=False):
                                            lifted += 1
                                            os.rename(c.name, f".lift-{lifted}", src_dir_fd=fd, dst_dir_fd=root_fd)
                                        else:
                                            os.unlink(c.name, dir_fd=fd)
                                    except OSError:
                                        continue
                        finally:
                            os.close(fd)
                        os.rmdir(name, dir_fd=root_fd)
                    except OSError:
                        continue
        finally:
            os.close(root_fd)
        try:
            os.rmdir(root)
        except OSError:
            pass

    def _record(self, run: Path, t0: float, cpu_used: float, rc: int, visible: tuple[str, ...],
                before: set[str], viol_bytes: bytes, truncated: bool = False) -> RunRecord:
        written = sorted(self._snapshot(run) - before - {"_child.py"})
        violations: list[str] = []
        for line in viol_bytes.decode("utf-8", "replace").splitlines()[:1000]:
            try:
                d = json.loads(line)
                violations.append(f"{d['kind']}: {d['detail']}")
            except (ValueError, KeyError, TypeError, RecursionError):
                # a line the cap cut off still says what it was: a network attempt under a record cap
                # shorter than one line came back clean (a sixteenth red team)
                kind = next((k for k in ("network", "spawn") if line.startswith('{"kind": "' + k + '"')), None)
                violations.append(f"{kind}: (the record of it was cut off by the violation cap) {line[:200]}"
                                  if kind else "unparsed: " + line[:200])
        if truncated or viol_bytes.count(b"\n") > 1000:
            violations.append("... and more; the record was capped")
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
    spawn_lock: bool = False

    @property
    def provable(self) -> bool:
        return self.deterministic and self.deterministic_on_varied and self.input_dependent

    @property
    def proof_only(self) -> bool:
        """Reproducible, but unmoved by the gate's continuation. The continuation varies what
        the probes vary without forcing the relations they force, so a probe may still move
        the output -- an eighth red team's same-bar volume read was blocked here as unprovable
        while the probes convicted it. The probes run; only a proof is reported, because a
        clean result on a strategy the gate could not move would mean nothing."""
        return self.deterministic and self.deterministic_on_varied and not self.input_dependent

    def describe(self) -> str:
        lines = [f"isolation: {self.isolation}" +
                 (f" (inside: {', '.join(self.visible)}, read-only; plus /proc, /dev, /tmp and the run directory)"
                  if self.visible else "")]
        if self.isolation == "plain":
            lines.append("  plain tier: files written outside the run directory survive between runs, "
                         "so state CAN carry; the namespace tier is what prevents it")
        lines.append("same tape, 3 times         -> " + ("identical output" if self.deterministic else
                     "DIFFERENT output: the strategy is nondeterministic, so no divergence could be attributed to the data"))
        lines.append("same varied tape, 3 times  -> " + (
            "identical output" if self.deterministic_on_varied else
            "DIFFERENT output while the real tape reproduced 3 times: either the strategy distinguishes "
            "real data from varied data, or it is intermittently nondeterministic; neither can be audited"
            if self.deterministic else
            "DIFFERENT output, as on the real tape"))
        lines.append(f"bars from {self.first_boundary} on replaced -> " + ("different output" if self.input_dependent else
                     "IDENTICAL output: the output does not change when the bars we can vary change under a fresh "
                     "continuation of the tape, so a clean result would mean nothing; " +
                     ("the probes still run, for a proof only" if self.proof_only else
                      "and with a gate above failed, the probes do not run")))
        if self.files_written:
            lines.append(f"files written by the strategy during a run: {', '.join(self.files_written)} "
                         f"-- this is what a feature cache looks like")
        lines.append("hash seed pinned to 0 for every run, so the order of dicts and sets keyed on strings or "
                     "numbers cannot differ between runs; a set of objects hashed by identity still follows "
                     "memory addresses, which do differ, and shows up above as nondeterminism")
        lines.append("new processes refused by the kernel (seccomp), by any route" if self.spawn_lock else
                     "the kernel's spawn lock is not available here: only the audit hook refuses new processes, "
                     "and only by the routes it sees")
        lines.append("PROVABLE" if self.provable else
                     "PROOF ONLY: the probes run, and only a proof would be reported" if self.proof_only else
                     "UNPROVABLE: no probe result would mean anything; fix the above first")
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
    # Three of each, interleaved to space them in time. One pair per tape let a strategy that
    # flips one bar with a small probability, inspecting nothing, pass as deterministic on
    # the real tape and fail on the varied one -- and be accused of telling the two apart.
    a = [sandbox(tape)]; c = [sandbox(varied)]
    a.append(sandbox(tape)); c.append(sandbox(varied))
    a.append(sandbox(tape)); c.append(sandbox(varied))
    written = tuple(sorted({f for r in sandbox.records[-6:] for f in r.files_written}))
    return Precheck(deterministic=(a[0] == a[1] == a[2]), deterministic_on_varied=(c[0] == c[1] == c[2]),
                    input_dependent=(a[0] != c[0]), isolation=sandbox.isolation,
                    visible=sandbox.records[-1].visible, files_written=written, first_boundary=GATE_BOUNDARY,
                    spawn_lock=sandbox.records[-1].spawn_lock)


def prove(sandbox: Sandbox, tape: Sequence[Any], **kw: Any) -> tuple[Precheck, Report | None]:
    """Gates first, probes second, never the other way round. A strategy the gate's
    continuation could not move is still probed, and a proof against it is still a proof;
    a clean result on it is withheld. The tape and every argument are checked before the gates run
    the strategy at all: refused after them, a bad sigma or tape cost every run they made (a twentieth
    and a twenty-first red team)."""
    from .causality import _check_arguments
    args, _ = _check_arguments(tape, **kw)
    kw = {**kw, **{k: v for k, v in args.items() if k in kw}}
    pc = precheck(sandbox, tape)
    if pc.provable:
        return pc, check_causality(sandbox, tape, **kw)
    if pc.proof_only:
        report = check_causality(sandbox, tape, **kw)
        return pc, (report if report.leaks else None)
    return pc, None
