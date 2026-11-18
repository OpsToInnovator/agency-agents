#!/usr/bin/env bash
# Install (or remove) a nightly launchd job that runs `agent_ledger.py collect`.
#
#   ./install-launchd.sh            install, runs every night at 23:50
#   ./install-launchd.sh --remove   uninstall
#
# AGENT_LEDGER_OUT, if set, is passed through as the ledger folder.

set -euo pipefail

LABEL="life.apexform.agent-ledger"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/agent_ledger.py"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"
LOG="$HOME/Library/Logs/agent-ledger.log"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL."
  exit 0
fi

[[ "$(uname)" == "Darwin" ]] || { echo "This installer is for macOS."; exit 1; }
[[ -f "$SCRIPT" ]] || { echo "Can't find $SCRIPT"; exit 1; }

OUT_ARGS=""
if [[ -n "${AGENT_LEDGER_OUT:-}" ]]; then
  OUT_ARGS="<string>--out</string><string>${AGENT_LEDGER_OUT/#\~/$HOME}</string>"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT</string>
    <string>collect</string>
    $OUT_ARGS
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>50</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL: runs nightly at 23:50. Log: $LOG"
echo "Running once now to check it works..."
"$PYTHON" "$SCRIPT" collect ${AGENT_LEDGER_OUT:+--out "${AGENT_LEDGER_OUT/#\~/$HOME}"}
