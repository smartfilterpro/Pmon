# REVIEWED [Mission 2] — Virtual queue detection and handling.
"""Virtual queue detection and handling for retail checkout flows.

Provides queue detection (detect_queue) and a patient wait handler
(QueueHandler) that keeps the browser alive in retailer virtual queues
without navigating away.
"""

from __future__ import annotations

from pmon.queue.detector import detect_queue, QueueDetectionResult
from pmon.queue.handler import QueueHandler, QueueExitResult

__all__ = [
    "detect_queue",
    "QueueDetectionResult",
    "QueueHandler",
    "QueueExitResult",
]
