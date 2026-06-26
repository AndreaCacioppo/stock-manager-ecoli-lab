#!/usr/bin/env bash
# backup.sh — snapshot the SQLite database to /var/backups using SQLite's online
# backup API, then prune snapshots older than 30 days.
#
# Run by stock-backup.service (nightly via stock-backup.timer), or by hand:
#   sudo -u stock /opt/stock-manager/deploy/backup.sh
#
# The online backup API takes an atomic, consistent copy while the app is running.
# A plain `cp` of the live DB, or copying the -wal/-shm files separately, can
# capture a torn, unusable state.
set -euo pipefail

DB=/opt/stock-manager/instance/stock.db
DEST=/var/backups/stock-manager

# Fail if the database is missing, so a run can never produce a silent empty
# snapshot (sqlite3.connect would otherwise create an empty file).
if [ ! -f "$DB" ]; then
    echo "backup.sh: database not found at $DB" >&2
    exit 1
fi

mkdir -p "$DEST"
OUT="$DEST/stock-$(date +%F_%H%M).db"

# `python -` reads the program from stdin (the heredoc below); the two paths after
# it become sys.argv[1] (source) and sys.argv[2] (destination).
/opt/stock-manager/.venv/bin/python - "$DB" "$OUT" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
with destination:
    source.backup(destination)
destination.close()
source.close()
PY

# Keep about 30 days of snapshots.
find "$DEST" -name 'stock-*.db' -mtime +30 -delete
