"""Logging configuration for Strider."""

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root and application loggers with formatted stream output."""
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root logger handler
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    root_logger.setLevel(level)

    # Set specific logger levels
    logging.getLogger("src").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
