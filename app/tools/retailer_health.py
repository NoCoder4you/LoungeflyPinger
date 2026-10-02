"""Print sanitized persisted retailer health for production diagnosis."""
from __future__ import annotations
import argparse
import asyncio
from app.config import load_config
from app.database import Database

async def _run(failed_only: bool) -> None:
    config = load_config()
    async with Database(config.database_path) as database:
        assert database.connection is not None
        where = "WHERE health IN ('DEGRADED','FAILED','BLOCKED_BY_RETAILER')" if failed_only else ""
        rows = await (await database.connection.execute(f"""
            SELECT name, enabled, health, circuit_state, last_success, last_failure,
                   consecutive_failures, response_status, error_category,
                   circuit_open_until, request_duration
            FROM retailers {where} ORDER BY name
        """)).fetchall()
    headings = ("RETAILER", "ENABLED", "HEALTH", "CIRCUIT", "LAST SUCCESS", "LAST FAILURE",
                "FAILS", "STATUS", "CATEGORY", "NEXT PROBE", "SECONDS")
    values = [headings] + [tuple("-" if value is None else str(value) for value in row) for row in rows]
    widths = [max(len(row[index]) for row in values) for index in range(len(headings))]
    for number, row in enumerate(values):
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
        if number == 0: print("  ".join("-" * width for width in widths))

def main() -> None:
    parser = argparse.ArgumentParser(description="Show sanitized retailer health state")
    parser.add_argument("--failed", action="store_true", help="show only degraded, failed, or blocked retailers")
    args = parser.parse_args()
    asyncio.run(_run(args.failed))

if __name__ == "__main__":
    main()
