from __future__ import annotations

import logging
import sys
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig


class _RunLabelFilter(logging.Filter):
    def __init__(self, run_label: str) -> None:
        super().__init__()
        self.run_label = run_label

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_label = self.run_label
        return True


def setup_logging(config: LoggingConfig) -> str:
    run_label = uuid.uuid4().hex[:8]
    level = getattr(logging, config.level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [run:%(run_label)s] %(name)s: %(message)s"
    )
    run_filter = _RunLabelFilter(run_label)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if config.file:
        log_path = Path(config.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.addFilter(run_filter)
        root.addHandler(handler)

    if config.console:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.addFilter(run_filter)
        root.addHandler(handler)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    return run_label

