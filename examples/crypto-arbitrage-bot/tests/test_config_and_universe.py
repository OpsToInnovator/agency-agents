import pytest

from arbbot.config import ConfigError, load_config
from tests.conftest import ROOT
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
    with pytest.raises(ConfigError):
        load_config(None, {"paper": {"fill_model": "magic"}})
    with pytest.raises(ConfigError):
        load_config(None, {"detection": {"stable_rate_band": [1.1, 1.2]}})


def test_types_are_checked_and_tables_merge(tmp_path):
    with pytest.raises(ConfigError):
        load_config(None, {"universe": {"top_n": "60"}})
    with pytest.raises(ConfigError):
        load_config(None, {"universe": {"top_n": 60.5}})
    assert load_config(None, {"universe": {"top_n": 60.0}}).universe.top_n == 60
    with pytest.raises(ConfigError):
        load_config(None, {"detection": {"min_net_edge_bps": "1.0"}})
    with pytest.raises(ConfigError):
        load_config(None, {"live": {"enabled": "yes"}})
    cfg = load_config(None, {"detection": {"max_quote_age_ms_by_venue": {"binance": 9000.0}}})
    assert cfg.detection.max_quote_age_ms_by_venue == {"binance": 9000.0, "kraken": 5000.0}  # merged, not replaced
    for bad in ({"venues": {"taker_fee_bps": {"binanace": 2.0}}}, {"venues": {"taker_fee_bps": {"binance": -50.0}}},
                {"venues": {"taker_fee_bps": 5.0}}, {"detection": {"max_quote_age_ms_by_venue": {"ftx": 1}}},
                {"detection": {"max_quote_age_ms_by_venue": {"kraken": 0}}}, {"universe": {"venues": []}},
                {"universe": {"venues": ["binance", "binance"]}}):
        with pytest.raises(ConfigError):
            load_config(None, bad)
    p = tmp_path / "broken.toml"
    p.write_text("[detection\n")
    with pytest.raises(ConfigError):
        load_config(p)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_per_venue_tables_are_case_insensitive_and_elements_typed():
    cfg = load_config(None, {"detection": {"max_quote_age_ms_by_venue": {"Binance": 7000}}})
    assert cfg.detection.max_quote_age_ms_by_venue == {"binance": 7000.0, "kraken": 5000.0}
    cfg = load_config(None, {"venues": {"taker_fee_bps": {"KRAKEN": 26}}})
    assert cfg.venues.taker_fee_bps["kraken"] == 26.0 and "KRAKEN" not in cfg.venues.taker_fee_bps
    with pytest.raises(ConfigError):
        load_config(None, {"venues": {"taker_fee_bps": {"kraken": 26, "Kraken": 27}}})
    with pytest.raises(ConfigError):
        load_config(None, {"universe": {"venues": ["binance", 3]}})
    with pytest.raises(ConfigError):
        load_config(None, {"venues": {"taker_fee_bps": {"binance": "ten"}}})
    with pytest.raises(ConfigError):
        load_config(None, {"detection": {"stable_rate_band": ["a", "b"]}})


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.paper.fill_model == "arrival"
    assert cfg.live.enabled is False


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


def test_static_universe_fallbacks_respect_top_n_and_explicit_symbols():
    cfg = load_config(None, {"universe": {"venues": ["coinbase", "kraken"], "top_n": 5}})
    markets = static_universe(cfg)
    assert len({m.base for m in markets if m.base not in USD_FAMILY}) == 5
    cfg = load_config(None, {"universe": {"binance_symbols": ["ETHBTC"]}})
    markets = static_universe(cfg)
    assert {m.symbol for m in markets if m.venue == BINANCE} == {"ETHBTC"}
    assert not any(m.venue != BINANCE and m.base not in USD_FAMILY for m in markets)  # nothing the operator did not ask for


def test_cli_overrides_do_not_drop_zero_or_empty():
    from arbbot.cli import build_parser, _overrides

    args = build_parser().parse_args(["markets", "--static", "--top", "0"])
    with pytest.raises(ConfigError):
        load_config(None, _overrides(args))
    args = build_parser().parse_args(["markets", "--static", "--venues", ""])
    with pytest.raises(ConfigError):
        load_config(None, _overrides(args))


def test_reconnect_settings_are_validated():
    with pytest.raises(ConfigError):
        load_config(None, {"venues": {"reconnect_min_s": 0}})
    with pytest.raises(ConfigError):
        load_config(None, {"venues": {"reconnect_min_s": 10, "reconnect_max_s": 5}})


def test_static_universe_respects_symbols_and_venues():
    cfg = load_config(None, {"universe": {"binance_symbols": ["btcusdt", "ethbtc"], "venues": ["binance", "kraken"]}})
    markets = static_universe(cfg)
    assert {m.symbol for m in markets if m.venue == BINANCE} == {"BTCUSDT", "ETHBTC"}
    assert {m.symbol for m in markets if m.venue == KRAKEN} == {"BTC/USD", "USDT/USD", "USDC/USD"}
    assert not any(m.venue == COINBASE for m in markets)
    assert STATIC_BASES[0] == "BTC"


def test_live_example_config_is_a_stage_b_start_sized_to_the_stake():
    from arbbot.config import ConfigError, load_config

    cfg = load_config(ROOT / "live.example.toml")
    assert cfg.live.enabled is True and cfg.live.real_orders is False  # validation-only orders until flipped
    assert cfg.live.capital_usd == 500.0
    assert cfg.risk.max_notional_per_trade_usd == 0.10 * cfg.live.capital_usd
    assert cfg.risk.max_daily_loss_usd == 0.01 * cfg.live.capital_usd
    assert cfg.risk.max_drawdown_pct == 5.0 and cfg.risk.min_profit_usd == 0.005
    assert cfg.universe.venues == ["binance"] and cfg.detection.cross_exchange is False and cfg.detection.triangular is True
    assert cfg.universe.auto_discover is True  # live orders need exchange filters
    with pytest.raises(ConfigError):
        load_config(None, {"live": {"capital_usd": -1.0}})


def test_example_config_lists_every_key():
    """config.example.toml promises "every key ... these are the defaults": keep it true."""
    import tomllib
    from dataclasses import fields, is_dataclass

    from arbbot.config import Config

    data = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    for section in fields(Config):
        sub = getattr(Config(), section.name)
        assert is_dataclass(sub)
        assert set(data[section.name]) == {f.name for f in fields(sub)}, section.name


def test_live_cumulative_loss_budget_validates():
    from arbbot.config import ConfigError, load_config

    assert load_config(None, {"live": {"max_cumulative_loss_pct": 0.0}}).live.max_cumulative_loss_pct == 0.0
    for bad in (-1.0, 100.5):
        with pytest.raises(ConfigError):
            load_config(None, {"live": {"max_cumulative_loss_pct": bad}})
