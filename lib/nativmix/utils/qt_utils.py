from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)


def _slot_guard(func):
    """Catch exceptions in Qt slots, log them, and continue running."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("Unhandled exception in slot %s", func.__qualname__)
    return wrapper
