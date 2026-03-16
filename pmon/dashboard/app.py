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
        import base64
        import json
        import os

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

        # --- Vision fallback helpers ---
        vision_client = None
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                import anthropic
                vision_client = anthropic.Anthropic(api_key=api_key)
            except ImportError:
                logger.warning("Test login: anthropic package not installed, vision fallback disabled")
        else:
            logger.warning("Test login: ANTHROPIC_API_KEY not set, vision fallback disabled")

        async def screenshot_b64(pg):
            raw = await pg.screenshot(type="png")
            return base64.b64encode(raw).decode()

        def ask_vision(img_b64: str, prompt: str) -> str | None:
            if not vision_client:
                return None
            try:
                resp = vision_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=512,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": prompt},
                    ]}],
                )
                return resp.content[0].text
            except Exception as exc:
                logger.warning("Vision API call failed in test-login: %s", exc)
                return None

        async def vision_click(pg, description: str) -> bool:
            img = await screenshot_b64(pg)
            answer = ask_vision(
                img,
                f'I need to click the "{description}" button/link on this page. '
                f'Return ONLY a JSON object with the x,y pixel coordinates: '
                f'{{"x": N, "y": N}}. If not visible, return {{"x": null, "y": null}}.',
            )
            if not answer:
                logger.info("vision_click('%s'): no response from API", description)
                return False
            logger.info("vision_click('%s'): raw response: %s", description, answer.strip()[:200])
            try:
                coords = json.loads(answer.strip())
                if coords.get("x") is not None and coords.get("y") is not None:
                    logger.info("vision_click('%s'): clicking at (%s, %s)", description, coords["x"], coords["y"])
                    await pg.mouse.click(int(coords["x"]), int(coords["y"]))
                    return True
                else:
                    logger.info("vision_click('%s'): element not visible per vision", description)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("vision_click('%s'): failed to parse JSON: %s", description, exc)
            return False

        async def vision_fill(pg, description: str, value: str) -> bool:
            img = await screenshot_b64(pg)
            answer = ask_vision(
                img,
                f'I need to click the "{description}" input field to type into it. '
                f'Return ONLY a JSON object with x,y pixel coordinates: '
                f'{{"x": N, "y": N}}. If not visible, return {{"x": null, "y": null}}.',
            )
            if not answer:
                return False
            try:
                coords = json.loads(answer.strip())
                if coords.get("x") is not None and coords.get("y") is not None:
                    await pg.mouse.click(int(coords["x"]), int(coords["y"]))
                    await pg.keyboard.type(value, delay=50)
                    return True
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return False

        async def vision_read_page(pg) -> str | None:
            """Ask Claude what's on the page — used to diagnose blocks/errors."""
            img = await screenshot_b64(pg)
            return ask_vision(
                img,
                "Describe what you see on this page in 1-2 sentences. "
                "Is it a login form, an error page, a CAPTCHA, a block page, or something else?",
            )

        async def click_visible_button(pg, selector: str, timeout: int = 3000) -> bool:
            """Click the first *visible* element matching the comma-separated selector.
            Returns False if no visible match found — caller should use vision fallback."""
            for sel_part in selector.split(","):
                sel_part = sel_part.strip()
                try:
                    loc = pg.locator(sel_part)
                    if await loc.first.is_visible(timeout=500):
                        await loc.first.click(timeout=timeout)
                        return True
                except Exception:
                    continue
            return False

        LOGIN_URLS = {
            "target": "https://www.target.com/login",
            "walmart": "https://www.walmart.com/account/login",
            "bestbuy": "https://www.bestbuy.com/identity/global/signin",
            "pokemoncenter": "https://www.pokemoncenter.com/account/login",
        }

        # Fallback: navigate to homepage and click sign-in link if direct URL fails
        HOME_URLS = {
            "target": "https://www.target.com",
            "walmart": "https://www.walmart.com",
            "bestbuy": "https://www.bestbuy.com",
            "pokemoncenter": "https://www.pokemoncenter.com",
        }

        SIGNIN_LINK_SELECTORS = {
            "target": 'a[href*="/login"], a[href*="/account"], [data-test="@web/AccountLink"], #account, a:has-text("Sign in")',
            "walmart": 'a[href*="/account/login"], a[href*="/account"], button:has-text("Sign In"), a:has-text("Sign In")',
            "bestbuy": 'a[href*="/signin"], a[href*="/identity"], a:has-text("Sign In"), .account-button',
            "pokemoncenter": 'a[href*="/account/login"], a[href*="/account"], a:has-text("Sign In"), a:has-text("Log In")',
        }

        # Selectors for each retailer's login form
        SELECTORS = {
            "target": {
                "email": '#username, input[name="username"], input[type="email"], input[type="tel"], input[id*="username" i], input[name*="email" i], input[autocomplete="username"], input[autocomplete="email tel"]',
                "password": '#password, input[name="password"], input[type="password"], input[id*="password" i]',
                "submit": 'button:has-text("Continue with email"), button:has-text("Continue"), button:has-text("Sign in"), button[type="submit"]',
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

        # Stealth JS to inject into every page to reduce bot detection
        STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        window.chrome = {runtime: {}};
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(parameters);
        """

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=VizDisplayCompositor",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="America/New_York",
            )
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()

            try:
                # --- Navigate to login page ---
                landed_on_login = False
                nav_failed = False

                try:
                    await page.goto(LOGIN_URLS[retailer], wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    nav_failed = True

                if not nav_failed:
                    await page.wait_for_timeout(3000)

                current_url = page.url

                # Determine if we actually landed on a login page
                login_indicators = ["/login", "/signin", "/sign-in", "/identity", "access.pokemon.com", "sso.pokemon.com"]
                landed_on_login = any(ind in current_url.lower() for ind in login_indicators)

                # Check for bot-block pages
                if "blocked" in current_url or "captcha" in current_url or "challenge" in current_url:
                    landed_on_login = False

                # --- Fallback: if redirected away from login, go to homepage and find sign-in link ---
                if not landed_on_login or nav_failed:
                    logger.info("Test login %s: direct login URL missed (landed on %s), trying homepage approach", retailer_name, current_url)
                    try:
                        await page.goto(HOME_URLS[retailer], wait_until="domcontentloaded", timeout=45000)
                    except Exception as home_err:
                        page_desc = await vision_read_page(page)
                        msg = f"{retailer_name} page failed to load"
                        if page_desc:
                            msg += f": {page_desc}"
                        return {"ok": False, "message": msg}

                    await page.wait_for_timeout(3000)

                    # Try to click the sign-in link from the homepage
                    signin_clicked = False
                    try:
                        signin_link = page.locator(SIGNIN_LINK_SELECTORS[retailer])
                        if await signin_link.first.is_visible(timeout=5000):
                            await signin_link.first.click()
                            signin_clicked = True
                            await page.wait_for_timeout(4000)
                    except Exception:
                        pass

                    if not signin_clicked:
                        # Vision fallback: find and click sign-in link on homepage
                        if await vision_click(page, "Sign in or Account link"):
                            signin_clicked = True
                            await page.wait_for_timeout(4000)

                    if not signin_clicked:
                        page_desc = await vision_read_page(page)
                        msg = f"{retailer_name}: could not find sign-in link on homepage"
                        if page_desc:
                            msg += f" (page shows: {page_desc})"
                        return {"ok": False, "message": msg}

                    current_url = page.url

                # --- Resolve selectors based on final URL ---
                if "access.pokemon.com" in current_url or "sso.pokemon.com" in current_url:
                    email_sel = 'input[name="email"], input[name="username"], input[type="email"], input[type="text"]'
                    pass_sel = 'input[type="password"], input[name="password"]'
                    submit_sel = 'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Continue")'
                else:
                    email_sel = sel["email"]
                    pass_sel = sel["password"]
                    submit_sel = sel["submit"]

                # Wait for the email field to appear (up to 15s for JS-heavy pages)
                email_found = False
                try:
                    await page.locator(email_sel).first.wait_for(state="visible", timeout=15000)
                    email_found = True
                except Exception:
                    pass

                # Vision fallback: if selectors didn't find the email field, ask Claude
                if not email_found:
                    if await vision_fill(page, "email or username input", email):
                        email_found = True
                        await page.wait_for_timeout(1000)
                    else:
                        page_desc = await vision_read_page(page)
                        msg = f"{retailer_name} login page did not load — no email/username field found at {current_url}"
                        if page_desc:
                            msg += f" (page shows: {page_desc})"
                        return {"ok": False, "message": msg}

                # Use keyboard.type() for human-like input instead of fill() which is a bot tell
                if email_found and not await _page_has_value(page, email_sel, email):
                    await page.locator(email_sel).first.click()
                    await page.locator(email_sel).first.press("Control+a")
                    await page.keyboard.type(email, delay=40)

                # Check if password field is visible yet (single-step) or needs submit first (multi-step)
                pass_visible = False
                try:
                    pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1500)
                except Exception:
                    pass

                if pass_visible:
                    # Single-step: fill both and submit
                    pw_loc = page.locator(pass_sel).first
                    await pw_loc.click()
                    await page.wait_for_timeout(300)
                    await page.keyboard.type(password, delay=40)
                    await page.wait_for_timeout(300)
                    # Verify password was entered; if empty, fall back to fill()
                    try:
                        pw_val = await pw_loc.input_value(timeout=1000)
                        if not pw_val:
                            await pw_loc.fill(password)
                            await page.wait_for_timeout(300)
                    except Exception:
                        pass
                    # Try selector click, fall back to vision
                    if not await click_visible_button(page, submit_sel):
                        await vision_click(page, "Sign In / Continue button")
                else:
                    # Multi-step: submit email/phone first
                    # Use multiple strategies to find and click the submit/continue button
                    submit_clicked = await click_visible_button(page, submit_sel)
                    if submit_clicked:
                        logger.info("Test login %s: clicked submit via CSS selector", retailer_name)
                    if not submit_clicked:
                        # Try Playwright's get_by_role which handles text matching much better
                        for btn_text in ["Continue with email", "Continue", "Sign in", "Next"]:
                            try:
                                btn = page.get_by_role("button", name=btn_text, exact=False)
                                if await btn.first.is_visible(timeout=500):
                                    await btn.first.click()
                                    submit_clicked = True
                                    logger.info("Test login %s: clicked submit via get_by_role('%s')", retailer_name, btn_text)
                                    break
                            except Exception:
                                continue
                    if not submit_clicked:
                        # Try any visible link with matching text
                        for link_text in ["Continue with email", "Continue"]:
                            try:
                                link = page.get_by_text(link_text, exact=False)
                                if await link.first.is_visible(timeout=500):
                                    await link.first.click()
                                    submit_clicked = True
                                    logger.info("Test login %s: clicked submit via get_by_text('%s')", retailer_name, link_text)
                                    break
                            except Exception:
                                continue
                    if not submit_clicked:
                        # Last resort: vision
                        logger.info("Test login %s: all selectors failed, trying vision click for submit button", retailer_name)
                        clicked = await vision_click(page, "Continue with email button (NOT passkey)")
                        logger.info("Test login %s: vision click result: %s", retailer_name, clicked)

                    await page.wait_for_timeout(3000)

                    # Log what page we're on after submit attempt
                    post_submit_url = page.url
                    logger.info("Test login %s: after submit, URL is %s", retailer_name, post_submit_url)

                    # Check for "Something went wrong" error and retry once
                    error_banner = page.locator('[role="alert"], .error-message, [class*="error" i], [data-test="error"]')
                    try:
                        if await error_banner.first.is_visible(timeout=2000):
                            banner_text = await error_banner.first.inner_text(timeout=1000)
                            if "something went wrong" in banner_text.lower() or "try again" in banner_text.lower():
                                logger.info("Test login %s: server error on first attempt, retrying", retailer_name)
                                await page.wait_for_timeout(2000)
                                # Clear and re-type email, then submit again
                                try:
                                    await page.locator(email_sel).first.click()
                                    await page.locator(email_sel).first.press("Control+a")
                                    await page.keyboard.type(email, delay=40)
                                    await page.wait_for_timeout(500)
                                    # Re-use the same multi-strategy click
                                    for btn_text in ["Continue with email", "Continue", "Sign in"]:
                                        try:
                                            btn = page.get_by_role("button", name=btn_text, exact=False)
                                            if await btn.first.is_visible(timeout=500):
                                                await btn.first.click()
                                                break
                                        except Exception:
                                            continue
                                    await page.wait_for_timeout(3000)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    # Some sites (e.g. Target) show an auth method picker
                    # Target shows: "Use a passkey", "Get a code", "Enter your password"
                    pw_option_clicked = False

                    # Strategy 1: get_by_role with exact text variations
                    for option_text in ["Enter your password", "Enter password", "Password", "Use password"]:
                        try:
                            opt = page.get_by_role("button", name=option_text, exact=False)
                            if await opt.first.is_visible(timeout=500):
                                await opt.first.click()
                                pw_option_clicked = True
                                logger.info("Test login %s: clicked auth method via get_by_role('%s')", retailer_name, option_text)
                                break
                        except Exception:
                            continue

                    # Strategy 2: get_by_text (catches divs/links acting as buttons)
                    if not pw_option_clicked:
                        for option_text in ["Enter your password", "Enter password"]:
                            try:
                                opt = page.get_by_text(option_text, exact=False)
                                if await opt.first.is_visible(timeout=500):
                                    await opt.first.click()
                                    pw_option_clicked = True
                                    logger.info("Test login %s: clicked auth method via get_by_text('%s')", retailer_name, option_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 3: CSS selectors
                    if not pw_option_clicked:
                        password_option = page.locator('button:has-text("password"), a:has-text("password"), [data-test*="password" i], div:has-text("Enter your password")')
                        try:
                            if await password_option.first.is_visible(timeout=1000):
                                await password_option.first.click()
                                pw_option_clicked = True
                                logger.info("Test login %s: clicked auth method via CSS selector", retailer_name)
                        except Exception:
                            pass

                    # Strategy 4: Vision fallback
                    if not pw_option_clicked:
                        logger.info("Test login %s: trying vision for auth method picker", retailer_name)
                        pw_option_clicked = await vision_click(page, "Enter your password option")

                    if pw_option_clicked:
                        await page.wait_for_timeout(2000)
                    else:
                        logger.warning("Test login %s: could not find password auth method option", retailer_name)

                    # Wait for password field — selectors first, then vision
                    pass_found = False
                    try:
                        await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
                        pass_found = True
                    except Exception:
                        pass

                    if pass_found:
                        # Click the password field to focus it, then type
                        pw_locator = page.locator(pass_sel).first
                        await pw_locator.click()
                        await page.wait_for_timeout(300)
                        await page.keyboard.type(password, delay=40)
                        await page.wait_for_timeout(300)
                        # Verify password was entered; if empty, fall back to fill()
                        try:
                            pw_value = await pw_locator.input_value(timeout=1000)
                            if not pw_value:
                                logger.info("Test login %s: keyboard.type() did not fill password, falling back to fill()", retailer_name)
                                await pw_locator.fill(password)
                                await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        if not await click_visible_button(page, submit_sel):
                            await vision_click(page, "Sign In / Continue button")
                    else:
                        # Vision fallback for password entry
                        if await vision_fill(page, "password input", password):
                            await page.wait_for_timeout(500)
                            await vision_click(page, "Sign In / Continue button")
                        else:
                            page_desc = await vision_read_page(page)
                            msg = f"{retailer_name} login: submitted email but password field did not appear"
                            if page_desc:
                                msg += f" (page shows: {page_desc})"
                            return {"ok": False, "message": msg}

                await page.wait_for_timeout(5000)

                # Check for success indicators
                final_url = page.url
                logger.info("Test login %s: final URL after submit: %s", retailer_name, final_url)

                still_on_login = "/login" in final_url or "/signin" in final_url or "/sign-in" in final_url or "/identity" in final_url

                # If we navigated away from the login page, that's a strong success signal
                if not still_on_login:
                    # Check for explicit error messages on the page (login-specific errors only)
                    error_el = page.locator(sel["error"])
                    has_error = False
                    error_text = ""
                    try:
                        if await error_el.first.is_visible(timeout=1000):
                            error_text = (await error_el.first.inner_text(timeout=1000)).strip()
                            # Only count as login error if text is login-related
                            login_error_keywords = ["password", "credentials", "sign in", "login", "incorrect", "invalid", "unauthorized"]
                            if error_text and any(kw in error_text.lower() for kw in login_error_keywords):
                                has_error = True
                    except Exception:
                        pass

                    if has_error and error_text:
                        msg = f"{retailer_name} login failed: {error_text[:200]}"
                        logger.warning("Test login failed for %s user=%s: %s", retailer_name, email, error_text)
                        db.add_error_log(user["id"], "WARNING", "test-login", msg, "")
                        return {"ok": False, "message": msg}

                    # Not on login page + no login error = success
                    logger.info("Test login successful for %s user=%s (navigated to %s)", retailer_name, email, final_url)
                    return {"ok": True, "message": f"{retailer_name} login successful"}

                # Still on login page — check why
                # Look for error messages
                error_el = page.locator(sel["error"])
                error_text = ""
                try:
                    if await error_el.first.is_visible(timeout=1000):
                        error_text = (await error_el.first.inner_text(timeout=1000)).strip()
                except Exception:
                    pass

                if error_text:
                    msg = f"{retailer_name} login failed: {error_text[:200]}"
                    logger.warning("Test login failed for %s user=%s: %s", retailer_name, email, error_text)
                    db.add_error_log(user["id"], "WARNING", "test-login", msg, "")
                    return {"ok": False, "message": msg}

                # No error text but still on login page
                page_desc = await vision_read_page(page)
                msg = f"{retailer_name} login failed — still on login page (wrong email/password?)"
                if page_desc:
                    msg += f" (page shows: {page_desc})"
                logger.warning("Test login failed for %s user=%s: still on login page", retailer_name, email)
                db.add_error_log(user["id"], "WARNING", "test-login", msg, "")
                return {"ok": False, "message": msg}

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

    async def _page_has_value(page, selector: str, value: str) -> bool:
        """Check if an input already contains a value (e.g. filled by vision path)."""
        try:
            actual = await page.locator(selector).first.input_value(timeout=1000)
            return actual == value
        except Exception:
            return False

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
