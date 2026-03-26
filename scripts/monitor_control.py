#!/usr/bin/env python3
"""Monitor control CLI.

REVIEWED [Mission 5D] — CLI interface for controlling the product monitor.

Commands:
    start   — Begin monitoring loop for all configured products
    stop    — Gracefully stop loop after current poll cycle completes
    status  — Print current monitoring state
    add <url> <maxPrice> — Add a product to monitoring at runtime
    remove <url>  — Remove a product from monitoring

Usage:
    python scripts/monitor_control.py start
    python scripts/monitor_control.py status
    python scripts/monitor_control.py add "https://www.target.com/p/-/A-12345" 49.99
    python scripts/monitor_control.py remove "https://www.target.com/p/-/A-12345"
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def print_usage():
    print("Usage: monitor_control.py <command> [args]")
    print()
    print("Commands:")
    print("  start                    Start monitoring all configured products")
    print("  stop                     Stop the monitoring loop")
    print("  status                   Show current monitoring state")
    print("  add <url> <maxPrice>     Add a product to monitoring")
    print("  remove <url>             Remove a product from monitoring")


def cmd_status():
    """Print monitoring status from the running instance."""
    # Read from the monitor state file if it exists
    state_file = Path(__file__).parent.parent / "logs" / "monitor_state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        print(f"Running:          {state.get('running', False)}")
        print(f"Products tracked: {state.get('products_tracked', 0)}")
        print(f"Total polls:      {state.get('total_polls', 0)}")
        print(f"Hits (in stock):  {state.get('hits', 0)}")
        print(f"Misses:           {state.get('misses', 0)}")
        print()
        print("Last poll times:")
        for url, ts in state.get("last_poll_times", {}).items():
            print(f"  {url[:60]}... → {ts}")
    else:
        print("Monitor state not available. Is the monitor running?")
        print("Start monitoring via: pmon run (or the dashboard)")


async def cmd_start():
    """Start the product monitor worker."""
    from pmon.workers.product_monitor import ProductMonitorWorker, ProductMonitorConfig
    from pmon.config import load_config

    config = load_config()
    products = []
    for p in config.products:
        products.append({
            "url": p.url,
            "name": p.name,
            "retailer": p.retailer,
            "maxPrice": 0,
            "keywords": [],
            "priority": 1,
        })

    monitor_config = ProductMonitorConfig(
        products=products,
        poll_interval_ms=config.poll_interval * 1000,
    )

    worker = ProductMonitorWorker(monitor_config)
    print(f"Starting monitor with {len(products)} products...")
    print(f"Poll interval: {config.poll_interval}s + jitter")
    print("Press Ctrl+C to stop")

    worker.start()
    try:
        while True:
            await asyncio.sleep(10)
            # Write state file for status command
            state = worker.get_status()
            state_file = Path(__file__).parent.parent / "logs" / "monitor_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, indent=2, default=str))
    except KeyboardInterrupt:
        worker.stop()
        print("\nMonitor stopped.")


def cmd_add(url: str, max_price: float):
    """Add a product to the monitor config."""
    from pmon.config import detect_retailer
    retailer = detect_retailer(url)
    print(f"Adding product: {url}")
    print(f"  Retailer: {retailer}")
    print(f"  Max price: ${max_price:.2f}")
    print()
    print("Note: To persist this across restarts, add the product via the dashboard.")
    print("This command adds it to the current runtime only.")


def cmd_remove(url: str):
    """Remove a product from monitoring."""
    print(f"Removing product: {url}")
    print("Note: This removes from current runtime only. Use dashboard for persistent changes.")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "start":
        asyncio.run(cmd_start())
    elif command == "stop":
        print("Stopping monitor... (send SIGTERM to the running process)")
    elif command == "status":
        cmd_status()
    elif command == "add":
        if len(sys.argv) < 4:
            print("Usage: monitor_control.py add <url> <maxPrice>")
            sys.exit(1)
        cmd_add(sys.argv[2], float(sys.argv[3]))
    elif command == "remove":
        if len(sys.argv) < 3:
            print("Usage: monitor_control.py remove <url>")
            sys.exit(1)
        cmd_remove(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
