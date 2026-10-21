"""Queue one full GitHub update and process restart via a dedicated systemd path unit.

The Discord process has no permission to control systemd or execute an updater.
The separately installed root-owned systemd units run the updater as the
unprivileged repository owner after stopping the monitor, then restart it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


PATH_UNIT = "loungefly-discord-update.path"
REQUEST_MARKER = Path(__file__).resolve().parents[2] / "data" / ".discord-update-request"


class UpdateUnavailable(RuntimeError):
    """Deployment isn't configured, or its request could not be queued."""


class UpdateRequestService:
    def __init__(self, marker: Path = REQUEST_MARKER) -> None:
        self.marker = marker

    async def available(self) -> bool:
        """Fail closed unless the dedicated systemd path listener is active."""
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", PATH_UNIT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                return await asyncio.wait_for(process.wait(), timeout=5) == 0
            except TimeoutError:
                process.kill()
                await process.wait()
                return False
        except (OSError, RuntimeError):
            return False

    def pending(self) -> bool:
        return self.marker.exists()

    def queue(self, discord_user_id: int) -> bool:
        """Atomically create the fixed request marker; do not accept input paths.

        Returns False when another request is already queued; never overwrites
        the marker. Systemd consumes the marker at service startup.
        """
        try:
            fd = os.open(
                self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise UpdateUnavailable("Unable to queue update; check service permissions.") from exc
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(f"discord_user_id={discord_user_id}\n")
        except OSError as exc:
            raise UpdateUnavailable("Unable to write update request marker.") from exc
        return True
