#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

# LoungeflyPinger production updater
#
# Design goals:
#   - validate the incoming commit before touching the live checkout
#   - never update over local tracked changes
#   - only accept fast-forward updates
#   - keep the running monitor online during download/build/test
#   - stop the monitor only for backup + activation
#   - back up SQLite before the new code can run migrations
#   - atomically switch to a commit-specific virtual environment
#   - automatically roll back code, venv and database if startup fails
#   - serialize concurrent updater runs with flock
#
# Run as the repository owner (normally: pi), NOT as root:
#   cd /home/pi/LoungeflyPinger
#   ./update.sh
#
# If the monitor is currently running, sudo is used only for systemctl.

# Run from a temporary copy. This lets the updater safely clean/replace its own
# tracked update.sh file while the current process keeps executing the exact
# updater version the administrator launched.
ORIGINAL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${LOUNGEFLY_UPDATER_BOOTSTRAPPED:-0}" != "1" ]]; then
    BOOTSTRAP_COPY="$(mktemp "${TMPDIR:-/tmp}/loungefly-update.XXXXXX")"
    cp -- "${BASH_SOURCE[0]}" "$BOOTSTRAP_COPY"
    chmod 0700 "$BOOTSTRAP_COPY"
    export LOUNGEFLY_UPDATER_BOOTSTRAPPED=1
    export LOUNGEFLY_UPDATER_TEMP_SELF="$BOOTSTRAP_COPY"
    export APP_DIR="${APP_DIR:-$ORIGINAL_SCRIPT_DIR}"
    exec "$BOOTSTRAP_COPY" "$@"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-loungefly-monitor.service}"

# Pick the Python used to build candidate virtual environments.
# LoungeflyPinger requires Python 3.12+, but Raspberry Pi OS may still expose
# an older interpreter as `python3`. Never change the OS default just for this
# application: prefer an explicitly configured interpreter, then python3.12,
# then the interpreter from the existing production virtual environment.
if [[ -n "${PYTHON_BIN:-}" ]]; then
    :
elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
elif [[ -x "$APP_DIR/.venv/bin/python" ]] &&      "$APP_DIR/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    PYTHON_BIN="$APP_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1 &&      python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    PYTHON_BIN=""
fi

VENV_LINK="${VENV_LINK:-$APP_DIR/.venv}"
VENV_STORE="${VENV_STORE:-$APP_DIR/.venvs}"
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.update.lock}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP_DIR/.update-backups}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/updater.log}"

FETCH_ATTEMPTS="${FETCH_ATTEMPTS:-5}"
FETCH_RETRY_SECONDS="${FETCH_RETRY_SECONDS:-5}"
MAX_BACKUPS="${MAX_BACKUPS:-10}"
HEALTH_SECONDS="${HEALTH_SECONDS:-20}"
STOP_TIMEOUT_SECONDS="${STOP_TIMEOUT_SECONDS:-60}"
KEEP_VENVS="${KEEP_VENVS:-3}"

STAGE_ROOT=""
CANDIDATE_VENV=""
OLD_VENV_HOLD=""
STATE_BACKUP=""
OLD_COMMIT=""
NEW_COMMIT=""
DB_PATH=""
WAS_ACTIVE=0
SERVICE_STOPPED_BY_US=0
ACTIVATED=0
ROLLBACK_NEEDED=0
LOCAL_UPDATER_OVERRIDE=0
LOCAL_UPDATER_CLEARED=0
OLD_TRACKED_UPDATER_BLOB=""
NEW_TRACKED_UPDATER_BLOB=""

utc_now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
short_sha() { printf '%s' "$1" | cut -c1-12; }

log() {
    local line="[$(utc_now)] $*"
    printf '%s\n' "$line"
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
}

warn() { log "WARNING: $*"; }
die()  { log "ERROR: $*"; exit 1; }

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

systemctl_do() {
    if (( EUID == 0 )); then
        systemctl "$@"
        return
    fi

    if sudo -n true >/dev/null 2>&1; then
        sudo systemctl "$@"
        return
    fi

    if [[ -t 0 || -t 1 ]]; then
        sudo systemctl "$@"
        return
    fi

    log "ERROR: systemctl requires elevated privileges, but no interactive sudo is available."
    return 126
}

service_exists() {
    systemctl cat "$SERVICE" >/dev/null 2>&1
}

service_is_active() {
    systemctl is-active --quiet "$SERVICE" >/dev/null 2>&1
}

wait_until_inactive() {
    local elapsed=0
    while service_is_active; do
        if (( elapsed >= STOP_TIMEOUT_SECONDS )); then
            return 1
        fi
        sleep 1
        ((elapsed += 1))
    done
}

read_dotenv_value() {
    local key="$1"
    local env_file="$APP_DIR/.env"

    [[ -f "$env_file" ]] || return 0

    "$PYTHON_BIN" - "$env_file" "$key" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]

for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() != key:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
}

resolve_db_path() {
    local configured="${LOUNGEFLY_DATABASE_PATH:-}"

    if [[ -z "$configured" ]]; then
        configured="$(read_dotenv_value LOUNGEFLY_DATABASE_PATH || true)"
    fi
    [[ -n "$configured" ]] || configured="data/loungefly.db"

    if [[ "$configured" = /* ]]; then
        DB_PATH="$configured"
    else
        DB_PATH="$APP_DIR/$configured"
    fi
}

backup_database() {
    local src="$1"
    local dst="$2"

    [[ -f "$src" ]] || return 0
    mkdir -p -- "$(dirname -- "$dst")"

    "$PYTHON_BIN" - "$src" "$dst" <<'PY'
import sqlite3
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.parent.mkdir(parents=True, exist_ok=True)

source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {result!r}")
finally:
    target.close()
    source.close()
PY
}

restore_database() {
    local backup="$1"
    local dst="$2"

    [[ -f "$backup" ]] || return 0

    mkdir -p -- "$(dirname -- "$dst")"
    rm -f -- "${dst}-wal" "${dst}-shm" "${dst}-journal"

    if [[ -f "$dst" ]]; then
        mv -- "$dst" "${dst}.failed-update-$(date -u '+%Y%m%dT%H%M%SZ')"
    fi

    cp -a -- "$backup" "$dst"
}

create_state_backup() {
    local stamp
    stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
    mkdir -p -- "$BACKUP_ROOT"
    STATE_BACKUP="$(mktemp -d "$BACKUP_ROOT/${stamp}.XXXXXX")"

    printf '%s\n' "$OLD_COMMIT" >"$STATE_BACKUP/commit"
    printf '%s\n' "$NEW_COMMIT" >"$STATE_BACKUP/target-commit"

    if [[ -f "$APP_DIR/.env" ]]; then
        cp -a -- "$APP_DIR/.env" "$STATE_BACKUP/.env"
    fi

    if [[ -f "$DB_PATH" ]]; then
        log "Creating verified SQLite backup."
        backup_database "$DB_PATH" "$STATE_BACKUP/loungefly.db"
    fi

    log "Pre-update state saved to $STATE_BACKUP"
}

prune_backups() {
    (( MAX_BACKUPS >= 0 )) || return 0

    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\0' 2>/dev/null \
        | sort -zrn \
        | tail -z -n "+$((MAX_BACKUPS + 1))" \
        | cut -z -d' ' -f2- \
        | xargs -0r rm -rf --
}

prune_venvs() {
    [[ -d "$VENV_STORE" ]] || return 0

    local current_target=""
    if [[ -L "$VENV_LINK" ]]; then
        current_target="$(readlink -f -- "$VENV_LINK" 2>/dev/null || true)"
    fi

    local kept=0
    while IFS= read -r -d '' path; do
        if [[ -n "$current_target" && "$(readlink -f -- "$path" 2>/dev/null || true)" == "$current_target" ]]; then
            continue
        fi

        ((kept += 1))
        if (( kept >= KEEP_VENVS )); then
            rm -rf -- "$path"
        fi
    done < <(find "$VENV_STORE" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\0' 2>/dev/null \
        | sort -zrn | cut -z -d' ' -f2-)
}

cleanup_stage() {
    [[ -z "$STAGE_ROOT" || ! -e "$STAGE_ROOT" ]] || rm -rf -- "$STAGE_ROOT"
}

restore_old_venv() {
    rm -f -- "$VENV_LINK" 2>/dev/null || true
    if [[ -n "$OLD_VENV_HOLD" && -e "$OLD_VENV_HOLD" ]]; then
        mv -- "$OLD_VENV_HOLD" "$VENV_LINK"
        OLD_VENV_HOLD=""
    fi
}

rollback_live_install() {
    warn "Rolling back the failed update."

    if service_exists && service_is_active; then
        systemctl_do stop "$SERVICE" || warn "Could not stop failed service during rollback."
        wait_until_inactive || warn "Service did not fully stop during rollback."
    fi

    if [[ -n "$OLD_COMMIT" ]]; then
        git -C "$APP_DIR" reset --hard "$OLD_COMMIT" \
            || warn "Git rollback to $(short_sha "$OLD_COMMIT") failed."
    fi

    restore_old_venv || warn "Virtual-environment rollback failed."

    if [[ -n "$STATE_BACKUP" && -f "$STATE_BACKUP/loungefly.db" ]]; then
        restore_database "$STATE_BACKUP/loungefly.db" "$DB_PATH" \
            || warn "Database rollback failed."
    fi

    if (( WAS_ACTIVE == 1 )) && service_exists; then
        if systemctl_do start "$SERVICE"; then
            sleep 2
            if service_is_active; then
                log "Previous version restored and service restarted."
            else
                warn "Previous version was restored, but the service is not active."
            fi
        else
            warn "Previous version restored, but restarting the service failed."
        fi
    fi
}

restore_local_updater_override() {
    if (( LOCAL_UPDATER_OVERRIDE != 1 || LOCAL_UPDATER_CLEARED != 1 )); then
        return 0
    fi
    [[ -n "${LOUNGEFLY_UPDATER_TEMP_SELF:-}" && -f "${LOUNGEFLY_UPDATER_TEMP_SELF:-}" ]] || return 0

    local tmp="$APP_DIR/.update.sh.restore.$$"
    cp -- "$LOUNGEFLY_UPDATER_TEMP_SELF" "$tmp"
    chmod 0755 "$tmp"
    mv -f -- "$tmp" "$APP_DIR/update.sh"
    LOCAL_UPDATER_CLEARED=0
}

on_exit() {
    local status=$?
    trap - EXIT
    set +e

    if (( status != 0 )); then
        if (( ROLLBACK_NEEDED == 1 )); then
            rollback_live_install
        elif (( SERVICE_STOPPED_BY_US == 1 && WAS_ACTIVE == 1 )) && service_exists; then
            warn "Update failed before activation; restarting the unchanged service."
            systemctl_do start "$SERVICE" || warn "Could not restart the service."
        fi
    fi

    if (( status != 0 )); then
        restore_local_updater_override || warn "Could not restore the locally installed updater."
    fi

    cleanup_stage

    if (( status != 0 )) && [[ -n "$CANDIDATE_VENV" && -d "$CANDIDATE_VENV" ]]; then
        if [[ "$(readlink -f -- "$VENV_LINK" 2>/dev/null || true)" != "$(readlink -f -- "$CANDIDATE_VENV" 2>/dev/null || true)" ]]; then
            rm -rf -- "$CANDIDATE_VENV"
        fi
    fi

    if [[ -n "${LOUNGEFLY_UPDATER_TEMP_SELF:-}" && -f "${LOUNGEFLY_UPDATER_TEMP_SELF:-}" ]]; then
        rm -f -- "$LOUNGEFLY_UPDATER_TEMP_SELF"
    fi

    exit "$status"
}
trap on_exit EXIT

# ---------- preflight ----------

[[ "$FETCH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || die "FETCH_ATTEMPTS must be a positive integer."
[[ "$FETCH_RETRY_SECONDS" =~ ^[0-9]+$ ]] || die "FETCH_RETRY_SECONDS must be a non-negative integer."
[[ "$MAX_BACKUPS" =~ ^[0-9]+$ ]] || die "MAX_BACKUPS must be a non-negative integer."
[[ "$HEALTH_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "HEALTH_SECONDS must be a positive integer."
[[ "$STOP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "STOP_TIMEOUT_SECONDS must be a positive integer."
[[ "$KEEP_VENVS" =~ ^[1-9][0-9]*$ ]] || die "KEEP_VENVS must be a positive integer."

require_command git
require_command flock
require_command tar
[[ -n "$PYTHON_BIN" ]] || die "No Python 3.12+ interpreter was found. Install Python 3.12 or set PYTHON_BIN explicitly."
require_command "$PYTHON_BIN"

[[ -d "$APP_DIR/.git" ]] || die "Not a Git checkout: $APP_DIR"

APP_OWNER_UID="$(stat -c '%u' "$APP_DIR")"
if (( EUID != APP_OWNER_UID )); then
    die "Run this updater as the owner of $APP_DIR (normally 'pi'), not with sudo."
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
    || die "Python 3.12+ is required; found $($PYTHON_BIN --version 2>&1)."

mkdir -p -- "$LOG_DIR" "$BACKUP_ROOT" "$VENV_STORE"

exec 9>"$LOCK_FILE"
flock -n 9 || die "Another LoungeflyPinger update is already running."

cd "$APP_DIR"

CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
[[ "$CURRENT_BRANCH" == "$BRANCH" ]] \
    || die "Expected branch '$BRANCH'; current checkout is '${CURRENT_BRANCH:-detached HEAD}'."

git remote get-url "$REMOTE" >/dev/null 2>&1 \
    || die "Git remote '$REMOTE' does not exist."

# Refuse real application/config changes, but tolerate a locally replaced
# update.sh. That is common when installing a newer updater before the matching
# repository commit exists. The updater runs from a temporary copy, so update.sh
# can be cleaned just before activation without interrupting this process.
TRACKED_CHANGES=()
while IFS= read -r -d '' changed_path; do
    TRACKED_CHANGES+=("$changed_path")
done < <(
    {
        git diff --name-only -z --diff-filter=ACDMRTUXB
        git diff --cached --name-only -z --diff-filter=ACDMRTUXB
    } | sort -zu
)

UNSAFE_CHANGES=()
for changed_path in "${TRACKED_CHANGES[@]}"; do
    if [[ "$changed_path" == "update.sh" ]]; then
        LOCAL_UPDATER_OVERRIDE=1
    else
        UNSAFE_CHANGES+=("$changed_path")
    fi
done

if (( ${#UNSAFE_CHANGES[@]} > 0 )); then
    printf 'Tracked files contain local changes that the updater will not overwrite:\n' >&2
    printf '  - %s\n' "${UNSAFE_CHANGES[@]}" >&2
    die "Commit/stash those changes before updating. A local change to update.sh itself is allowed automatically."
fi

OLD_COMMIT="$(git rev-parse --verify HEAD)"
if (( LOCAL_UPDATER_OVERRIDE == 1 )); then
    OLD_TRACKED_UPDATER_BLOB="$(git rev-parse "$OLD_COMMIT:update.sh" 2>/dev/null || true)"
    log "Detected a local update.sh replacement; it is allowed and will be preserved unless the remote updater changed."
fi
log "Using $($PYTHON_BIN --version 2>&1) at $PYTHON_BIN."
log "Checking $REMOTE/$BRANCH for updates (current $(short_sha "$OLD_COMMIT"))."

# ---------- fetch ----------

FETCH_OK=0
for ((attempt = 1; attempt <= FETCH_ATTEMPTS; attempt++)); do
    if git fetch --prune "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"; then
        FETCH_OK=1
        break
    fi

    warn "Git fetch attempt $attempt/$FETCH_ATTEMPTS failed."
    (( attempt == FETCH_ATTEMPTS )) || sleep "$FETCH_RETRY_SECONDS"
done

(( FETCH_OK == 1 )) || die "Could not fetch $REMOTE/$BRANCH after $FETCH_ATTEMPTS attempts."

NEW_COMMIT="$(git rev-parse --verify "$REMOTE/$BRANCH^{commit}")"
if (( LOCAL_UPDATER_OVERRIDE == 1 )); then
    NEW_TRACKED_UPDATER_BLOB="$(git rev-parse "$NEW_COMMIT:update.sh" 2>/dev/null || true)"
fi

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    log "Already up to date at $(short_sha "$OLD_COMMIT")."
    exit 0
fi

git merge-base --is-ancestor "$OLD_COMMIT" "$NEW_COMMIT" \
    || die "Remote history is not a fast-forward from the installed commit; refusing update."

log "Candidate update: $(short_sha "$OLD_COMMIT") -> $(short_sha "$NEW_COMMIT")."

# ---------- build + validate candidate while live service keeps running ----------

STAGE_ROOT="$(mktemp -d "$APP_DIR/.update-stage.XXXXXX")"
STAGE_SRC="$STAGE_ROOT/src"
mkdir -p -- "$STAGE_SRC"

git archive "$NEW_COMMIT" | tar -x -C "$STAGE_SRC"

CANDIDATE_VENV="$VENV_STORE/$NEW_COMMIT"
rm -rf -- "$CANDIDATE_VENV"

log "Building isolated Python environment for candidate commit."
"$PYTHON_BIN" -m venv "$CANDIDATE_VENV"
"$CANDIDATE_VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
"$CANDIDATE_VENV/bin/python" -m pip install --disable-pip-version-check -r "$STAGE_SRC/requirements.txt"
"$CANDIDATE_VENV/bin/python" -m pip check

log "Running compile checks and full test suite before activation."
(
    cd "$STAGE_SRC"
    "$CANDIDATE_VENV/bin/python" -m compileall -q app tests
    "$CANDIDATE_VENV/bin/python" -c 'import app.main'
    "$CANDIDATE_VENV/bin/python" -m pytest -q
)

log "Candidate passed validation."

# If this updater was installed locally over the repository's tracked update.sh,
# clean only that file now so the fast-forward cannot be blocked by itself.
# All other tracked modifications were rejected above.
if (( LOCAL_UPDATER_OVERRIDE == 1 )); then
    log "Temporarily restoring the repository copy of update.sh for clean activation."
    git reset -q HEAD -- update.sh
    git checkout -- update.sh
    LOCAL_UPDATER_CLEARED=1
fi

# ---------- activation window ----------

if service_exists && service_is_active; then
    WAS_ACTIVE=1
    log "Stopping $SERVICE for safe activation."
    systemctl_do stop "$SERVICE"
    SERVICE_STOPPED_BY_US=1
    wait_until_inactive || die "$SERVICE did not stop within ${STOP_TIMEOUT_SECONDS}s."
else
    log "$SERVICE is not currently active; activation will not start it automatically."
fi

resolve_db_path
create_state_backup

# From this point onward a failure requires a full rollback.
ROLLBACK_NEEDED=1

log "Fast-forwarding live checkout to $(short_sha "$NEW_COMMIT")."
git merge --ff-only "$NEW_COMMIT"
ACTIVATED=1

# Preserve the existing .venv exactly until the new service has survived health verification.
if [[ -e "$VENV_LINK" || -L "$VENV_LINK" ]]; then
    OLD_VENV_HOLD="$APP_DIR/.venv.rollback.$(date -u '+%Y%m%dT%H%M%SZ').$$"
    mv -- "$VENV_LINK" "$OLD_VENV_HOLD"
fi

NEW_LINK="$APP_DIR/.venv.new.$$"
rm -f -- "$NEW_LINK"
ln -s -- ".venvs/$NEW_COMMIT" "$NEW_LINK"
mv -T -- "$NEW_LINK" "$VENV_LINK"

# Final sanity check using the exact interpreter systemd will execute.
"$VENV_LINK/bin/python" -m pip check
(
    cd "$APP_DIR"
    "$VENV_LINK/bin/python" -c 'import app.main'
)

if (( WAS_ACTIVE == 1 )); then
    log "Starting $SERVICE with the new version."
    systemctl_do start "$SERVICE"

    log "Verifying service stability for ${HEALTH_SECONDS}s."
    for ((second = 1; second <= HEALTH_SECONDS; second++)); do
        sleep 1
        if ! service_is_active; then
            die "$SERVICE became inactive during post-update health verification."
        fi
    done

    MAIN_PID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)"
    [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]] \
        || die "$SERVICE reports no live MainPID after update."

    log "$SERVICE is healthy (MainPID $MAIN_PID)."
fi

# ---------- commit success ----------

ROLLBACK_NEEDED=0
SERVICE_STOPPED_BY_US=0

if [[ -n "$OLD_VENV_HOLD" && -e "$OLD_VENV_HOLD" ]]; then
    rm -rf -- "$OLD_VENV_HOLD"
    OLD_VENV_HOLD=""
fi

if (( LOCAL_UPDATER_OVERRIDE == 1 && LOCAL_UPDATER_CLEARED == 1 )); then
    if [[ -n "$OLD_TRACKED_UPDATER_BLOB" && "$OLD_TRACKED_UPDATER_BLOB" == "$NEW_TRACKED_UPDATER_BLOB" ]]; then
        restore_local_updater_override
        log "Preserved the locally installed update.sh because the remote updater did not change."
    else
        LOCAL_UPDATER_CLEARED=0
        log "The remote update includes a new update.sh; keeping the repository version."
    fi
fi

printf '%s\n' "$NEW_COMMIT" >"$BACKUP_ROOT/last-good-commit.txt"
printf '%s\n' "$OLD_COMMIT" >"$STATE_BACKUP/previous-commit"
printf '%s\n' "$NEW_COMMIT" >"$STATE_BACKUP/installed-commit"

prune_backups
prune_venvs
cleanup_stage
STAGE_ROOT=""

log "Update successful: $(short_sha "$OLD_COMMIT") -> $(short_sha "$NEW_COMMIT")."
