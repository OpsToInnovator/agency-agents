#!/usr/bin/env bash
# Install arbbot as system services on a Debian/Ubuntu box (run as root, from the repo checkout).
# Creates the arbbot user, /opt/arbbot (code + venv), /etc/arbbot (configs, keys) and
# /var/lib/arbbot (state, logs). Idempotent. Starts NOTHING live: the runbook says what to start when.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
if [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo $0)" >&2; exit 2; fi
command -v python3 >/dev/null || { echo "python3 is required (3.11+)" >&2; exit 2; }
python3 - <<'PY' || { echo "python 3.11+ is required" >&2; exit 2; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
id arbbot >/dev/null 2>&1 || useradd --system --home /var/lib/arbbot --shell /usr/sbin/nologin arbbot
install -d -o root -g root -m 755 /opt/arbbot
install -d -o root -g arbbot -m 750 /etc/arbbot
install -d -o arbbot -g arbbot -m 750 /var/lib/arbbot /var/lib/arbbot/logs
# code: a plain copy of the checkout (no .git, no logs), plus a venv with the two runtime deps
rsync -a --delete --exclude .git --exclude logs --exclude '__pycache__' --exclude '.pytest_cache' "$SRC/" /opt/arbbot/
[ -x /opt/arbbot/venv/bin/python ] || python3 -m venv /opt/arbbot/venv
/opt/arbbot/venv/bin/pip install --quiet --upgrade pip
/opt/arbbot/venv/bin/pip install --quiet /opt/arbbot
# configs: never overwrite an edited one
[ -f /etc/arbbot/live.toml ] || install -o root -g arbbot -m 640 "$SRC/live.example.toml" /etc/arbbot/live.toml
[ -f /etc/arbbot/measure7d.toml ] || install -o root -g arbbot -m 640 "$SRC/measure7d.toml" /etc/arbbot/measure7d.toml
[ -f /etc/arbbot/live.env ] || install -o root -g arbbot -m 640 "$SRC/ops/live.env.example" /etc/arbbot/live.env
chmod 640 /etc/arbbot/live.env
# units
install -m 644 "$SRC"/ops/arbbot-live.service "$SRC"/ops/arbbot-measure.service "$SRC"/ops/arbbot-rtt.service \
        "$SRC"/ops/arbbot-rollup.service "$SRC"/ops/arbbot-rollup.timer /etc/systemd/system/
systemctl daemon-reload
cat <<MSG

installed. Nothing is running yet. Next, in this order (see /opt/arbbot/RUNBOOK.md):
  1. as the arbbot user, from /var/lib/arbbot:
       sudo -u arbbot /opt/arbbot/venv/bin/python -m arbbot preflight --connectivity --config /etc/arbbot/live.toml
  2. put the API key in /etc/arbbot/live.env (chmod 640 root:arbbot is set); read your fees:
       sudo -u arbbot env \$(grep -v '^#' /etc/arbbot/live.env | xargs) /opt/arbbot/venv/bin/python /opt/arbbot/scripts/read_fees.py
     and paste the printed [venues] block into /etc/arbbot/live.toml
  3. systemctl enable --now arbbot-rtt arbbot-measure arbbot-rollup.timer      (public feeds, no keys)
  4. fund the account, then:
       sudo -u arbbot env \$(grep -v '^#' /etc/arbbot/live.env | xargs) /opt/arbbot/venv/bin/python -m arbbot preflight --config /etc/arbbot/live.toml
  5. stage B: systemctl enable --now arbbot-live        (validation-only orders; ARBBOT_LIVE_FLAGS empty)
MSG
