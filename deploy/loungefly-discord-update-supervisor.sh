#!/usr/bin/env bash
# Install as root:root /usr/local/libexec/loungefly-discord-update.
# Never run a repo-writable shell script as root. Privileged work here is limited
# to starting/stopping the named systemd service and supervising the handshake.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

APP_DIR="${APP_DIR:-/home/pi/LoungeflyPinger}"
SERVICE="${SERVICE:-loungefly-monitor.service}"
UPDATER_USER="${UPDATER_USER:-pi}"
SYSTEMCTL="${SYSTEMCTL:-/usr/bin/systemctl}"
RUNUSER="${RUNUSER:-/usr/sbin/runuser}"
HEALTH_SECONDS="${HEALTH_SECONDS:-20}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-1200}"
WORK_DIR="${WORK_DIR:-/run}"
IN_PROGRESS="$APP_DIR/data/.discord-update-in-progress"
SESSION=""
CHILD=""
RESTART_REQUIRED=0
ORIGINAL_REV=""
ORIGINAL_VENV=""

log() { printf '%s\n' "discord-updater: $*" >&2; }
is_active() { "$SYSTEMCTL" is-active --quiet "$SERVICE"; }
installed_revision() {
    "$RUNUSER" -u "$UPDATER_USER" -- /usr/bin/git -C "$APP_DIR" rev-parse --verify HEAD
}
prior_version_restored() {
    local installed=""
    installed="$(installed_revision)" || return 1
    [[ "$installed" == "$ORIGINAL_REV" ]] || {
        log "Rollback incomplete: Git checkout is not the previous commit."
        return 1
    }
    if [[ -n "$ORIGINAL_VENV" ]]; then
        [[ "$(readlink -f -- "$APP_DIR/.venv" 2>/dev/null || true)" == "$ORIGINAL_VENV" ]] || {
            log "Rollback incomplete: previous virtual environment is not active."
            return 1
        }
    fi
}

health_check() {
    local main_pid="" observed=""
    "$SYSTEMCTL" start "$SERVICE" || return 1
    for ((second = 1; second <= HEALTH_SECONDS; second++)); do
        sleep 1
        is_active || return 1
        observed="$("$SYSTEMCTL" show -p MainPID --value "$SERVICE" 2>/dev/null)" || return 1
        [[ "$observed" =~ ^[1-9][0-9]*$ ]] || return 1
        if [[ -n "$main_pid" && "$main_pid" != "$observed" ]]; then
            log "Monitor PID changed during startup health verification."
            return 1
        fi
        main_pid="$observed"
    done
    log "$SERVICE remained healthy for ${HEALTH_SECONDS}s (MainPID $main_pid)."
}

cleanup() {
    local status=$?
    trap - EXIT HUP INT TERM
    set +e
    if [[ -n "$CHILD" ]]; then
        # The unprivileged updater must never restore SQLite while a candidate
        # might still be running. Stop it *before* acknowledging failure.
        "$SYSTEMCTL" stop "$SERVICE" >/dev/null 2>&1
        printf 'failed\n' >"$SESSION/ack"
        wait "$CHILD" >/dev/null 2>&1
        CHILD=""
    fi
    if (( RESTART_REQUIRED == 1 )); then
        # Never restart a potentially broken candidate when rollback failed.
        if [[ -n "$ORIGINAL_REV" ]] && prior_version_restored; then
            "$SYSTEMCTL" start --no-block "$SERVICE" || true
        else
            log "Not restarting because the previous revision was not restored."
        fi
    fi
    rm -f -- "$IN_PROGRESS"
    if [[ -n "$SESSION" ]]; then rm -rf -- "$SESSION"; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

[[ "$HEALTH_SECONDS" =~ ^[1-9][0-9]*$ ]] || { log "Invalid HEALTH_SECONDS"; exit 1; }
[[ "$READY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { log "Invalid READY_TIMEOUT_SECONDS"; exit 1; }
[[ -f "$APP_DIR/update.sh" ]] || { log "No production updater found"; exit 1; }
[[ ! -e "$IN_PROGRESS" ]] || { log "Another update already owns the marker"; exit 1; }
ORIGINAL_REV="$(installed_revision)"
ORIGINAL_VENV="$(readlink -f -- "$APP_DIR/.venv" 2>/dev/null || true)"

# Prevent loungefly-update.service (a Wants= dependency of monitor.service)
# from starting a second updater while we verify the new candidate.
printf 'active\n' >"$IN_PROGRESS"
SESSION="$(mktemp -d "$WORK_DIR/loungefly-health.XXXXXXXX")"
chmod 0755 "$SESSION"
touch "$SESSION/ready"
chown "$UPDATER_USER:$UPDATER_USER" "$SESSION/ready"
chmod 0600 "$SESSION/ready"
printf 'pending\n' >"$SESSION/ack"
chmod 0644 "$SESSION/ack"

log "Stopping monitor before starting the unprivileged production updater."
"$SYSTEMCTL" stop "$SERVICE"
RESTART_REQUIRED=1
if is_active; then
    log "Monitor remained active after stop; refusing an unsafe activation."
    exit 1
fi

"$RUNUSER" -u "$UPDATER_USER" -- /usr/bin/env \
    "LOUNGEFLY_EXTERNAL_READY_FILE=$SESSION/ready" \
    "LOUNGEFLY_EXTERNAL_ACK_FILE=$SESSION/ack" \
    "EXTERNAL_ACK_TIMEOUT_SECONDS=300" \
    "$APP_DIR/update.sh" &
CHILD="$!"

state=""
for ((second = 1; second <= READY_TIMEOUT_SECONDS; second++)); do
    state="$(cat -- "$SESSION/ready" 2>/dev/null || true)"
    case "$state" in
        candidate_ready|no_change|failed) break ;;
    esac
    sleep 1
done

case "$state" in
    candidate_ready)
        log "Candidate activated. Starting monitor and checking startup health."
        if health_check; then
            printf 'healthy\n' >"$SESSION/ack"
            log "Candidate passed startup verification; updater may finalise."
            if ! wait "$CHILD"; then
                CHILD=""
                log "Updater failed after verification; operator review required."
                exit 1
            fi
            CHILD=""
            RESTART_REQUIRED=0
            log "Discord update committed after healthy startup."
        else
            log "Candidate startup verification failed. Stopping it before rollback."
            "$SYSTEMCTL" stop "$SERVICE" || true
            if is_active; then
                log "Cannot safely roll back: candidate did not stop."
                exit 1
            fi
            printf 'failed\n' >"$SESSION/ack"
            wait "$CHILD" || true
            CHILD=""
            log "Restarting the restored previous version."
            prior_version_restored || exit 1
            health_check || { log "Restored monitor failed startup checks."; exit 1; }
            RESTART_REQUIRED=0
            exit 1
        fi
        ;;
    no_change)
        log "Already up to date; restarting the current monitor."
        if ! wait "$CHILD"; then
            CHILD=""
            exit 1
        fi
        CHILD=""
        health_check || { log "Current monitor failed restart checks."; exit 1; }
        RESTART_REQUIRED=0
        ;;
    *)
        log "Updater failed or timed out before activation: $state"
        "$SYSTEMCTL" stop "$SERVICE" || true
        printf 'failed\n' >"$SESSION/ack"
        wait "$CHILD" || true
        CHILD=""
        log "Restarting the previous version."
        prior_version_restored || exit 1
        health_check || { log "Previous monitor failed restart checks."; exit 1; }
        RESTART_REQUIRED=0
        exit 1
        ;;
esac
