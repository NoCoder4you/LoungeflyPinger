#!/usr/bin/env bash
# Deterministic safety/static integration checks. Python tests exercise the unit
# and ignore policies; this suite deliberately never invokes a host service.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

bash -n update.sh deploy/update.sh deploy/restore-backup.sh deploy/loungefly-discord-update-supervisor.sh
grep -Fq 'git merge --ff-only' update.sh
grep -Fq 'merge-base --is-ancestor' update.sh
grep -Fq 'flock -n' update.sh
grep -Fq 'source.backup(target)' update.sh
grep -Fq 'PRAGMA integrity_check' update.sh
grep -Fq 'mv -T -- "$NEW_LINK" "$VENV_LINK"' update.sh
grep -Fq 'sudo -n systemctl "$@"' update.sh
! grep -Fq 'sudo -n true' update.sh
grep -Fq 'cleanup_stage' update.sh

for path in .update-backups/x/.env .update-backups/x/loungefly.db \
  .update-stage.x/src .update.lock .venvs/a/bin/python .venv.new.1 \
  .venv.rollback.1 data/loungefly.db.failed-update-x; do
    git check-ignore --quiet -- "$path"
done

# Guard the explicit application-root boundary and whitespace-safe operations.
grep -Fq '[[ -d "$APP_DIR/.git" ]]' update.sh
grep -Fq 'git -C "$APP_DIR"' update.sh
grep -Fq 'rm -rf -- "$STAGE_ROOT"' update.sh
echo "updater integration/static safeguards: PASS"

# End-to-end fast-forward in disposable repositories. All Python build actions
# and service discovery are faked; no installed checkout or service is touched.
tmp=$(mktemp -d "${TMPDIR:-/tmp}/loungefly updater test.XXXXXX")
trap 'result=$?; if (( result != 0 )); then echo "Updater integration failed near line $LINENO" >&2; for path in "$tmp"/failed-health.log "$tmp"/healthy.log "$tmp"/no-change.log "$tmp"/application*/logs/updater.log; do if [[ -f "$path" ]]; then echo "===== $path =====" >&2; tail -n 60 "$path" >&2; fi; done; fi; rm -rf -- "$tmp"; exit "$result"' EXIT
git init --bare -q "$tmp/remote.git"
git init -q -b main "$tmp/source"
git -C "$tmp/source" config user.email test@example.invalid
git -C "$tmp/source" config user.name updater-test
cp update.sh "$tmp/source/update.sh"
printf 'aiohttp==3.12.15\n' >"$tmp/source/requirements.txt"
mkdir -p "$tmp/source/app" "$tmp/source/tests"
touch "$tmp/source/app/__init__.py" "$tmp/source/app/main.py"
git -C "$tmp/source" add .
git -C "$tmp/source" commit -qm initial
git -C "$tmp/source" remote add origin "$tmp/remote.git"
git -C "$tmp/source" push -q -u origin main
git clone -q "$tmp/remote.git" "$tmp/application with spaces"
git -C "$tmp/application with spaces" checkout -q main
printf 'updated\n' >"$tmp/source/version"
git -C "$tmp/source" add version
git -C "$tmp/source" commit -qm update
git -C "$tmp/source" push -q

cat >"$tmp/fake-python" <<'FAKEPY'
#!/usr/bin/env bash
set -eu
if [[ ${1:-} == -m && ${2:-} == venv ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
fi
exit 0
FAKEPY
chmod +x "$tmp/fake-python"
PATH="$tmp/fake-bin:$PATH" mkdir -p "$tmp/fake-bin"
cat >"$tmp/fake-bin/systemctl" <<'FAKESYSTEMCTL'
#!/usr/bin/env bash
exit 1
FAKESYSTEMCTL
chmod +x "$tmp/fake-bin/systemctl"

APP_DIR="$tmp/application with spaces" PYTHON_BIN="$tmp/fake-python" \
  PATH="$tmp/fake-bin:$PATH" HEALTH_SECONDS=1 FETCH_RETRY_SECONDS=0 \
  "$tmp/application with spaces/update.sh" >/dev/null
test -f "$tmp/application with spaces/version"
test -L "$tmp/application with spaces/.venv"
test -x "$tmp/application with spaces/.venv/bin/python"
! compgen -G "$tmp/application with spaces/.update-stage.*" >/dev/null
echo "updater disposable fast-forward integration: PASS"


# Exercise production startup verification with fake systemctl + Python:
# a commit passing compile/import/tests must still roll back if its service dies.
cat >"$tmp/fake-python" <<'FAKEPY2'
#!/usr/bin/env bash
set -eu
if [[ ${1:-} == -m && ${2:-} == venv ]]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
elif [[ ${1:-} == - ]]; then
    exec python3 "$@"
fi
exit 0
FAKEPY2
chmod +x "$tmp/fake-python"
cat >"$tmp/fake-bin/systemctl" <<'SYSTEMCTL'
#!/usr/bin/env bash
set -eu
cmd="$1"
shift
if [[ "$cmd" == start && "${1:-}" == --no-block ]]; then shift; fi
case "$cmd" in
  stop) printf 'inactive\n' >"$APP_DIR/monitor-state";;
  start)
    printf 'active\n' >"$APP_DIR/monitor-state"
    if [[ "$(cat "$APP_DIR/version")" == "broken" ]]; then
      printf 'broken-database' >"$APP_DIR/data/loungefly.db"
    fi
    ;;
  is-active)
    [[ "$(cat "$APP_DIR/monitor-state")" == active &&
       "$(cat "$APP_DIR/version")" != broken ]] || exit 3;;
  show) printf '4123\n';;
  cat) exit 0;;
  *) echo "unexpected systemctl $cmd" >&2; exit 99;;
esac
SYSTEMCTL
chmod +x "$tmp/fake-bin/systemctl"
cat >"$tmp/fake-runuser" <<'RUNUSER'
#!/usr/bin/env bash
set -eu
[[ "$1" == -u && "$3" == -- ]] || exit 2
shift 3
exec "$@"
RUNUSER
chmod +x "$tmp/fake-runuser"
mkdir -p "$tmp/application with spaces/data"
python3 - "$tmp/application with spaces/data/loungefly.db" <<'PREPAREDB'
import sqlite3
import sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE marker(value TEXT)")
c.execute("INSERT INTO marker VALUES ('prior data')")
c.commit()
c.close()
PREPAREDB
printf 'active\n' >"$tmp/application with spaces/monitor-state"
old_commit="$(git -C "$tmp/application with spaces" rev-parse HEAD)"
old_venv="$(readlink "$tmp/application with spaces/.venv")"
printf 'broken\n' >"$tmp/source/version"
git -C "$tmp/source" add version
git -C "$tmp/source" commit -qm broken-production-startup
git -C "$tmp/source" push -q
supervisor="$PWD/deploy/loungefly-discord-update-supervisor.sh"
common_env=(APP_DIR="$tmp/application with spaces" SYSTEMCTL="$tmp/fake-bin/systemctl"
            RUNUSER="$tmp/fake-runuser" UPDATER_USER="$(id -un)"
            PYTHON_BIN="$tmp/fake-python" HEALTH_SECONDS=2 READY_TIMEOUT_SECONDS=30
            FETCH_RETRY_SECONDS=0 WORK_DIR="$tmp" PATH="$tmp/fake-bin:$PATH")
if env "${common_env[@]}" bash "$supervisor" >"$tmp/failed-health.log" 2>&1; then
  echo 'A broken production startup must fail the Discord update' >&2
  exit 1
fi
test "$(git -C "$tmp/application with spaces" rev-parse HEAD)" = "$old_commit"
test "$(readlink "$tmp/application with spaces/.venv")" = "$old_venv"
test "$(cat "$tmp/application with spaces/monitor-state")" = active
python3 - "$tmp/application with spaces/data/loungefly.db" <<'CHECKDB'
import sqlite3
import sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert c.execute("SELECT value FROM marker").fetchone()[0] == "prior data"
c.close()
CHECKDB
grep -Fq 'Candidate startup verification failed' "$tmp/failed-health.log"
grep -Fq 'Rolling back the failed update' "$tmp/application with spaces/logs/updater.log"
test ! -e "$tmp/application with spaces/data/.discord-update-in-progress"
echo "Discord updater failed-startup rollback: PASS"

printf 'working\n' >"$tmp/source/version"
git -C "$tmp/source" add version
git -C "$tmp/source" commit -qm healthy-production-startup
git -C "$tmp/source" push -q
env "${common_env[@]}" bash "$supervisor" >"$tmp/healthy.log" 2>&1
test "$(cat "$tmp/application with spaces/version")" = working
test "$(git -C "$tmp/application with spaces" rev-parse HEAD)" = \
  "$(git -C "$tmp/source" rev-parse HEAD)"
test "$(cat "$tmp/application with spaces/monitor-state")" = active
test -f "$tmp/application with spaces/.update-backups/last-good-commit.txt"
grep -Fq 'Discord update committed after healthy startup' "$tmp/healthy.log"
test ! -e "$tmp/application with spaces/data/.discord-update-in-progress"
echo "Discord updater verified healthy activation: PASS"

env "${common_env[@]}" bash "$supervisor" >"$tmp/no-change.log" 2>&1
grep -Fq 'Already up to date; restarting the current monitor' "$tmp/no-change.log"
test "$(cat "$tmp/application with spaces/monitor-state")" = active
echo "Discord updater already-current restart: PASS"
