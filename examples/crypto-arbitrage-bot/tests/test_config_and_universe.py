import pytest

from arbbot.config import ConfigError, load_config
from arbbot.models import BINANCE, COINBASE, KRAKEN, USD_FAMILY
from arbbot.universe import STATIC_BASES, split_binance_symbol, static_universe, summarize


def test_defaults_are_paper_and_safe():
    cfg = load_config()
    assert cfg.live.enabled is False and cfg.live.real_orders is False
    assert cfg.detection.min_net_edge_bps > 0
    assert cfg.risk.max_notional_per_trade_usd <= 100
    assert cfg.detection.max_quote_age_ms_by_venue == {"binance": 5000.0, "kraken": 5000.0}


def test_toml_and_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[detection]\nmin_net_edge_bps = 3.5\n[venues]\ntaker_fee_bps = { binance = 7.5 }\n')
    cfg = load_config(p, {"risk": {"max_notional_per_trade_usd": 20}})
    assert cfg.detection.min_net_edge_bps == 3.5
    assert cfg.venues.taker_fee_bps == {"binance": 7.5}
    assert cfg.risk.max_notional_per_trade_usd == 20


def test_unknown_key_is_an_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[detection]\nmin_net_edge_bp = 3.5\n')
    with pytest.raises(ConfigError):
        load_config(p)
    with pytest.raises(ConfigError):
        load_config(None, {"universe": {"venues": ["ftx"]}})
    with pytest.raises(ConfigError):
        load_config(None, {"paper": {"fill_fraction": 0}})
    with pytest.raises(ConfigError):
        load_config(None, {"detection": {"max_plausible_net_edge_bps": 0.5}})


def test_split_binance_symbol():
    assert split_binance_symbol("BTCUSDT") == ("BTC", "USDT")
    assert split_binance_symbol("ETHBTC") == ("ETH", "BTC")
    assert split_binance_symbol("USDCUSDT") == ("USDC", "USDT")
    assert split_binance_symbol("USDT") is None


def test_static_universe_shape():
    cfg = load_config()
    markets = static_universe(cfg)
    by_venue = {}
    for m in markets:
        by_venue.setdefault(m.venue, []).append(m)
    assert len(by_venue[BINANCE]) >= 60 + 10
    assert len(by_venue[COINBASE]) >= 40 and len(by_venue[KRAKEN]) >= 50
    assert len({m.key for m in markets}) == len(markets)
    assert any(m.symbol == "USDT-USD" for m in by_venue[COINBASE])
    bases = {m.base for m in markets if m.quote in USD_FAMILY and m.base not in USD_FAMILY}
    assert len(bases) >= 50  # "scans over 50 markets"
    assert "ETHBTC" in {m.symbol for m in by_venue[BINANCE]}  # triangles exist
    assert "x" not in summarize(markets)


def test_static_universe_respects_symbols_and_venues():
    cfg = load_config(None, {"universe": {"binance_symbols": ["btcusdt", "ethbtc"], "venues": ["binance", "kraken"]}})
    markets = static_universe(cfg)
    assert {m.symbol for m in markets if m.venue == BINANCE} == {"BTCUSDT", "ETHBTC"}
    assert {m.symbol for m in markets if m.venue == KRAKEN} == {"BTC/USD", "USDT/USD", "USDC/USD"}
    assert not any(m.venue == COINBASE for m in markets)
    assert STATIC_BASES[0] == "BTC"
