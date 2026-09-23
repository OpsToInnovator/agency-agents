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

    def on_xcpu(signum, frame):
        finish({"ok": False, "reason": "cpu_limit"}, 3)

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
        body = dumps({"ok": True, "signals": values, "files_written": sorted(files)[:2000], "run_dir_over_limit": over})
    except BaseException as e:  # noqa: BLE001 -- the error IS the report
        # The message is repr'd so a newline inside it cannot smuggle a reassuring last line
        # into the parent's summary; the formatted traceback rides along as an attachment.
        body = dumps({"ok": False,
                      "error": {"type": type(e).__qualname__, "message": repr(str(e))[:300]},
                      "traceback": format_exc()[-4000:]})
    try:
        write_all(out_fd, body.encode("utf-8"))
    finally:
        exit_(0)


if __name__ == "__main__":
    main(sys.argv)
