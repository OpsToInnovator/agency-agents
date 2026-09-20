# Runbook: the US$500 live start

This is the operator's sequence for running arbbot with real money on Binance triangles,
step by step, with the command to type, what a good result looks like, and what to do
when it is not. Read the README's "Go-live protocol" first for why the numbers are what
they are. Nothing in this document changes the expectation: at the fee floor a retail
account can reach, the median attempted cycle loses money, and the stake buys real fill
and latency data inside a fixed loss budget. If that is not what you want, stop here.

Two rules that hold throughout:

- **The API key exists in one place**: `/etc/arbbot/live.env` on the trading machine, mode
  640, owner root, group arbbot. Never in the config file, never in a chat, never in git.
  Spot trading enabled, withdrawals disabled, restricted to the machine's IP.
- **Do not tunnel.** If the trading host answers HTTP 451 from the machine, that machine
  is not the one. A VPN or proxy around it breaches the Binance terms and the usual outcome
  is a frozen account with the stake inside.

The budgets, as fractions of a US$500 stake (all set in `live.toml`):

| Rule | Setting | Enforced by |
| --- | --- | --- |
| Per trade | US$50 | `risk.max_notional_per_trade_usd` |
| Per UTC day | US$5 realized loss, then halt for the day | `risk.max_daily_loss_usd`, persisted |
| Drawdown | US$25 below the day's opening PnL or its intraday peak | `risk.max_drawdown_pct` against `live.capital_usd` |
| Kill | US$50 cumulative: free USDT below 450 | `live.max_cumulative_loss_pct`, preflight refuses to re-arm |
| Instant stop | `touch /var/lib/arbbot/STOP` | checked before every leg |

## Day 0: machine, account, keys, fees, funding

**1. The machine.** An always-on box with a static IP in a region Binance serves. From
Australia, a home machine on a static IP works; a small Tokyo VPS (US$5 to 20 a month)
cuts the round trip to Binance's matching engine from about 100 ms to about 20 ms. Do
not rent anything before step 2 passes from it. Debian or Ubuntu with Python 3.11 or
newer.

**2. Install and check connectivity.** As root, from a checkout of this repository:

```bash
sudo ops/install.sh
sudo -u arbbot /opt/arbbot/venv/bin/python -m arbbot preflight --connectivity --config /etc/arbbot/live.toml
```

Good: `OK: https://api.binance.com serves this machine's IP (HTTP 200 on /api/v3/ping)`.
Bad: a line starting `FAIL` with HTTP 451. That machine cannot be the trading machine.
Pick another region; do not proceed on this one.

**3. Start the public-feed services now.** They need no keys and the measurement should
run alongside the live stake from the first day:

```bash
sudo systemctl enable --now arbbot-rtt arbbot-measure arbbot-rollup.timer
sudo systemctl status arbbot-measure --no-pager | head -5
tail -f /var/lib/arbbot/measure7d.log      # summary lines every 10 s; Ctrl-C to leave
```

**4. The account and the key.** A Binance account KYC'd in your own name (from Australia,
the Binance Australia entity). Create an API key with **Enable Spot & Margin Trading**
only: no withdrawals, no futures, no margin transfers. Under "Restrict access to trusted
IPs only" enter the machine's public IP. Put the key and secret into `/etc/arbbot/live.env`
on the machine (the installer created it with the right permissions), nothing else there
yet. `ARBBOT_LIVE_FLAGS` stays empty.

**5. Read your real fees.** Still no funds needed:

```bash
sudo -u arbbot env $(grep -v '^#' /etc/arbbot/live.env | xargs) \
  /opt/arbbot/venv/bin/python /opt/arbbot/scripts/read_fees.py BTCUSDT ETHUSDT BNBUSDT UNIUSDT UNIBTC ETHBTC
```

It prints a `[venues] taker_fee_bps = { binance = ... }` line. Paste that value into
`/etc/arbbot/live.toml`. Use the undiscounted number unless you will hold BNB for the
whole run and the account has "Using BNB to pay for fees" switched on; the discounted
figure is printed next to it.

**6. Fund.** Deposit 500 USDT to the spot wallet. If paying fees in BNB, also about
US$50 of BNB (a filled US$50 cycle costs about US$0.11 of it; a day at the daily cap
burns about US$5). From Australia the cheapest verified ramp is AUD to USDT on an
exchange with a deep AUD/USDT book, then a withdrawal to Binance; budget 0.8 to 1.8 %
of the amount for the round trip, and note that CommBank caps payments to exchanges at
A$10,000 a calendar month.

**7. The full preflight.**

```bash
sudo -u arbbot env $(grep -v '^#' /etc/arbbot/live.env | xargs) \
  /opt/arbbot/venv/bin/python -m arbbot preflight --config /etc/arbbot/live.toml
```

Good: one line starting `OK:` naming the host, the clock offset, key restrictions,
balances and `order/test accepted`. Every `FAIL` line names exactly what to fix:

| FAIL says | Do |
| --- | --- |
| HTTP 451 / 403 / cannot reach | wrong machine or IP whitelist; see step 2 |
| clock skew | `sudo timedatectl set-ntp true`, wait a minute, re-run |
| API key can WITHDRAW | delete the key on Binance, make a new one without withdrawals |
| no spot trading permission | edit the key's permissions on Binance |
| free USDT below 2x max notional / below the kill floor | the deposit has not landed, or the stake is not there |
| status BREAK / no exchange filters | the symbol is suspended; `auto_discover = true` must stay on |
| kill switch file already exists | `rm /var/lib/arbbot/STOP` if you meant to start |
| trading is halted (sticky) | see "When it halts" below; reconcile first |

## Day 1: stage B, validation-only orders

`real_orders` is `false` in `live.toml` and `ARBBOT_LIVE_FLAGS` is empty, so every order
the bot would send goes to Binance's validation endpoint and never reaches the matching
engine. This is the first contact between this code's signing, sizing and filters and
your account; it exists so the first real order is not the first order.

```bash
sudo systemctl enable --now arbbot-live
sudo systemctl status arbbot-live --no-pager | head -8
tail -f /var/lib/arbbot/live.log
```

The log shows `preflight passed`, then `starting in LIVE/test-endpoint mode`, then a
summary line every 10 seconds. `TRADE TEST` lines are validated orders. Leave it for a
full day. Pass criteria to move on: at least one `TRADE TEST` line, and in `live.log`
zero occurrences of `filter`, `-1021`, `HTTP 418`, `HTTP 429` and `TRADING HALTED`:

```bash
grep -cE 'TRADE TEST' /var/lib/arbbot/live.log
grep -E 'LOT_SIZE|NOTIONAL|PRICE_FILTER|-1021|HTTP 418|HTTP 429|TRADING HALTED' /var/lib/arbbot/live.log
```

If the second command prints anything, stop and read it; a filter rejection means the
sizing is wrong for that symbol, a timestamp error means the clock, 418/429 means the
request rate. None of these is fixed by going live.

## Day 2 onward: stage C, real orders

```bash
sudo systemctl stop arbbot-live
sudo sed -i 's/^real_orders = false/real_orders = true/' /etc/arbbot/live.toml
sudo sed -i 's/^ARBBOT_LIVE_FLAGS=.*/ARBBOT_LIVE_FLAGS=--i-know-this-sends-real-orders/' /etc/arbbot/live.env
sudo systemctl start arbbot-live
grep -m1 'starting in LIVE/REAL ORDERS' /var/lib/arbbot/live.log || echo "not armed: read live.log"
```

From here every `TRADE FILLED` / `TRADE PARTIAL` line is a real fill. Duration: 7 days or
100 settled trades, whichever comes later. Nothing to babysit minute by minute; the caps
do that. Twice a day:

```bash
sudo systemctl status arbbot-live --no-pager | head -5           # active (running)?
tail -n 3 /var/lib/arbbot/live.log                                # the last summary line
sudo -u arbbot env $(grep -v '^#' /etc/arbbot/live.env | xargs) \
  /opt/arbbot/venv/bin/python -m arbbot reconcile --config /etc/arbbot/live.toml
```

`reconcile` prints every balance marked in USDT, whether free USDT is still at the stake,
inside the kill budget, or below the floor, any open orders, the persisted halt state, the
last ten order intents and responses, and a verdict: `FLAT` or `NOT FLAT` with exactly what
to sell. Exit code 0 means flat and not halted; 1 means it needs you.

Once a day, `/var/lib/arbbot/rollup-live.txt` has the roll-up and scorecard on live fills
(newest report on top; the measurement's is in `rollup-measure.txt`). The numbers that
matter daily: settled trades, realized PnL, fill rate, one-legged rate, realized versus
promised. The scorecard's GO criteria are the week's question, not the day's.

## When it halts

The bot halts itself for four reasons. Each one prints `TRADING HALTED: <reason>` in
`live.log` and the same reason in `reconcile`.

- **Daily loss cap** (`daily loss cap ... reached`): US$5 of realized loss today. Nothing
  to do; it lifts at 00:00 UTC. Two such days in any seven is a kill criterion.
- **Drawdown cap** (`drawdown cap hit`): US$25 below the day's opening PnL or its peak.
  Lifts at 00:00 UTC. Behind the daily cap it can only fire after a day ran up more than
  US$20 and gave it back, so if you see it, read the day's fills.
- **Sticky halt** (`cycle aborted mid-way`, `fill state unknown`, `error after order`,
  `shutdown while an order was in flight`): the bot does not know, or does not like, what
  the account holds. It stays halted across restarts until you clear it. See below.
- **Kill switch**: `/var/lib/arbbot/STOP` exists. Remove the file to allow trading again.

**Clearing a sticky halt** is a reconciliation, not a reset:

1. `reconcile`. Read the reason and the last journal entries. The `intent` line says what
   was sent, the `response` line what Binance answered, an `error` line what went wrong.
2. If the verdict is `NOT FLAT`, it names the inventory: for example 5.5 UNI worth about
   49 USDT. Sell it back to USDT on the Binance spot page (a market order at that size is
   fine). The bot never does this for you.
3. `reconcile` again until it says `FLAT`. If Binance shows a balance the report calls
   `UNPRICED`, settle it by hand too.
4. Lift the halt: `reconcile --config /etc/arbbot/live.toml --clear-halt`. It refuses
   while inventory is outstanding; `--force` overrides only if you are keeping the
   position on purpose.
5. `sudo systemctl restart arbbot-live` and confirm `preflight passed` in the log. If the
   preflight now says free USDT is below the kill floor, the kill rule has fired: do not
   top up and restart on the same settings.

Write down what happened before touching the config. A halt that repeats is data.

## Kill criteria

Any one of these ends stage C: `touch /var/lib/arbbot/STOP`, `sudo systemctl stop
arbbot-live`, write the post-mortem, and do not restart on the same settings.

- Free USDT below 450 (the preflight enforces this one).
- The daily loss cap on two days in any seven.
- Any sticky halt you cannot reconcile within the hour, or one that left a one-legged
  position twice.
- Fill rate under 30 % over the last 50 sends, or realized under a quarter of promised
  over the last 50 trades (both in the daily roll-up).
- Any HTTP 418 or 429, or `-2010` insufficient balance, in `live.log`.
- `read_fees.py` shows a different fee than `live.toml` (re-run it monthly and after any
  Binance fee announcement).
- A venue-disagreement or identity-mismatch anomaly on an asset you traded that day.
- The machine's clock drifting over a second (`timedatectl` shows it).

## Weekly review and the scale rule

Sunday, on `rollup-live.txt` and `rollup-measure.txt`: compare live realized per trade
with the paper run's over the same days (live should be at least half), live fill rate
with paper (at least 0.8 of it), and check that the commissions on Binance's trade export
match the fee in `live.toml`. Reconcile the intent journal against the exchange's trade
history: every real `intent` with a `FILLED` response must appear there, and nothing must
appear there that is not in the journal.

The per-trade cap stays at 10 % of the stake. It grows only with the stake, and the stake
grows only after a month of positive live realized PnL and a passing scorecard: double it,
never more, and re-run this runbook's day 0 preflight at the new size.

## Stopping

```bash
sudo systemctl disable --now arbbot-live            # stops trading; finishes an in-flight cycle first
sudo -u arbbot env $(grep -v '^#' /etc/arbbot/live.env | xargs) \
  /opt/arbbot/venv/bin/python -m arbbot reconcile --config /etc/arbbot/live.toml   # must say FLAT
```

Then delete the API key on Binance, and keep `/var/lib/arbbot/logs/live/` (the trade
records and the intent journal) with the exchange's monthly trade exports. In Australia
every leg of every triangle is a CGT event, the USDT included, and a bot trading daily is
likely a business for tax purposes; the records are what an accountant will ask for.
