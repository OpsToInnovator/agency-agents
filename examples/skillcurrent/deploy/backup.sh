#!/bin/sh
# Daily consistent backup of the SkillCurrent database (safe while serving),
# keeping 14 days. Run from cron, e.g.:
#   15 3 * * *  /var/lib/skillcurrent/backup.sh
# With Docker:  docker compose exec -T skillcurrent skillcurrent backup /data/backups/$(date +%F).sqlite
# Copy the backups directory off the machine as well; a backup on the same disk is not a backup.
set -eu
DB="${SKILLCURRENT_DB:-/var/lib/skillcurrent/skillcurrent.sqlite}"
DIR="${BACKUP_DIR:-/var/lib/skillcurrent/backups}"
BIN="${SKILLCURRENT_BIN:-/var/lib/skillcurrent/venv/bin/skillcurrent}"
mkdir -p "$DIR"
"$BIN" --db "$DB" backup "$DIR/$(date +%F).sqlite"
find "$DIR" -name '*.sqlite' -mtime +14 -delete
