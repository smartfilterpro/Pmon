"""CLI entry point for Pmon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn
from rich.console import Console
from rich.logging import RichHandler

from pmon.config import load_config, save_config, CONFIG_PATH
from pmon.engine import PmonEngine
from pmon.dashboard.app import create_app

console = Console()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    # Suppress httpx/httpcore INFO-level request logging (floods terminal)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Also capture WARNING+ to database
    from pmon.log_handler import DatabaseLogHandler
    logging.getLogger().addHandler(DatabaseLogHandler())


def main():
    parser = argparse.ArgumentParser(
        prog="pmon",
        description="Pmon - Pokemon card stock monitor and auto-checkout bot",
    )
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "init"], help="Command to execute (default: run)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--config", type=Path, help="Path to config file")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable the web dashboard")
    parser.add_argument("--no-checkout", action="store_true", help="Disable auto-checkout (monitor only)")
    parser.add_argument("--host", default=None, help="Dashboard host")
    parser.add_argument("--port", type=int, default=None, help="Dashboard port")
    parser.add_argument("--visible", action="store_true",
                        help="Run Chrome in visible (non-headless) mode so you can see and interact with the browser")
    parser.add_argument("--chrome-profile", type=str, default=None,
                        help="Path to Chrome user data directory to reuse existing login sessions")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "init":
        return cmd_init(args)

    return cmd_run(args)


def cmd_init(args):
    if CONFIG_PATH.exists():
        console.print(f"[yellow]Config already exists at {CONFIG_PATH}[/yellow]")
        return

    example = CONFIG_PATH.parent / "config.example.yaml"
    if example.exists():
        import shutil
        shutil.copy(example, CONFIG_PATH)
        console.print(f"[green]Created config at {CONFIG_PATH}[/green]")
        console.print("Edit this file to add your products and credentials.")
    else:
        config = load_config()
        save_config(config)
        console.print(f"[green]Created default config at {CONFIG_PATH}[/green]")


def cmd_run(args):
    config = load_config(args.config)

    if not config.products:
        console.print("[yellow]No products configured.[/yellow]")
        console.print(f"Add products to your config file: {args.config or CONFIG_PATH}")
        console.print("Or use the dashboard to add products after starting.")

    # Support Railway's PORT env var and 0.0.0.0 binding
    config.dashboard_host = args.host or os.environ.get("HOST", config.dashboard_host)
    env_port = os.environ.get("PORT")
    if args.port:
        config.dashboard_port = args.port
    elif env_port:
        config.dashboard_port = int(env_port)

    # Visible Chrome mode (--visible flag overrides config)
    if args.visible:
        config.headless = False
    if args.chrome_profile:
        config.chrome_profile_dir = args.chrome_profile

    asyncio.run(_run(config, args))


async def _run(config, args):
    engine = PmonEngine(config)

    # Initialize checkout engine (API-first, browser is optional fallback)
    if not args.no_checkout:
        await engine.init_checkout()
        if config.headless:
            console.print("[green]Checkout engine ready (API-first, headless browser fallback)[/green]")
        else:
            console.print("[green]Checkout engine ready (VISIBLE Chrome mode)[/green]")
            console.print("[blue]Chrome will open visibly — you can log in manually and the session persists.[/blue]")

    # Start dashboard in background thread
    if not args.no_dashboard:
        app = create_app(engine)
        dash_config = uvicorn.Config(
            app,
            host=config.dashboard_host,
            port=config.dashboard_port,
            log_level="warning",
        )
        server = uvicorn.Server(dash_config)

        console.print(f"[blue]Dashboard: http://{config.dashboard_host}:{config.dashboard_port}[/blue]")

        # Run dashboard in a background task
        dash_task = asyncio.create_task(server.serve())

    console.print("[green]Pmon started! Press Ctrl+C to stop.[/green]")

    # Use a shutdown event so that dashboard start/stop doesn't kill the app.
    # Only Ctrl+C (or SIGINT/SIGTERM) triggers a full shutdown.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal as _signal
    import sys as _sys
    if _sys.platform != "win32":
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_event.set)
    else:
        # Windows doesn't support add_signal_handler; use a thread-based fallback
        def _win_signal_handler(signum, frame):
            loop.call_soon_threadsafe(shutdown_event.set)
        _signal.signal(_signal.SIGINT, _win_signal_handler)
        _signal.signal(_signal.SIGTERM, _win_signal_handler)

    # Start monitoring as a background task so the dashboard can
    # freely stop and restart it without tearing down the process.
    engine.start_monitoring_task()

    try:
        await shutdown_event.wait()
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")
        engine.stop_monitoring()
        await engine.cleanup()
        if not args.no_dashboard:
            server.should_exit = True
            await dash_task


if __name__ == "__main__":
    main()
