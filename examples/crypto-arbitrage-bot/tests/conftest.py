import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The suite must pass offline: any accidental socket use fails loudly."""
    import websockets

    def _refuse(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

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
