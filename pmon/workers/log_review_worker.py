"""Post-session log review worker.

REVIEWED [Mission 4C] — Runs after each bot session completes. Reads the
session log, sends a structured summary to Claude API for analysis, and
merges returned patterns into NavigationMemory.

Usage:
    from pmon.workers.log_review_worker import LogReviewWorker
    worker = LogReviewWorker()
    await worker.review_session(session_id)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent.parent / "logs"
SESSION_LOGS_DIR = LOGS_DIR / "sessions"

# System prompt for the log review AI
SYSTEM_PROMPT = """You are the internal intelligence layer of Pmon, a retail checkout automation bot.
Analyze this session log. Identify:
1. Selector failures and what ultimately resolved them
2. Popup patterns encountered and how they were handled
3. Checkout flow deviations from the happy path
4. Any repeated retry patterns that suggest a fragile step

Return a JSON array of memory objects with keys:
- context: string (e.g. "checkout_popup", "login_flow", "delivery_selection")
- trigger: string (description of the visual/behavioral pattern)
- action: string (what action resolved or should resolve it)
- confidence: float (0.0-1.0, your confidence in this recommendation)
- recommendation: string (human-readable suggestion for improvement)

Return ONLY the JSON array, no other text."""


class LogReviewWorker:
    """Analyzes session logs using Claude API and updates NavigationMemory."""

    def __init__(self):
        self._anthropic = None
        self._init_client()
        self._last_run: datetime | None = None

    def _init_client(self):
        """Initialize the Anthropic client if API key is available."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.info("LogReviewWorker: ANTHROPIC_API_KEY not set — log review disabled")
            return
        try:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=api_key)
            logger.info("LogReviewWorker: Claude API client initialized")
        except ImportError:
            logger.info("LogReviewWorker: anthropic package not installed")

    async def review_session(self, session_id: str) -> list[dict]:
        """Analyze a session log and update NavigationMemory.

        Parameters
        ----------
        session_id : identifier for the session log file

        Returns
        -------
        List of pattern dicts merged into memory, or empty list on failure.
        """
        if not self._anthropic:
            return []

        # Read session log
        log_path = SESSION_LOGS_DIR / f"{session_id}.jsonl"
        if not log_path.exists():
            logger.warning("LogReviewWorker: session log not found: %s", log_path)
            return []

        try:
            log_lines = log_path.read_text().strip().split("\n")
            # Limit to last 200 entries to stay within token limits
            if len(log_lines) > 200:
                log_lines = log_lines[-200:]
            log_content = "\n".join(log_lines)
        except Exception as exc:
            logger.error("LogReviewWorker: failed to read session log: %s", exc)
            return []

        # Send to Claude for analysis
        try:
            resp = self._anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Session log for analysis:\n\n{log_content}",
                }],
            )
            raw = resp.content[0].text.strip()

            # Extract JSON from response
            if raw.startswith("```"):
                lines = raw.split("\n")
                inner = []
                for line in lines[1:]:
                    if line.strip() == "```":
                        break
                    inner.append(line)
                raw = "\n".join(inner).strip()

            patterns = json.loads(raw)
            if not isinstance(patterns, list):
                patterns = [patterns]

        except Exception as exc:
            logger.error("LogReviewWorker: Claude analysis failed: %s", exc)
            return []

        # Merge patterns into NavigationMemory
        from pmon.memory.navigation_memory import NavigationMemory
        memory = NavigationMemory()
        merged = []

        for pattern in patterns:
            if not isinstance(pattern, dict):
                continue
            if not pattern.get("context") or not pattern.get("trigger"):
                continue

            memory.upsert_pattern(pattern)
            merged.append(pattern)

            # Log high-confidence recommendations
            confidence = pattern.get("confidence", 0)
            if confidence > 0.9:
                logger.info(
                    "LogReviewWorker: high-confidence insight — %s: %s (%.2f)",
                    pattern["context"],
                    pattern.get("recommendation", pattern.get("action", "")),
                    confidence,
                )

        self._last_run = datetime.now(timezone.utc)
        logger.info(
            "LogReviewWorker: analyzed session %s, merged %d patterns",
            session_id, len(merged),
        )
        return merged

    @property
    def last_run_timestamp(self) -> str | None:
        """ISO timestamp of last review run, or None."""
        return self._last_run.isoformat() if self._last_run else None


def write_session_log(session_id: str, entry: dict):
    """Append an entry to a session log file.

    Called from the checkout engine to log each step for later review.
    """
    try:
        SESSION_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = SESSION_LOGS_DIR / f"{session_id}.jsonl"
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass
