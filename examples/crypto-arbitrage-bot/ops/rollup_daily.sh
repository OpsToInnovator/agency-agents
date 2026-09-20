#!/usr/bin/env bash
# Daily roll-up of both runs into /var/lib/arbbot/rollup-*.txt (the latest report on top of the file
# is the one to read). Run by arbbot-rollup.timer; safe to run by hand from /var/lib/arbbot.
set -u
cd /var/lib/arbbot || exit 2
PY=/opt/arbbot/venv/bin/python
STAMP=$(date -u +%FT%TZ)
CAPITAL=$(sed -n 's/^capital_usd *= *\([0-9.]*\).*/\1/p' /etc/arbbot/live.toml | head -1)
COOLDOWN=$(sed -n 's/^cooldown_s *= *\([0-9.]*\).*/\1/p' /etc/arbbot/live.toml | head -1)
{
  echo "===== $STAMP live (stake ${CAPITAL:-?}, cooldown ${COOLDOWN:-?}) ====="
  if [ -d logs/live ]; then
    $PY /opt/arbbot/scripts/rollup.py logs/live --scan-log live.log ${CAPITAL:+--capital-usd "$CAPITAL"} \
        ${COOLDOWN:+--cooldown-s "$COOLDOWN"} $( [ -f rtt.log ] && echo --rtt-log rtt.log ) 2>&1
  else
    echo "no logs/live yet"
  fi
  echo
} > rollup-live.new && cat rollup-live.txt >> rollup-live.new 2>/dev/null; mv rollup-live.new rollup-live.txt
{
  echo "===== $STAMP measure7d ====="
  if [ -d logs/measure7d ]; then
    $PY /opt/arbbot/scripts/rollup.py logs/measure7d --scan-log measure7d.log $( [ -f rtt.log ] && echo --rtt-log rtt.log ) 2>&1
  else
    echo "no logs/measure7d yet"
  fi
  echo
} > rollup-measure.new && cat rollup-measure.txt >> rollup-measure.new 2>/dev/null; mv rollup-measure.new rollup-measure.txt
# keep the files bounded: the last ~60 reports
for f in rollup-live.txt rollup-measure.txt; do
  n=$(grep -c '^=====' "$f" 2>/dev/null || echo 0)
  if [ "$n" -gt 60 ]; then
    awk -v keep=60 '/^=====/{c++} c<=keep' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
done
