import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _scratch_cwd(tmp_path, monkeypatch):
    """A stray STOP kill-switch file or logs/ in the repo must never leak into tests."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The suite must pass offline: any accidental socket use fails loudly."""
    import websockets

    def _refuse(*args, **kwargs):
        # pytest.fail raises outside the Exception hierarchy, so a gate that maps
        # "any exception" to a reason cannot turn a network attempt into a pass
        pytest.fail("network access attempted in an offline test")

    monkeypatch.setattr(websockets, "connect", _refuse)
    try:
        import websockets.asyncio.client as wac

        monkeypatch.setattr(wac, "connect", _refuse)
    except ImportError:
        pass
    try:
        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", _refuse)
    except ImportError:
        pass
