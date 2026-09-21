"""Runs inside the sandbox. Loads a tape, imports the customer's strategy, writes its decisions.

Deliberately self-contained: it must not import edgecheck, because inside the sandbox the
package may be hidden, the environment is scrubbed and the interpreter runs with -s -B. The
Bar defined here has the same five fields as the one in the fixtures; the strategy contract
only requires attribute access, so a different class is fine.

The network ban is installed HERE, before the strategy is imported, rather than through
sitecustomize -- there is nothing for site machinery to pick up under a scrubbed
environment, and putting it in the runner means it cannot be forgotten. It is a
recording ban: the attempt is written to violations.jsonl before the exception is raised,
so a strategy that swallows the exception is still on record. In namespace isolation the
kernel blocks the connection as well; this layer exists so the report can name the call.
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


def _install_network_ban(run_dir: str) -> None:
    import socket

    def record(kind: str, detail: str) -> None:
        with open(os.path.join(run_dir, "violations.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "detail": detail}) + "\n")

    class BannedSocket(socket.socket):
        def __init__(self, *a, **kw):  # noqa: D401
            record("network", f"socket.socket{a!r}")
            raise OSError("edgecheck: network is not available inside the sandbox")

    def banned(name):
        def _f(*a, **kw):
            record("network", f"socket.{name}{a[:2]!r}")
            raise OSError(f"edgecheck: socket.{name} is not available inside the sandbox")
        return _f

    socket.socket = BannedSocket  # type: ignore[misc]
    socket.create_connection = banned("create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = banned("getaddrinfo")  # type: ignore[assignment]


def _write(run_dir: str, payload: dict) -> None:
    with open(os.path.join(run_dir, "out.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _install_cpu_notice(run_dir: str) -> None:
    """At the soft CPU limit the kernel sends SIGXCPU; the hard limit, a little later, is
    SIGKILL and cannot be caught. This window is for writing down what happened, because
    the process that launched us cannot always tell a signal death from a clean exit."""
    import signal

    def on_xcpu(signum, frame):
        _write(run_dir, {"ok": False, "reason": "cpu_limit"})
        os._exit(3)

    signal.signal(signal.SIGXCPU, on_xcpu)


def main(argv: list[str]) -> int:
    run_dir, entry, func = argv[1], argv[2], argv[3]
    os.chdir(run_dir)
    _install_cpu_notice(run_dir)
    _install_network_ban(run_dir)

    bars = []
    with open(os.path.join(run_dir, "tape.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            bars.append(Bar(d["ts"], d["open"], d["high"], d["low"], d["close"], d["volume"]))

    sys.path.insert(0, os.path.join(run_dir, "strategy"))
    try:
        module = importlib.import_module(entry)
        signals = getattr(module, func)
        out = signals(bars)
        # No coercion here. int(0.5) is 0, a valid position, and a strategy emitting
        # probabilities would be audited as though it emitted decisions. numpy scalars are
        # unwrapped with .item(), which keeps int64 an int and float64 a float; the parent
        # then insists on ints in {-1, 0, 1} and refuses anything else.
        payload = {"ok": True, "signals": [x.item() if hasattr(x, "item") else x for x in out]}
    except BaseException:  # noqa: BLE001 -- the traceback IS the report
        payload = {"ok": False, "traceback": traceback.format_exc()[-4000:]}

    _write(run_dir, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
