from pathlib import Path

import pytest

from app.config import ConfigurationError, load_config


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("monitor:\n  concurrency_limit: 2\ndatabase:\n  path: custom.db\nretailers: {}\n", encoding="utf-8")
    config = load_config(path, tmp_path / ".env")
    assert config.monitor.concurrency_limit == 2
    assert config.database_path == Path("custom.db")


def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("monitor:\n  concurrency_limit: 0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="greater than zero"):
        load_config(path, tmp_path / ".env")


@pytest.mark.parametrize("level", ["10", "null"])
def test_non_string_logging_level_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.delenv("LOUNGEFLY_LOG_LEVEL", raising=False)
    path = tmp_path / "settings.yaml"
    path.write_text(f"logging:\n  level: {level}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="logging level must be a string"):
        load_config(path, tmp_path / ".env")
