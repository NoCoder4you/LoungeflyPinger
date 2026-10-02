#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR=/home/pi/LoungeflyPinger
SERVICE=loungefly-monitor.service
DB="$APP_DIR/data/loungefly.db"
BACKUP=${1:-}

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0 BACKUP.db" >&2; exit 2; }
[[ -n $BACKUP && -f $BACKUP ]] || { echo "Select an existing backup file." >&2; exit 2; }
case "$(readlink -f -- "$BACKUP")" in "$APP_DIR"/*) ;; *) echo "Backup must be inside $APP_DIR" >&2; exit 2;; esac
if systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE must already be stopped." >&2
    exit 2
fi

check_integrity() {
    [[ "$(sqlite3 "$1" 'PRAGMA integrity_check;')" == "ok" ]]
}

check_integrity "$BACKUP" || { echo "Selected backup failed integrity_check." >&2; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
preserved="$APP_DIR/data/pre-restore-$stamp-$$"
mkdir -m 0700 -- "$preserved"
for path in "$DB" "$DB-wal" "$DB-shm"; do
    [[ ! -e $path ]] || cp -a -- "$path" "$preserved/"
done

temporary="$APP_DIR/data/.loungefly.db.restore-$stamp-$$"
trap 'rm -f -- "$temporary"' EXIT
cp -- "$BACKUP" "$temporary"
chown pi:pi "$temporary"
chmod 0600 "$temporary"
check_integrity "$temporary" || { echo "Temporary replacement failed integrity_check." >&2; exit 1; }
mv -f -- "$temporary" "$DB"
rm -f -- "$DB-wal" "$DB-shm"

if ! systemctl start "$SERVICE" || ! systemctl is-active --quiet "$SERVICE" || ! check_integrity "$DB"; then
    echo "Restore validation failed. Service has been stopped." >&2
    systemctl stop "$SERVICE" || true
    echo "Rollback: copy $preserved/loungefly.db (and any sidecars) back to $DB while stopped, chown pi:pi, chmod 0600, then start and validate." >&2
    exit 1
fi
echo "Restore verified. Pre-restore state: $preserved"
