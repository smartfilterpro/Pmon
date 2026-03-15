"""Custom logging handler that stores errors in the database."""

from __future__ import annotations

import logging
import traceback

from pmon import database as db


class DatabaseLogHandler(logging.Handler):
    """Captures WARNING+ log messages into the error_log table."""

    def __init__(self, level=logging.WARNING):
        super().__init__(level)

    def emit(self, record: logging.LogRecord):
        try:
            # Extract user_id if attached to the record
            user_id = getattr(record, "user_id", None)
            details = ""
            if record.exc_info and record.exc_info[1]:
                details = traceback.format_exception(*record.exc_info)
                details = "".join(details)

            db.add_error_log(
                user_id=user_id,
                level=record.levelname,
                source=record.name,
                message=record.getMessage(),
                details=details,
            )
        except Exception:
            # Don't let logging errors crash the app
            pass
