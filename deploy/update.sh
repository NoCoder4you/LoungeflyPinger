#!/usr/bin/env bash
set -Eeuo pipefail

# Safe, unattended updater for the systemd installation documented in README.md.
# The monitor must be stopped while this runs (the supplied units enforce that).

APP_DIR="${APP_DIR:-/opt/loungefly-monitor}"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.update.lock}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/.update-backups}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/updater.log}"
MAX_BACKUPS="${MAX_BACKUPS:-10}"
FETCH_ATTEMPTS="${FETCH_ATTEMPTS:-5}"
FETCH_RETRY_SECONDS="${FETCH_RETRY_SECONDS:-5}"

timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() {
    local line="[$(timestamp)] $*"
    printf '%s\n' "$line"
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
}
warn() { log "WARNING: $*"; }
fatal() { log "ERROR: $*"; exit 1; }

OLD_COMMIT=""
UPDATE_APPLIED=0
STAGED_VENV=""
cleanup() {
    local status=$?
    if (( status != 0 && UPDATE_APPLIED == 1 )) && [[ -n "$OLD_COMMIT" ]]; then
        warn "Update failed; restoring commit ${OLD_COMMIT:0:12}."
        git reset --hard "$OLD_COMMIT" || warn "Automatic Git rollback failed."
    fi
    [[ -z "$STAGED_VENV" ]] || rm -rf -- "$STAGED_VENV"
    exit "$status"
}
trap cleanup EXIT

[[ "$MAX_BACKUPS" =~ ^[0-9]+$ ]] || fatal "MAX_BACKUPS must be a non-negative integer."
[[ "$FETCH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fatal "FETCH_ATTEMPTS must be a positive integer."
[[ "$FETCH_RETRY_SECONDS" =~ ^[0-9]+$ ]] || fatal "FETCH_RETRY_SECONDS must be a non-negative integer."
command -v flock >/dev/null || fatal "flock is not installed."
command -v git >/dev/null || fatal "git is not installed."
command -v "$PYTHON_BIN" >/dev/null || fatal "Python was not found: $PYTHON_BIN"
[[ -d "$APP_DIR/.git" ]] || fatal "Not a Git working tree: $APP_DIR"

cd "$APP_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || fatal "Another update is already running."

"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
    || fatal "Python 3.12 or newer is required ($("$PYTHON_BIN" --version 2>&1))."
git remote get-url "$REMOTE" >/dev/null 2>&1 || fatal "Git remote '$REMOTE' does not exist."

log "Starting update for $APP_DIR ($REMOTE/$BRANCH)."
OLD_COMMIT="$(git rev-parse --verify HEAD)"
CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
[[ "$CURRENT_BRANCH" == "$BRANCH" ]] \
    || fatal "Expected branch '$BRANCH'; currently on '${CURRENT_BRANCH:-detached HEAD}'."

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    warn "Tracked files have local modifications; leaving the installation unchanged."
    exit 0
fi

FETCHED=0
for ((attempt = 1; attempt <= FETCH_ATTEMPTS; attempt++)); do
    if git fetch --prune "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"; then
        FETCHED=1
        break
    fi
    warn "Fetch attempt $attempt of $FETCH_ATTEMPTS failed."
    (( attempt == FETCH_ATTEMPTS )) || sleep "$FETCH_RETRY_SECONDS"
done
if (( FETCHED == 0 )); then
    warn "Remote is unavailable; continuing with commit ${OLD_COMMIT:0:12}."
    exit 0
fi

REMOTE_COMMIT="$(git rev-parse --verify "$REMOTE/$BRANCH^{commit}")"
if [[ "$OLD_COMMIT" == "$REMOTE_COMMIT" ]]; then
    log "Already up to date at ${OLD_COMMIT:0:12}."
    exit 0
fi
git merge-base --is-ancestor "$OLD_COMMIT" "$REMOTE_COMMIT" \
    || fatal "Remote history is not a fast-forward; refusing to overwrite the installation."

mkdir -p "$BACKUP_DIR"
STATE_BACKUP="$(mktemp -d "$BACKUP_DIR/$(date -u '+%Y%m%dT%H%M%SZ').XXXXXX")"
printf '%s\n' "$OLD_COMMIT" >"$STATE_BACKUP/commit"
[[ ! -f .env ]] || cp -a .env "$STATE_BACKUP/.env"
if [[ -d data ]]; then
    mkdir "$STATE_BACKUP/data"
    find data -maxdepth 1 -type f \
        \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.db-journal' \) \
        -exec cp -a -t "$STATE_BACKUP/data" -- {} +
fi
log "Runtime state backed up to $STATE_BACKUP."

NEW_REQUIREMENTS="$(git show "$REMOTE_COMMIT:requirements.txt" 2>/dev/null || true)"
STAGED_VENV="${VENV_DIR}.new.$$"
rm -rf -- "$STAGED_VENV"
log "Building an isolated replacement virtual environment."
"$PYTHON_BIN" -m venv "$STAGED_VENV"
"$STAGED_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
[[ -z "$NEW_REQUIREMENTS" ]] || \
    "$STAGED_VENV/bin/python" -m pip install -r <(printf '%s\n' "$NEW_REQUIREMENTS")
VALIDATION_PYTHON="$STAGED_VENV/bin/python"

git merge --ff-only "$REMOTE/$BRANCH"
UPDATE_APPLIED=1
log "Validating commit ${REMOTE_COMMIT:0:12}."
"$VALIDATION_PYTHON" -m compileall -q app tests
if [[ -d tests ]]; then
    "$VALIDATION_PYTHON" -m pytest -q
fi

OLD_VENV="${VENV_DIR}.old.$$"
[[ ! -e "$VENV_DIR" ]] || mv "$VENV_DIR" "$OLD_VENV"
if ! mv "$STAGED_VENV" "$VENV_DIR"; then
    [[ ! -e "$OLD_VENV" ]] || mv "$OLD_VENV" "$VENV_DIR"
    fatal "Could not activate the replacement virtual environment."
fi
STAGED_VENV=""
rm -rf -- "$OLD_VENV"

UPDATE_APPLIED=0
printf '%s\n' "$REMOTE_COMMIT" >"$BACKUP_DIR/last-good-commit.txt"
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\0' \
    | sort -zrn | tail -z -n "+$((MAX_BACKUPS + 1))" | cut -z -d' ' -f2- \
    | xargs -0r rm -rf --
log "Update complete: ${OLD_COMMIT:0:12} -> ${REMOTE_COMMIT:0:12}."
