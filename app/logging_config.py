"""Console and size-rotating file logging setup."""

import logging
from logging.handlers import RotatingFileHandler

from app.config import LoggingConfig


def configure_logging(config: LoggingConfig) -> None:
    config.path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        config.path, maxBytes=config.max_bytes, backupCount=config.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=config.level, handlers=[console, file_handler], force=True)
