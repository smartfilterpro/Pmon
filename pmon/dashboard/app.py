"""FastAPI dashboard for managing the Pokemon card bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

if TYPE_CHECKING:
    from pmon.engine import PmonEngine

DASHBOARD_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))


def create_app(engine: "PmonEngine") -> FastAPI:
    app = FastAPI(title="Pmon Dashboard")

    static_dir = DASHBOARD_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {
            "request": request,
            "config": engine.config,
            "state": engine.state,
        })

    @app.get("/api/status")
    async def status():
        products = []
        for url, result in engine.state.products.items():
            products.append({
                "url": result.url,
                "name": result.product_name,
                "retailer": result.retailer,
                "status": result.status.value,
                "price": result.price,
                "timestamp": result.timestamp.isoformat(),
                "error": result.error_message,
            })

        checkouts = []
        for c in engine.state.checkout_attempts[-20:]:
            checkouts.append({
                "url": c.url,
                "name": c.product_name,
                "retailer": c.retailer,
                "status": c.status.value,
                "order_number": c.order_number,
                "error": c.error_message,
                "timestamp": c.timestamp.isoformat(),
            })

        return {
            "is_running": engine.state.is_running,
            "started_at": engine.state.started_at.isoformat() if engine.state.started_at else None,
            "products": products,
            "checkouts": checkouts,
        }

    @app.post("/api/products")
    async def add_product(request: Request):
        data = await request.json()
        from pmon.config import Product, save_config
        product = Product(
            url=data["url"],
            name=data.get("name", ""),
            auto_checkout=data.get("auto_checkout", False),
        )
        engine.config.products.append(product)
        save_config(engine.config)
        return {"ok": True, "product": {"url": product.url, "name": product.name, "retailer": product.retailer}}

    @app.delete("/api/products")
    async def remove_product(request: Request):
        data = await request.json()
        url = data["url"]
        engine.config.products = [p for p in engine.config.products if p.url != url]
        from pmon.config import save_config
        save_config(engine.config)
        engine.state.products.pop(url, None)
        return {"ok": True}

    @app.post("/api/products/{action}")
    async def product_action(action: str, request: Request):
        data = await request.json()
        url = data["url"]

        if action == "toggle_auto":
            for p in engine.config.products:
                if p.url == url:
                    p.auto_checkout = not p.auto_checkout
                    from pmon.config import save_config
                    save_config(engine.config)
                    return {"ok": True, "auto_checkout": p.auto_checkout}

        if action == "checkout_now":
            product = next((p for p in engine.config.products if p.url == url), None)
            if product and engine.checkout_engine:
                asyncio.create_task(engine.manual_checkout(product))
                return {"ok": True, "message": "Checkout attempt started"}

        return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)

    @app.post("/api/monitor/{action}")
    async def monitor_action(action: str):
        if action == "start":
            asyncio.create_task(engine.start_monitoring())
            return {"ok": True}
        elif action == "stop":
            engine.stop_monitoring()
            return {"ok": True}
        return JSONResponse({"ok": False}, status_code=400)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        data = await request.json()
        if "poll_interval" in data:
            engine.config.poll_interval = int(data["poll_interval"])
        if "discord_webhook" in data:
            engine.config.discord_webhook = data["discord_webhook"]
        from pmon.config import save_config
        save_config(engine.config)
        return {"ok": True}

    return app
