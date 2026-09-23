"""Runs inside the sandbox. Loads a tape, imports the customer's strategy, writes its decisions.

Deliberately self-contained: it must not import edgecheck, because inside the sandbox the
package may be hidden, the environment is scrubbed and the interpreter runs with -s -B. The
Bar defined here has the same five fields as the one in the fixtures; the strategy contract
only requires attribute access, so a different class is fine.

Everything this process reports goes back over two pipes the parent created and handed us
as file descriptors -- never through a file in the run directory. The red team showed why:
a file the strategy's own process can write is a file the strategy can rewrite, delete, or
race with a thread that wakes after the official write. A pipe cannot be unlinked, and the
moment the result is written this process calls os._exit, so no thread, atexit handler or
interpreter shutdown ever runs afterwards. The descriptors are made non-inheritable first
thing, so a process the strategy spawns cannot hold them open and stall the parent.

The network and spawn record is a PEP 578 audit hook rather than a monkeypatch. A
monkeypatch on socket.socket is undone by importlib.reload(socket); an audit hook cannot be
removed and fires for every in-process socket use and every attempt to start a process,
whichever module made the call. It records first and refuses second, so a strategy that
swallows the exception is still on record. This is about naming the call in the report;
in namespace isolation the kernel blocks the connection regardless.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
import traceback


@dataclasses.dataclass(frozen=True, slots=True)
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


def lock_spawns() -> bool:
    """Have the kernel refuse every new process from here on: execve and execveat, fork and
    vfork, and every clone that is not a thread. A seccomp filter, installed with
    no_new_privs, which needs no privilege and cannot be removed by the process it binds.

    The audit hook names the stdlib's ways of starting a process, and round seven replaced
    _posixsubprocess.fork_exec, which raises no audit event. An eighth red team popped that
    module from sys.modules, imported a fresh copy with the real fork_exec, and started a
    process with nothing recorded; no audit event fires for any step of that. The kernel does
    not care which module asked. Threads are clones with CLONE_THREAD and still work; clone3,
    whose flags a filter cannot read, is answered ENOSYS so that libc falls back to clone.
    Returns False, and nothing is installed, on an architecture this table does not know."""
    import ctypes
    import platform
    import sys as _sys
    table = {
        "x86_64": (0xC000003E, (59, 322, 57, 58), 56, 435, 0x40000000),
        "aarch64": (0xC00000B7, (221, 281), 220, 435, None),
    }
    if _sys.byteorder != "little" or platform.machine() not in table:
        return False
    arch, denied, clone, clone3, x32 = table[platform.machine()]
    LD, JEQ, JGE, JSET, RET = 0x20, 0x15, 0x35, 0x45, 0x06
    ALLOW, EPERM, ENOSYS = 0x7FFF0000, 0x00050000 | 1, 0x00050000 | 38
    CLONE_THREAD = 0x00010000
    # (code, jump-if-true label, jump-if-false label, k); None falls through
    body = [(LD, None, None, 4), (JEQ, None, "deny", arch), (LD, None, None, 0)]
    if x32 is not None:
        body.append((JGE, "deny", None, x32))          # the x32 ABI's numbers, all of them
    body += [(JEQ, "deny", None, nr) for nr in denied]
    body += [(JEQ, "nosys", None, clone3), (JEQ, None, "allow", clone),
             (LD, None, None, 16), (JSET, "allow", "deny", CLONE_THREAD)]
    labels = {"allow": len(body), "deny": len(body) + 1, "nosys": len(body) + 2}
    body += [(RET, None, None, ALLOW), (RET, None, None, EPERM), (RET, None, None, ENOSYS)]

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte),
                    ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    rel = lambda i, lab: 0 if lab is None else labels[lab] - (i + 1)
    ins = (Filter * len(body))(*[Filter(c, rel(i, t), rel(i, f), k) for i, (c, t, f, k) in enumerate(body)])
    prog = Program(len(body), ctypes.cast(ins, ctypes.POINTER(Filter)))
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:              # PR_SET_NO_NEW_PRIVS
            return False
        return libc.prctl(22, 2, ctypes.byref(prog), 0, 0) == 0   # PR_SET_SECCOMP, FILTER
    except (OSError, AttributeError):
        return False


def main(argv: list[str]) -> int:
    run_dir, entry, func = argv[1], argv[2], argv[3]
    out_fd, viol_fd = int(argv[4]), int(argv[5])
    max_entries = int(argv[6]) if len(argv) > 6 else 20_000
    max_depth = int(argv[7]) if len(argv) > 7 else 64
    os.set_inheritable(out_fd, False)
    os.set_inheritable(viol_fd, False)
    os.chdir(run_dir)

    # Everything this function needs AFTER the strategy has run is bound to a local name
    # here, before the strategy is imported. A red team rebound __main__._write_all to a
    # no-op and the result was never written; a name looked up in this module's globals at
    # call time is a name the strategy can replace. A local is not.
    write, exit_, dumps, format_exc = os.write, os._exit, json.dumps, traceback.format_exc

    def write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[write(fd, view):]

    def finish(payload: dict, code: int) -> None:
        try:
            write_all(out_fd, dumps(payload).encode("utf-8"))
        finally:
            exit_(code)

    def record(kind: str, detail: str) -> None:
        try:
            write_all(viol_fd, (dumps({"kind": kind, "detail": detail[:300]}) + "\n").encode("utf-8"))
        except OSError:
            pass

    # -- the CPU limit: SIGXCPU at the soft limit is catchable, SIGKILL at the hard one is not ----
    import signal

    # The CPU this process has used, read when the signal arrives, goes with the claim: the
    # parent's own count covers the namespace setup as well, and a twelfth red team's strategy
    # raised SIGXCPU itself at 1.95s of 2 and was reported as having hit the limit.
    import resource
    getrusage, RUSAGE_SELF = resource.getrusage, resource.RUSAGE_SELF

    def on_xcpu(signum, frame):
        try:
            ru = getrusage(RUSAGE_SELF)
            used = ru.ru_utime + ru.ru_stime
        except Exception:  # noqa: BLE001 -- the claim still goes out, with nothing to back it
            used = None
        finish({"ok": False, "reason": "cpu_limit", "cpu": used}, 3)

    signal.signal(signal.SIGXCPU, on_xcpu)

    # -- the record: every socket use and every spawn, from any module, unremovable ------------
    NETWORK = {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"}
    SPAWN = {"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
             "os.forkpty"}

    def audit(event: str, args: tuple) -> None:
        if event in NETWORK:
            record("network", f"{event}{args[1:3]!r}" if len(args) > 1 else event)
            raise OSError(f"edgecheck: {event} is not available inside the sandbox")
        if event in SPAWN:
            record("spawn", f"{event}{args[:2]!r}")
            raise RuntimeError(f"edgecheck: {event} is not available inside the sandbox")

    sys.addaudithook(audit)

    # multiprocessing's spawn and forkserver contexts start a process through
    # _posixsubprocess.fork_exec directly, which raises no audit event, so a seventh red team
    # started one with nothing recorded. The stdlib looks the function up on the module at
    # call time, so replacing it here, before the strategy is imported, closes that path.
    import _posixsubprocess

    def refuse_fork_exec(*args, **kwargs):
        record("spawn", "_posixsubprocess.fork_exec")
        raise RuntimeError("edgecheck: _posixsubprocess.fork_exec is not available inside the sandbox")

    _posixsubprocess.fork_exec = refuse_fork_exec

    # And the kernel's refusal behind both, for every route neither of them sees.
    spawn_lock = lock_spawns()

    # -- the tape ----------------------------------------------------------------------------------
    bars = []
    with open(os.path.join(run_dir, "tape.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            bars.append(Bar(d["ts"], d["open"], d["high"], d["low"], d["close"], d["volume"]))

    def written(limit_entries: int = 20_000, limit_depth: int = 64) -> tuple[list, bool]:
        """Files under the run directory beyond those we put there. Inside the namespace the
        run directory is a tmpfs that dies with this process, so the parent cannot look for
        itself; this listing is a hint about caches, never evidence, and the parent treats it
        as such. Bounded: an explicit stack, a ceiling on entries and depth."""
        out, seen, over = [], 0, False
        stack = [(run_dir, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        seen += 1
                        if seen > limit_entries or depth > limit_depth:
                            return out, True
                        rel = os.path.relpath(e.path, run_dir)
                        if rel in ("tape.jsonl", "_child.py") or rel.startswith("strategy"):
                            continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((e.path, depth + 1))
                            elif e.is_file(follow_symlinks=False):
                                out.append(rel)
                        except OSError:
                            continue
            except OSError:
                continue
        return out, over

    # -- the strategy ------------------------------------------------------------------------------
    sys.path.insert(0, os.path.join(run_dir, "strategy"))
    try:
        module = importlib.import_module(entry)
        signals = getattr(module, func)
        out = signals(bars)
        # No coercion. int(0.5) is 0, a valid position, and a strategy emitting probabilities
        # would be audited as though it emitted decisions. numpy scalars are unwrapped with
        # .item(), which keeps int64 an int and float64 a float; the parent insists on ints in
        # {-1, 0, 1}. json.dumps happens inside the try so an unserialisable value is an error,
        # not a half-written result.
        values = [x.item() if hasattr(x, "item") else x for x in out]
        files, over = written(max_entries, max_depth)
        body = dumps({"ok": True, "signals": values, "files_written": sorted(files)[:2000],
                      "run_dir_over_limit": over, "spawn_lock": spawn_lock})
    except BaseException as e:  # noqa: BLE001 -- the error IS the report
        # The message is repr'd so a newline inside it cannot smuggle a reassuring last line
        # into the parent's summary; the formatted traceback rides along as an attachment.
        body = dumps({"ok": False,
                      "error": {"type": type(e).__qualname__, "message": repr(str(e))[:300]},
                      "traceback": format_exc()[-4000:], "spawn_lock": spawn_lock})
    try:
        write_all(out_fd, body.encode("utf-8"))
    finally:
        exit_(0)


if __name__ == "__main__":
    main(sys.argv)
