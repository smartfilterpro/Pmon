"""Navigation memory store for AI-augmented popup handling.

REVIEWED [Mission 4] — Persistent pattern store that allows the bot to learn
from past runs, reducing Claude Vision API calls and manual intervention.

The memory file is loaded at startup and persisted after each update.
Patterns are matched by (context + trigger) and scored by confidence.

Schema:
{
    "patterns": [
        {
            "context": "checkout_popup",
            "trigger": "description of visual pattern",
            "action": "what action resolved it",
            "confidence": 0.92,
            "successCount": 14,
            "failureCount": 1,
            "lastSeen": "2026-03-20T14:22:00Z"
        }
    ]
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent.parent.parent / "memory"
MEMORY_FILE = MEMORY_DIR / "navigationMemory.json"
HIGH_CONFIDENCE_FILE = MEMORY_DIR / "highConfidenceInsights.md"

# Minimum confidence threshold to use a remembered action without calling API
CONFIDENCE_THRESHOLD = 0.85

# Confidence delta for success/failure
SUCCESS_BOOST = 0.02
FAILURE_PENALTY = 0.08

# Initial confidence for patterns learned from Claude Vision API
INITIAL_CONFIDENCE = 0.6


class NavigationMemory:
    """Persistent navigation pattern memory.

    Loaded from disk at startup, updated in-memory, and flushed to disk
    after each modification.
    """

    def __init__(self, memory_path: Path | None = None):
        self._path = memory_path or MEMORY_FILE
        self._patterns: list[dict] = []
        self._load()

    def _load(self):
        """Load patterns from disk."""
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._patterns = data.get("patterns", [])
                logger.info(
                    "NavigationMemory: loaded %d patterns from %s",
                    len(self._patterns), self._path,
                )
            else:
                self._patterns = []
        except Exception as exc:
            logger.warning("NavigationMemory: failed to load from %s: %s", self._path, exc)
            self._patterns = []

    def _save(self):
        """Persist patterns to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"patterns": self._patterns}
            self._path.write_text(json.dumps(data, indent=2, default=str))
        except Exception as exc:
            logger.error("NavigationMemory: failed to save: %s", exc)

    def find_pattern(self, context: str, url_pattern: str = "") -> dict | None:
        """Find a high-confidence pattern matching the given context.

        Parameters
        ----------
        context : the type of UI situation (e.g. "checkout_popup", "health_consent")
        url_pattern : optional URL pattern to narrow the match

        Returns
        -------
        The best matching pattern with confidence >= CONFIDENCE_THRESHOLD,
        or None if no suitable match exists.
        """
        best = None
        best_confidence = 0.0

        for pattern in self._patterns:
            if pattern["context"] != context:
                continue
            if url_pattern and pattern.get("url_pattern") and url_pattern not in pattern.get("url_pattern", ""):
                continue
            if pattern["confidence"] >= CONFIDENCE_THRESHOLD and pattern["confidence"] > best_confidence:
                best = pattern
                best_confidence = pattern["confidence"]

        return best

    def record_success(self, context: str, trigger: str, action: str):
        """Record that an action succeeded for a given context+trigger.

        Increments successCount and boosts confidence.
        """
        pattern = self._find_exact(context, trigger)
        if pattern:
            pattern["successCount"] = pattern.get("successCount", 0) + 1
            pattern["confidence"] = min(1.0, pattern["confidence"] + SUCCESS_BOOST)
            pattern["lastSeen"] = datetime.now(timezone.utc).isoformat()
        else:
            # New pattern — store with initial confidence
            pattern = {
                "context": context,
                "trigger": trigger,
                "action": action,
                "confidence": INITIAL_CONFIDENCE,
                "successCount": 1,
                "failureCount": 0,
                "lastSeen": datetime.now(timezone.utc).isoformat(),
            }
            self._patterns.append(pattern)

        self._save()

        # Track high-confidence insights
        if pattern["confidence"] > 0.9:
            self._append_high_confidence(pattern)

    def record_failure(self, context: str, trigger: str):
        """Record that a remembered action failed. Decrements confidence."""
        pattern = self._find_exact(context, trigger)
        if pattern:
            pattern["failureCount"] = pattern.get("failureCount", 0) + 1
            pattern["confidence"] = max(0.0, pattern["confidence"] - FAILURE_PENALTY)
            pattern["lastSeen"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def upsert_pattern(self, pattern_data: dict):
        """Insert or update a pattern by (context, trigger).

        Used by the log review worker to merge AI-analyzed patterns.
        """
        context = pattern_data.get("context", "")
        trigger = pattern_data.get("trigger", "")
        existing = self._find_exact(context, trigger)

        if existing:
            # Merge: average confidence, sum counts
            existing["confidence"] = (
                existing["confidence"] + pattern_data.get("confidence", 0.5)
            ) / 2
            existing["action"] = pattern_data.get("action", existing["action"])
            existing["lastSeen"] = datetime.now(timezone.utc).isoformat()
        else:
            pattern_data.setdefault("successCount", 0)
            pattern_data.setdefault("failureCount", 0)
            pattern_data.setdefault("lastSeen", datetime.now(timezone.utc).isoformat())
            self._patterns.append(pattern_data)

        self._save()

    def get_stats(self) -> dict:
        """Return memory statistics for the health dashboard."""
        if not self._patterns:
            return {
                "total_patterns": 0,
                "avg_confidence": 0.0,
                "high_confidence_count": 0,
                "recently_used_count": 0,
            }

        from datetime import timedelta
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)

        confidences = [p["confidence"] for p in self._patterns]
        recently_used = 0
        for p in self._patterns:
            try:
                last = datetime.fromisoformat(p["lastSeen"].replace("Z", "+00:00"))
                if last > day_ago:
                    recently_used += 1
            except (ValueError, KeyError):
                pass

        return {
            "total_patterns": len(self._patterns),
            "avg_confidence": sum(confidences) / len(confidences),
            "high_confidence_count": sum(1 for c in confidences if c >= CONFIDENCE_THRESHOLD),
            "recently_used_count": recently_used,
        }

    def get_all_patterns(self) -> list[dict]:
        """Return all patterns (for debugging/display)."""
        return list(self._patterns)

    def _find_exact(self, context: str, trigger: str) -> dict | None:
        """Find a pattern by exact (context, trigger) match."""
        for pattern in self._patterns:
            if pattern["context"] == context and pattern["trigger"] == trigger:
                return pattern
        return None

    def _append_high_confidence(self, pattern: dict):
        """Append a high-confidence pattern to the insights file for human review."""
        try:
            HIGH_CONFIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HIGH_CONFIDENCE_FILE, "a") as f:
                f.write(
                    f"\n## {pattern['context']} — Confidence: {pattern['confidence']:.2f}\n"
                    f"- **Trigger**: {pattern['trigger']}\n"
                    f"- **Action**: {pattern['action']}\n"
                    f"- **Success/Fail**: {pattern.get('successCount', 0)}/{pattern.get('failureCount', 0)}\n"
                    f"- **Last Seen**: {pattern.get('lastSeen', 'N/A')}\n"
                )
        except Exception:
            pass
