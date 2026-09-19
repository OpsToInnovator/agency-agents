# Crypto Arbitrage Bot — the honest version of "that viral post"

You have probably seen the post: someone "built a trading bot with Claude Code in
2 days", it "scans over 50 markets simultaneously", "syncs live BTC data from
Binance every second", "spots price errors before humans even notice", and turned
**$68 into $6,732 the first night and $750,000 in total**.

This directory contains a real, working version of that bot. It does everything
the post describes that is technically possible, and it tells you the truth about
the part that is not: **the money**.

| The post says | What this bot actually does |
|---|---|
| Scans over 50 markets simultaneously | Watches the top 60 Binance USDT markets plus their BTC/ETH/BNB crosses, and the same assets on Coinbase and Kraken: ~190 markets on one asyncio loop, ~1,000 quotes/second, 0.1 ms median detection latency |
| Syncs live BTC data from Binance every second | Uses Binance's real-time `bookTicker` WebSocket stream, which pushes every top-of-book change (polling once a second would be 20–200× too slow to matter) |
| Spots price errors before humans notice | Runs three detectors on every quote: cross-exchange spreads, triangular cycles inside Binance, and quote anomalies (jumps, cross-venue deviation, crossed books) |
| Executes on mispricing across dozens of markets | Paper-trades every opportunity that is still positive **after taker fees**, at the displayed top-of-book size, and reports realized PnL against the marks |
| $68 → $6,732 overnight | Reports, every 10 seconds, how many opportunities were gross-positive and how many survived fees. On the recorded sample tape and in our live runs the second number is **zero or single digits**, and the best net edge on BTC is **negative** |

If you only remember one thing: **the numbers in the post are not unlikely, they are
incompatible with the exchanges' fee schedules.** The reality check below shows the
arithmetic.

## The reality check

Taker fees are the floor an arbitrage trade must clear. Lowest public tiers, verified
2026-09-19 (see [Sources](#sources)):

| Venue | Taker fee (entry tier) | Notes |
|---|---|---|
| Binance | 0.10% (10 bps) | 0.075% paying fees in BNB |
| Coinbase | 0.60% (60 bps) | fresh retail Advanced accounts pay 0.90–1.20% |
| Kraken | 0.80% (80 bps) | restructured July 2026; older references still say 0.40% |

So the gross spread a trade must show before it nets zero, with these fees (the bot
prints this table at startup from whatever fees you configure):

| Trade | Break-even gross spread |
|---|---|
| Buy Binance, sell Coinbase (60 bps) + 5 bps USDT haircut | 75.4 bps |
| Buy Binance, sell Kraken (80 bps) + 5 bps haircut | 95.7 bps |
| Buy Coinbase, sell Kraken | 141.1 bps |
| Buy Kraken, sell Coinbase | 140.8 bps |
| Three-leg triangle inside Binance (3 × 10 bps) | 30.1 bps |

Against that, here is what the market actually offered while this was being written:

- Live BTC snapshot, 2026-09-19 05:13 UTC: Binance BTCUSDT 81,077.95/81,077.96,
  Coinbase BTC-USD 81,053.91/81,053.92, Kraken XBT/USD 81,067.1/81,067.2. Best gross
  gap: **2.97 bps**. Convert Binance's USDT price to dollars at the live USDT/USD rate
  (0.99965) and the "Binance premium" becomes **−0.5 bps before any fees**.
- The recorded fixture in `tests/fixtures/` (10 s of live quotes, 10 assets on Coinbase
  and Kraken, 9 of them also on Binance): 380 gross-positive cross-exchange observations, 27 gross-positive
  triangles, **0** net-positive. Best net edge: −70 bps (cross-exchange), −30 bps
  (triangular).
- Academic results say the same. Muck, Schmidl & Wolf (2025) implemented triangular
  arbitrage on Binance for a week: 4,879 candidates, most with gross returns of
  0–0.025%, about 18 profitable after fees and depth, total realizable profit on the
  order of **$12**. Crépellière, Pelster & Zeisberger (2023): cross-exchange
  opportunities shrank sharply from April 2018 onward and are "barely possible to
  exploit". A 2026 study of one-way arbitrage sequences on Binance and Kraken found
  average profit **under $1 per sequence** after fees.

Now the return math. $68 → $6,732 is a **99×** multiple in one night. With full-capital
compounding and no losing trades, that takes:

| Net edge per round trip | Consecutive winning round trips needed |
|---|---|
| 100 bps (fantasy) | 462 |
| 10 bps | 4,597 |
| 1 bp | 45,953 |

And $68 cannot even be deployed: Binance's minimum order on BTCUSDT is 5 USDT, so the
bankroll is at most 13 minimum-size orders before fees. Cross-exchange arbitrage needs
inventory pre-positioned on **both** venues (cash on the cheap one, coins on the dear
one), because moving coins between exchanges costs ~0.0002 BTC (~$16, a quarter of the
bankroll) and takes 20–40+ minutes to be credited, by which time the gap is long gone.
The firms that do capture these gaps (Wintermute, Jump and friends) run co-located
market-making systems with sub-millisecond hedging and pay 0.02–0.05% in fees, not
0.1–0.9%.

### Where the "profit" comes from: the fee sweep

`scripts/sweep.py` replays the bundled tape with the taker fees scaled down and
prints what happens. Nothing is net-positive until the fees are switched off
entirely, and even then two basis points of slippage turn the "profit" negative:

| Taker fees (× retail) | Gross-positive | Net-positive | Paper orders sent | Filled | Realized (2 bps slippage) | Realized (no slippage) |
|---|---|---|---|---|---|---|
| 1.00 | 407 | 0 | 0 | 0 | $0.00 | $0.00 |
| 0.50 | 407 | 0 | 0 | 0 | $0.00 | $0.00 |
| 0.25 | 407 | 0 | 0 | 0 | $0.00 | $0.00 |
| 0.00, 5 bps USDT haircut | 407 | 13 | 13 | 13 | −$0.22 | +$0.30 |
| 0.00, no haircut | 407 | 307 | 307 | 296 | −$1.98 | +$5.45 |

(The sweep gives the paper desk effectively unlimited inventory so that fees, not
running out of stock, decide the numbers.) Every "this bot prints money" screenshot you
will ever see lives in that last row: a fee assumption no retail account gets, no
slippage, and fills that were never actually sent to a venue. Run it yourself:
`python3 scripts/sweep.py`.

### What our own first live run "earned"

The first 30-second paper-trading run of this bot reported **+$385 realized profit**.
It was not profit. Binance lists an asset with ticker `ONE` at $0.0024 and Kraken lists
a different asset with ticker `ONE` at $0.14; the bot "bought" on Binance and "sold" on
Kraken. `U` on Binance is a stablecoin; `U/USD` on Kraken is not. This is exactly the
kind of "price error a human would never notice" that a naive scanner reports as a
5,800% opportunity. The bot now quarantines any asset whose venues disagree by more
than 20% (`identity_mismatch_bps`) and treats any net edge above 2%
(`max_plausible_net_edge_bps`) as bad data; both are logged as anomalies, never as
opportunities.
If a screenshot of a bot's profit does not come with the trade IDs, assume it is one of
these.

## What it does

```
feeds (WebSocket)            quote book               detectors                 risk            execution
─────────────────            ──────────               ─────────                 ────            ─────────
binance bookTicker ─┐    latest top-of-book     cross_exchange (2 legs)    min net edge     paper: simulated fills
coinbase ticker    ─┼──▶ per (venue, symbol) ─▶ triangular (3 legs)   ─▶  staleness    ─▶  at top-of-book + fees
kraken ticker(bbo) ─┘    + USDT→USD rate        anomaly (report only)      notional cap     live: Binance only, gated
                         + quarantine list                                 daily loss cap
                                                                           kill switch
                                                                           cooldowns
```

- **Cross-exchange**: for every asset quoted in dollars on two venues, buy at the cheaper
  ask and sell at the dearer bid, out of inventory already held on each venue. Binance's
  USDT prices are converted to USD with the live USDT-USD rate from Coinbase/Kraken and
  charged an extra `stable_haircut_bps` (default 5) for the risk of that conversion.
- **Triangular**: every USDT → X → Y → USDT cycle that exists in the Binance universe is
  indexed once; a quote update only re-evaluates the cycles it touches. Size is capped
  by the displayed size on all three legs and by the per-trade notional limit.
- **Anomaly** (report-only): a quote that jumps more than `anomaly_threshold_bps` from its
  own recent EWMA, a venue more than that far from the median of the other venues, a
  crossed or locked book, or a ticker collision.
- **Fee model**: taker fees on every leg, charged on the notional in the quote asset, at
  the rates in `[venues] taker_fee_bps`. Gross and net edge are always reported side by
  side.
- **Paper execution**, two models. `arrival` (default): the order reaches the venue
  `assumed_rtt_ms` (150 ms) after detection and is an IOC limit at the detection price
  against the book *at that time*; if the touch moved away the leg is **missed**,
  cross-exchange legs travel in parallel (so one side can fill while the other misses,
  leaving you holding inventory), and a triangle's legs are sequential. The summary
  prints promised vs realized PnL and the difference, the **latency tax**. `instant`:
  fills at detection, all legs at once, the generous model. Both charge taker fees on
  every leg, cap size at the displayed top-of-book quantity times `fill_fraction`, and
  add `slippage_bps`. If paper mode is not profitable, live will not be.
- **Risk manager** (runs before any executor): minimum net edge, maximum plausible edge,
  minimum profit in dollars, quote staleness, detection latency, per-trade notional cap,
  daily realized-loss cap (halts for the UTC day, persisted to `logs/risk_state.json`
  so a restart cannot reset it) and a drawdown-from-peak cap on the session's PnL
  curve (paper mode, where equity can be marked),
  trades-per-minute limit, per-opportunity cooldown, and a kill-switch file (`STOP` in
  the working directory stops all trading instantly, checked again before every live leg).
- **Feeds**: one WebSocket connection per venue, exponential backoff with jitter on
  reconnect, quotes dropped when a venue disconnects, Binance's 24-hour connection limit
  and `serverShutdown` handled, Kraken's 1 s heartbeat used as a liveness check.

## Quick start

Python 3.11+ (uses `tomllib`). Dependencies are only `websockets` and `aiohttp`.

```bash
cd examples/crypto-arbitrage-bot
python3 -m pip install -r requirements-dev.txt

# 1. Offline: replay the recorded tape through the full pipeline (no network)
python3 -m arbbot replay tests/fixtures/feed_fixture.jsonl

# 2. Run the tests (offline)
python3 -m pytest

# 3. Live market data, paper trading, 60 seconds, ~190 markets
python3 -m arbbot scan --duration 60

# See which markets would be scanned
python3 -m arbbot markets

# Record your own tape for replay / bug reports
python3 -m arbbot scan --duration 30 --record my_tape.jsonl
python3 -m arbbot replay my_tape.jsonl --speed 2
```

`scan` discovers the universe from the venues' REST APIs (top-N Binance USDT markets by
24 h volume, the crosses that make triangles, and the same assets' USD markets on
Coinbase and Kraken). `--static` skips discovery and uses the snapshot built into
`arbbot/universe.py`. `--symbols BTCUSDT,ETHUSDT,ETHBTC` pins the Binance list.

Note on Binance in restricted regions: `api.binance.com` answers HTTP 451 from the US
and some other locations. Public market data is served identically by
`data-api.binance.vision` / `data-stream.binance.vision`, which the bot uses by
default. Those mirrors do **not** serve order endpoints, so live mode needs an
unrestricted network and your own account; using a VPN to get around the block violates
Binance's terms.

## Reading the output

Every 10 seconds:

```
[    60s] quotes=67999 (binance=50340 coinbase=1095 kraken=16564) 1133/s unchanged=9 markets=194 stale=59 |
gross>0: cross_exchange=20871 triangular=14414 | net>=1bps: cross_exchange=64 | anomalies=35 |
trades=0 realized=+0.0000 promised=+0.0000 missed_legs=2 pending=0 equity=3,198.55 contributed=3,198.55 unrealized=+0.0000 |
detect p50=0.09ms p99=0.39ms
```

- `gross>0`: observations where the raw prices crossed (before fees). There are thousands
  because dollar quotes on three venues always differ by a few bps.
- `net>=1bps`: observations that were still positive after all fees, the stablecoin
  haircut, and the minimum edge. This is the number that matters.
- `trades` / `realized` / `promised` / `missed_legs` / `pending`: paper orders sent,
  the mark-to-market PnL they produced once they "arrived", what the detector promised
  at detection time, legs that missed because the touch moved, and orders still in
  flight. `equity` is portfolio value, `contributed` is what was put in (paper balances
  are funded lazily per asset per venue), both marked at the same marks, so
  `unrealized` is inventory mark-to-market and a book with no trades shows zero PnL.
  The `stable_haircut_bps` safety margin is demanded at decision time but is not a cost:
  `net_edge_bps` includes it, `expected_profit_usd` (the promise) does not.
- `stale`: markets whose last quote is older than the venue's `max_quote_age_ms`.
  Coinbase's `ticker` channel only fires on trades, so illiquid Coinbase products are
  often stale; stale quotes are never traded.
- `detect p50/p99`: quote receipt → opportunity emission.

The final summary adds the best gross and best **net** edge seen per strategy, the
anomaly breakdown, risk rejections, and any quarantined tickers. Actionable
opportunities, anomalies and trades are written as JSONL under `logs/<run-id>/`; use
`--no-jsonl` to skip that, or `report.log_every_opportunity = true` to log every
gross-positive observation (large files).

## Configuration

Copy `config.example.toml` to `config.toml`, edit, and pass `--config config.toml`.
Every key has a safe default and unknown keys are an error. The ones worth knowing:

| Key | Default | Why it matters |
|---|---|---|
| `venues.taker_fee_bps` | binance 10, coinbase 60, kraken 80 | The single biggest lever. Flattering these is the #1 way scanners "find" profit. Set what your account pays. |
| `detection.min_net_edge_bps` | 1.0 | Act only above this net edge |
| `detection.stable_haircut_bps` | 5.0 | Extra edge demanded when comparing USDT with USD prices |
| `detection.max_quote_age_ms` (+ per venue) | 2000; binance/kraken 5000 | Push-on-change feeds can be silent when unchanged; trade-triggered feeds cannot |
| `detection.identity_mismatch_bps` | 2000 | Venues this far apart are quoting different assets |
| `detection.max_plausible_net_edge_bps` | 200 | Larger "edges" are bad data |
| `risk.max_notional_per_trade_usd` | 100 | Per-trade size cap (also caps detector sizing) |
| `risk.max_daily_loss_usd` | 25 | Realized loss that halts trading for the UTC day (persisted in `risk.state_file`) |
| `risk.max_drawdown_pct` | 5.0 | Session PnL this far (as % of capital) below its peak halts for the day (0 = off; paper mode) |
| `risk.min_profit_usd` | 0.05 | Dust-sized opportunities are not actionable |
| `risk.kill_switch_file` | `STOP` | Create the file to stop instantly |
| `paper.fill_model` / `assumed_rtt_ms` | `arrival` / 150 | Orders arrive later and can miss; `instant` is the generous model |
| `paper.slippage_bps` / `fill_fraction` | 2.0 / 1.0 | How generous the fill simulation is |

## Live trading (off by default, and mostly not implemented on purpose)

Three independent switches must all be on before a single order request leaves the
machine, and even then it goes to Binance's **validation-only** endpoint:

1. `[live] enabled = true` in the config file **and** `--live` on the command line
   → orders go to `POST /api/v3/order/test`, which checks the key, signature, parameter
   format and symbol filters but never reaches the matching engine (no fill, no
   balance check). `TradeRecord.status` is `"test"` and PnL is zero.
2. Additionally `[live] real_orders = true` **and** `--i-know-this-sends-real-orders`
   → `POST /api/v3/order` with LIMIT IOC orders priced at the current top of book
   (fills what is there at that price or better, never chases the book). Keys come from the environment
   (`BINANCE_API_KEY`, `BINANCE_API_SECRET`); they are never read from the config file
   and never written to any log.
3. The risk manager still applies: kill switch, daily loss cap, notional cap, plausibility.

What the live path does to protect you:

- `python3 -m arbbot preflight` (also run automatically by `scan --live`) refuses to arm
  unless the clock offset to the exchange is small, the symbols are `TRADING`, the API
  key **cannot withdraw**, free USDT covers two trades, and an `order/test` call succeeds.
- Every order gets a `newClientOrderId`; its intent is appended to
  `logs/live_intents.jsonl` and flushed to disk **before** the request is sent.
- A timeout or ambiguous response is never retried blind. The order is looked up by
  client id; if that fails, trading halts with a sticky reason (persisted, survives
  restarts) until a human reconciles.
- Before every leg: kill switch, halt flag and the current book are re-checked; a
  cycle whose edge decayed is not sent. HTTP 418/429 halt trading.
- Live executions run one at a time, off the quote path.

Only single-venue (triangular) opportunities can be sent live. Cross-exchange
execution would need order placement on Coinbase and Kraken as well; this project does
not implement that, because a bot that can fire market orders on three exchanges from a
2-day-old codebase is a liability, not a feature. Live mode does not rebalance
inventory or unwind a cycle that fails halfway (it halts and tells you). Treat the live
executor as a reference for signing, filters and idempotency, not as a production
trading system. None of it could be exercised from the machine this was written on:
Binance's order endpoints and even its testnet answer HTTP 451 there.

## Limitations you should know about

- **Top-of-book only.** Sizes are the displayed best bid/ask quantity. Real fills larger
  than that walk the book. The default $100 notional keeps this mostly honest.
- **Coinbase's `ticker` channel is trade-triggered.** Its best bid/ask can be seconds old
  on illiquid products. The 2 s staleness limit and the `exch_lag_ms` field on each
  opportunity make this visible; some "opportunities" against Coinbase quotes are lag
  artifacts.
- **USDT ≠ USD.** The bot converts at the live rate and charges a haircut; if you turn
  `track_stable_rates` off it assumes par, which manufactures ~3–5 bps of fake edge.
- **Inventory is assumed, not moved.** Paper balances are pre-funded on every venue.
  Nothing rebalances, and nothing accounts for the capital cost of holding inventory
  on three exchanges.
- **Fees are a config table with a "last verified" date, not a live lookup.** Both
  Coinbase and Kraken changed their schedules in 2026; check yours.
- **Ticker collisions are detected by price, not by name.** An asset that is genuinely
  the same on two venues but happens to be >20% apart during a crash would also be
  quarantined for the run (that is the safe failure).
- **Market data terms.** Binance's and Coinbase's terms restrict commercial use and
  redistribution of their market data. This is a personal/educational tool: do not
  rebroadcast the feeds, sell signals, or monetize a hosted dashboard built on them.

## Red flags checklist for "AI trading bot" posts

Built from this post plus the CFTC, SEC/FINRA and FTC advisories:

- Returns stated without trade IDs, statements, or a verifiable account.
- No mention of fees, minimum order sizes, venues, or where the inventory sits.
- A tool gets the credit ("Claude Code built it") and the author gets the DMs.
- "Free setup, 24 hours only" urgency, or a course/subscription/Telegram behind it.
- Linked repositories that ask for exchange API keys with withdrawal permission.
- Screenshots of dashboards. Anyone can make one; this repo makes one in 30 seconds.

The FTC reported $2.1 billion lost to scams that started on social media in 2025, more
than half of it investment scams. The CFTC's advisory puts it plainly: AI cannot predict
the future or sudden market changes.

## Disclaimer

This software is for education and research. It is not financial advice, it makes no
profit claims, and its paper-trading results are simulations that ignore queue
position, partial fills and market impact. Live mode requires your own KYC'd exchange
account in a jurisdiction where the exchange serves you; you are responsible for the
exchange's terms of service, tax reporting, and any losses. The authors have no
affiliation with any exchange.

## Project layout

```
arbbot/
  cli.py            scan / replay / markets / preflight commands
  config.py         dataclasses + TOML loader (unknown keys are errors)
  models.py         Market, Quote, Leg, Opportunity, Fill, TradeRecord
  quotes.py         QuoteBook: latest top-of-book, staleness, USDT→USD rate, marks, quarantine
  fees.py           fee schedule and edge arithmetic
  filters.py        tick/step/min-notional rounding (Decimal)
  universe.py       market discovery (REST) and the static snapshot
  engine.py         feeds → book → detectors → risk → executor → reporter
  report.py         periodic and final summaries, JSONL logs
  feeds/            binance.py, coinbase.py, kraken.py, replay.py, base.py (reconnects)
  detectors/        cross_exchange.py, triangular.py, anomaly.py
  execution/        paper.py (instant + arrival fill models), risk.py (persisted caps), live.py (Binance, gated)
scripts/sweep.py    fee / haircut / slippage sensitivity sweep over a recorded tape
tests/              pytest suite; fixtures/feed_fixture.jsonl is 10 s of live quotes (2026-09-19)
```

## Sources

- Binance fee schedule: https://www.binance.com/en/fee/trading — Binance spot API docs
  (filters, rate limits, `data-api.binance.vision`):
  https://developers.binance.com/docs/binance-spot-api-docs
- Kraken fee schedule: https://www.kraken.com/features/fee-schedule — Kraken WebSocket v2:
  https://docs.kraken.com/api/docs/websocket-v2/ticker
- Coinbase Exchange API: https://docs.cdp.coinbase.com/exchange — fee revision of
  2026-09-16 (press coverage; Coinbase's own fee page blocks automated fetching):
  https://www.securities.io/coinbase-lowers-advanced-trading-fees-with-tiers-starting-at-10-000/
- Muck, Schmidl & Wolf, "Wish or reality? On the exploitability of triangular arbitrage in
  cryptocurrency markets", Finance Research Letters 73 (2025):
  https://www.sciencedirect.com/science/article/pii/S154461232401537X
- Crépellière, Pelster & Zeisberger, "Arbitrage in the market for cryptocurrencies",
  Journal of Financial Markets 64 (2023)
- Makarov & Schoar, "Trading and Arbitrage in Cryptocurrency Markets", Journal of
  Financial Economics 135 (2020)
- Hautsch, Scheuch & Voigt, "Building trust takes time: limits to arbitrage for blockchain-
  based assets", Review of Finance 28 (2024): https://arxiv.org/abs/1812.00595
- "A Truckload of Satoshis: Detecting and Measuring One-Way Arbitrage in the Wild",
  arXiv 2607.09491 (2026)
- CFTC Customer Advisory on AI scams (2024): https://www.cftc.gov/PressRoom/PressReleases/8854-24
- SEC/NASAA/FINRA Investor Alert on AI and investment fraud (2024):
  https://www.finra.org/investors/insights/artificial-intelligence-and-investment-fraud
- FTC, social-media scam losses (2026):
  https://www.ftc.gov/news-events/news/press-releases/2026/04/new-ftc-data-show-people-have-lost-billions-social-media-scams
