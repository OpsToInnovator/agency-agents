#!/usr/bin/env python3
"""Read the REAL taker fees of your own exchange accounts and print a [venues] block for arbbot.

Keys come from the environment only (never written anywhere):
  BINANCE_API_KEY / BINANCE_API_SECRET          -> GET /api/v3/account/commission?symbol=...   (per symbol!)
  KRAKEN_API_KEY / KRAKEN_API_SECRET            -> POST /0/private/TradeVolume  (needs 'Query Funds' permission)
  COINBASE_EXCHANGE_KEY / _SECRET / _PASSPHRASE -> GET /fees on api.exchange.coinbase.com (Coinbase Exchange accounts)
  COINBASE_CDP_KEY_NAME / COINBASE_CDP_PRIVATE_KEY -> GET /api/v3/brokerage/transaction_summary (Advanced Trade, retail;
                                                    needs `pip install PyJWT cryptography`)
Usage: python3 scripts/read_fees.py BTCUSDT ETHUSDT UNIUSDT UNIBTC ...   (Binance symbols to query; Kraken/Coinbase
       report account-level fees).  Optional: KRAKEN_PAIRS="XBTUSD,ETHUSD" to change the Kraken pairs asked about.
Read-only endpoints; use API keys with NO withdrawal permission. Nothing is written to disk; the printed
report omits the venues' raw payloads. Docs verified 2026-09-19:
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints
  https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getfees
  https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/fees/get-transaction-summary
  https://docs.kraken.com/api/docs/rest-api/get-trade-volume
"""
import base64, hashlib, hmac, json, os, sys, time, urllib.parse, urllib.request

def http(method, url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def binance(symbols):
    key, sec = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
    if not (key and sec):
        return None, "BINANCE_API_KEY/SECRET not set"
    base = os.environ.get("BINANCE_TRADE_REST", "https://api.binance.com")
    out = {}
    for sym in symbols:
        q = urllib.parse.urlencode({"symbol": sym, "timestamp": int(time.time() * 1000), "recvWindow": 5000})
        sig = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = http("GET", f"{base}/api/v3/account/commission?{q}&signature={sig}", {"X-MBX-APIKEY": key})
        taker = float(r["standardCommission"]["taker"]) + float(r.get("taxCommission", {}).get("taker", 0) or 0)
        disc = r.get("discount", {})
        eff = taker * (1 - float(disc.get("discount", 0) or 0)) if disc.get("enabledForAccount") and disc.get("enabledForSymbol") else taker
        out[sym] = {"taker_bps": taker * 1e4, "taker_bps_after_bnb_discount": eff * 1e4, "raw": r}
    return out, ""

def kraken(pairs):
    key, sec = os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET")
    if not (key and sec):
        return None, "KRAKEN_API_KEY/SECRET not set"
    path = "/0/private/TradeVolume"
    nonce = str(int(time.time() * 1000))
    post = urllib.parse.urlencode({"nonce": nonce, "pair": ",".join(pairs), "fee-info": "true"})
    msg = path.encode() + hashlib.sha256((nonce + post).encode()).digest()
    sig = base64.b64encode(hmac.new(base64.b64decode(sec), msg, hashlib.sha512).digest()).decode()
    r = http("POST", "https://api.kraken.com" + path, {"API-Key": key, "API-Sign": sig,
             "Content-Type": "application/x-www-form-urlencoded"}, post.encode())
    if r.get("error"):
        return None, str(r["error"])
    res = r["result"]
    return {"volume_30d": res.get("volume"), "taker_pct_by_pair": {p: v["fee"] for p, v in res.get("fees", {}).items()},
            "maker_pct_by_pair": {p: v["fee"] for p, v in res.get("fees_maker", {}).items()}, "raw": res}, ""

def coinbase_exchange():
    key, sec, pw = (os.environ.get("COINBASE_EXCHANGE_KEY"), os.environ.get("COINBASE_EXCHANGE_SECRET"),
                    os.environ.get("COINBASE_EXCHANGE_PASSPHRASE"))
    if not (key and sec and pw):
        return None, "COINBASE_EXCHANGE_KEY/SECRET/PASSPHRASE not set"
    ts = str(time.time()); path = "/fees"
    sig = base64.b64encode(hmac.new(base64.b64decode(sec), (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    r = http("GET", "https://api.exchange.coinbase.com" + path, {"CB-ACCESS-KEY": key, "CB-ACCESS-SIGN": sig,
             "CB-ACCESS-TIMESTAMP": ts, "CB-ACCESS-PASSPHRASE": pw, "Accept": "application/json"})
    return {"taker_bps": float(r["taker_fee_rate"]) * 1e4, "maker_bps": float(r["maker_fee_rate"]) * 1e4,
            "usd_volume_30d": r.get("usd_volume"), "raw": r}, ""

def coinbase_advanced():
    name, pem = os.environ.get("COINBASE_CDP_KEY_NAME"), os.environ.get("COINBASE_CDP_PRIVATE_KEY")
    if not (name and pem):
        return None, "COINBASE_CDP_KEY_NAME/PRIVATE_KEY not set"
    try:
        import jwt  # PyJWT
    except ImportError:
        return None, "pip install PyJWT cryptography"
    import secrets
    host, path = "api.coinbase.com", "/api/v3/brokerage/transaction_summary"
    now = int(time.time())
    token = jwt.encode({"sub": name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"GET {host}{path}"},
                       pem.replace("\\n", "\n"), algorithm="ES256", headers={"kid": name, "nonce": secrets.token_hex()})
    r = http("GET", f"https://{host}{path}", {"Authorization": f"Bearer {token}"})
    ft = r.get("fee_tier", {})
    return {"pricing_tier": ft.get("pricing_tier"), "taker_bps": float(ft.get("taker_fee_rate", "nan")) * 1e4,
            "maker_bps": float(ft.get("maker_fee_rate", "nan")) * 1e4, "total_volume_usd": r.get("total_volume"), "raw": r}, ""

if __name__ == "__main__":
    symbols = sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "BNBUSDT", "UNIUSDT", "UNIBTC", "ETHBTC"]
    report = {}
    kraken_pairs = [p for p in os.environ.get("KRAKEN_PAIRS", "XBTUSD,ETHUSD").split(",") if p]
    for name, fn, args in [("binance", binance, (symbols,)), ("kraken", kraken, (kraken_pairs,)),
                           ("coinbase_exchange", coinbase_exchange, ()), ("coinbase_advanced", coinbase_advanced, ())]:
        try:
            data, err = fn(*args)
        except Exception as exc:  # show the venue's error text, never the key
            data, err = None, f"{type(exc).__name__}: {exc}"
        report[name] = data if data is not None else {"error": err}
        print(f"[{name}] {'OK' if data is not None else 'SKIP: ' + err}")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'raw'} for k, v in report.items()}, indent=2, default=str))
    # arbbot takes ONE taker fee per venue: use the WORST (max) taker across the symbols you will trade.
    b = report.get("binance", {})
    b_max = max((v["taker_bps"] for v in b.values() if isinstance(v, dict) and "taker_bps" in v), default=None)
    k = report.get("kraken", {}); k_max = max((float(x) * 100 for x in k.get("taker_pct_by_pair", {}).values()), default=None)
    c = report.get("coinbase_advanced", {}); c_t = c.get("taker_bps") or report.get("coinbase_exchange", {}).get("taker_bps")
    print("\n# paste into config.toml (fees as bps; a missing venue keeps arbbot's conservative default):\n[venues]")
    print("taker_fee_bps = { " + ", ".join(f"{v} = {x:.2f}" for v, x in (("binance", b_max), ("coinbase", c_t), ("kraken", k_max)) if x is not None) + " }")
    print(f"# read on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}; Binance fees are PER SYMBOL - re-run if you change the universe")
