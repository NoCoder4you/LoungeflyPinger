"""Persistent release state, history, and reminder deduplication."""

from datetime import UTC, datetime

from app.database import Database, release_history_key
from app.models import ReleaseInfo, ReleasePrecision


class ReleaseService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _values(info: ReleaseInfo) -> tuple[object, ...]:
        return (
            info.release_date.isoformat() if info.release_date else None,
            info.release_time.isoformat() if info.release_time else None,
            info.timezone,
            info.release_datetime.isoformat() if info.release_datetime else None,
            info.precision.value, info.text, info.source, info.timezone_inferred,
            info.release_month, info.release_year,
        )

    async def current(self, product_id: int) -> ReleaseInfo | None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        row = await (await connection.execute(
            """SELECT release_date, release_time, release_timezone, release_datetime,
                      release_precision, release_text, release_source, release_timezone_inferred,
                      release_month, release_year
                 FROM product_states WHERE product_id=?""", (product_id,)
        )).fetchone()
        if row is None or row[4] is None:
            return None
        return ReleaseInfo(
            ReleasePrecision(row[4]),
            datetime.fromisoformat(row[0]).date() if row[0] else None,
            datetime.fromisoformat(f"2000-01-01T{row[1]}").time() if row[1] else None,
            row[2], datetime.fromisoformat(row[3]) if row[3] else None,
            row[5], row[6], bool(row[7]), row[8], row[9],
        )

    async def record(self, product_id: int, info: ReleaseInfo) -> None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        values = self._values(info)
        history_key = release_history_key(values)
        await connection.execute(
            """UPDATE product_states SET release_date=?, release_time=?, release_timezone=?,
                      release_datetime=?, release_precision=?, release_text=?, release_source=?,
                      release_timezone_inferred=?, release_month=?, release_year=? WHERE product_id=?""",
            (*values, product_id)
        )
        await connection.execute(
            """INSERT OR IGNORE INTO release_history
               (release_date, release_time, release_timezone, release_datetime, release_precision,
                release_text, release_source, release_timezone_inferred, release_month, release_year,
                release_key, product_id, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*values, history_key, product_id, datetime.now(UTC).isoformat()),
        )

    async def reminder_sent(self, product_id: int, instant: datetime, seconds: int) -> bool:
        connection = self.database.connection
        assert connection is not None
        row = await (await connection.execute(
            "SELECT 1 FROM release_reminders WHERE product_id=? AND release_datetime=? AND reminder_seconds=?",
            (product_id, instant.isoformat(), seconds),
        )).fetchone()
        return row is not None

    async def mark_reminder(self, product_id: int, instant: datetime, seconds: int) -> None:
        connection = self.database.connection
        assert connection is not None
        await connection.execute(
            "INSERT OR IGNORE INTO release_reminders VALUES (?, ?, ?, ?)",
            (product_id, instant.isoformat(), seconds, datetime.now(UTC).isoformat()),
        )
