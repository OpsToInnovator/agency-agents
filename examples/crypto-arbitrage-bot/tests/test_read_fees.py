"""read_fees.py is the one script whose output the operator pastes into the config, so its
arithmetic, its secret handling and its key loading are pinned offline (every venue call is stubbed)."""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("read_fees", ROOT / "scripts" / "read_fees.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


read_fees = _load()

# the documented example payload of GET /api/v3/account/commission
BINANCE_PAYLOAD = {
    "symbol": "BTCUSDT",
    "standardCommission": {"maker": "0.00000010", "taker": "0.001", "buyer": "0", "seller": "0"},
    "taxCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
    "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.75000000"},
}


@pytest.fixture
def binance_env(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "dummy-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "dummy-secret")
    for var in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "COINBASE_EXCHANGE_KEY", "COINBASE_EXCHANGE_SECRET",
                "COINBASE_EXCHANGE_PASSPHRASE", "COINBASE_CDP_KEY_NAME", "COINBASE_CDP_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)


def _stub(monkeypatch, payload):
    calls = []

    def fake_http(method, url, headers=None, data=None, timeout=15):
        calls.append((method, url, headers))
        return json.loads(json.dumps(payload))

    monkeypatch.setattr(read_fees, "http", fake_http)
    return calls


def test_bnb_discount_is_the_multiplier_that_remains(binance_env, monkeypatch):
    calls = _stub(monkeypatch, BINANCE_PAYLOAD)
    out, err = read_fees.binance(["BTCUSDT"])
    assert err == "" and out["BTCUSDT"]["taker_bps"] == pytest.approx(10.0)
    assert out["BTCUSDT"]["taker_bps_after_bnb_discount"] == pytest.approx(7.5)  # 25% off, as fees.py documents
    assert out["BTCUSDT"]["bnb_discount_multiplier"] == 0.75
    assert "raw" not in out["BTCUSDT"]
    method, url, headers = calls[0]
    assert method == "GET" and "signature=" in url and headers["X-MBX-APIKEY"] == "dummy-key"


def test_tax_commission_is_added_but_not_discounted(binance_env, monkeypatch):
    payload = json.loads(json.dumps(BINANCE_PAYLOAD))
    payload["taxCommission"]["taker"] = "0.0005"
    _stub(monkeypatch, payload)
    out, _ = read_fees.binance(["BTCUSDT"])
    assert out["BTCUSDT"]["taker_bps"] == pytest.approx(15.0)
    assert out["BTCUSDT"]["taker_bps_after_bnb_discount"] == pytest.approx(12.5)


@pytest.mark.parametrize("mutate", [lambda p: p["discount"].update({"enabledForSymbol": False}),
                                    lambda p: p["discount"].update({"enabledForAccount": False}),
                                    lambda p: p.pop("discount")])
def test_no_discount_when_it_is_off_or_absent(binance_env, monkeypatch, mutate):
    payload = json.loads(json.dumps(BINANCE_PAYLOAD))
    mutate(payload)
    _stub(monkeypatch, payload)
    out, _ = read_fees.binance(["BTCUSDT"])
    assert out["BTCUSDT"]["taker_bps"] == 10.0 and out["BTCUSDT"]["taker_bps_after_bnb_discount"] == 10.0


def test_main_prints_a_paste_block_from_the_worst_symbol_and_never_the_raw_payload(binance_env, monkeypatch, capsys):
    payloads = {"BTCUSDT": json.loads(json.dumps(BINANCE_PAYLOAD)), "UNIBTC": json.loads(json.dumps(BINANCE_PAYLOAD))}
    payloads["UNIBTC"]["standardCommission"]["taker"] = "0.0012"

    def fake_http(method, url, headers=None, data=None, timeout=15):
        sym = url.split("symbol=")[1].split("&")[0]
        return payloads[sym]

    monkeypatch.setattr(read_fees, "http", fake_http)
    assert read_fees.main(["BTCUSDT", "UNIBTC"]) == 0
    out = capsys.readouterr().out
    assert "taker_fee_bps = { binance = 12.00 }" in out  # the worst symbol, undiscounted
    assert "standardCommission" not in out and "discountAsset" not in out and "dummy" not in out
    assert "[kraken] SKIP" in out and "[coinbase_exchange] SKIP" in out and "[coinbase_advanced] SKIP" in out


def test_signed_requests_never_follow_a_redirect():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("X-MBX-APIKEY")))
            if self.path.startswith("/first"):
                self.send_response(302)
                self.send_header("Location", "/second")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/first"
        with pytest.raises(urllib.error.HTTPError) as exc:
            read_fees.http("GET", url, {"X-MBX-APIKEY": "dummy-key"})
        assert exc.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
    assert hits == [("/first", "dummy-key")]  # the key was never re-sent to /second


def test_cdp_key_loader_picks_the_jwt_algorithm_from_the_key_type():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    pem = lambda key: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ed_key = ed25519.Ed25519PrivateKey.generate()
    assert read_fees.load_cdp_key(pem(ec_key))[1] == "ES256"
    assert read_fees.load_cdp_key(pem(ed_key).replace("\n", "\\n"))[1] == "EdDSA"  # env-var style escaped newlines
    seed = ed_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = ed_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert read_fees.load_cdp_key(base64.b64encode(seed).decode())[1] == "EdDSA"
    assert read_fees.load_cdp_key(base64.b64encode(seed + pub).decode())[1] == "EdDSA"
    with pytest.raises(ValueError) as exc:
        read_fees.load_cdp_key("not a key at all")
    assert "not a key" not in str(exc.value)  # the secret is never echoed
    with pytest.raises(ValueError):
        read_fees.load_cdp_key(base64.b64encode(b"short").decode())
