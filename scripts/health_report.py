#!/usr/bin/env python3
"""Health report CLI script.

REVIEWED [Mission 4D] — Outputs system health metrics:
- Navigation memory stats (total patterns, avg confidence, recently used)
- Session success rate (last 7 days)
- Top 3 most frequent failure points
- Last log review timestamp
- Notification accuracy stats

Usage:
    python scripts/health_report.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def get_memory_stats() -> dict:
    """Get navigation memory statistics."""
    try:
        from pmon.memory.navigation_memory import NavigationMemory
        memory = NavigationMemory()
        return memory.get_stats()
    except Exception as exc:
        return {"error": str(exc)}


def get_session_success_rate(days: int = 7) -> dict:
    """Calculate checkout success rate from the database."""
    try:
        from pmon import database as db
        conn = db.get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM checkout_log WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()["cnt"]

        successes = conn.execute(
            "SELECT COUNT(*) as cnt FROM checkout_log WHERE status = 'success' AND created_at >= ?",
            (cutoff,),
        ).fetchone()["cnt"]

        failures = conn.execute(
            "SELECT COUNT(*) as cnt FROM checkout_log WHERE status = 'failed' AND created_at >= ?",
            (cutoff,),
        ).fetchone()["cnt"]

        rate = (successes / total * 100) if total > 0 else 0.0

        return {
            "period_days": days,
            "total_attempts": total,
            "successes": successes,
            "failures": failures,
            "success_rate_pct": round(rate, 1),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_top_failures(limit: int = 3) -> list[dict]:
    """Get the most frequent failure points."""
    try:
        from pmon import database as db
        conn = db.get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        rows = conn.execute(
            """SELECT error_message, COUNT(*) as cnt, retailer
               FROM checkout_log
               WHERE status = 'failed' AND error_message != '' AND created_at >= ?
               GROUP BY error_message
               ORDER BY cnt DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()

        return [{"error": r["error_message"][:120], "count": r["cnt"], "retailer": r["retailer"]} for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]


def get_notification_stats() -> dict:
    """Get notification accuracy statistics."""
    try:
        from pmon.notifications.notify import get_notification_stats
        return get_notification_stats(hours=24)
    except Exception as exc:
        return {"error": str(exc)}


def get_log_review_status() -> str:
    """Get last log review timestamp."""
    try:
        from pmon.workers.log_review_worker import LogReviewWorker
        worker = LogReviewWorker()
        return worker.last_run_timestamp or "Never"
    except Exception:
        return "Unknown"


def main():
    print("=" * 60)
    print("  PMON HEALTH REPORT")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Navigation Memory
    print("\n--- Navigation Memory ---")
    mem_stats = get_memory_stats()
    if "error" not in mem_stats:
        print(f"  Total patterns:        {mem_stats['total_patterns']}")
        print(f"  Average confidence:    {mem_stats['avg_confidence']:.2f}")
        print(f"  High-confidence (≥85%): {mem_stats['high_confidence_count']}")
        print(f"  Used in last 24h:      {mem_stats['recently_used_count']}")
    else:
        print(f"  Error: {mem_stats['error']}")

    # Session Success Rate
    print("\n--- Session Success Rate (7 days) ---")
    rate_stats = get_session_success_rate()
    if "error" not in rate_stats:
        print(f"  Total attempts:  {rate_stats['total_attempts']}")
        print(f"  Successes:       {rate_stats['successes']}")
        print(f"  Failures:        {rate_stats['failures']}")
        print(f"  Success rate:    {rate_stats['success_rate_pct']}%")
    else:
        print(f"  Error: {rate_stats['error']}")

    # Top Failures
    print("\n--- Top 3 Failure Points ---")
    failures = get_top_failures()
    if failures and "error" not in failures[0]:
        for i, f in enumerate(failures, 1):
            print(f"  {i}. [{f['retailer']}] ({f['count']}x) {f['error']}")
    else:
        print("  No failure data available")

    # Notification Accuracy
    print("\n--- Notification Accuracy (24h) ---")
    notif_stats = get_notification_stats()
    if "error" not in notif_stats:
        print(f"  Total sent:     {notif_stats['total']}")
        print(f"  Accurate:       {notif_stats['accurate']}")
        print(f"  Inaccurate:     {notif_stats['inaccurate']}")
        print(f"  Unmarked:       {notif_stats['unmarked']}")
    else:
        print(f"  Error: {notif_stats['error']}")

    # Log Review
    print("\n--- Log Review Worker ---")
    print(f"  Last run: {get_log_review_status()}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
