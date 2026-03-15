"""FastAPI dashboard with auth and per-user data."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

import qrcode
import qrcode.constants
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from pmon import database as db
from pmon.auth import (
    register_user, login_user, setup_totp, confirm_totp,
    disable_totp, decode_token, create_initial_admin,
)
from pmon.config import detect_retailer

if TYPE_CHECKING:
    from pmon.engine import PmonEngine

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent
DIST_DIR = DASHBOARD_DIR / "static" / "dist"


def get_current_user(request: Request) -> dict:
    """Extract and validate JWT from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get_user_by_id(payload["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def create_app(engine: "PmonEngine") -> FastAPI:
    app = FastAPI(title="Pmon Dashboard")

    # Initialize DB and create admin from env vars
    db.get_db()
    create_initial_admin()

    # --- Auth endpoints (no auth required) ---

    @app.post("/api/auth/register")
    async def api_register(request: Request):
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        if not username or not password:
            return JSONResponse({"error": "Username and password required"}, 400)
        try:
            result = register_user(username, password)
            return {"ok": True, **result}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)

    @app.post("/api/auth/login")
    async def api_login(request: Request):
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        totp_code = data.get("totp_code")
        try:
            result = login_user(username, password, totp_code)
            return {"ok": True, **result}
        except ValueError as e:
            error_msg = str(e)
            status = 401
            if "2FA code required" in error_msg:
                return JSONResponse({"error": error_msg, "needs_totp": True}, 401)
            if "pending admin approval" in error_msg:
                return JSONResponse({"error": error_msg, "pending": True}, 403)
            return JSONResponse({"error": error_msg}, status)

    @app.get("/api/auth/check")
    async def api_auth_check(user: dict = Depends(get_current_user)):
        return {
            "ok": True,
            "user_id": user["id"],
            "username": user["username"],
            "is_admin": bool(user.get("is_admin", 0)),
            "totp_enabled": bool(user["totp_enabled"]),
        }

    @app.get("/api/auth/has-users")
    async def api_has_users():
        """Check if any users exist (for showing register vs login)."""
        return {"has_users": db.get_user_count() > 0}

    # --- 2FA endpoints ---

    @app.post("/api/auth/totp/setup")
    async def api_totp_setup(user: dict = Depends(get_current_user)):
        result = setup_totp(user["id"])
        return {"ok": True, "secret": result["secret"], "uri": result["uri"]}

    @app.get("/api/auth/totp/qr")
    async def api_totp_qr(user: dict = Depends(get_current_user)):
        result = setup_totp(user["id"])
        img = qrcode.make(result["uri"], error_correction=qrcode.constants.ERROR_CORRECT_L)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")

    @app.post("/api/auth/totp/confirm")
    async def api_totp_confirm(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        code = data.get("code", "")
        if confirm_totp(user["id"], code):
            return {"ok": True}
        return JSONResponse({"error": "Invalid code"}, 400)

    @app.post("/api/auth/totp/disable")
    async def api_totp_disable(user: dict = Depends(get_current_user)):
        disable_totp(user["id"])
        return {"ok": True}

    # --- Product endpoints (per-user) ---

    @app.get("/api/status")
    async def api_status(user: dict = Depends(get_current_user)):
        user_id = user["id"]
        products = db.get_user_products(user_id)

        product_list = []
        for p in products:
            # Get live stock status if available
            stock = engine.state.products.get(p["url"])
            product_list.append({
                "url": p["url"],
                "name": p["name"],
                "retailer": p["retailer"],
                "quantity": p["quantity"],
                "auto_checkout": bool(p["auto_checkout"]),
                "status": stock.status.value if stock else "unknown",
                "price": stock.price if stock else "",
                "timestamp": stock.timestamp.isoformat() if stock else "",
                "error": stock.error_message if stock else "",
            })

        checkouts = db.get_checkout_log(user_id, limit=30)

        return {
            "is_running": engine.state.is_running,
            "started_at": engine.state.started_at.isoformat() if engine.state.started_at else None,
            "products": product_list,
            "checkouts": checkouts,
        }

    @app.post("/api/products")
    async def api_add_product(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        url = data.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "URL required"}, 400)

        name = data.get("name", "")
        retailer = detect_retailer(url)
        quantity = max(1, int(data.get("quantity", 1)))
        auto = bool(data.get("auto_checkout", False))

        db.add_product(user["id"], url, name, retailer, quantity, auto)
        engine.sync_products_from_db()
        return {"ok": True}

    @app.delete("/api/products")
    async def api_remove_product(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        db.remove_product(user["id"], data["url"])
        engine.state.products.pop(data["url"], None)
        engine.sync_products_from_db()
        return {"ok": True}

    @app.post("/api/products/toggle_auto")
    async def api_toggle_auto(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        new_val = db.toggle_product_auto(user["id"], data["url"])
        engine.sync_products_from_db()
        return {"ok": True, "auto_checkout": new_val}

    @app.post("/api/products/set_quantity")
    async def api_set_quantity(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        qty = max(1, int(data.get("quantity", 1)))
        db.update_product_quantity(user["id"], data["url"], qty)
        engine.sync_products_from_db()
        return {"ok": True, "quantity": qty}

    @app.post("/api/products/checkout_now")
    async def api_checkout_now(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        url = data["url"]
        products = db.get_user_products(user["id"])
        product = next((p for p in products if p["url"] == url), None)
        if not product:
            return JSONResponse({"error": "Product not found"}, 404)

        from pmon.config import Product
        p = Product(url=url, name=product["name"], auto_checkout=True)
        asyncio.create_task(engine.manual_checkout(p, user_id=user["id"]))
        return {"ok": True, "message": "Checkout attempt started"}

    # --- Retailer accounts ---

    @app.get("/api/accounts")
    async def api_get_accounts(user: dict = Depends(get_current_user)):
        accounts = db.get_retailer_accounts(user["id"])
        # Don't send passwords back
        safe = {}
        for retailer, acc in accounts.items():
            safe[retailer] = {"email": acc["email"], "has_password": bool(acc["password"])}
        return {"accounts": safe}

    @app.post("/api/accounts")
    async def api_set_account(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        retailer = data.get("retailer", "").strip()
        email = data.get("email", "").strip()
        password = data.get("password", "")
        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter"):
            return JSONResponse({"error": "Invalid retailer"}, 400)
        db.set_retailer_account(user["id"], retailer, email, password)
        return {"ok": True}

    @app.post("/api/accounts/test")
    async def api_test_account(request: Request, user: dict = Depends(get_current_user)):
        """Test retailer login credentials using Playwright browser automation."""
        data = await request.json()
        retailer = data.get("retailer", "").strip()
        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter"):
            return JSONResponse({"error": "Invalid retailer"}, 400)

        accounts = db.get_retailer_accounts(user["id"])
        acc = accounts.get(retailer)
        if not acc or not acc.get("email") or not acc.get("password"):
            return JSONResponse({"error": "No credentials saved for this retailer"}, 400)

        email = acc["email"]
        password = acc["password"]

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"ok": False, "message": "Playwright package not installed — run: pip install playwright && playwright install chromium"}

        LOGIN_URLS = {
            "target": "https://www.target.com/login?client_id=ecom-web-1.0.0&ui_namespace=ui-default&back_button_action=browser&keep_me_signed_in=true&kmsi_default=true&actions=create_session_request_username",
            "walmart": "https://www.walmart.com/account/login",
            "bestbuy": "https://www.bestbuy.com/identity/global/signin",
            "pokemoncenter": "https://www.pokemoncenter.com/account/login",
        }

        # Selectors for each retailer's login form
        SELECTORS = {
            "target": {
                "email": '#username, input[name="username"], input[type="email"], input[type="tel"], input[id*="username" i], input[name*="email" i], input[autocomplete="username"], input[autocomplete="email tel"]',
                "password": '#password, input[name="password"], input[type="password"], input[id*="password" i]',
                "submit": 'button[type="submit"], button:has-text("Sign in"), button:has-text("Continue")',
                "success": '#account, [data-test="accountNav"], a[href*="/account"], [data-test="@web/AccountLink"]',
                "error": '[data-test="error"], .error-message, #error, [class*="error" i], [role="alert"]',
            },
            "walmart": {
                "email": 'input[name="email"], input[type="email"], input[id*="email" i]',
                "password": 'input[type="password"], input[name="password"]',
                "submit": 'button[type="submit"], button:has-text("Sign in"), button:has-text("Continue")',
                "success": 'a[href*="/account"], [data-automation-id="account"], [data-tl-id*="account"]',
                "error": '[data-automation-id="error"], .error-message, [class*="error" i], [role="alert"]',
            },
            "bestbuy": {
                "email": '#fld-e, input[id="user.emailAddress"], input[type="email"], input[name="email"]',
                "password": '#fld-p1, input[type="password"], input[name="password"]',
                "submit": 'button[type="submit"], button:has-text("Sign In")',
                "success": 'a[href*="/account"], .account-menu, .v-p-right-xxs',
                "error": '.c-alert, .error-message, [class*="error" i], [role="alert"]',
            },
            "pokemoncenter": {
                "email": 'input[type="email"], input[name="email"], input[type="text"][autocomplete="email"], input[type="text"][autocomplete="username"], input[id*="email" i], input[id*="login" i], input[name*="email" i], input[name*="login" i]',
                "password": 'input[type="password"], input[name="password"], input[id*="password" i]',
                "submit": 'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Continue")',
                "success": 'a[href*="/account"], .account-nav, [href*="/account/dashboard"], [data-testid*="account"]',
                "error": '.error-message, [class*="error" i], .alert, [data-testid*="error"], [role="alert"]',
            },
        }

        sel = SELECTORS[retailer]
        retailer_name = {"target": "Target", "walmart": "Walmart", "bestbuy": "Best Buy", "pokemoncenter": "Pokemon Center"}[retailer]

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()

            try:
                # Navigate to login page
                await page.goto(LOGIN_URLS[retailer], wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                # Pokemon Center may redirect to access.pokemon.com SSO
                current_url = page.url
                if "access.pokemon.com" in current_url or "sso.pokemon.com" in current_url:
                    # SSO login page — find the email/username and password fields there
                    email_sel = 'input[name="email"], input[name="username"], input[type="email"], input[type="text"]'
                    pass_sel = 'input[type="password"], input[name="password"]'
                    submit_sel = 'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Continue")'
                else:
                    email_sel = sel["email"]
                    pass_sel = sel["password"]
                    submit_sel = sel["submit"]

                # Wait for the email field to appear (up to 15s for JS-heavy pages)
                try:
                    await page.locator(email_sel).first.wait_for(state="visible", timeout=15000)
                except Exception:
                    return {"ok": False, "message": f"{retailer_name} login page did not load — no email/username field found at {current_url}"}

                # Fill credentials — some retailers use multi-step login (email first, then password)
                await page.fill(email_sel, email)

                # Check if password field is visible yet (single-step) or needs submit first (multi-step)
                pass_visible = False
                try:
                    pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1000)
                except Exception:
                    pass

                if pass_visible:
                    # Single-step: fill both and submit
                    await page.fill(pass_sel, password)
                    await page.click(submit_sel)
                else:
                    # Multi-step: submit email/phone first
                    await page.click(submit_sel)
                    await page.wait_for_timeout(2000)

                    # Some sites (e.g. Target) show an auth method picker
                    # (password, OTP, etc.) — click "password" option if present
                    password_option = page.locator('button:has-text("Password"), a:has-text("Password"), [data-test*="password" i], button:has-text("Use password")')
                    try:
                        if await password_option.first.is_visible(timeout=3000):
                            await password_option.first.click()
                            await page.wait_for_timeout(1000)
                    except Exception:
                        pass

                    # Wait for password field
                    try:
                        await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
                    except Exception:
                        return {"ok": False, "message": f"{retailer_name} login: submitted email but password field did not appear"}
                    await page.fill(pass_sel, password)
                    await page.click(submit_sel)

                await page.wait_for_timeout(5000)

                # Check for success indicators
                final_url = page.url
                success_el = page.locator(sel["success"])
                error_el = page.locator(sel["error"])

                # Check if we landed on an account/home page (away from login)
                still_on_login = "/login" in final_url or "/signin" in final_url
                has_success = await success_el.first.is_visible(timeout=2000) if not still_on_login else False
                has_error = False
                error_text = ""
                try:
                    if await error_el.first.is_visible(timeout=1000):
                        has_error = True
                        error_text = await error_el.first.inner_text(timeout=1000)
                except Exception:
                    pass

                if has_error and error_text:
                    msg = f"{retailer_name} login failed: {error_text.strip()[:200]}"
                    logger.warning("Test login failed for %s user=%s: %s", retailer_name, email, error_text.strip())
                    db.add_error_log(user["id"], "WARNING", "test-login", msg, "")
                    return {"ok": False, "message": msg}

                if has_success or (not still_on_login and not has_error):
                    logger.info("Test login successful for %s user=%s", retailer_name, email)
                    return {"ok": True, "message": f"{retailer_name} login successful"}

                if still_on_login:
                    msg = f"{retailer_name} login failed — still on login page (wrong email/password?)"
                    logger.warning("Test login failed for %s user=%s: still on login page", retailer_name, email)
                    db.add_error_log(user["id"], "WARNING", "test-login", msg, "")
                    return {"ok": False, "message": msg}

                # Ambiguous — couldn't determine success or failure
                return {"ok": False, "message": f"{retailer_name} login result unclear — check credentials and try in a browser"}

            finally:
                await page.close()
                await context.close()
                await browser.close()
                await pw.stop()

        except Exception as e:
            err_str = str(e).lower()
            if "executable" in err_str and "exist" in err_str:
                msg = "Chromium browser not installed on server — run: playwright install chromium"
                logger.error("Playwright Chromium binary missing")
                db.add_error_log(user["id"], "ERROR", "test-login", msg, "")
                return {"ok": False, "message": msg}
            msg = f"Error testing {retailer_name}: {str(e)}"
            logger.error("Test login error for %s: %s", retailer_name, e, exc_info=True)
            db.add_error_log(user["id"], "ERROR", "test-login", msg, "")
            return {"ok": False, "message": msg}

    # --- Settings ---

    @app.get("/api/settings")
    async def api_get_settings(user: dict = Depends(get_current_user)):
        settings = db.get_user_settings(user["id"])
        return {"settings": settings}

    @app.post("/api/settings")
    async def api_update_settings(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        db.update_user_settings(
            user["id"],
            poll_interval=data.get("poll_interval"),
            discord_webhook=data.get("discord_webhook"),
        )
        return {"ok": True}

    # --- Error log ---

    @app.get("/api/errors")
    async def api_errors(user: dict = Depends(get_current_user)):
        errors = db.get_error_log(user["id"], limit=100)
        return {"errors": errors}

    # --- Admin endpoints ---

    def require_admin(user: dict = Depends(get_current_user)) -> dict:
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Admin access required")
        return user

    @app.get("/api/admin/users")
    async def api_admin_users(user: dict = Depends(require_admin)):
        users = db.get_all_users()
        return {"users": users}

    @app.get("/api/admin/pending")
    async def api_admin_pending(user: dict = Depends(require_admin)):
        pending = db.get_pending_users()
        return {"pending": pending}

    @app.post("/api/admin/approve")
    async def api_admin_approve(request: Request, user: dict = Depends(require_admin)):
        data = await request.json()
        db.approve_user(data["user_id"])
        return {"ok": True}

    @app.post("/api/admin/reject")
    async def api_admin_reject(request: Request, user: dict = Depends(require_admin)):
        data = await request.json()
        db.reject_user(data["user_id"])
        return {"ok": True}

    @app.post("/api/admin/set_admin")
    async def api_admin_set_admin(request: Request, user: dict = Depends(require_admin)):
        data = await request.json()
        target_id = data["user_id"]
        if target_id == user["id"]:
            return JSONResponse({"error": "Cannot change your own admin status"}, 400)
        db.set_user_admin(target_id, data.get("is_admin", False))
        return {"ok": True}

    # --- Monitor control ---

    @app.post("/api/monitor/{action}")
    async def api_monitor_action(action: str, user: dict = Depends(get_current_user)):
        if action == "start":
            asyncio.create_task(engine.start_monitoring())
            return {"ok": True}
        elif action == "stop":
            engine.stop_monitoring()
            return {"ok": True}
        return JSONResponse({"ok": False}, 400)

    # --- Serve React app ---
    if DIST_DIR.exists():
        assets_dir = DIST_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index = DIST_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse(
            {"error": "Frontend not built. Run: cd frontend && npm run build"},
            status_code=503,
        )

    return app
