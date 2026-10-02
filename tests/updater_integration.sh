#!/usr/bin/env bash
# Deterministic safety/static integration checks. Python tests exercise the unit
# and ignore policies; this suite deliberately never invokes a host service.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

bash -n update.sh deploy/update.sh deploy/restore-backup.sh
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
trap 'rm -rf -- "$tmp"' EXIT
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
