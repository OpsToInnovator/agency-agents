"""Configuration: dataclasses with safe defaults, optionally loaded from TOML.

Unknown keys are an error so a typo like `min_net_edge_bp` cannot silently
leave a default in place.
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .models import BINANCE, COINBASE, KRAKEN


@dataclass
class UniverseConfig:
    # Explicit Binance symbols (e.g. ["BTCUSDT", "ETHBTC"]). Empty = discover top_n by volume.
    binance_symbols: list[str] = field(default_factory=list)
    top_n: int = 60
    auto_discover: bool = True
    venues: list[str] = field(default_factory=lambda: [BINANCE, COINBASE, KRAKEN])
    # Quote assets to add cross pairs for (X/BTC, X/ETH ...) so triangles exist.
    triangle_quotes: list[str] = field(default_factory=lambda: ["BTC", "ETH", "BNB"])
    # Subscribe to USDT-USD style markets so USDT prices can be converted to USD.
    track_stable_rates: bool = True


@dataclass
class VenuesConfig:
    taker_fee_bps: dict[str, float] = field(default_factory=dict)  # overrides fees.DEFAULT_TAKER_BPS
    # Public market-data endpoints. api.binance.com answers HTTP 451 in some
    # regions; the binance.vision mirror serves the same public data.
    binance_ws: str = "wss://data-stream.binance.vision/stream"
    binance_rest: str = "https://data-api.binance.vision"
    # Signed endpoints in live mode, and the unsigned reachability ping that `preflight`
    # and `preflight --connectivity` send (no keys, no [live] section needed).
    binance_trade_rest: str = "https://api.binance.com"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"
    coinbase_rest: str = "https://api.exchange.coinbase.com"
    kraken_ws: str = "wss://ws.kraken.com/v2"
    kraken_rest: str = "https://api.kraken.com"
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 60.0


@dataclass
class DetectionConfig:
    cross_exchange: bool = True
    triangular: bool = True
    anomaly: bool = True
    min_net_edge_bps: float = 1.0  # act only when net-of-fee edge is at least this
    max_quote_age_ms: float = 2000.0  # ignore quotes older than this
    # Binance bookTicker and Kraken (bbo trigger) push on every top-of-book
    # change, so silence means "unchanged"; Coinbase's ticker only fires on
    # trades, so silence means "unknown". Quotes are dropped on disconnect.
    max_quote_age_ms_by_venue: dict[str, float] = field(default_factory=lambda: {"binance": 5000.0, "kraken": 5000.0})
    # A cross-venue gap this wide is not a price error, it is two different
    # assets sharing a ticker (Binance ONE vs Kraken ONE). The asset is
    # quarantined from cross-exchange comparison for the rest of the run.
    identity_mismatch_bps: float = 2000.0
    # Net edges above this are treated as bad data and never traded.
    max_plausible_net_edge_bps: float = 200.0
    anchor_venue: str = "binance"  # marks for quarantined assets come from here
    # A learned USDT/USD rate outside this band is bad data: keep the last sane one.
    stable_rate_band: list[float] = field(default_factory=lambda: [0.97, 1.03])
    stable_haircut_bps: float = 5.0  # extra edge demanded when USDT is compared with USD
    anomaly_threshold_bps: float = 50.0  # |deviation| from reference to call it a "price error"
    anomaly_ewma_halflife_s: float = 10.0
    anomaly_cooldown_s: float = 5.0
    triangle_start_assets: list[str] = field(default_factory=lambda: ["USDT"])


@dataclass
class RiskConfig:
    max_notional_per_trade_usd: float = 100.0
    max_daily_loss_usd: float = 25.0
    max_trades_per_minute: int = 30
    cooldown_s: float = 2.0  # per opportunity key
    max_detect_latency_ms: float = 250.0
    kill_switch_file: str = "STOP"
    min_profit_usd: float = 0.05  # dust-sized "opportunities" are not actionable
    # Halt when equity falls this far below its running peak (a slow bleed that stays
    # under the daily cap). 0 disables. Peak persists with the daily state.
    max_drawdown_pct: float = 5.0
    # Daily realized loss and halt state persist here so a restart cannot reset the cap.
    state_file: str = "logs/risk_state.json"


@dataclass
class PaperConfig:
    starting_quote_per_venue_usd: float = 1000.0
    starting_base_inventory_usd: float = 200.0  # lazily funded per base asset per venue
    slippage_bps: float = 2.0
    fill_fraction: float = 1.0  # fraction of displayed top-of-book size assumed fillable
    # "arrival": an order reaches the venue assumed_rtt_ms after detection and fills as an
    # IOC limit at the detection price against the book AT THAT TIME (misses if the touch
    # moved away). "instant": fills at detection, the generous model.
    fill_model: str = "arrival"
    assumed_rtt_ms: float = 150.0  # detection -> venue, per leg; triangle legs are sequential


@dataclass
class LiveConfig:
    # Both must be true AND the CLI must be run with --live for orders to be sent.
    enabled: bool = False
    real_orders: bool = False  # false = POST /api/v3/order/test (validates, never fills)
    # The stake you consider at risk, in USD. Live balances live on the exchange, so the
    # drawdown-from-peak cap (risk.max_drawdown_pct) is measured against this figure;
    # 0 leaves that cap off for live mode (the daily loss cap and kill switch still apply).
    capital_usd: float = 0.0
    # The kill budget as a percentage of capital_usd. Preflight refuses to re-arm once the
    # USDT on the exchange (free plus locked) is below capital less this budget: a spent
    # budget means "do not restart on the same settings", not "top up and carry on".
    max_cumulative_loss_pct: float = 10.0
    # BNB deposited to pay fees with, in USD. `reconcile` treats that much BNB as a fee
    # float and anything above it as inventory a cycle left behind (0 = any BNB is inventory).
    fee_float_usd: float = 0.0
    # Compounding: before the first cycle of each UTC day, re-base the stake from the USDT
    # on the exchange (free plus locked) and scale the per-trade cap, the daily loss cap and
    # the drawdown base with it (each keeps its ratio to capital_usd). The kill floor stays
    # anchored to the original capital_usd, so a slow bleed cannot re-base it away: a re-base
    # that finds the stake below capital_usd less max_cumulative_loss_pct halts sticky.
    compound: bool = False
    # A cycle that fills leg 1 and then fails leaves the account holding an asset it never
    # wanted, with full market exposure, until a human runs `arbbot reconcile`. On: sell it
    # straight back to the cycle's start asset (a bounded LIMIT IOC) and keep trading when
    # that worked. Off: book the loss and halt sticky, as before. It NEVER runs when the
    # fill state is unknown, when the venue is rate-limiting us, or when the STOP file
    # exists, and it is inert unless real_orders is on.
    auto_unwind: bool = True
    unwind_max_slippage_bps: float = 25.0  # worst price accepted, ANCHORED to the touch at the abort
    unwind_max_attempts: int = 3           # TOTAL orders for one broken cycle, not per asset
    unwind_dust_usd: float = 5.0           # at or below this, a filter-rejected residue is dust
    unwind_dust_halt_usd: float = 25.0     # cumulative dust written off this session before halting
    unwind_halt_after: int = 3             # successful unwinds in a rolling hour before halting sticky
    api_key_env: str = "BINANCE_API_KEY"
    api_secret_env: str = "BINANCE_API_SECRET"
    recv_window_ms: int = 5000
    intent_log: str = "logs/live_intents.jsonl"  # every order intent is journaled here before it is sent


@dataclass
class ReportConfig:
    interval_s: float = 10.0
    log_dir: str = "logs"
    write_jsonl: bool = True
    log_every_opportunity: bool = False


@dataclass
class Config:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    venues: VenuesConfig = field(default_factory=VenuesConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    def enabled_venues(self) -> list[str]:
        return [v.lower() for v in self.universe.venues]


class ConfigError(ValueError):
    pass


def _coerce(kind: str, value: Any, where: str) -> Any:
    """Check a TOML/override value against the dataclass field annotation."""
    kind = kind.split("|")[0].strip()
    base = kind.split("[")[0]
    if base == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if base == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        if isinstance(value, float):
            if not value.is_integer():
                raise ConfigError(f"{where}: expected an integer, got {value!r}")
            value = int(value)
        return value
    if base == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if base == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        return value
    inner = kind[len(base) + 1:-1] if "[" in kind else ""
    if base == "list":
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list, got {value!r}")
        return [_coerce(inner, v, f"{where}[{i}]") if inner else v for i, v in enumerate(value)]
    if base == "dict":
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table, got {value!r}")
        vkind = inner.split(",", 1)[1].strip() if "," in inner else ""
        return {k: (_coerce(vkind, v, f"{where}.{k}") if vkind else v) for k, v in value.items()}
    return value


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'root'}: expected a table, got {type(data).__name__}")
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown config key {path + key!r}")
        current = getattr(obj, key)
        where = f"{path}{key}"
        if is_dataclass(current) and not isinstance(current, type):
            _apply(current, value, f"{where}.")
        elif isinstance(current, dict):
            # per-venue tables merge over the defaults, so one venue can be set alone;
            # keys are venue names and compare case-insensitively everywhere else
            incoming = _coerce(str(known[key].type), value, where)
            lowered: dict[str, Any] = {}
            for k, v in incoming.items():
                lk = str(k).lower()
                if lk in lowered:
                    raise ConfigError(f"{where}: key {k!r} given twice (case-insensitive)")
                lowered[lk] = v
            merged = dict(current)
            merged.update(lowered)
            setattr(obj, key, merged)
        else:
            setattr(obj, key, _coerce(str(known[key].type), value, where))


def load_config(path: str | os.PathLike[str] | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Build a Config from defaults, then a TOML file, then a dict of overrides."""
    cfg = Config()
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
        _apply(cfg, data)
    if overrides:
        _apply(cfg, overrides)
    _validate(cfg)
    return cfg


KNOWN_VENUES = (BINANCE, COINBASE, KRAKEN)


def _validate(cfg: Config) -> None:
    if not cfg.universe.venues:
        raise ConfigError("universe.venues must list at least one of binance, coinbase, kraken")
    lowered = [str(v).lower() for v in cfg.universe.venues]
    for v in lowered:
        if v not in KNOWN_VENUES:
            raise ConfigError(f"unknown venue {v!r}")
    if len(set(lowered)) != len(lowered):
        raise ConfigError("universe.venues lists a venue twice")
    for key, value in cfg.venues.taker_fee_bps.items():
        if str(key).lower() not in KNOWN_VENUES:
            raise ConfigError(f"venues.taker_fee_bps: unknown venue {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 10000:
            raise ConfigError(f"venues.taker_fee_bps.{key} must be a non-negative number of basis points")
    for key, value in cfg.detection.max_quote_age_ms_by_venue.items():
        if str(key).lower() not in KNOWN_VENUES:
            raise ConfigError(f"detection.max_quote_age_ms_by_venue: unknown venue {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"detection.max_quote_age_ms_by_venue.{key} must be a positive number of milliseconds")
    if cfg.detection.max_quote_age_ms <= 0:
        raise ConfigError("detection.max_quote_age_ms must be positive")
    if cfg.detection.anchor_venue.lower() not in (BINANCE, COINBASE, KRAKEN):
        raise ConfigError(f"unknown anchor_venue {cfg.detection.anchor_venue!r}")
    if cfg.detection.max_plausible_net_edge_bps <= cfg.detection.min_net_edge_bps:
        raise ConfigError("detection.max_plausible_net_edge_bps must exceed min_net_edge_bps")
    if cfg.risk.max_notional_per_trade_usd <= 0:
        raise ConfigError("risk.max_notional_per_trade_usd must be positive")
    if cfg.risk.max_daily_loss_usd <= 0:
        raise ConfigError("risk.max_daily_loss_usd must be positive")
    # Paths resolve against the directory the bot was started in, once, so a kill
    # switch touched "in the repo" from another cwd cannot silently miss.
    if cfg.risk.kill_switch_file:
        cfg.risk.kill_switch_file = str(Path(cfg.risk.kill_switch_file).expanduser().resolve())
    if cfg.risk.state_file:
        cfg.risk.state_file = str(Path(cfg.risk.state_file).expanduser().resolve())
    if cfg.live.intent_log:
        cfg.live.intent_log = str(Path(cfg.live.intent_log).expanduser().resolve())
    if not 0 < cfg.paper.fill_fraction <= 1:
        raise ConfigError("paper.fill_fraction must be in (0, 1]")
    if cfg.paper.slippage_bps < 0:
        raise ConfigError("paper.slippage_bps must be >= 0")
    if not 0 < cfg.venues.reconnect_min_s <= cfg.venues.reconnect_max_s:
        raise ConfigError("venues.reconnect_min_s must be > 0 and <= reconnect_max_s")
    if cfg.paper.fill_model not in ("arrival", "instant"):
        raise ConfigError("paper.fill_model must be 'arrival' or 'instant'")
    if cfg.paper.assumed_rtt_ms < 0:
        raise ConfigError("paper.assumed_rtt_ms must be >= 0")
    if cfg.live.capital_usd < 0:
        raise ConfigError("live.capital_usd must be >= 0 (0 = drawdown cap off in live mode)")
    if not 0 <= cfg.live.max_cumulative_loss_pct <= 100:
        raise ConfigError("live.max_cumulative_loss_pct must be between 0 and 100")
    if cfg.live.fee_float_usd < 0:
        raise ConfigError("live.fee_float_usd must be >= 0")
    if cfg.live.compound and cfg.live.capital_usd <= 0:
        raise ConfigError("live.compound needs live.capital_usd > 0: the caps are scaled as ratios to it")
    if not 0 < cfg.live.unwind_max_slippage_bps <= 200:
        raise ConfigError("live.unwind_max_slippage_bps must be in (0, 200]: a wider bound costs more than the halt it avoids")
    if not 1 <= cfg.live.unwind_max_attempts <= 10:
        raise ConfigError("live.unwind_max_attempts must be between 1 and 10")
    if cfg.live.unwind_dust_usd < 0:
        raise ConfigError("live.unwind_dust_usd must be >= 0")
    if cfg.live.unwind_dust_halt_usd < cfg.live.unwind_dust_usd:
        raise ConfigError("live.unwind_dust_halt_usd must be >= live.unwind_dust_usd")
    if cfg.live.unwind_halt_after < 1:
        raise ConfigError("live.unwind_halt_after must be >= 1")
    band = cfg.detection.stable_rate_band
    if len(band) != 2 or not 0 < band[0] < 1 < band[1]:
        raise ConfigError("detection.stable_rate_band must be [low, high] around 1.0")
    if cfg.universe.top_n <= 0:
        raise ConfigError("universe.top_n must be positive")


def to_dict(cfg: Config) -> dict[str, Any]:
    return dataclasses.asdict(cfg)
