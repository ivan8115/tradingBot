"""Loguru-based logging configuration."""

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure loguru with console + rotating file sinks."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove default sink
    logger.remove()

    # Console: human-readable
    logger.add(
        sys.stdout,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # File: structured, rotating daily, kept 30 days
    logger.add(
        log_path / "tradingbot_{time:YYYY-MM-DD}.log",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
        rotation="00:00",       # new file at midnight
        retention="30 days",
        compression="gz",
        enqueue=True,           # thread-safe async logging
    )

    # Separate file for trades only
    logger.add(
        log_path / "trades_{time:YYYY-MM-DD}.log",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        filter=lambda record: record["extra"].get("trade_log", False),
        rotation="00:00",
        retention="90 days",
        compression="gz",
        enqueue=True,
    )


def get_logger(name: str):
    return logger.bind(name=name)
