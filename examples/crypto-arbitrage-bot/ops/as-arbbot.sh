#!/usr/bin/env bash
# Run one arbbot command as the service user, with the live keys in its environment and the
# working directory the service uses, so relative paths in live.toml (STOP, logs/live/...)
# resolve to the same files the running bot writes. Root only: the keys file is 640 root:arbbot.
#
#   sudo /opt/arbbot/ops/as-arbbot.sh /opt/arbbot/venv/bin/python -m arbbot reconcile --config /etc/arbbot/live.toml
#
# The keys never appear on a command line (sudo logs argv; this sources them into the
# environment instead) and the child runs with arbbot's uid/gid, so anything it writes
# (the risk state after --clear-halt) stays readable by the service.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "run as root: sudo $0 ..." >&2; exit 2; fi
if [ "$#" -eq 0 ]; then echo "usage: $0 <command> [args...]" >&2; exit 2; fi
set -a
# shellcheck disable=SC1091
. /etc/arbbot/live.env
set +a
cd /var/lib/arbbot
exec setpriv --reuid arbbot --regid arbbot --init-groups "$@"
