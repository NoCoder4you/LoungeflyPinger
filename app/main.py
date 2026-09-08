"""Command-line entry point: ``python -m app.main``."""

import asyncio
import logging
import signal

from app.application import Application
from app.config import ConfigurationError, load_config
from app.logging_config import configure_logging


async def async_main() -> None:
    config = load_config()
    configure_logging(config.logging)
    application = Application(config)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, application.request_shutdown)
    await application.run()


def main() -> int:
    try:
        asyncio.run(async_main())
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
