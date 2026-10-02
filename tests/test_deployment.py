from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_monitor_unit_has_bounded_restart_policy() -> None:
    unit = (ROOT / "deploy/loungefly-monitor.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in unit
    assert "RestartSec=10s" in unit
    assert "StartLimitIntervalSec=5min" in unit
    assert "StartLimitBurst=5" in unit
    assert "TimeoutStopSec=45s" in unit


def test_updater_logrotate_policy_is_bounded_and_unprivileged() -> None:
    policy = (ROOT / "deploy/loungefly-updater.logrotate").read_text(encoding="utf-8")
    for directive in ("size 5M", "rotate 3", "compress", "missingok", "notifempty",
                      "su pi pi", "create 0600 pi pi"):
        assert directive in policy


def test_runtime_updater_state_is_ignored() -> None:
    paths = [
        ".update-backups/x/.env", ".update-backups/x/loungefly.db", ".update-stage.abc/src",
        ".update.lock", ".venvs/commit/bin/python", ".venv.new.1", ".venv.rollback.1",
        "data/loungefly.db.failed-update-20260101T000000Z",
    ]
    for path in paths:
        result = subprocess.run(["git", "check-ignore", "--quiet", "--", path], cwd=ROOT)
        assert result.returncode == 0, path


def test_shell_scripts_parse() -> None:
    subprocess.run(["bash", "-n", "update.sh", "deploy/update.sh"], cwd=ROOT, check=True)
