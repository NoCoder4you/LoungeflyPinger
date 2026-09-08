"""Send a synthetic Discord notification without a retailer event."""

import argparse
import asyncio

from app.config import load_config
from app.database import Database
from app.models import Alert, AlertType, Availability, Product
from app.notifications import DiscordNotifier


async def _send(admin: bool) -> bool:
    config = load_config()
    alert = Alert(
        alert_type=AlertType.MONITOR_RECOVERED if admin else AlertType.RESTOCK,
        product=None if admin else Product(
            retailer="Developer Test",
            retailer_product_id="discord-test",
            name="Discord Test Mini Backpack",
            url="https://example.com/loungefly-test",
            availability=Availability.IN_STOCK,
            price="79.99",
            currency="GBP",
        ),
        message="Manual Discord admin notification test" if admin else None,
        new_state="manual-test",
    )
    async with Database(config.database_path) as database:
        notifier = DiscordNotifier(config.notifications, database)
        try:
            return await notifier.send(alert)
        finally:
            await notifier.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin", action="store_true", help="send to the administrator webhook")
    args = parser.parse_args()
    delivered = asyncio.run(_send(args.admin))
    print("Test notification delivered." if delivered else "Test notification was not delivered.")
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
