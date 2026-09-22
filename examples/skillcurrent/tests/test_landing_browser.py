"""Run the browser test of the landing page's waitlist form when Playwright is available.

The test itself is ``tests/browser/landing_form.mjs`` (Node + Playwright). It is
skipped, not failed, when Node or the ``playwright`` package is missing, so the
standard-library-only test suite still runs everywhere.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "browser" / "landing_form.mjs"


def _node_env() -> dict | None:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node:
        return None
    env = dict(os.environ)
    paths = [p for p in env.get("NODE_PATH", "").split(os.pathsep) if p]
    if npm:
        try:
            global_root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
            if global_root:
                paths.append(global_root)
        except (OSError, subprocess.SubprocessError):
            pass
    env["NODE_PATH"] = os.pathsep.join(paths)
    probe = subprocess.run([node, "-e", "require('playwright')"], env=env, capture_output=True, text=True, timeout=60)
    return env if probe.returncode == 0 else None


def test_landing_form_in_a_real_browser():
    env = _node_env()
    if env is None:
        pytest.skip("node with the playwright package is not available")
    env.setdefault("PYTHON", shutil.which("python3") or "python3")
    result = subprocess.run(["node", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "landing form: ok" in result.stdout
