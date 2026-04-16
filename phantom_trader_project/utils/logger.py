"""Structured logging."""
from __future__ import annotations
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIR, LOG_LEVEL, LOG_RETENTION_DAYS


def setup_logger(name: str = "phantom") -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)-18s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # File (daily rotation)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fh = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, f"phantom_{today}.log"),
        when="midnight",
        backupCount=LOG_RETENTION_DAYS,
        utc=True,
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logger()
