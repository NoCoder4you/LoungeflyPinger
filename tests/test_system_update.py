"""The Discord update request never runs the updater or systemctl as the bot."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.system_update_service import PATH_UNIT, UpdateRequestService, UpdateUnavailable


def test_request_marker_is_exclusive_and_private(tmp_path: Path) -> None:
    marker = tmp_path / "data" / ".discord-update-request"
    marker.parent.mkdir()
    service = UpdateRequestService(marker)
    assert service.pending() is False
    assert service.queue(42) is True
    assert service.pending() is True
    assert marker.read_text() == "discord_user_id=42\n"
    assert (marker.stat().st_mode & 0o777) == 0o600
    assert service.queue(77) is False
    assert marker.read_text() == "discord_user_id=42\n"


def test_update_request_is_busy_during_startup_verification(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    listener = UpdateRequestService(data / ".discord-update-request")
    assert listener.pending() is False
    (data / ".discord-update-in-progress").write_text("active\n")
    assert listener.pending() is True
    (data / ".discord-update-in-progress").unlink()
    assert listener.pending() is False


def test_missing_request_directory_fails_closed(tmp_path: Path) -> None:
    service = UpdateRequestService(tmp_path / "missing" / "request")
    with pytest.raises(UpdateUnavailable):
        service.queue(42)


@pytest.mark.asyncio
async def test_update_listener_check_is_fail_closed(monkeypatch) -> None:
    service = UpdateRequestService()
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    invoked = []

    async def start(*args, **kwargs):
        invoked.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    assert await service.available() is True
    assert invoked[0][0] == ("systemctl", "is-active", "--quiet", PATH_UNIT)
    assert invoked[0][1]["stdin"] == asyncio.subprocess.DEVNULL

    process.wait.return_value = 3
    assert await service.available() is False

    async def unavailable(*args, **kwargs):
        raise OSError("not present")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    assert await service.available() is False


def test_systemd_units_use_fixed_bounded_commands_not_shell() -> None:
    root = Path(__file__).parents[1]
    service = (root / "deploy/loungefly-discord-update.service").read_text()
    path = (root / "deploy/loungefly-discord-update.path").read_text()
    assert "PathExists=/home/pi/LoungeflyPinger/data/.discord-update-request" in path
    assert "ExecStart=/usr/local/libexec/loungefly-discord-update" in service
    assert "ExecStopPost=" not in service
    assert "ExecStart=/usr/sbin/runuser" not in service
    assert "TimeoutStartSec=30min" in service
    script = (root / "deploy/loungefly-discord-update-supervisor.sh").read_text()
    assert '"$RUNUSER" -u "$UPDATER_USER" -- /usr/bin/env' in script
    assert '"$SYSTEMCTL" stop "$SERVICE"' in script
    assert '"$SYSTEMCTL" start "$SERVICE"' in script
    assert "candidate_ready|no_change|failed" in script
    assert "printf 'healthy\\n' >\"$SESSION/ack\"" in script
    assert "printf 'failed\\n' >\"$SESSION/ack\"" in script
    assert 'wait "$CHILD"' in script
    assert "chown \"$UPDATER_USER:$UPDATER_USER\"" in script
    normal_updater = (root / "deploy/loungefly-update.service").read_text()
    assert "ConditionPathExists=!/home/pi/LoungeflyPinger/data/.discord-update-in-progress" in normal_updater
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "/bin/sh -c" not in service
    assert "sudo" not in service


def test_deploy_does_not_expand_monitor_sandbox_write_permissions() -> None:
    root = Path(__file__).parents[1]
    service = (root / "deploy/loungefly-monitor.service").read_text()
    assert "ReadWritePaths=/home/pi/LoungeflyPinger/data /home/pi/LoungeflyPinger/logs" in service


def test_updater_defers_commit_and_rollback_cleanup_to_health_ack() -> None:
    root = Path(__file__).parents[1]
    script = (root / "update.sh").read_text()
    assert "LOUNGEFLY_EXTERNAL_READY_FILE" in script
    assert "LOUNGEFLY_EXTERNAL_ACK_FILE" in script
    assert "printf 'candidate_ready\\n' >\"$EXTERNAL_READY_FILE\"" in script
    assert 'if [[ "$EXTERNAL_ACK_RESULT" != "healthy" ]]; then' in script
    assert script.index('if [[ "$EXTERNAL_ACK_RESULT" != "healthy" ]]; then') < script.index('ROLLBACK_NEEDED=0', script.index("# ---------- commit success ----------"))
    assert 'if (( EXTERNAL_HEALTH == 1 )) && service_is_active; then' in script
    assert 'restore_database "$STATE_BACKUP/loungefly.db" "$DB_PATH"' in script
    assert 'if (( EXTERNAL_HEALTH == 1 )); then' in script
