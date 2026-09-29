"""Logging configuration for Strider."""

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root and application loggers with formatted stream output."""
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root logger handlers
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # File handler
        from pathlib import Path
        from logging.handlers import RotatingFileHandler
        from src.core.config import REPO_ROOT
        
        log_dir = REPO_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_dir / "strider.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    root_logger.setLevel(level)

    # Set specific logger levels
    logging.getLogger("src").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
