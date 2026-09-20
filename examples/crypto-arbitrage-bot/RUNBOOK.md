# Runbook: the US$500 live start

This is the operator's sequence for running arbbot with real money on Binance triangles,
step by step, with the command to type, what a good result looks like, and what to do
when it is not. Read the README's "Go-live protocol" first for why the numbers are what
they are. Nothing in this document changes the expectation: at the fee floor a retail
account can reach, the median attempted cycle loses money, and the stake buys real fill
and latency data inside a fixed loss budget. If that is not what you want, stop here.

Three rules that hold throughout:

- **The API key exists in one place**: `/etc/arbbot/live.env` on the trading machine, mode
  640, owner root, group arbbot. Never in the config file, never in a chat, never in git,
  never on a command line. Spot trading enabled, withdrawals disabled, restricted to the
  machine's IP.
- **Every `arbbot` command goes through the wrapper**: `sudo /opt/arbbot/ops/as-arbbot.sh
  <command>`. It loads the keys into the environment, switches to the arbbot user, and
  runs from `/var/lib/arbbot`, the directory the service's relative paths (`STOP`,
  `logs/live/...`) resolve against. Run from anywhere else and `reconcile` looks at the
  wrong state file and reports a halted bot as clean.
- **Do not tunnel.** If the trading host answers HTTP 451 from the machine, that machine
  is not the one. A VPN or proxy around it breaches the Binance terms and the usual outcome
  is a frozen account with the stake inside.

Below, `RUN` stands for `sudo /opt/arbbot/ops/as-arbbot.sh /opt/arbbot/venv/bin/python`.

The budgets, as fractions of a US$500 stake (all set in `/etc/arbbot/live.toml`):

| Rule | Setting | Enforced by |
| --- | --- | --- |
| Per trade | US$50 | `risk.max_notional_per_trade_usd` |
| Per UTC day | US$5 realized loss, then halt for the day | `risk.max_daily_loss_usd`, persisted |
| Drawdown | US$25 below the day's opening PnL or its intraday peak | `risk.max_drawdown_pct` against `live.capital_usd` |
| Kill | US$50 cumulative: free USDT below 450 | `live.max_cumulative_loss_pct`, preflight refuses to re-arm |
| Instant stop | `touch /var/lib/arbbot/STOP` | checked before every leg; halts `arbbot-live` only, the measurement has its own `STOP-measure` |

## Day 0: machine, account, keys, fees, funding

**1. The machine.** An always-on box with a static IP in a region Binance serves. From
Australia, a home machine on a static IP works; a small Tokyo VPS (US$5 to 20 a month)
cuts the round trip to Binance's matching engine from about 100 ms to about 20 ms. Do
not rent anything before step 2 passes from it. Debian or Ubuntu with Python 3.11 or
newer, `rsync` and `setpriv` (util-linux) installed.

**2. Install and check connectivity.** As root, from a checkout of this repository:

```bash
sudo ops/install.sh
RUN -m arbbot preflight --connectivity --config /etc/arbbot/live.toml
```

Good: `OK: https://api.binance.com serves this machine's IP (HTTP 200 on /api/v3/ping)`.
Bad: a line starting `FAIL` with HTTP 451. That machine cannot be the trading machine.
Pick another region; do not proceed on this one.

**3. Start the public-feed services now.** They need no keys and the measurement should
run alongside the live stake from the first day:

```bash
sudo systemctl enable --now arbbot-rtt arbbot-measure arbbot-rollup.timer
sudo systemctl status arbbot-measure --no-pager | head -5
sudo tail -f /var/lib/arbbot/measure7d.log      # summary lines every 10 s; Ctrl-C to leave
```

**4. The account and the key.** A Binance account KYC'd in your own name (from Australia,
the Binance Australia entity). Create an API key with **Enable Spot & Margin Trading**
only: no withdrawals, no futures, no margin transfers. Under "Restrict access to trusted
IPs only" enter the machine's public IP. Put the key and secret into `/etc/arbbot/live.env`
on the machine (the installer created it with the right permissions), nothing else there
yet. `ARBBOT_LIVE_FLAGS` stays empty.

**5. Read your real fees.** Still no funds needed:

```bash
RUN /opt/arbbot/scripts/read_fees.py BTCUSDT ETHUSDT BNBUSDT UNIUSDT UNIBTC ETHBTC
```

It prints a `[venues] taker_fee_bps = { binance = ... }` line. Paste that value into
`/etc/arbbot/live.toml`. Use the undiscounted number unless you will hold BNB for the
whole run and the account has "Using BNB to pay for fees" switched on; the discounted
figure is printed next to it.

**6. Fund.** Deposit 500 USDT to the spot wallet. If paying fees in BNB, also about
US$50 of BNB (a filled US$50 cycle costs about US$0.11 of it; a day at the daily cap
burns about US$5), and set `fee_float_usd` in `live.toml` to what you deposited so
`reconcile` knows how much BNB is float and how much is a leftover position; if you pay
fees in USDT, set it to 0. From Australia the cheapest verified ramp is AUD to USDT on an
exchange with a deep AUD/USDT book, then a withdrawal to Binance; budget 0.8 to 1.8 %
of the amount for the round trip, and note that CommBank caps payments to exchanges at
A$10,000 a calendar month.

**7. The full preflight.**

```bash
RUN -m arbbot preflight --config /etc/arbbot/live.toml
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
| kill switch file already exists | `sudo rm /var/lib/arbbot/STOP` if you meant to start |
| trading is halted (sticky) | see "When it halts" below; reconcile first |

## Day 1: stage B, validation-only orders

`real_orders` is `false` in `live.toml` and `ARBBOT_LIVE_FLAGS` is empty, so every order
the bot would send goes to Binance's validation endpoint and never reaches the matching
engine. This is the first contact between this code's signing, sizing and filters and
your account; it exists so the first real order is not the first order.

```bash
sudo systemctl enable --now arbbot-live
sudo systemctl status arbbot-live --no-pager | head -8
sudo tail -f /var/lib/arbbot/live.log
```

The log shows `preflight passed`, then `starting in LIVE/test-endpoint mode`, then a
summary line every 10 seconds. `TRADE TEST` lines are validated orders. Leave it for a
full day. Pass criteria to move on: at least one `TRADE TEST` line, and zero filter
rejections, whether Binance's (`LOT_SIZE`, `NOTIONAL`, `PRICE_FILTER`) or the bot's own
before the send (`TRADE REJECTED ... below min_qty`, `below min_notional`, `quantity
rounds to zero`), and zero timestamp, rate-limit or halt lines:

```bash
grep -cE 'TRADE TEST' /var/lib/arbbot/live.log
grep -E 'LOT_SIZE|NOTIONAL|PRICE_FILTER|below min_qty|below min_notional|rounds to zero|-1021|HTTP 418|HTTP 429|TRADING HALTED' /var/lib/arbbot/live.log
```

If the second command prints anything, stop and read it. A filter rejection, Binance's
`Filter failure` or the bot's own `TRADE REJECTED` sizing line, means the sizing is wrong
for that symbol; a `-1021` means the clock; 418 or 429 means the request rate. None of
these is fixed by going live. Other `TRADE REJECTED` lines (edge decayed, no fresh quote,
moved N bps) are the bot declining a stale opportunity and are expected.

## Day 2 onward: stage C, real orders

```bash
sudo touch /var/lib/arbbot/STOP                 # no new leg starts while the service stops
sudo systemctl stop arbbot-live                 # can take up to about three minutes if an order is being reconciled
sudo rm /var/lib/arbbot/STOP
sudo sed -i 's/^real_orders = false/real_orders = true/' /etc/arbbot/live.toml
sudo sed -i 's/^ARBBOT_LIVE_FLAGS=.*/ARBBOT_LIVE_FLAGS=--i-know-this-sends-real-orders/' /etc/arbbot/live.env
n=$(wc -l < /var/lib/arbbot/live.log)           # only lines after this belong to the new run
sudo systemctl start arbbot-live
for i in $(seq 1 12); do                        # discovery and preflight take a few seconds
  sleep 5
  tail -n +$((n + 1)) /var/lib/arbbot/live.log | grep -E 'starting in |preflight FAILED:|refusing ' && break
  systemctl is-active --quiet arbbot-live || { sudo systemctl status arbbot-live --no-pager | head -5; break; }
done
```

The line must read `starting in LIVE/REAL ORDERS mode`. `starting in LIVE/test-endpoint
mode` means `ARBBOT_LIVE_FLAGS` is still empty; `refusing --i-know-this-sends-real-orders`
means `live.toml` was not edited; `preflight FAILED: <reason>` means the service exited
and stays down until the reason is fixed.

From here every `TRADE FILLED` / `TRADE PARTIAL` line is a real fill. Duration: 7 days or
100 settled trades, whichever comes later. Nothing to babysit minute by minute; the caps
do that. Twice a day:

```bash
sudo systemctl status arbbot-live --no-pager | head -5           # active (running)?
sudo tail -n 3 /var/lib/arbbot/live.log                           # the last summary line
RUN -m arbbot reconcile --config /etc/arbbot/live.toml
```

`reconcile` prints every balance marked in USDT, whether free USDT is still at the stake,
inside the kill budget, or below the floor, any resting orders, the persisted halt state,
whether the `STOP` file is present, the last ten order intents and responses, and a
verdict: `FLAT`, or `NOT FLAT` with exactly what to sell or cancel. Exit code 0 means flat
and not halted; 1 means it needs you; 2 means the key was refused; 3 means Binance could
not be reached.

Once a day, `/var/lib/arbbot/rollup-live.txt` has the roll-up and scorecard on live fills
(newest report on top; the measurement's is in `rollup-measure.txt`). The numbers that
matter daily: settled trades, realized PnL, fill rate, one-legged rate, realized versus
promised. The scorecard's GO criteria are the week's question, not the day's.

**Stopping or restarting in stage C, every time:** `sudo touch /var/lib/arbbot/STOP` first,
so no new leg starts, then `systemctl stop`; the stop waits for an in-flight cycle, up to
about three minutes when an order has to be looked up. Remove the file before the next
start or the preflight refuses with `kill switch file ... already exists`.

## When it halts

The bot halts itself for three reasons. Each prints `TRADING HALTED: <reason>` in
`live.log` and persists the same reason, which `reconcile` shows as `HALTED: <reason>`.

- **Daily loss cap** (`daily loss cap hit (-5.xx USD)`): US$5 of realized loss today.
  Nothing to do; it lifts at 00:00 UTC. Two such days in any seven is a kill criterion.
- **Drawdown cap** (`drawdown cap hit`): US$25 below the day's opening PnL or its peak.
  Lifts at 00:00 UTC. Behind the daily cap it can only fire after a day ran up more than
  US$20 and gave it back, so if you see it, read the day's fills.
- **Sticky halt**: any reason ending in `reconcile manually` (`cycle aborted mid-way`,
  `error after order ... fill state unknown`, `ambiguous order state for <id>`, `empty
  response for real order <id>`, `shutdown while an order was in flight`, `executor
  crashed mid-cycle`), plus `binance rate limit (418|429); back off before re-arming`,
  which is also sticky but leaves no inventory: wait out the ban window, then clear it.
  A sticky halt survives restarts until you clear it. See below.

The kill switch is different: `/var/lib/arbbot/STOP` is a gate, not a halt. While it
exists no leg is sent, `live.log` shows only `SKIP (kill switch file ... present)` lines
when an opportunity arrives, nothing is written to the state file, and `reconcile` prints
`kill switch: ... is PRESENT`. On the next start the preflight refuses until the file is
removed. The one exception: if `STOP` lands between two legs of a cycle, the cycle aborts
and that is a sticky `cycle aborted mid-way (leg N: kill switch file ... present)` halt.

**Clearing a sticky halt** is a reconciliation, not a reset. A halted service keeps
scanning and rewrites its in-memory halt to the state file when the UTC day rolls, so the
halt is cleared with the service stopped:

1. `RUN -m arbbot reconcile --config /etc/arbbot/live.toml`. Read the reason and the last
   journal entries. The `intent` line says what was sent, the `response` line what Binance
   answered, an `error` line what went wrong. This step is read-only and safe with the
   service still up.
2. If the verdict is `NOT FLAT`, it names what to do: sell the inventory back to USDT on
   the Binance spot page (a market order at that size is fine); cancel any resting order it
   lists, or let it fill and run again; if part of a balance is locked, cancel that order
   first. BNB above the declared fee float is inventory too, and while the halted cycle
   traded a BNB market any BNB is. The bot never sells for you.
3. `reconcile` again until it says `FLAT`. If it calls a balance `UNPRICED`, settle it by
   hand too. If it says the risk state file is unreadable, fix or move that file by hand
   before going on; the halt cannot be cleared through an unreadable file.
4. `sudo touch /var/lib/arbbot/STOP && sudo systemctl stop arbbot-live`.
5. `RUN -m arbbot reconcile --config /etc/arbbot/live.toml --clear-halt`. It refuses
   while inventory, a resting order or an unlisted order book is outstanding; `--force`
   overrides only if you are keeping the position on purpose.
6. `sudo rm /var/lib/arbbot/STOP && sudo systemctl start arbbot-live`, then confirm with
   the same new-lines-only check as in stage C that the new run logged `preflight passed`.
   If the preflight now says free USDT is below the kill floor, the kill rule has fired:
   do not top up and restart on the same settings.

Write down what happened before touching the config. A halt that repeats is data.

## Kill criteria

Any one of these ends stage C: `sudo touch /var/lib/arbbot/STOP`, `sudo systemctl stop
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
appear there that is not in the journal. A week with zero sends in `rollup-measure.txt`
is worth a look at `grep -c 'kill switch' /var/lib/arbbot/measure7d.log`: the
measurement's own switch is `/var/lib/arbbot/STOP-measure`.

The per-trade cap stays at 10 % of the stake. It grows only with the stake, and the stake
grows only after a month of positive live realized PnL and a passing scorecard: double it,
never more, and re-run this runbook's day 0 preflight at the new size.

## Stopping

```bash
sudo touch /var/lib/arbbot/STOP
sudo systemctl disable --now arbbot-live            # waits for an in-flight cycle, up to about three minutes
RUN -m arbbot reconcile --config /etc/arbbot/live.toml   # must say FLAT
```

Then delete the API key on Binance, and keep `/var/lib/arbbot/logs/live/` (the trade
records and the intent journal) with the exchange's monthly trade exports. In Australia
every leg of every triangle is a CGT event, the USDT included, and a bot trading daily is
likely a business for tax purposes; the records are what an accountant will ask for.

## Upgrading an existing install

`sudo ops/install.sh` again from a fresh checkout, with `arbbot-live` stopped (the
installer refuses otherwise). It keeps the venv and never overwrites an edited file in
`/etc/arbbot`, so new keys in the example configs (`fee_float_usd`, the measurement's
`kill_switch_file = "STOP-measure"`) have to be added to `/etc/arbbot/live.toml` and
`/etc/arbbot/measure7d.toml` by hand, then `systemctl restart arbbot-measure`.
