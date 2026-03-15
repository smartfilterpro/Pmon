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


def main():
    parser = argparse.ArgumentParser(
        prog="pmon",
        description="Pmon - Pokemon card stock monitor and auto-checkout bot",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--config", type=Path, help="Path to config file")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable the web dashboard")
    parser.add_argument("--no-checkout", action="store_true", help="Disable auto-checkout (monitor only)")
    parser.add_argument("--host", default=None, help="Dashboard host")
    parser.add_argument("--port", type=int, default=None, help="Dashboard port")

    sub = parser.add_subparsers(dest="command")

    # Run command (default)
    sub.add_parser("run", help="Start monitoring and dashboard")

    # Init command
    sub.add_parser("init", help="Create a config file from the example")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "init":
        return cmd_init(args)

    # Default to "run"
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

    asyncio.run(_run(config, args))


async def _run(config, args):
    engine = PmonEngine(config)

    # Initialize checkout if enabled
    if not args.no_checkout:
        has_any_auto = any(p.auto_checkout for p in config.products)
        has_any_creds = bool(config.accounts)
        if has_any_auto and has_any_creds:
            try:
                await engine.init_checkout()
                console.print("[green]Checkout engine ready[/green]")
            except Exception as e:
                console.print(f"[yellow]Checkout engine unavailable: {e}[/yellow]")
                console.print("Install playwright browsers: playwright install chromium")

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

    try:
        await engine.start_monitoring()
    except KeyboardInterrupt:
        pass
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")
        engine.stop_monitoring()
        await engine.cleanup()
        if not args.no_dashboard:
            server.should_exit = True
            await dash_task


if __name__ == "__main__":
    main()
