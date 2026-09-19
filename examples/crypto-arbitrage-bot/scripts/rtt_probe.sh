#!/usr/bin/env bash
# Measure round-trip time from THIS machine to each venue's REST edge once a minute.
#   warm_ms  = time_starttransfer - time_appconnect: request -> first byte on an already-open TLS
#              connection, which is what an order on a kept-alive session costs. Feed its p90 for
#              the SLOWEST venue you trade into paper.assumed_rtt_ms.
#   total_ms = the full cold request (DNS + TCP + TLS + request); what a reconnect costs.
# Usage:  scripts/rtt_probe.sh >> rtt.log &        then  python3 scripts/rollup.py <logs> --rtt-log rtt.log
set -u
while true; do
  for u in "binance https://api.binance.com/api/v3/ping" "coinbase https://api.exchange.coinbase.com/time" "kraken https://api.kraken.com/0/public/Time"; do
    set -- $u
    out=$(curl -sS -o /dev/null -w '%{time_appconnect} %{time_starttransfer} %{http_code}' --max-time 5 "$2" 2>/dev/null) || out="nan nan 000"
    echo "$(date -u +%FT%TZ) $1 $(awk -v a="${out% *}" -v c="${out##* }" 'BEGIN{split(a,t," "); if (t[1]=="nan"||t[2]=="nan"||t[2]==0) {print "warm_ms nan total_ms nan http " c} else {printf "warm_ms %.0f total_ms %.0f http %s", (t[2]-t[1])*1000, t[2]*1000, c}}')"
  done
  sleep 60
done
