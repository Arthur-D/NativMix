"""
Colored console + rotating file logging for NativMix.

Log path from get_log_dir() (XDG: ~/.cache/nativmix/logs/nativmix.log).
Color map: DEBUG=cyan, INFO=green, WARNING=yellow, ERROR=red, CRITICAL=bold red.
No third-party dependencies — pure stdlib (logging + ANSI escape codes).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

# ---------------------------------------------------------------------------
# ANSI color codes
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_COLORS = {
    logging.DEBUG:    "\033[36m",   # cyan
    logging.INFO:     "\033[32m",   # green
    logging.WARNING:  "\033[33m",   # yellow
    logging.ERROR:    "\033[31m",   # red
    logging.CRITICAL: "\033[1;31m", # bold red
}

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class _ColoredFormatter(logging.Formatter):
    """Formatter that prefixes log level with an ANSI color code."""

    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelno, "")
        record = logging.makeLogRecord(record.__dict__)
        record.levelname = f"{color}{record.levelname}{_RESET}"
        return super().format(record)


def setup_logging(debug_enabled: bool) -> None:
    """
    Replace basicConfig handlers with a RotatingFileHandler + colored console handler.
    Called after ConfigManager is loaded so we know the desired log level.
    """
    from nativmix.utils.paths import get_log_dir, log_platform_info

    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "nativmix.log"

    level = logging.DEBUG if debug_enabled else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Console handler — colored output
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColoredFormatter(_FMT))
    root.addHandler(console)

    # File handler — plain text, no ANSI codes
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FMT))
    root.addHandler(file_handler)

    _logger = logging.getLogger(__name__)
    _logger.info(
        "Logging initialized (level=%s, file=%s)", logging.getLevelName(level), log_file
    )
    log_platform_info()


def get_logger(name: str) -> logging.Logger:
    """Thin wrapper around logging.getLogger — kept for clarity."""
    return logging.getLogger(name)
