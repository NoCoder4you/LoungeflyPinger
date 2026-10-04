"""Console and size-rotating file logging setup."""

import logging
from logging.handlers import RotatingFileHandler

from app.config import LoggingConfig


class _DiscordVoiceWarningFilter(logging.Filter):
    """Suppress the expected discord.py warning when voice support is unused."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "discord.client"
            and record.getMessage().startswith("PyNaCl is not installed")
        )


def configure_logging(config: LoggingConfig) -> None:
    config.path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    voice_warning_filter = _DiscordVoiceWarningFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(voice_warning_filter)

    file_handler = RotatingFileHandler(
        config.path,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(voice_warning_filter)

    logging.basicConfig(
        level=config.level,
        handlers=[console, file_handler],
        force=True,
    )
